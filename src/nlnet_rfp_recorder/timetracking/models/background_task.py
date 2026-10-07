from __future__ import annotations

import os
import time
from collections.abc import Callable
from datetime import datetime, timedelta

from django.db import OperationalError, connection, models
from django.db.migrations.loader import MigrationLoader
from django.db.migrations.recorder import MigrationRecorder
from django.utils import timezone

from .time_record import TimeRecord

# A background task that hasn't been heard from for longer than this
# (it checks in every minute) is considered dead, e.g. killed or crashed.
STALE_AFTER = timedelta(minutes=3)

# A one-second check that finds more than this much time has passed means
# the machine was suspended in between.
SUSPEND_GAP = timedelta(minutes=1)
HEARTBEAT_INTERVAL = timedelta(minutes=1)


def _process_exists(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


class BackgroundTask(models.Model):
    """The single record of `rfp background-task` - whether it is running.

    The task updates `last_seen` every minute, so other commands can
    tell a live task from one that died without cleaning up.
    """

    started = models.DateTimeField(help_text="When the background task was started.")
    last_seen = models.DateTimeField(
        help_text="The last time the background task reported that it is running."
    )
    pid = models.PositiveIntegerField(
        default=0, help_text="The process id of the background task."
    )
    is_running = models.BooleanField(
        default=False,
        help_text="False once the background task exited cleanly.",
    )

    @classmethod
    def get(cls) -> BackgroundTask | None:
        return cls.objects.first()

    @classmethod
    def is_alive(cls, now: datetime | None = None) -> bool:
        task = cls.get()
        if task is None or not task.is_running:
            return False
        if not _process_exists(task.pid):
            return False
        return (now or timezone.now()) - task.last_seen < STALE_AFTER

    @classmethod
    def register(cls, now: datetime | None = None) -> BackgroundTask:
        """Record that a background task starts running now.

        Raises ValueError if another one is still alive.
        """
        now = now or timezone.now()
        if cls.is_alive(now):
            raise ValueError("A background task is already running.")
        cls.objects.all().delete()
        return cls.objects.create(
            started=now, last_seen=now, pid=os.getpid(), is_running=True
        )

    @classmethod
    def recover(
        cls, now: datetime | None = None
    ) -> tuple[BackgroundTask, TimeRecord | None, TimeRecord | None] | None:
        """Clean up after a background task that never shut down properly.

        A task that died without unregistering (the machine was shut
        down or crashed - no clean exit can be relied on) leaves its
        row marked running. The time entry it was watching over then
        gets the same treatment as for a suspend: it is ended at the
        task's `last_seen`, the last moment the machine was known to be
        up. An entry that started after that is left alone.

        Returns the dead task's row, the entry that was stopped (or None)
        and the running entry that was left alone because it started
        after `last_seen` (or None). Returns None if there is nothing to
        recover from - no previous task, one that exited cleanly, or one
        that is still alive. Call this before register(), which replaces
        the row.
        """
        previous = cls.get()
        if previous is None or not previous.is_running or cls.is_alive(now):
            return None
        stopped = left_running = None
        running = TimeRecord.get_running()
        if running is not None:
            if running.start_time < previous.last_seen:
                stopped = TimeRecord.stop(end_time=previous.last_seen)
            else:
                left_running = running
        previous.unregister()
        return previous, stopped, left_running

    def touch(self, now: datetime | None = None) -> None:
        self.last_seen = now or timezone.now()
        self.save(update_fields=["last_seen"])

    def unregister(self) -> None:
        self.is_running = False
        self.save(update_fields=["is_running"])

    def watch(
        self,
        on_suspend: Callable[[datetime, datetime, TimeRecord | None], None]
        | None = None,
        sleep: Callable[[float], None] = time.sleep,
        now: Callable[[], datetime] = datetime.now,
        ticks: int | None = None,
    ) -> bool:
        """Run the loop: check the time every second and detect suspends.

        A gap of more than SUSPEND_GAP between two checks means the
        machine was suspended; the running time entry (if any) is then
        ended at the last check before it. `on_suspend(before, after,
        stopped)` is called with the times around the gap and the entry
        that was stopped, or None. Also reports in every minute (see
        touch()). `ticks` limits the number of checks (None: forever).

        Returns True if it stopped because the database schema was
        changed by a newer version (noticed at the next heartbeat, or
        sooner by a query failing against the new schema) - the caller
        should restart the process so it runs the new code - and False
        when `ticks` ran out.
        """
        known_migrations = set(MigrationLoader(None).disk_migrations)
        previous = heartbeat = now()
        while ticks is None or ticks > 0:
            sleep(1)
            current = now()
            try:
                if current - previous > SUSPEND_GAP:
                    stopped = None
                    running = TimeRecord.get_running()
                    if running is not None:
                        stopped = TimeRecord.stop(
                            end_time=max(previous, running.start_time)
                        )
                    if on_suspend is not None:
                        on_suspend(previous, current, stopped)
                if current - heartbeat >= HEARTBEAT_INTERVAL:
                    self.touch(current)
                    heartbeat = current
                    if self.schema_changed(known_migrations):
                        return True
            except OperationalError:
                # A newer version migrated the database between two
                # heartbeats, so this process's models no longer match
                # its tables (e.g. "no such column"). Anything else is a
                # real error.
                if self.schema_changed(known_migrations):
                    return True
                raise
            previous = current
            if ticks is not None:
                ticks -= 1
        return False

    @staticmethod
    def schema_changed(known_migrations: set[tuple[str, str]]) -> bool:
        """Whether the database has migrations applied that this process's
        code doesn't have - i.e. a newer version migrated it."""
        applied = MigrationRecorder(connection).applied_migrations()
        return not set(applied) <= known_migrations
