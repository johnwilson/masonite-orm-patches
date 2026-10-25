"""Runtime fixes for known Masonite ORM bugs.

In a Masonite app, register ``masonite_orm_patches.providers.OrmPatchProvider``.
When using the ORM standalone, call ``apply_patches()`` once at startup.
"""

from .patches import apply_patches, remove_patches

__version__ = "0.1.0"

__all__ = ["apply_patches", "remove_patches", "__version__"]
