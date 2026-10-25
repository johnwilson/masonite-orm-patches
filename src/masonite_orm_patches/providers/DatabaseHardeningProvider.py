import logging
import os

from masonite.providers import Provider

from ..patches.database_hardening import DEFAULT_MAX_IDLE_SECONDS, patch_base_connection


class DatabaseHardeningProvider(Provider):
    """Hardens ORM connections for queue workers and other long-running processes.

    The idle limit comes from the ``DB_MAX_IDLE_SECONDS`` environment variable
    (``0`` disables recycling), falling back to ``max_idle_seconds``. A
    connection in ``DATABASES`` can override it with a ``"max_idle_seconds"``
    key.

    Like ``OrmPatchProvider``, this patches in ``register()`` because Masonite 5
    never calls ``boot()`` for ``craft`` commands such as ``queue:work``.
    """

    max_idle_seconds = DEFAULT_MAX_IDLE_SECONDS

    def register(self):
        applied = patch_base_connection(max_idle_seconds=self._max_idle_seconds())
        if applied:
            self._logger().info(
                "masonite-orm-patches: hardened " + ", ".join(applied)
            )

    def boot(self):
        pass

    def _max_idle_seconds(self):
        value = os.getenv("DB_MAX_IDLE_SECONDS", "").strip()
        if not value:
            return self.max_idle_seconds
        try:
            return float(value) or None
        except ValueError:
            self._logger().warning(
                f"masonite-orm-patches: ignoring invalid DB_MAX_IDLE_SECONDS={value!r}"
            )
            return self.max_idle_seconds

    def _logger(self):
        # Bound by LoggingProvider when it is registered before this provider.
        if self.application.has("logger"):
            return self.application.make("logger")
        return logging.getLogger("masonite_orm_patches")
