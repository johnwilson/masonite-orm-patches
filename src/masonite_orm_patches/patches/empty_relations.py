"""Eager-loaded to-many relations with no rows resolve to ``None``.

``QueryBuilder._register_relationships_to_model`` only distributes results
when there are some; otherwise it falls through to::

    hydrated_model.add_relation({relation_key: related_result or None})

An empty result collection is falsy, so ``User.with_("roles").first().roles``
is ``None`` for a user without roles (and every parent of a ``get()`` gets
``None`` when no parent has any). To-many relations should yield an empty
``Collection``; to-one relations (``HasOne``, ``BelongsTo``, ...) keep
``None``, which is the correct empty value for them.
"""

from masoniteorm.collection import Collection
from masoniteorm.query import QueryBuilder
from masoniteorm.relationships.BelongsToMany import BelongsToMany
from masoniteorm.relationships.HasMany import HasMany
from masoniteorm.relationships.HasManyThrough import HasManyThrough
from masoniteorm.relationships.MorphMany import MorphMany

TO_MANY_RELATIONSHIPS = (BelongsToMany, HasMany, HasManyThrough, MorphMany)

# Captured at import, before apply_patches() replaces it.
_upstream_register = QueryBuilder._register_relationships_to_model


def patched_register_relationships_to_model(
    self, related, related_result, hydrated_model, relation_key
):
    """Register eager-loaded results, defaulting to-many relations to ``[]``."""
    if related_result or not isinstance(related, TO_MANY_RELATIONSHIPS):
        return _upstream_register(
            self, related, related_result, hydrated_model, relation_key
        )

    models = hydrated_model if isinstance(hydrated_model, Collection) else [hydrated_model]
    for model in models:
        # A separate collection per model, so mutating one leaves the rest alone.
        model.add_relation({relation_key: Collection([])})
    return self
