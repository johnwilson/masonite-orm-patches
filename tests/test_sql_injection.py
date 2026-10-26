"""Tests for the masonitedev/orm#26 order_by / group_by injection patch."""

import pytest
from masoniteorm.connections import MySQLConnection
from masoniteorm.query import QueryBuilder
from masoniteorm.query.grammars import (
    MSSQLGrammar,
    MySQLGrammar,
    PostgresGrammar,
    SQLiteGrammar,
)

GRAMMARS = [MySQLGrammar, PostgresGrammar, SQLiteGrammar, MSSQLGrammar]

# The grammar-specific identifier quote character an attacker would break out of.
QUOTE = {
    "MySQLGrammar": "`",
    "PostgresGrammar": '"',
    "SQLiteGrammar": '"',
    "MSSQLGrammar": "]",
}

pytestmark = pytest.mark.usefixtures("patched")


def builder(grammar):
    return QueryBuilder(
        grammar=grammar,
        table="users",
        connection_class=MySQLConnection,
        dry=True,
    )


# --- legitimate usage keeps working -----------------------------------------


@pytest.mark.parametrize("grammar", GRAMMARS)
def test_plain_order_by_still_compiles(grammar):
    sql = builder(grammar).order_by("email").to_sql()
    assert "ORDER BY" in sql and "email" in sql


@pytest.mark.parametrize("grammar", GRAMMARS)
def test_order_by_direction_and_multiple_columns(grammar):
    sql = builder(grammar).order_by("name, created_at", "DESC").to_sql()
    assert sql.count(",") == 1
    assert "DESC" in sql


@pytest.mark.parametrize("grammar", GRAMMARS)
def test_trailing_direction_keyword_is_allowed(grammar):
    sql = builder(grammar).order_by("name desc").to_sql()
    assert "DESC" in sql


@pytest.mark.parametrize("grammar", GRAMMARS)
def test_table_qualified_column_is_allowed(grammar):
    sql = builder(grammar).order_by("users.id").to_sql()
    assert "ORDER BY" in sql


@pytest.mark.parametrize("grammar", GRAMMARS)
def test_plain_group_by_still_compiles(grammar):
    sql = builder(grammar).group_by("country").to_sql()
    assert "GROUP BY" in sql and "country" in sql


# --- injection attempts are rejected ----------------------------------------


@pytest.mark.parametrize("grammar", GRAMMARS)
def test_quote_breakout_in_order_by_column_is_rejected(grammar):
    # A column carrying the grammar's own quote char would otherwise terminate
    # the identifier quoting and emit the tail as raw SQL.
    payload = "id" + QUOTE[grammar.__name__] + " extra"
    with pytest.raises(ValueError):
        builder(grammar).order_by(payload)


@pytest.mark.parametrize("grammar", GRAMMARS)
def test_quote_breakout_in_group_by_column_is_rejected(grammar):
    payload = "id" + QUOTE[grammar.__name__] + " extra"
    with pytest.raises(ValueError):
        builder(grammar).group_by(payload)


@pytest.mark.parametrize("grammar", GRAMMARS)
def test_non_whitelisted_direction_is_rejected(grammar):
    # Anything other than ASC/DESC is interpolated verbatim after the column.
    with pytest.raises(ValueError):
        builder(grammar).order_by("id", "DESC, (SELECT 1)")


@pytest.mark.parametrize("grammar", GRAMMARS)
def test_parenthesised_expression_column_is_rejected(grammar):
    with pytest.raises(ValueError):
        builder(grammar).order_by("(SELECT secret FROM admins)")


def test_raw_escape_hatch_is_untouched():
    # order_by_raw remains the supported way to pass a raw expression.
    sql = builder(SQLiteGrammar).order_by_raw("RANDOM()").to_sql()
    assert "RANDOM()" in sql
