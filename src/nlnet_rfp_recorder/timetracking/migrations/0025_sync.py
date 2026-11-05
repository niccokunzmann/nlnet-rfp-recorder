import uuid

from django.db import migrations, models


def fill_uuids(apps, schema_editor):
    for model_name in ("Link", "TimeRecord"):
        model = apps.get_model("timetracking", model_name)
        for obj in model.objects.all():
            obj.uuid = uuid.uuid4()
            obj.save(update_fields=["uuid"])


class Migration(migrations.Migration):
    dependencies = [
        ("timetracking", "0024_report_status"),
    ]

    operations = [
        migrations.AddField(
            model_name="link",
            name="uuid",
            field=models.UUIDField(null=True),
        ),
        migrations.AddField(
            model_name="timerecord",
            name="uuid",
            field=models.UUIDField(null=True),
        ),
        migrations.RunPython(fill_uuids, migrations.RunPython.noop),
        migrations.AlterField(
            model_name="link",
            name="uuid",
            field=models.UUIDField(
                default=uuid.uuid4,
                editable=False,
                help_text="Identifies this link across computers for `rfp sync`.",
                unique=True,
            ),
        ),
        migrations.AlterField(
            model_name="timerecord",
            name="uuid",
            field=models.UUIDField(
                default=uuid.uuid4,
                editable=False,
                help_text="Identifies this time entry across computers for `rfp sync`.",
                unique=True,
            ),
        ),
        migrations.CreateModel(
            name="SyncSettings",
            fields=[
                (
                    "id",
                    models.BigAutoField(
                        auto_created=True,
                        primary_key=True,
                        serialize=False,
                        verbose_name="ID",
                    ),
                ),
                (
                    "url",
                    models.CharField(
                        blank=True,
                        default="",
                        help_text="The private git repository to sync with (`rfp sync --set-url`).",
                        max_length=1024,
                    ),
                ),
                (
                    "name",
                    models.CharField(
                        blank=True,
                        default="",
                        help_text="Author name of sync commits (`rfp sync --set-name`).",
                        max_length=255,
                    ),
                ),
                (
                    "email",
                    models.CharField(
                        blank=True,
                        default="",
                        help_text="Author email of sync commits (`rfp sync --set-email`).",
                        max_length=255,
                    ),
                ),
                (
                    "machine",
                    models.CharField(
                        blank=True,
                        default="",
                        help_text="This computer's name in the repository: its file is <machine>.jsonl. The host name plus a random part, so two computers never share one.",
                        max_length=255,
                    ),
                ),
            ],
        ),
        migrations.CreateModel(
            name="SyncState",
            fields=[
                (
                    "id",
                    models.BigAutoField(
                        auto_created=True,
                        primary_key=True,
                        serialize=False,
                        verbose_name="ID",
                    ),
                ),
                (
                    "model",
                    models.CharField(
                        help_text="'link' or 'time_record'.", max_length=32
                    ),
                ),
                ("uuid", models.UUIDField(help_text="The object's uuid.")),
                (
                    "digest",
                    models.CharField(
                        blank=True,
                        default="",
                        help_text="SHA-256 of its data.",
                        max_length=64,
                    ),
                ),
                (
                    "updated_at",
                    models.CharField(
                        help_text="When it last changed: ISO 8601 in UTC.",
                        max_length=32,
                    ),
                ),
                (
                    "deleted",
                    models.BooleanField(
                        default=False,
                        help_text="Deleted here, not pushed as deleted yet.",
                    ),
                ),
            ],
            options={
                "constraints": [
                    models.UniqueConstraint(
                        fields=("model", "uuid"), name="unique_sync_state"
                    )
                ],
            },
        ),
    ]
