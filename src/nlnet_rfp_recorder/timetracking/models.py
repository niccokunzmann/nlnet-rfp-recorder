from django.db import models


class TimeRecordManager(models.Manager):
    def running(self) -> models.QuerySet[TimeRecord]:
        return self.filter(end_time__isnull=True)


class TimeRecord(models.Model):
    start_time = models.DateTimeField()
    end_time = models.DateTimeField(null=True, blank=True)

    objects = TimeRecordManager()

    @property
    def is_running(self) -> bool:
        return self.end_time is None

    def __str__(self) -> str:
        return f"TimeRecord({self.start_time} - {self.end_time or 'running'})"


class Link(models.Model):
    time_record = models.ForeignKey(
        TimeRecord, related_name="links", on_delete=models.CASCADE
    )
    url = models.URLField()

    def __str__(self) -> str:
        return self.url
