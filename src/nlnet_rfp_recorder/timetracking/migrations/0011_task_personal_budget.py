from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [
        ("timetracking", "0010_task_allocated_budget"),
    ]

    operations = [
        migrations.AddField(
            model_name="task",
            name="personal_budget",
            field=models.FloatField(blank=True, null=True),
        ),
    ]
