from datetime import datetime
from zoneinfo import ZoneInfo

from django.db import migrations

# Django's default TIME_ZONE ("America/Chicago") used to override the
# process time zone, so every stored naive timestamp is Chicago time.
# settings.TIME_ZONE is now None (system zone) - shift the old values.
CHICAGO = ZoneInfo("America/Chicago")


def _to_local(value: datetime | None) -> datetime | None:
    if value is None:
        return None
    return value.replace(tzinfo=CHICAGO).astimezone().replace(tzinfo=None)


def shift(apps, schema_editor):
    TimeRecord = apps.get_model("timetracking", "TimeRecord")
    Report = apps.get_model("timetracking", "Report")
    for record in TimeRecord.objects.all():
        record.start_time = _to_local(record.start_time)
        record.end_time = _to_local(record.end_time)
        record.save(update_fields=["start_time", "end_time"])
    for report in Report.objects.all():
        report.created = _to_local(report.created)
        report.save(update_fields=["created"])


class Migration(migrations.Migration):
    dependencies = [
        ("timetracking", "0014_sync_field_options"),
    ]

    operations = [migrations.RunPython(shift, migrations.RunPython.noop)]
