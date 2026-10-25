"""Runtime patches for known Masonite ORM bugs."""

from masoniteorm.models import Model
from masoniteorm.query import QueryBuilder
from masoniteorm.relationships.BelongsToMany import BelongsToMany

from .belongs_to_many import patched_map_related, patched_register_related
from .empty_relations import patched_register_relationships_to_model
from .is_dirty import patched_is_dirty

# (class, method name, replacement)
PATCHES = [
    (Model, "is_dirty", patched_is_dirty),
    (BelongsToMany, "map_related", patched_map_related),
    (BelongsToMany, "register_related", patched_register_related),
    (
        QueryBuilder,
        "_register_relationships_to_model",
        patched_register_relationships_to_model,
    ),
]

# Marker set on every function we install, so repeated calls are no-ops.
_MARKER = "__masonite_orm_patch__"


def _is_patched(owner, name):
    return getattr(owner.__dict__.get(name), _MARKER, False)


def apply_patches():
    """Install every patch. Safe to call more than once.

    Returns the names of the patches that were newly installed.
    """
    applied = []
    for owner, name, replacement in PATCHES:
        if _is_patched(owner, name):
            continue

        def patched(*args, _replacement=replacement, **kwargs):
            return _replacement(*args, **kwargs)

        patched.__name__ = name
        patched.__doc__ = replacement.__doc__
        patched.__wrapped__ = replacement
        # None when the method was inherited (BelongsToMany.map_related).
        patched.__original__ = owner.__dict__.get(name)
        setattr(patched, _MARKER, True)

        setattr(owner, name, patched)
        applied.append(f"{owner.__name__}.{name}")
    return applied


def remove_patches():
    """Restore the original upstream implementations."""
    for owner, name, _ in PATCHES:
        if not _is_patched(owner, name):
            continue
        original = owner.__dict__[name].__original__
        if original is None:
            delattr(owner, name)
        else:
            setattr(owner, name, original)


__all__ = [
    "apply_patches",
    "remove_patches",
    "patched_is_dirty",
    "patched_map_related",
    "patched_register_related",
    "patched_register_relationships_to_model",
]
