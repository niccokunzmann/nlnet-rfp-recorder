from datetime import timedelta

from django.core.validators import RegexValidator
from django.db import models
from django.utils import timezone

task_name_validator = RegexValidator(
    regex=r"^\d+[a-z]+$",
    message="Task name must look like '10a': a number followed by letters.",
)


class Task(models.Model):
    name = models.CharField(max_length=16, validators=[task_name_validator])
    selected = models.BooleanField(default=True)

    @classmethod
    def select(cls, name: str) -> Task:
        cls(name=name).full_clean(exclude=["selected"])

        cls.objects.exclude(name=name).update(selected=False)
        task, _ = cls.objects.update_or_create(name=name, defaults={"selected": True})
        return task

    @classmethod
    def get_selected(cls) -> Task | None:
        return cls.objects.filter(selected=True).first()

    def __str__(self) -> str:
        return self.name


class TimeRecordManager(models.Manager):
    def running(self) -> models.QuerySet[TimeRecord]:
        return self.filter(end_time__isnull=True)


class TimeRecord(models.Model):
    task = models.ForeignKey(
        Task,
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name="time_records",
    )
    start_time = models.DateTimeField()
    end_time = models.DateTimeField(null=True, blank=True)

    objects = TimeRecordManager()

    @classmethod
    def start(cls, link: str, task: Task | None = None) -> TimeRecord:
        task = task or Task.get_selected()
        if task is None:
            raise ValueError("No task selected. Run `rfp task <name>` first.")
        cls.stop()
        record = cls.objects.create(task=task, start_time=timezone.now())
        record.links.create(url=link)
        return record

    @classmethod
    def get_running(cls) -> TimeRecord | None:
        return cls.objects.running().order_by("-start_time").first()

    @classmethod
    def stop(cls) -> TimeRecord | None:
        record = cls.get_running()
        if record is None:
            return None
        record.end_time = timezone.now()
        record.save()
        return record

    @property
    def is_running(self) -> bool:
        return self.end_time is None

    @property
    def duration(self) -> timedelta:
        return (self.end_time or timezone.now()) - self.start_time

    def __str__(self) -> str:
        return f"TimeRecord({self.start_time} - {self.end_time or 'running'})"


class Link(models.Model):
    time_record = models.ForeignKey(
        TimeRecord, related_name="links", on_delete=models.CASCADE
    )
    url = models.URLField()

    def __str__(self) -> str:
        return self.url
