from importlib import import_module

from django.apps import AppConfig
from django.conf import settings


class CoreConfig(AppConfig):
    default_auto_field = "django.db.models.BigAutoField"
    name = "core"

    def ready(self):
        for mod in getattr(settings, "JIRRABIT_HOOK_MODULES", []):
            try:
                import_module(mod)
            except ImportError:
                pass
