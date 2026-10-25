import pytest
from masoniteorm.models import Model


class User(Model):
    __table__ = "users"
    __fillable__ = ["name", "email", "settings"]
    __casts__ = {"settings": "json"}


def hydrated(**attributes):
    attributes = {"id": 1, "name": "Ada", "email": "ada@example.com", **attributes}
    return User.hydrate(attributes)


pytestmark = pytest.mark.usefixtures("patched")


def test_clean_hydrated_model_is_not_dirty():
    assert hydrated().is_dirty() is False


def test_assigning_the_same_value_is_not_dirty():
    user = hydrated()
    user.email = "ada@example.com"
    assert user.is_dirty() is False


def test_fill_with_the_same_value_is_not_dirty():
    # The exact scenario from masonitedev/orm#17.
    user = hydrated().fill({"email": "ada@example.com"})
    assert user.is_dirty() is False


def test_modified_attribute_is_dirty():
    user = hydrated()
    user.email = "grace@example.com"
    assert user.is_dirty() is True


def test_fill_with_a_new_value_is_dirty():
    # Upstream returns False here because fill() bypasses __dirty_attributes__.
    user = hydrated().fill({"email": "grace@example.com"})
    assert user.is_dirty() is True


def test_newly_assigned_key_is_dirty():
    user = hydrated()
    user.nickname = "countess"
    assert user.is_dirty() is True


def test_deleted_key_is_dirty():
    user = hydrated()
    user.delete_attribute("email")
    assert user.is_dirty() is True


def test_targeted_check_only_inspects_named_attributes():
    user = hydrated()
    user.name = "Ada Lovelace"
    user.email = "ada@example.com"

    assert user.is_dirty("name") is True
    assert user.is_dirty("email") is False
    assert user.is_dirty("email", "name") is True
    assert user.is_dirty("missing") is False


def test_cast_attribute_reassigned_with_equal_value_is_not_dirty():
    user = hydrated(settings='{"theme": "dark"}')
    user.settings = {"theme": "dark"}
    assert user.is_dirty("settings") is False

    user.settings = {"theme": "light"}
    assert user.is_dirty("settings") is True


def test_date_reassigned_with_equal_value_is_not_dirty():
    user = hydrated(created_at="2026-01-02 03:04:05")
    user.created_at = "2026-01-02 03:04:05"
    assert user.is_dirty() is False


def test_new_model_with_attributes_is_dirty():
    user = User()
    assert user.is_dirty() is False
    user.name = "Ada"
    assert user.is_dirty() is True


def test_missing_attribute_dicts_fall_back_to_empty():
    user = User()
    for name in ("__attributes__", "__original_attributes__", "__dirty_attributes__"):
        user.__dict__.pop(name)
    assert user.is_dirty() is False
