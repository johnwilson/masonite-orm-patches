# masonite-orm-patches

A Masonite 5 service provider that fixes two Masonite ORM 3.x bugs at runtime,
so you don't need to fork the ORM or edit vendored files.

| Upstream issue | Symptom | Patched method(s) |
| --- | --- | --- |
| [masonitedev/orm#12](https://github.com/masonitedev/orm/issues/12) | `Post.with_("tags").get()` raises `NotImplementedError: BelongsToMany relationship does not implement the 'related_result' method` | `BelongsToMany.map_related`, `BelongsToMany.register_related` |
| [masonitedev/orm#17](https://github.com/masonitedev/orm/issues/17) | `is_dirty()` returns `True` after assigning a value equal to the original, and `False` after `fill()` with a changed value | `Model.is_dirty` |
| — | `User.with_("roles").first().roles` is `None` instead of an empty collection when the user has no roles | `QueryBuilder._register_relationships_to_model` |

Requires Python 3.10+, `masonite-framework` 5.x and `masonite-framework-orm` 3.1+.

## Installation

From Git:

```bash
pip install "masonite-orm-patches @ git+https://github.com/<your-org>/masonite-orm-patches.git@v0.1.0"
```

In `requirements.txt`:

```text
masonite-orm-patches @ git+https://github.com/<your-org>/masonite-orm-patches.git@v0.1.0
```

From a private index:

```bash
pip install masonite-orm-patches --extra-index-url https://pypi.example.com/simple
```

## Register the provider

Add `OrmPatchProvider` to `config/providers.py` after `ORMProvider` and
`LoggingProvider`:

```python
# config/providers.py
from masonite_orm_patches.providers import OrmPatchProvider

PROVIDERS = [
    FrameworkProvider,
    # ...
    ORMProvider,
    LoggingProvider,
    AppProvider,
    OrmPatchProvider,
]
```

The provider applies the patches in `register()`, not `boot()`. Masonite 5 calls
`boot()` on every HTTP request and never for `craft` commands. `register()` runs
once per process, so the fixes also apply to queue workers, seeders, scheduled
tasks and tests.

When the patches are applied, one line is logged through the app's logger. If
the provider is registered before `LoggingProvider`, the line goes to the
standard `masonite_orm_patches` logger instead.

```text
masonite-orm-patches: patched Model.is_dirty, BelongsToMany.map_related, BelongsToMany.register_related, QueryBuilder._register_relationships_to_model
```

Applying the patches more than once has no effect.

### Without the provider

When you use the ORM outside a Masonite app, such as in a standalone script,
call this once at startup:

```python
from masonite_orm_patches import apply_patches

apply_patches()
```

`remove_patches()` restores the upstream methods.

## What changes

### `Model.is_dirty(*attributes)`

The model's current state is `__attributes__` overlaid with
`__dirty_attributes__`. It is compared by value against
`__original_attributes__`, the snapshot taken at hydration.

```python
user = User.where("email", "ada@example.com").first()
user.email = "ada@example.com"
user.is_dirty()                  # False (upstream: True)

user.fill({"name": "Ada Lovelace"})
user.is_dirty()                  # True  (upstream: False)
user.is_dirty("name")            # True
user.is_dirty("email")           # False
```

- With no arguments, the model is dirty if any key was added or removed, or any
  value differs.
- With arguments, only the named attributes are checked.
- Date columns and `__casts__` columns are normalized before comparing, so
  reassigning `"2026-01-02 03:04:05"` to a date or an equal dict to a `json`
  cast doesn't count as a change.
- Bookkeeping the ORM adds itself is ignored: the `builder` key, the
  `m_reserved*` pivot aliases and the attached `pivot` model. Models loaded
  through `BelongsToMany` therefore start out clean.
- If an attribute dict is missing, it is treated as empty.

`save()` doesn't call `is_dirty()`, so saving behaves the same as before.

### `BelongsToMany` eager loading

Upstream `BelongsToMany.get_related` already fetches every parent's related rows
in one joined query, with the parent key aliased as `<pivot_table>_id`. The query
builder then calls `map_related()` to hand those rows out to the parents.
`BelongsToMany` doesn't implement `map_related()`, which is the bug.

The patch makes two changes:

- It adds `map_related()`, which groups the rows by parent key once.
- It replaces `register_related()` so each parent's rows come from a dict lookup
  rather than a `Collection.where` scan per parent.

Parents without related rows get an empty `Collection`. Each related model keeps
its pivot in `get_original("pivot")`. Constrained eager loads work too, for
example `with_({"roles": lambda q: q.where(...)})`.

```python
users = User.with_("roles").get()   # 2 queries: users, then roles + pivot
for user in users:
    user.roles                       # Collection, no extra query
```

### Empty eager-loaded to-many relations

When an eager load finds no rows, upstream stores `None` for the relation. This
happens in two cases:

- The query returns a single model, as with `.first()` or `.find()`, and that
  model has no related rows.
- The query returns a collection and none of the parents has related rows.

The patch stores an empty `Collection` instead, so code like
`for role in user.roles` works without a `None` check. This applies to the
to-many relationships: `BelongsToMany`, `HasMany`, `HasManyThrough` and
`MorphMany`. Each model gets its own empty collection.

```python
linus = User.with_("roles").where("name", "Linus").first()
linus.roles          # Collection([])  (upstream: None)
```

To-one relationships (`HasOne`, `BelongsTo`, `HasOneThrough`, `MorphOne`,
`MorphTo`, `MorphToMany`) still resolve to `None` when there is no row, because
`None` is the correct empty value for a single related model.

## Development

```bash
pip install -e ".[test]"
pytest
```

The tests cover the following:

- `test_is_dirty.py`: `is_dirty` on hydrated models.
- `test_belongs_to_many.py`: eager loading against a temporary SQLite file. It
  checks that the unpatched ORM raises, then counts queries to confirm that the
  relation loads in one query.
- `test_empty_relations.py`: empty to-many relations become collections, and
  to-one relations stay `None`.
- `test_provider.py`: registers the provider on a real Masonite `Application`.

## Removing the package

Once upstream ships fixes for #12 and #17, remove `OrmPatchProvider` from
`config/providers.py` and uninstall the package.
