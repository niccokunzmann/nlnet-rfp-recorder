from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [
        ("timetracking", "0025_sync"),
    ]

    operations = [
        migrations.AddField(
            model_name="syncsettings",
            name="background",
            field=models.BooleanField(
                default=True,
                help_text="Whether every command syncs before and after it runs (`rfp sync --background yes/no`).",
            ),
        ),
    ]
