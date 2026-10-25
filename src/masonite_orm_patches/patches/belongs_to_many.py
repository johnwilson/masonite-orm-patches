"""Fix for masonitedev/orm#12: eager loading ``BelongsToMany`` raises.

When ``Model.with_("tags").get()`` returns several parents, the query builder
eager loads every relation in one query and then calls::

    mapped = relationship.map_related(result_set)
    for model in parents:
        relationship.register_related(relation_key, model, mapped)

``BelongsToMany`` never implemented ``map_related``, so the base class raises
``NotImplementedError: BelongsToMany relationship does not implement the
'related_result' method``.

``BelongsToMany.get_related`` already runs the single joined query and
attaches the pivot to each related model, aliasing the parent key as
``<pivot_table>_id``. These patches group that result by the alias once,
then assign each parent its group (or an empty collection) in constant time.
"""

from masoniteorm.collection import Collection


def _parent_key_alias(relationship) -> str:
    # Matches the alias selected in BelongsToMany.make_query().
    return f"{relationship._table}_id"


def patched_map_related(self, related_result):
    """Group the eager-loaded related models by their parent's key."""
    alias = _parent_key_alias(self)
    grouped = {}
    for related in related_result or []:
        parent_id = related.get_raw_attribute(alias)
        grouped.setdefault(parent_id, []).append(related)

    return {parent_id: Collection(items) for parent_id, items in grouped.items()}


def patched_register_related(self, key, model, collection):
    """Attach the parent's related models, defaulting to an empty collection."""
    parent_id = getattr(model, self.local_owner_key)

    if isinstance(collection, dict):
        related = collection.get(parent_id)
    else:
        # Called with an un-mapped result set (upstream's original contract).
        related = collection.where(_parent_key_alias(self), parent_id)

    model.add_relation({key: related if related is not None else Collection([])})
