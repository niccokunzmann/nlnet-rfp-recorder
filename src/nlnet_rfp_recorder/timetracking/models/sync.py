from __future__ import annotations

import re
import socket
import uuid
from datetime import UTC, datetime

from django.db import models

MACHINE_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$")


def sync_now() -> str:
    """Now in UTC with milliseconds - the same text SQLite's
    strftime('%Y-%m-%dT%H:%M:%fZ', 'now') makes, so both sort together."""
    moment = datetime.now(UTC)
    return f"{moment:%Y-%m-%dT%H:%M:%S}.{moment.microsecond // 1000:03d}Z"


class SyncSettings(models.Model):
    """How `rfp sync` reaches the git repository - one row per database.

    `name` and `email` are used for the sync commits only, through git's
    environment variables: they never touch the global or the repository's
    git config.
    """

    url = models.CharField(
        max_length=1024,
        blank=True,
        default="",
        help_text="The private git repository to sync with (`rfp sync --set-url`).",
    )
    name = models.CharField(
        max_length=255,
        blank=True,
        default="",
        help_text="Author name of sync commits (`rfp sync --set-name`).",
    )
    email = models.CharField(
        max_length=255,
        blank=True,
        default="",
        help_text="Author email of sync commits (`rfp sync --set-email`).",
    )
    auto = models.BooleanField(
        default=True,
        help_text=(
            "Whether every command syncs before and after it runs "
            "(`rfp sync --auto yes/no`)."
        ),
    )
    machine = models.CharField(
        max_length=64,
        blank=True,
        default="",
        help_text=(
            "This computer's id: its file in the repository is <machine>.jsonl "
            "and changes made here are marked with it (`rfp sync --set-machine`)."
        ),
    )
    host = models.CharField(
        max_length=255,
        blank=True,
        default="",
        help_text=(
            "The host name `machine` was made for. On another host the "
            "database was copied, and the copy gets an id of its own."
        ),
    )

    @classmethod
    def get(cls) -> SyncSettings:
        settings = cls.objects.order_by("id").first()
        if settings is None:
            settings = cls.objects.create()
        host = socket.gethostname()
        if not settings.machine or settings.host != host:
            settings.machine = cls.new_machine_id(host)
            settings.host = host
            settings.save(update_fields=["machine", "host"])
        return settings

    @staticmethod
    def new_machine_id(host: str) -> str:
        base = re.sub(r"[^A-Za-z0-9._-]", "-", host).strip("-.") or "computer"
        return f"{base[:56]}-{uuid.uuid4().hex[:6]}"


class SyncDeletion(models.Model):
    """A time entry or link deleted here, written by a database trigger.

    Kept until a push put the deletion into the repository - from then on
    the files remember it (as `"deleted": true`) and the row is removed.
    """

    model = models.CharField(max_length=32, help_text="'link' or 'time_record'.")
    uuid = models.CharField(max_length=32, help_text="The uuid, as 32 hex digits.")
    deleted_at = models.CharField(
        max_length=32, help_text="When it was deleted: ISO 8601 in UTC."
    )
    deleted_by = models.CharField(
        max_length=64,
        blank=True,
        default="",
        help_text="The computer it was deleted on.",
    )

    class Meta:
        constraints = [
            models.UniqueConstraint(
                fields=["model", "uuid"], name="unique_sync_deletion"
            )
        ]
