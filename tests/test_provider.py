import logging

import pytest
from masonite.foundation import Application
from masoniteorm.models import Model
from masoniteorm.relationships.BelongsToMany import BelongsToMany

from masonite_orm_patches import remove_patches
from masonite_orm_patches.patches import patched_is_dirty
from masonite_orm_patches.providers import OrmPatchProvider


@pytest.fixture
def application():
    # Container bindings live in a class-level dict shared by every instance.
    bindings = dict(Application.objects)
    yield Application(base_path="")
    Application.objects.clear()
    Application.objects.update(bindings)
    remove_patches()


class RecordingLogger:
    def __init__(self):
        self.messages = []

    def info(self, message):
        self.messages.append(message)


def test_register_installs_patches(application):
    application.add_providers(OrmPatchProvider)

    assert Model.is_dirty.__wrapped__ is patched_is_dirty
    assert "map_related" in BelongsToMany.__dict__
    assert "register_related" in BelongsToMany.__dict__


def test_logs_through_masonite_logger_once(application):
    logger = RecordingLogger()
    application.bind("logger", logger)

    application.add_providers(OrmPatchProvider)
    # Masonite 5 calls boot() on every request; it must not re-patch or re-log.
    provider = application.get_providers()[0]
    provider.boot()
    application.add_providers(OrmPatchProvider)

    assert logger.messages == [
        "masonite-orm-patches: patched Model.is_dirty, "
        "BelongsToMany.map_related, BelongsToMany.register_related, "
        "QueryBuilder._register_relationships_to_model, "
        "QueryBuilder.order_by, QueryBuilder.group_by"
    ]


def test_falls_back_to_stdlib_logging_without_logger_binding(application, caplog):
    caplog.set_level(logging.INFO, logger="masonite_orm_patches")
    application.add_providers(OrmPatchProvider)

    assert "patched Model.is_dirty" in caplog.text


def test_remove_patches_restores_upstream(application):
    original_is_dirty = Model.__dict__["is_dirty"]
    application.add_providers(OrmPatchProvider)
    remove_patches()

    assert Model.__dict__["is_dirty"] is original_is_dirty
    assert "map_related" not in BelongsToMany.__dict__


@pytest.fixture
def hardening_settings():
    from masonite_orm_patches import unpatch_base_connection
    from masonite_orm_patches.patches import database_hardening

    yield database_hardening._settings
    unpatch_base_connection()


def test_hardening_provider_patches_drivers_and_logs_once(
    application, hardening_settings, monkeypatch
):
    from masoniteorm.connections import MySQLConnection, PostgresConnection

    from masonite_orm_patches.providers import DatabaseHardeningProvider

    monkeypatch.delenv("DB_MAX_IDLE_SECONDS", raising=False)
    logger = RecordingLogger()
    application.bind("logger", logger)

    application.add_providers(DatabaseHardeningProvider)
    application.add_providers(DatabaseHardeningProvider)

    for driver in (MySQLConnection, PostgresConnection):
        for name in ("query", "begin", "commit", "rollback"):
            assert hasattr(driver.__dict__[name], "__original__")
    assert hardening_settings["max_idle_seconds"] == 300
    assert len(logger.messages) == 1
    assert logger.messages[0].startswith(
        "masonite-orm-patches: hardened MySQLConnection.query"
    )


@pytest.mark.parametrize(
    "value, expected", [("60", 60.0), ("0", None), (" ", 300), ("soon", 300)]
)
def test_hardening_provider_reads_idle_limit_from_env(
    application, hardening_settings, monkeypatch, value, expected
):
    from masonite_orm_patches.providers import DatabaseHardeningProvider

    monkeypatch.setenv("DB_MAX_IDLE_SECONDS", value)
    application.add_providers(DatabaseHardeningProvider)

    assert hardening_settings["max_idle_seconds"] == expected
