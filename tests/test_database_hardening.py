"""Connection hardening against the real upstream MySQL and Postgres drivers.

pymysql and psycopg2 are replaced by small fakes in ``sys.modules`` so the
upstream ``create_connection``/``query``/pooling code runs unchanged, while the
tests decide when a socket dies.
"""

import os
import sys
from types import ModuleType
from unittest import mock

import pytest
from masoniteorm.connections import (
    ConnectionResolver,
    MySQLConnection,
    PostgresConnection,
)
from masoniteorm.exceptions import QueryException

from masonite_orm_patches.patches import database_hardening as hardening
from masonite_orm_patches.patches.database_hardening import (
    CommitOutcomeUnknownError,
    TransactionAbortedError,
    is_disconnect_error,
    patch_base_connection,
    run_in_transaction,
    unpatch_base_connection,
)

MYSQL_POOL = sys.modules[MySQLConnection.__module__].CONNECTION_POOL
POSTGRES_POOL = sys.modules[PostgresConnection.__module__].CONNECTION_POOL


# ---------------------------------------------------------------------------
# Fake drivers
# ---------------------------------------------------------------------------


class OperationalError(Exception):
    pass


class InterfaceError(Exception):
    pass


class ProgrammingError(Exception):
    pass


class FakeCursor:
    def __init__(self, raw):
        self.raw = raw
        self.statusmessage = ""

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def execute(self, query, bindings=()):
        self.raw.check_alive()
        if self.raw.next_error is not None:
            error, self.raw.next_error = self.raw.next_error, None
            raise error
        self.raw.executed.append(query)
        self.statusmessage = "SELECT 1"

    def fetchall(self):
        return [{"id": self.raw.serial}]

    def fetchone(self):
        return {"id": self.raw.serial}

    def close(self):
        pass


class FakeRaw:
    """Shared behaviour of a DB-API connection whose socket can be killed."""

    created = 0
    dead_error = OperationalError(2013, "Lost connection to MySQL server during query")

    def __init__(self, **kwargs):
        FakeRaw.created += 1
        self.serial = FakeRaw.created
        self.dead = False
        self.next_error = None
        self.executed = []
        self.goodbye_sent = False  # COM_QUIT / Terminate reached the server
        self.socket_dropped = False

    def check_alive(self):
        if self.dead:
            raise self.dead_error

    def cursor(self, **kwargs):
        return FakeCursor(self)

    def begin(self):
        self.check_alive()

    def commit(self):
        self.check_alive()

    def rollback(self):
        self.check_alive()


class FakePyMySQLConnection(FakeRaw):
    def connect(self):
        self.dead = False

    def close(self):
        # Real pymysql: send COM_QUIT (best effort), then drop the socket.
        if not self.dead:
            self.goodbye_sent = True
        self._force_close()

    def _force_close(self):
        self.socket_dropped = True


class FakePsycopg2Connection(FakeRaw):
    dead_error = OperationalError("server closed the connection unexpectedly")

    def __init__(self, **kwargs):
        super().__init__(**kwargs)
        self.autocommit = False
        self.closed = 0
        self._read_fd, self._write_fd = os.pipe()

    def fileno(self):
        return self._write_fd

    def close(self):
        if not self.dead and not self.closed:
            self.goodbye_sent = True
        self.closed = 1


def _fake_pymysql():
    module = ModuleType("pymysql")
    module.connect = lambda **kwargs: FakePyMySQLConnection(**kwargs)
    module.converters = ModuleType("pymysql.converters")
    module.converters.conversions = {}
    module.converters.escape_datetime = lambda *args: None
    module.cursors = ModuleType("pymysql.cursors")
    module.cursors.DictCursor = object
    return module


def _fake_psycopg2():
    module = ModuleType("psycopg2")
    module.connect = lambda **kwargs: FakePsycopg2Connection(**kwargs)
    module.extras = ModuleType("psycopg2.extras")
    module.extras.RealDictCursor = object
    return module


DATABASES = {
    "default": "mysql",
    "mysql": {"driver": "mysql", "host": "db", "database": "app", "port": 3306},
    "postgres": {"driver": "postgres", "host": "db", "database": "app", "port": 5432},
}


@pytest.fixture(autouse=True)
def hardened(monkeypatch):
    pymysql, psycopg2 = _fake_pymysql(), _fake_psycopg2()
    monkeypatch.setitem(sys.modules, "pymysql", pymysql)
    monkeypatch.setitem(sys.modules, "pymysql.converters", pymysql.converters)
    monkeypatch.setitem(sys.modules, "pymysql.cursors", pymysql.cursors)
    monkeypatch.setitem(sys.modules, "psycopg2", psycopg2)
    monkeypatch.setitem(sys.modules, "psycopg2.extras", psycopg2.extras)
    for name in ("mysql", "postgres"):
        DATABASES[name].pop("max_idle_seconds", None)
        DATABASES[name].pop("connection_pooling_enabled", None)

    patch_base_connection(max_idle_seconds=300)
    yield
    unpatch_base_connection()
    MYSQL_POOL.clear()
    POSTGRES_POOL.clear()
    ConnectionResolver._connections.clear()
    hardening._ORPHANS.clear()


@pytest.fixture
def resolver():
    return ConnectionResolver().set_connection_details(DATABASES)


def connect(resolver, name="mysql"):
    driver = PostgresConnection if name == "postgres" else MySQLConnection
    return driver(
        **resolver.get_connection_information(name), name=name
    ).make_connection()


def age(raw, seconds):
    hardening._state(raw)[1] -= seconds


def opened_by(raw, pid):
    hardening._state(raw)[0] = pid


DRIVERS = pytest.mark.parametrize("name", ["mysql", "postgres"])


# ---------------------------------------------------------------------------
# Fork safety
# ---------------------------------------------------------------------------


@DRIVERS
def test_query_after_fork_drops_inherited_socket_and_reconnects(resolver, name):
    connection = connect(resolver, name)
    inherited = connection._connection
    opened_by(inherited, os.getpid() + 1)  # as if the parent opened it

    result = connection.query("select 1")

    assert inherited.executed == []
    assert inherited.goodbye_sent is False  # parent's session left alone
    if name == "mysql":
        assert inherited.socket_dropped
    else:
        assert os.fstat(inherited.fileno()).st_ino == os.stat(os.devnull).st_ino
    assert result == [{"id": inherited.serial + 1}]


def test_fork_mid_transaction_raises_transaction_aborted(resolver):
    connection = connect(resolver).begin()
    opened_by(connection._connection, os.getpid() + 1)

    with pytest.raises(TransactionAbortedError):
        connection.query("insert into jobs values (1)")

    assert connection.get_transaction_level() == 0
    assert connection._connection is None


def test_fork_hook_abandons_pooled_connections_without_goodbye():
    mysql_raw, postgres_raw = FakePyMySQLConnection(), FakePsycopg2Connection()
    MYSQL_POOL.append(mysql_raw)
    POSTGRES_POOL.append(postgres_raw)

    hardening._after_fork_in_child()

    assert MYSQL_POOL == [] and POSTGRES_POOL == []
    assert mysql_raw.socket_dropped and not mysql_raw.goodbye_sent
    # psycopg2 has no quiet close: our descriptor now points at /dev/null.
    assert not postgres_raw.goodbye_sent
    assert os.fstat(postgres_raw.fileno()).st_ino == os.stat(os.devnull).st_ino


@pytest.mark.skipif(not hasattr(os, "fork"), reason="requires os.fork")
def test_real_fork_child_gets_fresh_connection(resolver):
    DATABASES["mysql"]["connection_pooling_enabled"] = True
    connection = connect(resolver)
    connection.query("select 1")
    parent_raw = MYSQL_POOL[0]

    pid = os.fork()
    if pid == 0:  # child
        code = 1
        try:
            child_connection = connect(resolver)
            fresh = child_connection._connection
            child_connection.query("select 1")
            if (
                fresh is not parent_raw
                and parent_raw.socket_dropped
                and not parent_raw.goodbye_sent
            ):
                code = 0
        finally:
            os._exit(code)

    _, status = os.waitpid(pid, 0)
    assert os.waitstatus_to_exitcode(status) == 0
    assert MYSQL_POOL == [parent_raw] and not parent_raw.socket_dropped


# ---------------------------------------------------------------------------
# Idle recycling
# ---------------------------------------------------------------------------


@DRIVERS
def test_idle_connection_is_recycled(resolver, name):
    connection = connect(resolver, name)
    stale = connection._connection
    age(stale, 301)

    connection.query("select 1")

    assert stale.executed == []
    assert stale.goodbye_sent  # ours, so closed properly
    assert connection._connection is None or connection._connection is not stale


def test_connection_within_idle_limit_is_reused(resolver):
    connection = connect(resolver)
    raw = connection._connection
    age(raw, 299)

    connection.query("select 1")

    assert raw.executed == ["select 1"]


def test_per_connection_idle_limit_overrides_default(resolver):
    DATABASES["mysql"]["max_idle_seconds"] = 30
    connection = connect(resolver)
    stale = connection._connection
    age(stale, 31)

    connection.query("select 1")

    assert stale.executed == []


def test_idle_recycling_can_be_disabled(resolver):
    patch_base_connection(max_idle_seconds=None)
    connection = connect(resolver)
    raw = connection._connection
    age(raw, 10_000)

    connection.query("select 1")

    assert raw.executed == ["select 1"]


def test_idle_connection_is_not_recycled_mid_transaction(resolver):
    connection = connect(resolver).begin()
    raw = connection._connection
    age(raw, 10_000)

    connection.query("update jobs set done = 1")

    assert raw.executed == ["update jobs set done = 1"]
    assert connection.get_transaction_level() == 1


def test_idle_pooled_connections_are_pruned(resolver):
    DATABASES["mysql"]["connection_pooling_enabled"] = True
    fresh, stale = FakePyMySQLConnection(), FakePyMySQLConnection()
    age(fresh, 0)
    age(stale, 600)
    MYSQL_POOL.extend([fresh, stale])  # pop() takes the last one

    connection = connect(resolver)  # pops `stale`
    connection.query("select 1")

    assert stale.executed == [] and stale.goodbye_sent
    assert fresh.executed == ["select 1"]


# ---------------------------------------------------------------------------
# Disconnects outside a transaction
# ---------------------------------------------------------------------------


@DRIVERS
def test_standalone_query_reconnects_and_retries_once(resolver, name):
    connection = connect(resolver, name)
    dead = connection._connection
    dead.dead = True

    result = connection.query("select 1")

    assert result == [{"id": FakeRaw.created}]
    assert dead.executed == []
    assert FakeRaw.created == dead.serial + 1


def test_disconnect_drains_the_pool(resolver):
    DATABASES["mysql"]["connection_pooling_enabled"] = True
    # The server restarted: every pooled connection is dead.
    pooled = [FakePyMySQLConnection(), FakePyMySQLConnection()]
    for raw in pooled:
        raw.dead = True
        age(raw, 0)
    MYSQL_POOL.extend(pooled)
    connection = connect(resolver)

    connection.query("select 1")

    (replacement,) = MYSQL_POOL
    assert replacement not in pooled
    assert replacement.executed == ["select 1"]


def test_retry_happens_only_once(resolver):
    connection = connect(resolver)
    first = FakeRaw.created

    with mock.patch.object(
        FakePyMySQLConnection, "check_alive", side_effect=FakeRaw.dead_error
    ):
        with pytest.raises(QueryException, match="Lost connection"):
            connection.query("select 1")

    assert FakeRaw.created == first + 1
    assert connection._connection is None


def test_non_disconnect_errors_are_not_retried(resolver):
    connection = connect(resolver)
    raw = connection._connection
    raw.next_error = ProgrammingError(1064, "You have an error in your SQL syntax")

    with pytest.raises(QueryException, match="SQL syntax"):
        connection.query("selec 1")

    assert FakeRaw.created == raw.serial


def test_begin_on_dead_connection_reconnects(resolver):
    # MySQL sends BEGIN; psycopg2's begin() only flips autocommit locally.
    connection = connect(resolver)
    dead = connection._connection
    dead.dead = True
    dead.dead_error = InterfaceError(0, "")

    connection.begin()
    connection.query("insert into jobs values (1)")

    assert connection.get_transaction_level() == 1
    assert connection._connection is not dead
    assert connection._connection.executed == ["insert into jobs values (1)"]


# ---------------------------------------------------------------------------
# Transaction integrity
# ---------------------------------------------------------------------------


@DRIVERS
def test_disconnect_mid_transaction_raises_and_resets_level(resolver, name):
    connection = resolver.begin_transaction(name)
    connection.begin()  # nested
    connection.query("insert into jobs values (1)")
    raw = connection._connection
    raw.dead = True

    with pytest.raises(TransactionAbortedError) as info:
        connection.query("insert into jobs values (2)")

    assert is_disconnect_error(info.value.__cause__)
    assert connection.get_transaction_level() == 0
    assert raw.executed == ["insert into jobs values (1)"]  # not retried
    assert FakeRaw.created == raw.serial  # no silent reconnect
    assert name not in ConnectionResolver._connections


def test_statements_after_abort_run_outside_a_transaction(resolver):
    connection = resolver.begin_transaction("mysql")
    connection._connection.dead = True
    with pytest.raises(TransactionAbortedError):
        connection.query("insert into jobs values (1)")

    connection.query("select 1")

    assert connection.get_transaction_level() == 0
    assert connection.open == 0  # upstream closed it after an autocommit query


def test_transaction_context_manager_surfaces_transaction_aborted(resolver):
    with pytest.raises(TransactionAbortedError):
        with resolver.transaction("mysql"):
            connection = ConnectionResolver._connections["mysql"]
            connection._connection.dead = True
            connection.query("insert into jobs values (1)")


@DRIVERS
def test_rollback_on_dead_socket_is_silent_and_cleans_up(resolver, name):
    connection = resolver.begin_transaction(name)
    connection._connection.dead = True

    resolver.rollback(name)

    assert connection.get_transaction_level() == 0
    assert connection._connection is None and connection.open == 0
    assert name not in ConnectionResolver._connections


def test_rollback_after_abort_is_a_no_op(resolver):
    connection = resolver.begin_transaction("mysql")
    connection._connection.dead = True
    with pytest.raises(TransactionAbortedError):
        connection.query("insert into jobs values (1)")

    connection.rollback()
    resolver.rollback("mysql")  # global entry already gone; upstream: KeyError

    assert connection.get_transaction_level() == 0


def test_rollback_still_raises_unrelated_errors(resolver):
    connection = connect(resolver).begin()
    with mock.patch.object(
        FakePyMySQLConnection, "rollback", side_effect=ProgrammingError("nope")
    ):
        with pytest.raises(ProgrammingError):
            connection.rollback()


def test_commit_on_dead_socket_raises_outcome_unknown(resolver):
    connection = resolver.begin_transaction("mysql")
    connection._connection.dead = True

    with pytest.raises(CommitOutcomeUnknownError):
        resolver.commit("mysql")

    assert connection.get_transaction_level() == 0
    assert not issubclass(CommitOutcomeUnknownError, TransactionAbortedError)


# ---------------------------------------------------------------------------
# Retrying transaction blocks
# ---------------------------------------------------------------------------


def test_run_in_transaction_retries_whole_block(resolver):
    attempts = []

    def work():
        connection = ConnectionResolver._connections["mysql"]
        attempts.append(connection)
        if len(attempts) == 1:
            connection._connection.dead = True
        connection.query("insert into jobs values (1)")
        return "done"

    with mock.patch.object(hardening.time, "sleep") as sleep:
        result = run_in_transaction(work, connection="mysql", resolver=resolver)

    assert result == "done"
    assert len(attempts) == 2 and attempts[0] is not attempts[1]
    sleep.assert_called_once()
    assert "mysql" not in ConnectionResolver._connections


def test_run_in_transaction_gives_up_after_attempts(resolver):
    def work():
        raise TransactionAbortedError("still down")

    with mock.patch.object(hardening.time, "sleep") as sleep:
        with pytest.raises(TransactionAbortedError):
            run_in_transaction(work, connection="mysql", resolver=resolver, attempts=3)

    assert sleep.call_count == 2


def test_run_in_transaction_does_not_retry_unknown_commit(resolver):
    calls = []

    def work():
        calls.append(1)
        ConnectionResolver._connections["mysql"]._connection.dead = True

    with pytest.raises(CommitOutcomeUnknownError):
        run_in_transaction(work, connection="mysql", resolver=resolver)

    assert calls == [1]


# ---------------------------------------------------------------------------
# Patch mechanics and error classification
# ---------------------------------------------------------------------------


def test_patching_is_idempotent_and_reversible():
    query = MySQLConnection.__dict__["query"]

    assert patch_base_connection(max_idle_seconds=60) == []
    assert MySQLConnection.__dict__["query"] is query
    assert hardening._settings["max_idle_seconds"] == 60

    unpatch_base_connection()
    assert MySQLConnection.__dict__["query"] is query.__original__
    assert "ConnectionResolver.rollback" in patch_base_connection()


@pytest.mark.parametrize(
    "error",
    [
        OperationalError(2006, "MySQL server has gone away"),
        OperationalError(2013, "Lost connection to MySQL server during query"),
        OperationalError(4031, "The client was disconnected by the server"),
        InterfaceError(0, ""),
        OperationalError("server closed the connection unexpectedly"),
        OperationalError("SSL SYSCALL error: EOF detected"),
        OperationalError("terminating connection due to administrator command"),
        InterfaceError("connection already closed"),
        BrokenPipeError(32, "Broken pipe"),
        ConnectionResetError(54, "Connection reset by peer"),
    ],
)
def test_recognises_disconnects(error):
    assert is_disconnect_error(error)
    # Upstream wraps driver errors in QueryException.
    try:
        raise QueryException(str(error)) from error
    except QueryException as wrapped:
        assert is_disconnect_error(wrapped)


@pytest.mark.parametrize(
    "error",
    [
        ProgrammingError(1064, "You have an error in your SQL syntax"),
        OperationalError(1205, "Lock wait timeout exceeded"),
        OperationalError("canceling statement due to statement timeout"),
        TransactionAbortedError("lost connection to mysql server"),
    ],
)
def test_ignores_other_errors(error):
    assert not is_disconnect_error(error)
