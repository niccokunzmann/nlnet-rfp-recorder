from django.apps import AppConfig
from django.db.models.signals import post_migrate


def _install_sync_triggers(sender, using="default", **kwargs) -> None:
    from django.db import connections

    from .triggers import install

    install(connections[using])


class TimetrackingConfig(AppConfig):
    default_auto_field = "django.db.models.BigAutoField"
    name = "nlnet_rfp_recorder.timetracking"

    def ready(self) -> None:
        post_migrate.connect(_install_sync_triggers, sender=self)
