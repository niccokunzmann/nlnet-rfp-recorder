from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [
        ("timetracking", "0009_link_title"),
    ]

    operations = [
        migrations.AddField(
            model_name="task",
            name="allocated_budget",
            field=models.FloatField(blank=True, null=True),
        ),
    ]
