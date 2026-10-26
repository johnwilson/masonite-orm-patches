"""Fix for masonitedev/orm#26: SQL injection via ``order_by`` / ``group_by``.

``QueryBuilder.order_by(column, direction)`` and ``group_by(column)`` compile
their arguments as *identifiers*, which cannot be parameterised. Upstream
relies solely on wrapping the name in the grammar's quote character, and it
never:

* escapes that quote character when it appears inside the name, so a column
  such as ``name`` + quote + trailing text terminates the quoting early and the
  tail is emitted as raw SQL; and
* restricts ``direction``, which ``order_by_format`` (``"{column}
  {direction}"``) interpolates verbatim after the column.

Either lets an attacker who controls an ``order_by`` / ``group_by`` column or
direction (a common pattern: sorting straight from a request query string)
inject arbitrary SQL. These patches validate both arguments against a strict
allowlist before the value ever reaches the grammar, mirroring upstream's
behaviour for every legitimate identifier.

Intentional raw expressions remain available through the untouched
``order_by_raw`` / ``group_by_raw`` methods.
"""

import re

from masoniteorm.expressions.expressions import (
    GroupByExpression,
    OrderByExpression,
)

# A single identifier segment: a leading letter/underscore then word chars.
# ``$`` is allowed because MySQL permits it in identifiers. ``*`` is accepted
# as a whole segment (e.g. an aggregate-free ``group_by``) but nothing else.
_IDENTIFIER = re.compile(r"^(?:\*|[A-Za-z_][A-Za-z0-9_$]*)$")

# Trailing sort keyword baked into the column string, e.g. ``"name desc"``.
_TRAILING_DIRECTION = re.compile(r"\s+(asc|desc)$", re.IGNORECASE)

_DIRECTIONS = {"ASC", "DESC"}


def _reject(kind: str, value) -> "ValueError":
    return ValueError(
        f"masonite-orm-patches: refusing unsafe {kind} {value!r}. "
        "Only plain column identifiers (optionally 'table.column') and the "
        "directions ASC/DESC are allowed; use order_by_raw()/group_by_raw() "
        "for raw expressions."
    )


def _validate_identifier(column) -> None:
    """Raise unless ``column`` is a bare (optionally table-qualified) name."""
    if not isinstance(column, str):
        raise _reject("column", column)

    name = _TRAILING_DIRECTION.sub("", column.strip())

    parts = name.split(".")
    if len(parts) > 2 or any(not _IDENTIFIER.match(part.strip()) for part in parts):
        raise _reject("column", column)


def _validate_direction(direction) -> None:
    if not isinstance(direction, str) or direction.strip().upper() not in _DIRECTIONS:
        raise _reject("sort direction", direction)


def patched_order_by(self, column, direction="ASC"):
    """``order_by`` that rejects non-identifier columns and bad directions."""
    _validate_direction(direction)
    for col in column.split(","):
        _validate_identifier(col)
        self._order_by += (OrderByExpression(col, direction=direction),)
    return self


def patched_group_by(self, column):
    """``group_by`` that rejects anything but plain column identifiers."""
    for col in column.split(","):
        _validate_identifier(col)
        self._group_by += (GroupByExpression(column=col),)
    return self
