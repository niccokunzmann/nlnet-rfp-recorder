from django.db import migrations


class Migration(migrations.Migration):
    dependencies = [
        ("timetracking", "0011_task_personal_budget"),
    ]

    operations = [
        migrations.RemoveField(
            model_name="task",
            name="allocated_budget",
        ),
    ]
