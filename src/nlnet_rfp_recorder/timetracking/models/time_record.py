from __future__ import annotations

from collections.abc import Iterable
from datetime import datetime, timedelta

from django.conf import settings
from django.db import models
from django.utils import timezone

from nlnet_rfp_recorder.timesheet import TimesheetRow, format_hhmmss, parse_hhmmss

from .link import Link
from .mou import MoU
from .task import Task


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
    )
    start_time = models.DateTimeField()
    end_time = models.DateTimeField(null=True, blank=True)
    report_line = models.ForeignKey(
        "ReportLine",
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name="time_records",
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
        """
        task = task or Task.get_selected()
        if task is None:
            raise ValueError("No task selected. Run `rfp task <name>` first.")

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

    @property
    def is_running(self) -> bool:
        return self.end_time is None

    @property
    def duration(self) -> timedelta:
        return (self.end_time or timezone.now()) - self.start_time

    @property
    def budget(self) -> float | None:
        if settings.RFP_EUROS is None:
            return None
        return self.duration.total_seconds() / 3600 * settings.RFP_EUROS

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
