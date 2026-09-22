from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta
from typing import Literal

from django.conf import settings
from django.db import models
from django.utils import timezone

from nlnet_rfp_recorder.timesheet import TimesheetRow, format_hhmmss, parse_hhmmss

from .link import Link
from .mou import MoU
from .task import Task


def _window_bound(value: date | datetime | None) -> datetime | None:
    """Normalize a get_statistics() `start`/`end` bound to a datetime.

    A bare date means midnight of that day; a datetime passes through
    unchanged; None stays unbounded (the caller decides what that means
    on each side).
    """
    if value is None or isinstance(value, datetime):
        return value
    return datetime.combine(value, time.min)


@dataclass
class TimeSpan:
    """One TimeRecord's overlap with a get_statistics() window.

    `start`/`end` are the record's own start_time/end_time clipped to the
    window, not copies of the raw fields - a session that runs across the
    window's edge (e.g. over midnight) contributes only the part that
    actually falls inside it, via this span's own `duration`.
    """

    record: TimeRecord
    start: datetime
    end: datetime

    @property
    def duration(self) -> timedelta:
        return self.end - self.start


class TimeRecordManager(models.Manager):
    def running(self) -> models.QuerySet[TimeRecord]:
        return self.filter(end_time__isnull=True)


class TimeRecord(models.Model):
    link = models.ForeignKey(
        Link,
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name="time_records",
        help_text=(
            "The link this time entry was tracked against, or None if it has none."
        ),
    )
    start_time = models.DateTimeField(help_text="When this time entry started.")
    end_time = models.DateTimeField(
        null=True,
        blank=True,
        help_text="When this time entry stopped, or None while it's still running.",
    )
    report_line = models.ForeignKey(
        "ReportLine",
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name="time_records",
        help_text=(
            "The report line this time entry has been claimed by, or "
            "None if unreported."
        ),
    )

    objects = TimeRecordManager()

    @classmethod
    def start(
        cls,
        url: str,
        task: Task | None = None,
        tags: Iterable[str] = ("implementation",),
        start_time: datetime | None = None,
    ) -> TimeRecord:
        """Start (or resume) tracking `url` under `task`.

        If the running entry (if any) is already for this exact link,
        nothing is stopped or created - starting what's already running
        just reconciles its task/tags and returns it unchanged.

        Otherwise, whatever's running is stopped first. If the entry that
        was just stopped - or, when nothing was running, the most
        recently stopped one - was for this same link, it is reopened
        (its end_time cleared) instead of creating a new row: a quick
        stop/start on the same thing is one continuous session, not two
        fragments.

        `start_time` fixes when a freshly created entry is recorded as
        starting (and when the previous one is recorded as stopping) -
        callers capture it up front so unrelated delays elsewhere in the
        command never show up as recorded time. It has no effect on a
        resumed entry, which keeps its original start_time.

        `task` (or, absent that, the currently selected task) may be
        None - the entry still starts, tracked against a taskless link;
        the caller is responsible for getting it assigned a task
        afterwards (see cli._prompt_for_task).
        """
        task = task or Task.get_selected()
        link = Link.get_or_create_for_task(url, task)
        for tag in tags:
            link.add_tag(tag)

        running = cls.get_running()
        if running is not None and running.link_id == link.id:
            return running
        if running is not None:
            cls.stop(end_time=start_time)

        resumable = cls.get_last()
        if (
            resumable is not None
            and not resumable.is_running
            and resumable.link_id == link.id
        ):
            resumable.end_time = None
            resumable.save(update_fields=["end_time"])
            return resumable

        return cls.objects.create(link=link, start_time=start_time or timezone.now())

    @classmethod
    def get_running(cls) -> TimeRecord | None:
        return cls.objects.running().order_by("-start_time").first()

    @classmethod
    def get_last(cls) -> TimeRecord | None:
        return cls.objects.order_by("-start_time").first()

    @classmethod
    def stop(
        cls, url: str | None = None, end_time: datetime | None = None
    ) -> TimeRecord | None:
        record = cls.get_running()
        if record is None:
            return None
        if url is not None and record.link is not None and record.link.task is not None:
            record.link = Link.get_or_create_for_task(url, record.link.task)
        record.end_time = end_time or timezone.now()
        record.save()
        return record

    @classmethod
    def continue_last(cls, start_time: datetime | None = None) -> TimeRecord:
        """Start a brand new time entry for the same link as the most
        recently stopped one.

        Unlike start() called again on that same link - which reopens
        the existing entry (see start()'s docstring) - this always
        creates a fresh row, e.g. so a new work session becomes its own
        report line instead of merging into the last one.
        """
        last = cls.get_last()
        if last is None:
            raise ValueError("No previous time entry to continue.")
        if last.is_running:
            raise ValueError(
                "A time entry is already running. Stop it first (`rfp stop`)."
            )
        if last.link is None:
            raise ValueError("The last time entry has no link to continue.")

        return cls.objects.create(
            link=last.link, start_time=start_time or timezone.now()
        )

    @classmethod
    def get_statistics(
        cls,
        start: date | datetime | None = None,
        end: date | datetime | None = None,
    ) -> list[TimeSpan]:
        """Every time entry overlapping [start, end), clipped to it.

        The base method behind all stats computation - see
        nlnet_rfp_recorder.statistics.Statistics, which groups and sums
        these spans per task.

        `start`/`end` each accept a date (midnight of that day), an exact
        datetime, or None - `start=None` is unbounded (from the
        beginning), `end=None` means "now" (also what a still-running
        entry's live end is treated as).

        An entry doesn't have to fall entirely inside the window to be
        included: one that started earlier, or is still running past
        `end`, is returned with its start/end clipped to the window - so
        a session that crosses midnight splits correctly between, say,
        `Statistics.today()` and the day before, instead of being
        dropped or counted in full on just one side.
        """
        window_start = _window_bound(start)
        window_end = _window_bound(end) or timezone.now()

        records = cls.objects.filter(start_time__lt=window_end).select_related(
            "link__task"
        )
        if window_start is not None:
            records = records.filter(
                models.Q(end_time__gt=window_start) | models.Q(end_time__isnull=True)
            )

        spans = []
        for record in records:
            record_end = record.end_time or timezone.now()
            entry_start = (
                max(record.start_time, window_start)
                if window_start is not None
                else record.start_time
            )
            entry_end = min(record_end, window_end)
            if entry_end > entry_start:
                spans.append(TimeSpan(record=record, start=entry_start, end=entry_end))
        return spans

    @property
    def is_running(self) -> bool:
        return self.end_time is None

    @property
    def duration(self) -> timedelta:
        return (self.end_time or timezone.now()) - self.start_time

    def set_duration(
        self, duration: timedelta, fix: Literal["start", "end"] = "end"
    ) -> None:
        """Change this entry's duration to `duration` by moving one end
        of it, leaving the other exactly where it was.

        fix="end" (the default) keeps the end fixed - a stopped entry's
        end_time, or "now" for a still-running one - and moves the start
        back or forward to match. A running entry's end_time is never
        touched here, so it stays running; its duration then keeps
        growing from `duration` as time passes, same as any other
        running entry.

        fix="start" instead keeps start_time fixed and moves the end -
        but only for an entry that already has one. A running entry has
        no end_time to hold onto (and set_duration isn't `stop`, so it
        never invents one), so a running entry always behaves as if
        fix="end", no matter what was asked for.

        A negative `duration` (from add_duration subtracting more than
        was there) clamps to zero rather than putting the moved end
        before the fixed one.
        """
        duration = max(duration, timedelta())
        if self.is_running or fix == "end":
            effective_end = self.end_time or timezone.now()
            self.start_time = effective_end - duration
            self.save(update_fields=["start_time"])
        else:
            self.end_time = self.start_time + duration
            self.save(update_fields=["end_time"])

    def add_duration(
        self, delta: timedelta, fix: Literal["start", "end"] = "end"
    ) -> None:
        """Adjust this entry's duration by `delta` (negative to shrink
        it), via the same fix semantics as set_duration.
        """
        self.set_duration(self.duration + delta, fix=fix)

    @property
    def budget(self) -> float | None:
        if settings.RFP_EUROS_PER_HOUR is None:
            return None
        return self.duration.total_seconds() / 3600 * settings.RFP_EUROS_PER_HOUR

    @property
    def row(self) -> TimesheetRow:
        task = self.link.task if self.link else None
        mou = task.mou if task else None
        tags = (
            ",".join(sorted(tag.name for tag in self.link.tags.all()))
            if self.link
            else ""
        )
        return TimesheetRow(
            pk=self.pk,
            mou=mou.name if mou else "",
            task=task.name if task else "",
            start=self.start_time.isoformat(),
            duration=format_hhmmss(self.duration),
            link=self.link.url if self.link else "",
            tags=tags,
            report=self.report_line.report_id if self.report_line else "",
        )

    @classmethod
    def apply_row(
        cls, row: TimesheetRow, allow_create_with_pk: bool = False
    ) -> TimeRecord:
        from .report import Report

        try:
            mou = MoU.objects.get(name=row.mou)
        except MoU.DoesNotExist:
            raise ValueError(
                f"No such MoU: {row.mou}. Run `rfp mou add {row.mou}` first."
            ) from None
        try:
            task = Task.objects.get(mou=mou, name=row.task)
        except Task.DoesNotExist:
            raise ValueError(
                f"No such task: {row.task} for MoU {row.mou}. "
                f"Run `rfp task select {row.task}` first."
            ) from None

        link = Link.get_or_create_for_task(row.link, task) if row.link else None
        if link is not None:
            for tag in filter(None, (t.strip() for t in row.tags.split(","))):
                link.add_tag(tag)

        report = None
        if row.report:
            try:
                report = Report.objects.get(pk=row.report)
            except Report.DoesNotExist:
                raise ValueError(f"No such report: {row.report}.") from None

        try:
            start_time = datetime.fromisoformat(row.start)
        except ValueError:
            raise ValueError(
                f"Invalid start time: {row.start!r}; expected ISO format."
            ) from None
        duration = parse_hhmmss(row.duration)
        end_time = start_time + duration

        if row.pk is None:
            record = cls.objects.create(
                link=link, start_time=start_time, end_time=end_time
            )
        else:
            try:
                record = cls.objects.get(pk=row.pk)
            except cls.DoesNotExist:
                if not allow_create_with_pk:
                    raise ValueError(f"No such time record: {row.pk}.") from None
                record = cls.objects.create(
                    pk=row.pk, link=link, start_time=start_time, end_time=end_time
                )
            else:
                record.link = link
                record.start_time = start_time
                record.end_time = end_time
                record.save()

        if report is not None:
            report.add_time_record(record)
        elif record.report_line_id is not None:
            record.report_line.report.remove_time_record(record)

        return record

    def __str__(self) -> str:
        return f"TimeRecord({self.start_time} - {self.end_time or 'running'})"
