"""Fix for masonitedev/orm#17: ``Model.is_dirty()`` reports false positives.

Upstream implements ``is_dirty`` as ``bool(self.__dirty_attributes__)``, so
assigning an attribute its *current* value marks the model dirty, while
``fill()`` (which writes straight into ``__attributes__``) never does.

The model keeps three dicts:

* ``__original_attributes__`` - snapshot taken when the row was hydrated
* ``__attributes__``          - loaded values (``fill()`` writes here)
* ``__dirty_attributes__``    - values assigned via ``model.attr = value``

The effective current state is ``__attributes__`` overlaid with
``__dirty_attributes__``; this patch compares that against the original
snapshot by value.
"""

from typing import Any, Dict

from masoniteorm.models import Model

# Keys upstream writes into the attribute dicts for its own bookkeeping.
_INTERNAL_KEYS = {"builder"}
# BelongsToMany selects pivot columns under these aliases and then deletes
# them from ``__attributes__`` (but not from ``__original_attributes__``).
_RESERVED_PREFIX = "m_reserved"


def _attributes(model, name: str) -> Dict[str, Any]:
    # Read straight from __dict__: Model.__getattr__ would otherwise treat a
    # missing dict as a column lookup and raise.
    value = model.__dict__.get(name)
    return value if isinstance(value, dict) else {}


def _current_attributes(model) -> Dict[str, Any]:
    current = dict(_attributes(model, "__attributes__"))
    current.update(_attributes(model, "__dirty_attributes__"))
    for key in _INTERNAL_KEYS:
        current.pop(key, None)
    return current


def _original_attributes(model) -> Dict[str, Any]:
    original = {}
    for key, value in _attributes(model, "__original_attributes__").items():
        if key in _INTERNAL_KEYS or key.startswith(_RESERVED_PREFIX):
            continue
        # Pivot records are attached to the original snapshot only.
        if isinstance(value, Model):
            continue
        original[key] = value
    return original


def _normalize(model, key: str, value: Any) -> Any:
    """Bring a raw or assigned value into a comparable form.

    Assignments go through the model's setters (dates become datetime
    strings, casts are serialized), whereas hydrated values are stored as
    read from the database, so both sides are normalized before comparing.
    """
    if value is None:
        return None

    try:
        if key in model.get_dates():
            return model.get_new_datetime_string(value)

        if key in model.__casts__:
            return model._uncast_value(key, value)
    except Exception:
        # A value the cast cannot parse is compared as-is.
        pass

    return value


def _differs(model, key: str, current: Dict, original: Dict) -> bool:
    if (key in current) != (key in original):
        return True
    if key not in current:
        return False
    return _normalize(model, key, current[key]) != _normalize(
        model, key, original[key]
    )


def patched_is_dirty(self, *attributes: str) -> bool:
    """Return whether the model (or the given attributes) changed since load.

    ``model.is_dirty()`` checks every attribute; ``model.is_dirty("email",
    "name")`` checks only those columns.
    """
    current = _current_attributes(self)
    original = _original_attributes(self)

    if attributes:
        return any(_differs(self, key, current, original) for key in attributes)

    if set(current) - set(original) or set(original) - set(current):
        return True

    return any(_differs(self, key, current, original) for key in current)
