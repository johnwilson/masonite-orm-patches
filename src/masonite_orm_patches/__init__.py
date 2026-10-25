"""Runtime fixes for known Masonite ORM bugs.

In a Masonite app, register ``masonite_orm_patches.providers.OrmPatchProvider``.
When using the ORM standalone, call ``apply_patches()`` once at startup.

For long-running workers, also register
``masonite_orm_patches.providers.DatabaseHardeningProvider`` (or call
``patch_base_connection()``).
"""

from .patches import apply_patches, remove_patches
from .patches.database_hardening import (
    CommitOutcomeUnknownError,
    TransactionAbortedError,
    patch_base_connection,
    run_in_transaction,
    unpatch_base_connection,
)

__version__ = "0.1.0"

__all__ = [
    "apply_patches",
    "remove_patches",
    "patch_base_connection",
    "unpatch_base_connection",
    "run_in_transaction",
    "TransactionAbortedError",
    "CommitOutcomeUnknownError",
    "__version__",
]
