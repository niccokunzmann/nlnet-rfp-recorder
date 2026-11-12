from django.db import migrations

import nlnet_rfp_recorder.timetracking.models.sync


def date_task_aliases(apps, schema_editor):
    """`rfp sync` takes a task's alias with its version: the aliases a
    task has are dated now, like its values were in 0029_task_sync. A
    task without alias gets no version - it takes the one of a computer
    that has one."""
    Alias = apps.get_model("timetracking", "Alias")
    Task = apps.get_model("timetracking", "Task")
    SyncSettings = apps.get_model("timetracking", "SyncSettings")
    settings = SyncSettings.objects.order_by("id").first()
    machine = settings.machine if settings is not None else ""
    now = nlnet_rfp_recorder.timetracking.models.sync.sync_now()
    aliased = Alias.objects.filter(item_type="task", mou__isnull=False)
    for mou_id, target in aliased.values_list("mou_id", "target").distinct():
        for task in Task.objects.filter(mou_id=mou_id, name=target):
            field_versions = dict(task.field_versions or {})
            field_versions["alias"] = [now, machine]
            Task.objects.filter(pk=task.pk).update(
                field_versions=field_versions, modified=now, modified_by=machine
            )


class Migration(migrations.Migration):
    dependencies = [
        ("timetracking", "0030_timerecord_rfp_request"),
    ]

    operations = [
        migrations.RunPython(date_task_aliases, migrations.RunPython.noop),
    ]
