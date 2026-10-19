from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [
        ("timetracking", "0019_link_closed_at"),
    ]

    operations = [
        migrations.AddField(
            model_name="mou",
            name="rfp_project",
            field=models.CharField(
                blank=True,
                default="",
                help_text="NLnet project id (e.g. 2026-04-3e6). Set via `rfp mou secret`.",
                max_length=64,
            ),
        ),
        migrations.AddField(
            model_name="mou",
            name="rfp_secret",
            field=models.CharField(
                blank=True,
                default="",
                help_text="Secret of this MoU's NLnet Request for Payment page. Set via `rfp mou secret`; used by `rfp mou update`.",
                max_length=64,
            ),
        ),
        migrations.AddField(
            model_name="task",
            name="remaining_budget",
            field=models.FloatField(
                blank=True,
                help_text="Budget (EUR) NLnet still has available for this task, as scraped by `rfp mou update`.",
                null=True,
            ),
        ),
    ]
