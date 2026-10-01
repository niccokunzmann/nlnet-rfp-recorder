from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [
        ("timetracking", "0012_remove_task_allocated_budget"),
    ]

    operations = [
        migrations.AlterField(
            model_name="link",
            name="url",
            field=models.URLField(
                help_text=(
                    "The issue/PR/discussion URL (or other link) time was "
                    "tracked against."
                ),
            ),
        ),
        migrations.AddConstraint(
            model_name="link",
            constraint=models.UniqueConstraint(
                condition=models.Q(("url", ""), _negated=True),
                fields=("url",),
                name="unique_nonempty_link_url",
            ),
        ),
    ]
