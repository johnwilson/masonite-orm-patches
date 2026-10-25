import logging

from masonite.providers import Provider

from ..patches import apply_patches


class OrmPatchProvider(Provider):
    """Applies the masonite-orm-patches runtime fixes.

    Patches are installed in ``register()`` rather than ``boot()``: Masonite 5
    runs ``boot()`` on every HTTP request and never for ``craft`` commands
    (queue workers, seeders, scheduled tasks), while ``register()`` runs once
    per process in both cases.
    """

    def register(self):
        applied = apply_patches()
        if applied:
            self._logger().info("masonite-orm-patches: patched " + ", ".join(applied))

    def boot(self):
        pass

    def _logger(self):
        # Bound by LoggingProvider when it is registered before this provider.
        if self.application.has("logger"):
            return self.application.make("logger")
        return logging.getLogger("masonite_orm_patches")
