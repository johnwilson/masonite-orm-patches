import sys
from types import ModuleType

import pytest

DATABASES = {
    "default": "sqlite",
    "sqlite": {"driver": "sqlite", "database": None, "prefix": "", "log_queries": False},
}


def _install_config(database_path):
    """Provide the ``config.database`` module the ORM loads its settings from."""
    from masoniteorm.connections import ConnectionResolver

    DATABASES["sqlite"]["database"] = str(database_path)
    database = ModuleType("config.database")
    database.DATABASES = DATABASES
    database.DB = ConnectionResolver().set_connection_details(DATABASES)

    config = ModuleType("config")
    config.database = database
    sys.modules["config"] = config
    sys.modules["config.database"] = database


@pytest.fixture(autouse=True)
def orm_config(tmp_path):
    # Instantiating any model loads config.database, so every test needs it.
    # SQLite connections are opened per query, so ":memory:" would not
    # survive between statements; use a throwaway file instead.
    _install_config(tmp_path / "test.sqlite3")


@pytest.fixture
def database(orm_config):
    from masoniteorm.schema import Schema

    schema = Schema(connection="sqlite", connection_details=DATABASES)
    yield schema


@pytest.fixture
def patched():
    from masonite_orm_patches import apply_patches, remove_patches

    apply_patches()
    yield
    remove_patches()
