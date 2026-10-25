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
        "QueryBuilder._register_relationships_to_model"
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
