"""Connection hardening for long-running processes (queue workers, daemons).

Masonite ORM opens a fresh DB-API connection per query builder, but raw
connections outlive it: they are parked in each driver module's
``CONNECTION_POOL`` and, during a transaction, in
``ConnectionResolver._connections``. In a long-running worker those raw
connections go stale or get inherited by forked children. This patch:

* stamps every raw connection with the PID that opened it and the time it was
  last used;
* in a forked child, drops inherited sockets without sending the server a
  goodbye (which would end the *parent's* session) and reconnects;
* recycles connections idle for longer than ``max_idle_seconds`` while no
  transaction is open;
* on a disconnect error outside a transaction, throws away every pooled
  connection, reconnects and retries the query once;
* on a disconnect error inside a transaction, raises
  ``TransactionAbortedError``, because the server has already rolled the
  transaction back and retrying one statement would apply half of it;
* makes ``rollback()`` a no-op on a dead connection, so it is safe to call
  from an ``except`` block.

Upstream keeps ``query``/``begin``/``commit``/``rollback`` on each driver
class, not on ``BaseConnection``, and counts nesting in ``transaction_level``,
so those are what get patched.
"""

import logging
import os
import random
import sys
import time
import weakref

from masoniteorm.connections import (
    ConnectionResolver,
    MySQLConnection,
    PostgresConnection,
)
from masoniteorm.exceptions import QueryException

logger = logging.getLogger("masonite_orm_patches")

DEFAULT_MAX_IDLE_SECONDS = 300

HARDENED_CONNECTIONS = (MySQLConnection, PostgresConnection)


class TransactionAbortedError(QueryException):
    """The connection dropped mid-transaction, so the server rolled it back.

    Retry the whole transaction block, not the failed statement.
    """


class CommitOutcomeUnknownError(QueryException):
    """The connection dropped while ``COMMIT`` was in flight.

    The server may or may not have committed. Retry only if the transaction is
    idempotent, or check whether its writes landed first.
    """


# Lower-cased substrings of driver error messages that mean the link is gone.
DISCONNECT_MESSAGES = (
    # MySQL / MariaDB (pymysql)
    "server has gone away",
    "lost connection to mysql server",
    "lost connection to server",
    "disconnected by the server because of inactivity",
    "error while sending",
    "packet sequence number wrong",
    "connection was killed",
    # PostgreSQL (psycopg2)
    "server closed the connection unexpectedly",
    "terminating connection due to administrator command",
    "connection already closed",
    "connection not open",
    "no connection to the server",
    "could not receive data from server",
    "could not send data to server",
    "ssl syscall error",
    "ssl connection has been closed unexpectedly",
    "decryption failed or bad record mac",
    # Socket level, either driver
    "broken pipe",
    "connection reset by peer",
    "connection timed out",
)

# pymysql OperationalError codes: CR_SERVER_GONE_ERROR, CR_SERVER_LOST,
# CR_SERVER_LOST_EXTENDED, ER_CLIENT_INTERACTION_TIMEOUT.
DISCONNECT_ERROR_CODES = frozenset({2006, 2013, 2055, 4031})

_settings = {"max_idle_seconds": DEFAULT_MAX_IDLE_SECONDS}

# raw DB-API connection -> [pid that opened it, monotonic time last used]
_RAW_STATE = weakref.WeakKeyDictionary()

# Inherited connections that could not be detached safely. Holding a reference
# keeps them from being finalized, which would message the parent's session.
_ORPHANS = []

_MARKER = "__masonite_orm_hardening__"
_fork_hook_registered = False


def is_disconnect_error(exc):
    """Return whether ``exc`` (or anything it wraps) means the link is dead.

    Upstream re-raises driver errors as ``QueryException(str(e)) from e``, so
    the whole cause/context chain is checked.
    """
    seen = set()
    while exc is not None and id(exc) not in seen:
        seen.add(id(exc))
        if isinstance(exc, (TransactionAbortedError, CommitOutcomeUnknownError)):
            return False
        if isinstance(exc, ConnectionError):
            return True
        # PEP 249 InterfaceError: the connection, not the SQL, is the problem
        # (pymysql "(0, '')", psycopg2 "connection already closed").
        if type(exc).__name__ == "InterfaceError":
            return True
        args = getattr(exc, "args", ())
        if args and isinstance(args[0], int) and args[0] in DISCONNECT_ERROR_CODES:
            return True
        message = str(exc).lower()
        if any(pattern in message for pattern in DISCONNECT_MESSAGES):
            return True
        exc = exc.__cause__ or exc.__context__
    return False


# ---------------------------------------------------------------------------
# Raw connection bookkeeping
# ---------------------------------------------------------------------------


def _state(raw):
    """Return ``[pid, last_used]`` for ``raw``, stamping it on first sight."""
    try:
        state = _RAW_STATE.get(raw)
        if state is None:
            state = _RAW_STATE[raw] = [os.getpid(), time.monotonic()]
    except TypeError:
        # Not weak-referenceable: leave it untracked rather than fail queries.
        return None
    return state


def _touch(raw):
    state = _state(raw) if raw is not None else None
    if state is not None:
        state[1] = time.monotonic()


def _forget(raw):
    try:
        _RAW_STATE.pop(raw, None)
    except TypeError:
        pass


def _hard_close(raw):
    """Close ``raw`` for real, ignoring errors from an already dead socket."""
    _forget(raw)
    # MySQLConnection replaces the instance's close() with one that returns it
    # to the pool, so call the driver class's close() directly.
    close = getattr(type(raw), "close", None)
    if close is None:
        return
    try:
        close(raw)
    except Exception:
        pass


def _abandon(raw):
    """Drop a connection inherited from the parent process.

    Closing it the normal way sends the server a quit/terminate message over
    the shared socket, which would end the parent's session. Only this
    process's file descriptor is released.
    """
    _forget(raw)
    # pymysql: closes the socket without sending COM_QUIT.
    force_close = getattr(type(raw), "_force_close", None)
    if force_close is not None:
        try:
            force_close(raw)
            return
        except Exception:
            pass
    # psycopg2: point our copy of the descriptor at /dev/null, so whatever
    # libpq writes when the object is finalized goes nowhere.
    fileno = getattr(raw, "fileno", None)
    if callable(fileno):
        try:
            fd = fileno()
            devnull = os.open(os.devnull, os.O_RDWR)
            try:
                os.dup2(devnull, fd)
            finally:
                os.close(devnull)
            return
        except Exception:
            pass
    _ORPHANS.append(raw)


def _dispose(raw):
    """Close ``raw`` if this process opened it, otherwise abandon it."""
    try:
        state = _RAW_STATE.get(raw)
    except TypeError:
        state = None
    if state is not None and state[0] != os.getpid():
        _abandon(raw)
    else:
        _hard_close(raw)


def _pool_for(cls):
    for klass in cls.__mro__:
        pool = getattr(sys.modules.get(klass.__module__), "CONNECTION_POOL", None)
        if isinstance(pool, list):
            return pool
    return None


def _drain_pool(cls, dispose=_dispose):
    pool = _pool_for(cls)
    if not pool:
        return
    raws = list(pool)
    del pool[:]
    for raw in raws:
        dispose(raw)


def _prune_pool(cls, max_idle):
    """Close pooled connections idle for longer than ``max_idle``."""
    pool = _pool_for(cls)
    if not pool:
        return
    now = time.monotonic()
    for raw in list(pool):
        state = _state(raw)
        if state is not None and now - state[1] > max_idle:
            pool.remove(raw)
            _dispose(raw)


def _after_fork_in_child():
    if not _settings.get("active"):
        return
    for cls in HARDENED_CONNECTIONS:
        _drain_pool(cls, dispose=_abandon)


# ---------------------------------------------------------------------------
# Connection instance helpers
# ---------------------------------------------------------------------------


def _level(conn):
    return conn.get_transaction_level() or 0


def _max_idle(conn):
    """Per-connection ``max_idle_seconds`` from DATABASES, else the global one."""
    details = getattr(conn, "full_details", None) or {}
    value = details.get("max_idle_seconds", _settings["max_idle_seconds"])
    return float(value) if value else None


def _unregister_global(conn):
    connections = ConnectionResolver._connections
    if connections.get(conn.name) is conn:
        connections.pop(conn.name)


def _invalidate(conn, raw=None):
    """Tear the connection down and reset its client-side transaction state."""
    raw = raw if raw is not None else conn._connection
    conn._connection = None
    conn._cursor = None
    conn.open = 0
    conn.transaction_level = 0
    _unregister_global(conn)
    if raw is not None:
        _dispose(raw)
    # A dropped link usually means a server restart or network blip, so every
    # pooled connection is suspect.
    _drain_pool(type(conn))


def _ensure_connection(conn):
    """Attach a raw connection, recycling one that was forked or sat idle."""
    raw = conn._connection
    if raw is not None:
        state = _state(raw)
        if state is not None and state[0] != os.getpid():
            in_transaction = _level(conn) > 0
            logger.warning(
                "masonite-orm-patches: %s connection was opened by PID %s, "
                "reconnecting in PID %s",
                conn.name,
                state[0],
                os.getpid(),
            )
            _invalidate(conn, raw)
            if in_transaction:
                raise TransactionAbortedError(
                    f"The '{conn.name}' connection was inherited from another "
                    "process mid-transaction; retry the transaction block."
                )
        elif state is not None and _level(conn) <= 0:
            max_idle = _max_idle(conn)
            idle = time.monotonic() - state[1]
            if max_idle and idle > max_idle:
                logger.info(
                    "masonite-orm-patches: recycling %s connection idle for %.0fs",
                    conn.name,
                    idle,
                )
                conn._connection = None
                conn.open = 0
                _hard_close(raw)
                _prune_pool(type(conn), max_idle)

    if conn._connection is None:
        conn.make_connection()
    return conn._connection


# ---------------------------------------------------------------------------
# Patched methods
# ---------------------------------------------------------------------------


def _patched_query(original):
    def query(self, query, bindings=(), results="*"):
        if self._dry:
            return original(self, query, bindings, results)

        raw = _ensure_connection(self)
        in_transaction = _level(self) > 0
        try:
            result = original(self, query, bindings, results)
        except Exception as exc:
            if not is_disconnect_error(exc):
                raise
            _invalidate(self, raw)
            if in_transaction:
                raise TransactionAbortedError(
                    f"Lost the '{self.name}' connection mid-transaction; the "
                    "server rolled it back. Retry the transaction block."
                ) from exc

            logger.warning(
                "masonite-orm-patches: lost %s connection (%s), reconnecting "
                "and retrying once",
                self.name,
                exc,
            )
            raw = _ensure_connection(self)
            try:
                result = original(self, query, bindings, results)
            except Exception as retry_exc:
                if is_disconnect_error(retry_exc):
                    _invalidate(self, raw)
                raise

        _touch(raw)
        return result

    return query


def _patched_begin(original):
    def begin(self):
        outermost = _level(self) <= 0
        raw = _ensure_connection(self)
        try:
            result = original(self)
        except Exception as exc:
            if not is_disconnect_error(exc):
                raise
            _invalidate(self, raw)
            if not outermost:
                raise TransactionAbortedError(
                    f"Lost the '{self.name}' connection mid-transaction; the "
                    "server rolled it back. Retry the transaction block."
                ) from exc
            # Nothing has run yet, so reconnecting and starting over is safe.
            raw = _ensure_connection(self)
            result = original(self)

        _touch(raw)
        return result

    return begin


def _patched_commit(original):
    def commit(self):
        if self._connection is None:
            raise TransactionAbortedError(
                f"The '{self.name}' connection has no open transaction to "
                "commit; it was already committed, rolled back or aborted."
            )
        raw = self._connection
        try:
            result = original(self)
        except Exception as exc:
            if not is_disconnect_error(exc):
                raise
            _invalidate(self, raw)
            raise CommitOutcomeUnknownError(
                f"Lost the '{self.name}' connection during COMMIT; the "
                "transaction may or may not have been applied."
            ) from exc

        _touch(raw)
        return result

    return commit


def _patched_rollback(original):
    def rollback(self):
        if self._connection is None:
            # Torn down already; the server discarded the transaction.
            self.transaction_level = 0
            self.open = 0
            return None
        raw = self._connection
        try:
            result = original(self)
        except Exception as exc:
            if not is_disconnect_error(exc):
                raise
            logger.warning(
                "masonite-orm-patches: rollback on dead %s connection ignored (%s)",
                self.name,
                exc,
            )
            _invalidate(self, raw)
            return None

        _touch(raw)
        return result

    return rollback


def _patched_resolver_rollback(original):
    def rollback(self, name=None):
        # After TransactionAbortedError the global connection is already gone;
        # upstream would raise KeyError and hide the original error.
        key = name if name is not None else self.get_connection_details()["default"]
        if key not in self.get_global_connections():
            return None
        return original(self, name)

    return rollback


def _patch_targets():
    targets = [
        (cls, name, factory)
        for cls in HARDENED_CONNECTIONS
        for name, factory in (
            ("query", _patched_query),
            ("begin", _patched_begin),
            ("commit", _patched_commit),
            ("rollback", _patched_rollback),
        )
    ]
    targets.append((ConnectionResolver, "rollback", _patched_resolver_rollback))
    return targets


def _is_patched(owner, name):
    return getattr(owner.__dict__.get(name), _MARKER, False)


def patch_base_connection(max_idle_seconds=DEFAULT_MAX_IDLE_SECONDS):
    """Install the connection hardening. Safe to call more than once.

    ``max_idle_seconds`` is the default idle limit; ``None`` or ``0`` disables
    recycling. A connection in ``DATABASES`` can override it with its own
    ``"max_idle_seconds"`` key. Calling again only updates the limit.

    Returns the names of the methods that were newly patched.
    """
    global _fork_hook_registered

    _settings["max_idle_seconds"] = max_idle_seconds
    _settings["active"] = True
    if not _fork_hook_registered and hasattr(os, "register_at_fork"):
        os.register_at_fork(after_in_child=_after_fork_in_child)
        _fork_hook_registered = True

    applied = []
    for owner, name, factory in _patch_targets():
        if _is_patched(owner, name):
            continue
        original = owner.__dict__[name]
        patched = factory(original)
        patched.__name__ = name
        patched.__qualname__ = f"{owner.__name__}.{name}"
        patched.__doc__ = original.__doc__
        patched.__wrapped__ = original
        patched.__original__ = original
        setattr(patched, _MARKER, True)
        setattr(owner, name, patched)
        applied.append(f"{owner.__name__}.{name}")
    return applied


def unpatch_base_connection():
    """Restore the upstream methods."""
    _settings["active"] = False
    for owner, name, _ in _patch_targets():
        if _is_patched(owner, name):
            setattr(owner, name, owner.__dict__[name].__original__)


def run_in_transaction(
    callback,
    *,
    connection=None,
    attempts=3,
    base_delay=0.1,
    max_delay=2.0,
    resolver=None,
):
    """Run ``callback()`` in a transaction, retrying the whole block on abort.

    Retries on ``TransactionAbortedError`` with jittered exponential backoff.
    ``CommitOutcomeUnknownError`` is not retried, since the first attempt may
    have committed. ``callback`` runs again from the start on each attempt,
    so it must not have side effects outside the database.
    """
    if resolver is None:
        from masoniteorm.config import load_config

        resolver = load_config().DB

    for attempt in range(1, attempts + 1):
        try:
            with resolver.transaction(connection):
                return callback()
        except TransactionAbortedError:
            if attempt == attempts:
                raise
            delay = min(max_delay, base_delay * 2 ** (attempt - 1))
            delay *= random.uniform(0.5, 1.0)
            logger.warning(
                "masonite-orm-patches: transaction aborted (attempt %s/%s), "
                "retrying in %.2fs",
                attempt,
                attempts,
                delay,
            )
            time.sleep(delay)
