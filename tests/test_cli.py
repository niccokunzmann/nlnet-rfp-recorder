import csv
import io
import re
from datetime import timedelta
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import niquests
import pytest
import typer
from django.utils import timezone
from typer.testing import CliRunner

from nlnet_rfp_recorder.cli import app
from nlnet_rfp_recorder.github import TIMEOUT_SECONDS, Status
from nlnet_rfp_recorder.timetracking.models import (
    Alias,
    GitHubToken,
    Link,
    MoU,
    Report,
    ReportLine,
    Task,
    TimeRecord,
)


def _mock_github_session(state: str = "closed"):
    """A mock niquests.AsyncSession() context manager returning `state`."""
    response = MagicMock(status_code=200)
    response.json = MagicMock(return_value={"state": state})
    session = MagicMock()
    session.get = AsyncMock(return_value=response)
    session.__aenter__ = AsyncMock(return_value=session)
    session.__aexit__ = AsyncMock(return_value=False)
    return patch(
        "nlnet_rfp_recorder.github.niquests.AsyncSession", return_value=session
    )


def _mock_github_title_session(title: str = "Some title"):
    """A mock niquests.AsyncSession() context manager returning `title`."""
    response = MagicMock(status_code=200)
    response.json = MagicMock(return_value={"title": title})
    session = MagicMock()
    session.get = AsyncMock(return_value=response)
    session.__aenter__ = AsyncMock(return_value=session)
    session.__aexit__ = AsyncMock(return_value=False)
    return patch(
        "nlnet_rfp_recorder.github.niquests.AsyncSession", return_value=session
    )


def _mock_github_error_session(message: str = "the organization forbids this token"):
    """A mock niquests.AsyncSession() context manager whose requests fail with 403."""
    response = MagicMock(status_code=403)
    response.raise_for_status = MagicMock(
        side_effect=niquests.exceptions.HTTPError("403 Client Error: Forbidden")
    )
    response.json = MagicMock(return_value={"message": message})
    session = MagicMock()
    session.get = AsyncMock(return_value=response)
    session.__aenter__ = AsyncMock(return_value=session)
    session.__aexit__ = AsyncMock(return_value=False)
    return patch(
        "nlnet_rfp_recorder.github.niquests.AsyncSession", return_value=session
    )


pytestmark = pytest.mark.django_db

runner = CliRunner()


def _invoke_timesheet_import(*args: str, input: str | None = None):
    # The pytest-django test database is ":memory:", which shutil.copy2
    # can't back up - that path is covered for real in
    # test_cli_db_option.py via a real subprocess against a file-backed db.
    with patch("nlnet_rfp_recorder.cli.shutil.copy2"):
        return runner.invoke(app, ["timesheet", "import", *args], input=input)


def _invoke_report_import(*args: str, input: str | None = None):
    # See _invoke_timesheet_import: ":memory:" can't be backed up for real.
    with patch("nlnet_rfp_recorder.cli.shutil.copy2"):
        return runner.invoke(app, ["report", "import", *args], input=input)


def _invoke_mou_import(*args: str, input: str | None = None):
    # See _invoke_timesheet_import: ":memory:" can't be backed up for real.
    with patch("nlnet_rfp_recorder.cli.shutil.copy2"):
        return runner.invoke(app, ["mou", "import", *args], input=input)


def test_no_args_shows_help():
    result = runner.invoke(app, [])
    assert "Usage" in result.output


def test_version_command():
    result = runner.invoke(app, ["version"])
    assert result.exit_code == 0
    assert result.output.strip()


def test_migrate_command_succeeds():
    result = runner.invoke(app, ["migrate"])
    assert result.exit_code == 0, result.output


def test_migrate_command_prints_the_database_file():
    from django.conf import settings

    result = runner.invoke(app, ["migrate"])

    assert result.exit_code == 0, result.output
    assert settings.DATABASES["default"]["NAME"] in result.output


def test_task_fails_without_a_selected_mou():
    result = runner.invoke(app, ["task", "select", "10a"])

    assert result.exit_code != 0
    assert "MoU" in result.output


def test_task_selects_a_task():
    runner.invoke(app, ["mou", "add", "nlnet-2026"])
    result = runner.invoke(app, ["task", "select", "10a"])

    assert result.exit_code == 0, result.output
    task = Task.objects.get()
    assert task.name == "10a"
    assert task.selected is True


def test_task_select_without_a_name_deselects_the_current_task():
    runner.invoke(app, ["mou", "add", "nlnet-2026"])
    runner.invoke(app, ["task", "select", "10a"])

    result = runner.invoke(app, ["task", "select"])

    assert result.exit_code == 0, result.output
    assert "Deselected task: 10a" in result.output
    assert Task.objects.get(name="10a").selected is False
    assert Task.get_selected() is None


def test_task_select_without_a_name_reports_when_nothing_was_selected():
    runner.invoke(app, ["mou", "add", "nlnet-2026"])

    result = runner.invoke(app, ["task", "select"])

    assert result.exit_code == 0, result.output
    assert "No task was selected." in result.output


def test_task_bare_fails_after_select_deselects_the_current_task():
    runner.invoke(app, ["mou", "add", "nlnet-2026"])
    runner.invoke(app, ["task", "select", "10a"])
    runner.invoke(app, ["task", "select"])

    result = runner.invoke(app, ["task"])

    assert result.exit_code != 0
    assert "No task selected" in result.output


def test_task_output_mentions_the_mou():
    runner.invoke(app, ["mou", "add", "nlnet-2026"])
    result = runner.invoke(app, ["task", "select", "10a"])

    assert result.exit_code == 0, result.output
    assert "10a" in result.output
    assert "nlnet-2026" in result.output


def test_task_output_has_no_budget_line_when_no_max_budget_is_set():
    runner.invoke(app, ["mou", "add", "nlnet-2026"])

    result = runner.invoke(app, ["task", "select", "10a"])

    assert result.exit_code == 0, result.output
    assert "Left" not in result.output
    assert "Total" not in result.output


def test_task_output_shows_budget_without_time_left_when_rfp_euros_is_unset(
    tmp_path, settings
):
    settings.RFP_EUROS_PER_HOUR = None
    runner.invoke(app, ["mou", "add", "nlnet-2026"])
    budget_file = tmp_path / "budget.txt"
    budget_file.write_text("10a. Do the thing\t€ 500\n")
    _invoke_mou_import(str(budget_file))

    result = runner.invoke(app, ["task", "select", "10a"])

    assert result.exit_code == 0, result.output
    assert "0€/500€" in result.output
    assert "left" not in result.output


def test_task_output_shows_budget_and_time_left(tmp_path, settings):
    settings.RFP_EUROS_PER_HOUR = 20.0
    runner.invoke(app, ["mou", "add", "nlnet-2026"])
    budget_file = tmp_path / "budget.txt"
    budget_file.write_text("10a. Do the thing\t€ 500\n")
    _invoke_mou_import(str(budget_file))
    task = Task.objects.get(name="10a")
    link = Link.objects.create(task=task, url="https://example.com/issues/1")
    now = timezone.now()
    TimeRecord.objects.create(
        link=link, start_time=now - timedelta(hours=1), end_time=now
    )

    result = runner.invoke(app, ["task", "select", "10a"])

    assert result.exit_code == 0, result.output
    assert "20€/500€ 24:00 left" in result.output


def test_task_rejects_an_invalid_name():
    runner.invoke(app, ["mou", "add", "nlnet-2026"])
    result = runner.invoke(app, ["task", "select", "Write the RfP"])

    assert result.exit_code != 0


def test_task_warns_when_creating_a_new_task():
    runner.invoke(app, ["mou", "add", "nlnet-2026"])
    result = runner.invoke(app, ["task", "select", "10a"])

    assert result.exit_code == 0, result.output
    assert "10a is new" in result.stderr


def test_task_does_not_warn_when_reselecting_an_existing_task():
    runner.invoke(app, ["mou", "add", "nlnet-2026"])
    runner.invoke(app, ["task", "select", "10a"])

    result = runner.invoke(app, ["task", "select", "10a"])

    assert result.exit_code == 0, result.output
    assert result.stderr == ""


def test_task_bare_fails_without_a_selected_task():
    result = runner.invoke(app, ["task"])

    assert result.exit_code != 0


def test_task_bare_shows_the_current_task_status():
    runner.invoke(app, ["mou", "add", "nlnet-2026"])
    runner.invoke(app, ["task", "select", "10a"])

    result = runner.invoke(app, ["task"])

    assert result.exit_code == 0, result.output
    assert "MoU: nlnet-2026" in result.output
    assert "Selected task: 10a" in result.output


def test_task_status_subcommand_shows_the_current_task():
    runner.invoke(app, ["mou", "add", "nlnet-2026"])
    runner.invoke(app, ["task", "select", "10a"])

    result = runner.invoke(app, ["task", "status"])

    assert result.exit_code == 0, result.output
    assert "Selected task: 10a" in result.output


def test_task_status_shows_the_tasks_description_if_present():
    runner.invoke(app, ["mou", "add", "nlnet-2026"])
    runner.invoke(app, ["task", "select", "10a"])
    task = Task.objects.get(name="10a")
    task.description = "Do the thing"
    task.save(update_fields=["description"])

    result = runner.invoke(app, ["task", "status"])

    assert result.exit_code == 0, result.output
    assert "Do the thing" in result.output


def test_task_select_shows_the_tasks_description_if_present():
    mou = MoU.objects.create(name="nlnet-2026", selected=True)
    Task.objects.create(mou=mou, name="10a", description="Do the thing")

    result = runner.invoke(app, ["task", "select", "10a"])

    assert result.exit_code == 0, result.output
    assert "Do the thing" in result.output


def test_task_set_updates_budget_and_used():
    runner.invoke(app, ["mou", "add", "nlnet-2026"])
    runner.invoke(app, ["task", "select", "10a"])

    result = runner.invoke(
        app, ["task", "set", "10a", "--budget", "500", "--used", "100"]
    )

    assert result.exit_code == 0, result.output
    task = Task.objects.get(name="10a")
    assert task.max_budget == 500.0
    assert task.used_budget == 100.0
    assert "100€/500€" in result.output


def test_task_set_fails_for_an_unknown_task():
    runner.invoke(app, ["mou", "add", "nlnet-2026"])

    result = runner.invoke(app, ["task", "set", "10a", "--budget", "500"])

    assert result.exit_code != 0


def test_task_list_fails_without_a_selected_mou():
    result = runner.invoke(app, ["task", "list"])

    assert result.exit_code != 0


def test_task_list_reports_when_there_are_none():
    runner.invoke(app, ["mou", "add", "nlnet-2026"])

    result = runner.invoke(app, ["task", "list"])

    assert result.exit_code == 0, result.output
    assert "No tasks" in result.output


def test_task_list_marks_the_selected_task_and_shows_budget():
    runner.invoke(app, ["mou", "add", "nlnet-2026"])
    runner.invoke(app, ["task", "select", "10a"])
    runner.invoke(app, ["task", "set", "10a", "--budget", "500", "--used", "100"])
    runner.invoke(app, ["task", "select", "11b"])

    result = runner.invoke(app, ["task", "list"])

    assert result.exit_code == 0, result.output
    assert "  10a  100€/500€" in result.output
    assert "* 11b" in result.output


def test_task_list_shows_the_description():
    mou = MoU.objects.create(name="nlnet-2026", selected=True)
    Task.objects.create(mou=mou, name="10a", description="Do the thing")

    result = runner.invoke(app, ["task", "list"])

    assert result.exit_code == 0, result.output
    assert "10a  Do the thing" in result.output


def test_task_list_aligns_descriptions_within_a_task_group_only():
    mou = MoU.objects.create(name="nlnet-2026", selected=True)
    # Same group (10): names differ in length, so the description column
    # only lines up if padded to the widest name in this group.
    Task.objects.create(mou=mou, name="10a", description="short")
    Task.objects.create(mou=mou, name="10abc", description="longer name")
    # A different group (9): its own, unrelated (and shorter) column.
    Task.objects.create(mou=mou, name="9a", description="other group")

    result = runner.invoke(app, ["task", "list"])

    assert result.exit_code == 0, result.output
    lines = result.output.strip("\n").splitlines()
    assert len(lines) == 3
    # Groups sort by their number (9 before 10), so 9a lists first.
    nine_a, ten_a, ten_abc = lines
    assert ten_a.index("short") == ten_abc.index("longer name")
    # group 9's single, shorter name doesn't inherit group 10's width.
    assert nine_a.index("other group") < ten_a.index("short")


def test_task_list_description_starts_at_the_same_column_without_a_budget():
    # A task with no budget of its own still lines its description up
    # with the rest of its group, wherever the group's budget column
    # (from a sibling task that does have one) puts it.
    mou = MoU.objects.create(name="nlnet-2026", selected=True)
    Task.objects.create(
        mou=mou, name="10a", max_budget=500.0, used_budget=20.0, description="has one"
    )
    Task.objects.create(mou=mou, name="10b", description="has none")

    result = runner.invoke(app, ["task", "list"])

    assert result.exit_code == 0, result.output
    lines = result.output.strip("\n").splitlines()
    assert len(lines) == 2
    assert lines[0].index("has one") == lines[1].index("has none")


def test_task_list_aligns_time_left_within_a_task_group_only(settings):
    settings.RFP_EUROS_PER_HOUR = 20.0
    mou = MoU.objects.create(name="nlnet-2026", selected=True)
    # "20€/500€" (8 chars) vs "100€/500€" (9 chars): without padding the
    # shorter money figure, their "... left" times wouldn't line up.
    Task.objects.create(mou=mou, name="10a", max_budget=500.0, used_budget=20.0)
    Task.objects.create(mou=mou, name="10b", max_budget=500.0, used_budget=100.0)

    result = runner.invoke(app, ["task", "list"])

    assert result.exit_code == 0, result.output
    lines = result.output.strip("\n").splitlines()
    assert len(lines) == 2
    assert lines[0].index(" left") == lines[1].index(" left")
    assert " - " not in result.output


def test_task_list_right_aligns_the_hour_figure_within_a_task_group(settings):
    settings.RFP_EUROS_PER_HOUR = 20.0
    mou = MoU.objects.create(name="nlnet-2026", selected=True)
    # 121:57, 21:57, 1:57 remaining - the hour digits should right-align
    # on the widest one, like a table of numbers.
    Task.objects.create(mou=mou, name="10a", max_budget=2439.0)
    Task.objects.create(mou=mou, name="10b", max_budget=439.0)
    Task.objects.create(mou=mou, name="10c", max_budget=39.0)

    result = runner.invoke(app, ["task", "list"])

    assert result.exit_code == 0, result.output
    lines = result.output.strip("\n").splitlines()
    assert len(lines) == 3
    assert "121:57 left" in lines[0]
    assert " 21:57 left" in lines[1]
    assert "  1:57 left" in lines[2]


def test_task_remove_deletes_it():
    runner.invoke(app, ["mou", "add", "nlnet-2026"])
    runner.invoke(app, ["task", "select", "10a"])

    result = runner.invoke(app, ["task", "remove", "10a"])

    assert result.exit_code == 0, result.output
    assert Task.objects.count() == 0


def test_task_remove_fails_for_an_unknown_task():
    runner.invoke(app, ["mou", "add", "nlnet-2026"])

    result = runner.invoke(app, ["task", "remove", "does-not-exist"])

    assert result.exit_code != 0


def test_task_set_personal_budget_drives_the_budget_line():
    runner.invoke(app, ["mou", "add", "nlnet-2026"])
    runner.invoke(app, ["task", "select", "10a"])
    # A max budget that the personal budget below overrides for
    # completion/time-left purposes.
    runner.invoke(app, ["task", "set", "10a", "--budget", "500"])

    result = runner.invoke(app, ["task", "set", "10a", "--budget", "200"])

    assert result.exit_code == 0, result.output
    task = Task.objects.get(name="10a")
    assert task.personal_budget == 200.0
    assert "0€/200€" in result.output
    assert "4:00 left" in result.output


def test_task_status_budget_line_falls_back_to_max_budget():
    runner.invoke(app, ["mou", "add", "nlnet-2026"])
    runner.invoke(app, ["task", "select", "10a"])
    runner.invoke(app, ["task", "set", "10a", "--budget", "500"])

    result = runner.invoke(app, ["task", "status"])

    assert result.exit_code == 0, result.output
    assert "0€/500€" in result.output
    assert "10:00 left" in result.output


def test_task_status_shows_no_budget_line_when_nothing_is_set():
    runner.invoke(app, ["mou", "add", "nlnet-2026"])
    runner.invoke(app, ["task", "select", "10a"])

    result = runner.invoke(app, ["task", "status"])

    assert result.exit_code == 0, result.output
    assert "€" not in result.output


def _invoke_task_import(*args: str, input: str | None = None):
    # The pytest-django test database is ":memory:", which shutil.copy2
    # can't back up - that path is covered for real in
    # test_cli_db_option.py via a real subprocess against a file-backed db.
    with patch("nlnet_rfp_recorder.cli.shutil.copy2"):
        return runner.invoke(app, ["task", "import", *args], input=input)


def test_task_export_writes_a_csv_row_per_task(tmp_path):
    mou = MoU.objects.create(name="nlnet-2026", selected=True)
    Task.objects.create(
        mou=mou,
        name="10a",
        personal_budget=200.0,
        max_budget=500.0,
        used_budget=50.0,
        description="Do the thing",
    )
    Alias.create("task", "10a", "thing", mou=mou)
    path = tmp_path / "tasks.csv"

    result = runner.invoke(app, ["task", "export", str(path)])

    assert result.exit_code == 0, result.output
    rows = list(csv.DictReader(io.StringIO(path.read_text())))
    assert rows == [
        {
            "id": "10a",
            "alias": "thing",
            "personal_budget": "200.0",
            "max_budget": "500.0",
            "used_budget": "50.0",
            "description": "Do the thing",
        }
    ]


def test_task_export_falls_back_to_max_budget_when_personal_budget_is_unset(tmp_path):
    mou = MoU.objects.create(name="nlnet-2026", selected=True)
    Task.objects.create(mou=mou, name="10a", max_budget=500.0)
    path = tmp_path / "tasks.csv"

    result = runner.invoke(app, ["task", "export", str(path)])

    assert result.exit_code == 0, result.output
    row = next(csv.DictReader(io.StringIO(path.read_text())))
    assert row["personal_budget"] == "500.0"
    assert row["max_budget"] == "500.0"


def test_task_export_leaves_personal_budget_blank_without_a_max_budget_either(
    tmp_path,
):
    mou = MoU.objects.create(name="nlnet-2026", selected=True)
    Task.objects.create(mou=mou, name="10a")
    path = tmp_path / "tasks.csv"

    result = runner.invoke(app, ["task", "export", str(path)])

    assert result.exit_code == 0, result.output
    row = next(csv.DictReader(io.StringIO(path.read_text())))
    assert row["personal_budget"] == ""
    assert row["max_budget"] == ""


def test_task_export_fails_without_a_selected_mou():
    result = runner.invoke(app, ["task", "export"])

    assert result.exit_code != 0


def test_task_import_creates_tasks_from_csv(tmp_path):
    runner.invoke(app, ["mou", "add", "nlnet-2026"])
    path = tmp_path / "tasks.csv"
    path.write_text(
        "id,alias,personal_budget,max_budget,used_budget,description\n"
        "10a,thing,200,500,50,Do the thing\n"
    )

    result = _invoke_task_import(str(path))

    assert result.exit_code == 0, result.output
    task = Task.objects.get(name="10a")
    assert task.personal_budget == 200.0
    assert task.max_budget == 500.0
    assert task.description == "Do the thing"
    alias = Alias.objects.get(item_type="task", alias="thing")
    assert alias.target == "10a"


def test_task_import_treats_personal_budget_equal_to_max_budget_as_unset(tmp_path):
    mou = MoU.objects.create(name="nlnet-2026", selected=True)
    Task.objects.create(mou=mou, name="10a", personal_budget=200.0, max_budget=500.0)
    path = tmp_path / "tasks.csv"
    path.write_text(
        "id,alias,personal_budget,max_budget,used_budget,description\n10a,,500,500,,\n"
    )

    result = _invoke_task_import(str(path))

    assert result.exit_code == 0, result.output
    task = Task.objects.get(name="10a")
    assert task.personal_budget is None
    assert task.max_budget == 500.0


def test_task_import_keeps_personal_budget_that_differs_from_max_budget(tmp_path):
    mou = MoU.objects.create(name="nlnet-2026", selected=True)
    Task.objects.create(mou=mou, name="10a")
    path = tmp_path / "tasks.csv"
    path.write_text(
        "id,alias,personal_budget,max_budget,used_budget,description\n10a,,200,500,,\n"
    )

    result = _invoke_task_import(str(path))

    assert result.exit_code == 0, result.output
    task = Task.objects.get(name="10a")
    assert task.personal_budget == 200.0
    assert task.max_budget == 500.0


def test_task_import_clears_personal_budget_when_the_column_is_blank(tmp_path):
    mou = MoU.objects.create(name="nlnet-2026", selected=True)
    Task.objects.create(mou=mou, name="10a", personal_budget=200.0, max_budget=500.0)
    path = tmp_path / "tasks.csv"
    path.write_text(
        "id,alias,personal_budget,max_budget,used_budget,description\n10a,,,500,,\n"
    )

    result = _invoke_task_import(str(path))

    assert result.exit_code == 0, result.output
    task = Task.objects.get(name="10a")
    assert task.personal_budget is None
    assert task.max_budget == 500.0


def test_task_import_ignores_the_used_budget_column(tmp_path):
    mou = MoU.objects.create(name="nlnet-2026", selected=True)
    Task.objects.create(mou=mou, name="10a", used_budget=99.0)
    path = tmp_path / "tasks.csv"
    path.write_text(
        "id,alias,personal_budget,max_budget,used_budget,description\n10a,,,,999,\n"
    )

    result = _invoke_task_import(str(path))

    assert result.exit_code == 0, result.output
    task = Task.objects.get(name="10a")
    assert task.used_budget == 99.0


def test_task_import_updates_an_existing_task(tmp_path):
    mou = MoU.objects.create(name="nlnet-2026", selected=True)
    Task.objects.create(mou=mou, name="10a", max_budget=100.0)
    path = tmp_path / "tasks.csv"
    path.write_text(
        "id,alias,personal_budget,max_budget,used_budget,description\n"
        "10a,,,300,,Updated\n"
    )

    result = _invoke_task_import(str(path))

    assert result.exit_code == 0, result.output
    assert Task.objects.count() == 1
    task = Task.objects.get(name="10a")
    assert task.max_budget == 300.0
    assert task.description == "Updated"


def test_task_import_prompts_to_remove_missing_tasks_and_deletes_on_yes(tmp_path):
    mou = MoU.objects.create(name="nlnet-2026", selected=True)
    Task.objects.create(mou=mou, name="10a")
    path = tmp_path / "tasks.csv"
    path.write_text("id,alias,personal_budget,max_budget,used_budget,description\n")

    result = _invoke_task_import(str(path), input="y\n")

    assert result.exit_code == 0, result.output
    assert "Remove them?" in result.output
    assert Task.objects.count() == 0


def test_task_import_prompts_to_remove_missing_tasks_and_keeps_on_no(tmp_path):
    mou = MoU.objects.create(name="nlnet-2026", selected=True)
    Task.objects.create(mou=mou, name="10a")
    path = tmp_path / "tasks.csv"
    path.write_text("id,alias,personal_budget,max_budget,used_budget,description\n")

    result = _invoke_task_import(str(path), input="n\n")

    assert result.exit_code == 0, result.output
    assert Task.objects.count() == 1


def test_task_import_does_not_prompt_when_nothing_is_missing(tmp_path):
    mou = MoU.objects.create(name="nlnet-2026", selected=True)
    Task.objects.create(mou=mou, name="10a")
    path = tmp_path / "tasks.csv"
    path.write_text(
        "id,alias,personal_budget,max_budget,used_budget,description\n10a,,,,,\n"
    )

    result = _invoke_task_import(str(path))

    assert result.exit_code == 0, result.output
    assert "Remove them?" not in result.output
    assert Task.objects.count() == 1


def test_task_import_steals_an_alias_already_used_by_another_task(tmp_path):
    mou = MoU.objects.create(name="nlnet-2026", selected=True)
    Task.objects.create(mou=mou, name="10a")
    Task.objects.create(mou=mou, name="11b")
    Alias.create("task", "10a", "thing", mou=mou)
    path = tmp_path / "tasks.csv"
    path.write_text(
        "id,alias,personal_budget,max_budget,used_budget,description\n"
        "10a,,,,,\n"
        "11b,thing,,,,\n"
    )

    result = _invoke_task_import(str(path))

    assert result.exit_code == 0, result.output
    assert Alias.objects.get(item_type="task", alias="thing").target == "11b"


def test_task_import_prompts_to_delete_unused_aliases_and_deletes_on_yes(tmp_path):
    mou = MoU.objects.create(name="nlnet-2026", selected=True)
    Task.objects.create(mou=mou, name="10a")
    Alias.create("task", "10a", "thing", mou=mou)
    path = tmp_path / "tasks.csv"
    path.write_text(
        "id,alias,personal_budget,max_budget,used_budget,description\n10a,,,,,\n"
    )

    result = _invoke_task_import(str(path), input="y\n")

    assert result.exit_code == 0, result.output
    assert "task alias(es) are no longer used" in result.output
    assert not Alias.objects.filter(item_type="task", alias="thing").exists()


def test_task_import_prompts_to_delete_unused_aliases_and_keeps_on_no(tmp_path):
    mou = MoU.objects.create(name="nlnet-2026", selected=True)
    Task.objects.create(mou=mou, name="10a")
    Alias.create("task", "10a", "thing", mou=mou)
    path = tmp_path / "tasks.csv"
    path.write_text(
        "id,alias,personal_budget,max_budget,used_budget,description\n10a,,,,,\n"
    )

    result = _invoke_task_import(str(path), input="n\n")

    assert result.exit_code == 0, result.output
    assert Alias.objects.filter(item_type="task", alias="thing").exists()


def test_task_import_does_not_prompt_when_the_alias_is_reconfirmed(tmp_path):
    mou = MoU.objects.create(name="nlnet-2026", selected=True)
    Task.objects.create(mou=mou, name="10a")
    Alias.create("task", "10a", "thing", mou=mou)
    path = tmp_path / "tasks.csv"
    path.write_text(
        "id,alias,personal_budget,max_budget,used_budget,description\n10a,thing,,,,\n"
    )

    result = _invoke_task_import(str(path))

    assert result.exit_code == 0, result.output
    assert "no longer used" not in result.output


def test_task_import_yes_flag_skips_both_prompts(tmp_path):
    mou = MoU.objects.create(name="nlnet-2026", selected=True)
    Task.objects.create(mou=mou, name="10a")
    Task.objects.create(mou=mou, name="99z")
    Alias.create("task", "99z", "thing", mou=mou)
    path = tmp_path / "tasks.csv"
    path.write_text(
        "id,alias,personal_budget,max_budget,used_budget,description\n10a,,,,,\n"
    )

    result = _invoke_task_import(str(path), "--yes")

    assert result.exit_code == 0, result.output
    assert "Remove them?" not in result.output
    assert "Delete them?" not in result.output
    assert Task.objects.filter(name="99z").exists() is False
    assert not Alias.objects.filter(item_type="task", alias="thing").exists()


def test_task_import_rejects_an_invalid_id_without_writing_it(tmp_path):
    mou = MoU.objects.create(name="nlnet-2026", selected=True)
    path = tmp_path / "tasks.csv"
    path.write_text(
        "id,alias,personal_budget,max_budget,used_budget,description\nnot-a-task,,,,,\n"
    )

    result = _invoke_task_import(str(path))

    assert result.exit_code != 0
    assert not Task.objects.filter(mou=mou, name="not-a-task").exists()


def test_task_import_fails_without_a_selected_mou(tmp_path):
    path = tmp_path / "tasks.csv"
    path.write_text(
        "id,alias,personal_budget,max_budget,used_budget,description\n10a,,,,,\n"
    )

    result = _invoke_task_import(str(path))

    assert result.exit_code != 0


def test_task_export_import_round_trips_all_fields(tmp_path):
    mou = MoU.objects.create(name="nlnet-2026", selected=True)
    Task.objects.create(
        mou=mou,
        name="10a",
        personal_budget=200.0,
        max_budget=500.0,
        used_budget=50.0,
        description="Do the thing",
    )
    Alias.create("task", "10a", "thing", mou=mou)
    exported = runner.invoke(app, ["task", "export"])
    assert exported.exit_code == 0, exported.output
    path = tmp_path / "tasks.csv"
    path.write_text(exported.output)

    result = _invoke_task_import(str(path))

    assert result.exit_code == 0, result.output
    reexported = runner.invoke(app, ["task", "export"])
    assert reexported.exit_code == 0, reexported.output
    assert reexported.output == exported.output


def test_start_without_a_task_fails():
    # Also covers "no MoU selected": without one, `rfp task` (and thus any
    # task at all) can never succeed, so no task can ever be selected.
    result = runner.invoke(app, ["start", "https://example.com/issues/1"])

    assert result.exit_code != 0


def test_start_creates_a_time_entry_for_the_selected_task():
    runner.invoke(app, ["mou", "add", "nlnet-2026"])
    runner.invoke(app, ["task", "select", "10a"])
    result = runner.invoke(app, ["start", "https://example.com/issues/1"])

    assert result.exit_code == 0, result.output
    record = TimeRecord.objects.get()
    assert record.link.task.name == "10a"
    assert record.is_running is True
    assert [link.url for link in Link.objects.all()] == ["https://example.com/issues/1"]


def test_start_prints_the_previous_entry_being_stopped():
    runner.invoke(app, ["mou", "add", "nlnet-2026"])
    runner.invoke(app, ["task", "select", "10a"])
    runner.invoke(app, ["start", "https://example.com/issues/1"])
    result = runner.invoke(app, ["start", "https://example.com/issues/2"])

    assert result.exit_code == 0, result.output
    assert "Stopped 10a" in result.output
    assert "https://example.com/issues/1" in result.output
    assert "Started time entry for task 10a" in result.output


def test_start_uses_the_time_it_was_called_despite_a_slow_resolution():
    # If anything after capturing "now" (link resolution, the
    # implementation/review guess, ...) were to call timezone.now()
    # again, it would get a later value from this side_effect list -
    # proving the record's start_time is only ever the very first one.
    Task.objects.create(name="10a")
    now = timezone.now()
    later_values = [now + timedelta(seconds=n) for n in range(1, 6)]

    with patch("django.utils.timezone.now", side_effect=[now, *later_values]):
        result = runner.invoke(app, ["start", "https://example.com/issues/1"])

    assert result.exit_code == 0, result.output
    record = TimeRecord.objects.get()
    assert record.start_time == now


def test_start_prints_continuing_when_resuming_the_same_link():
    runner.invoke(app, ["mou", "add", "nlnet-2026"])
    runner.invoke(app, ["task", "select", "10a"])
    runner.invoke(app, ["start", "https://example.com/issues/1"])
    runner.invoke(app, ["stop"])

    result = runner.invoke(app, ["start", "https://example.com/issues/1"])

    assert result.exit_code == 0, result.output
    assert "Continuing time entry for task 10a" in result.output
    assert "Started time entry" not in result.output
    assert TimeRecord.objects.count() == 1


def test_start_with_an_explicit_task_selects_it_first():
    runner.invoke(app, ["mou", "add", "nlnet-2026"])
    runner.invoke(app, ["task", "select", "10a"])

    result = runner.invoke(app, ["start", "10b", "https://example.com/issues/1"])

    assert result.exit_code == 0, result.output
    record = TimeRecord.objects.get()
    assert record.link.task.name == "10b"
    assert Task.objects.get(name="10b").selected is True
    assert Task.objects.get(name="10a").selected is False


def test_start_with_too_many_positional_arguments_fails():
    runner.invoke(app, ["mou", "add", "nlnet-2026"])

    result = runner.invoke(app, ["start", "10a", "extra", "https://example.com/1"])

    assert result.exit_code != 0
    assert "Usage: rfp start" in result.output


def test_start_prints_the_tasks_description_if_present():
    runner.invoke(app, ["mou", "add", "nlnet-2026"])
    Task.objects.create(mou=MoU.objects.get(), name="10a", description="Do the thing")

    result = runner.invoke(app, ["start", "10a", "https://example.com/issues/1"])

    assert result.exit_code == 0, result.output
    assert "Do the thing" in result.output


def test_start_prints_nothing_extra_without_a_description():
    runner.invoke(app, ["mou", "add", "nlnet-2026"])
    runner.invoke(app, ["task", "select", "10a"])

    result = runner.invoke(app, ["start", "https://example.com/issues/1"])

    assert result.exit_code == 0, result.output
    assert result.output.strip().splitlines()[-1].startswith("Started time entry")


def test_stop_without_a_running_entry_succeeds():
    result = runner.invoke(app, ["stop"])

    assert result.exit_code == 0, result.output


def test_stop_ends_the_running_time_entry():
    runner.invoke(app, ["mou", "add", "nlnet-2026"])
    runner.invoke(app, ["task", "select", "10a"])
    runner.invoke(app, ["start", "https://example.com/issues/1"])
    result = runner.invoke(app, ["stop"])

    assert result.exit_code == 0, result.output
    record = TimeRecord.objects.get()
    assert record.is_running is False


def test_stop_with_a_url_replaces_the_running_records_link():
    runner.invoke(app, ["mou", "add", "nlnet-2026"])
    runner.invoke(app, ["task", "select", "10a"])
    runner.invoke(app, ["start", "https://example.com/issues/wrong"])

    result = runner.invoke(app, ["stop", "https://example.com/issues/right"])

    assert result.exit_code == 0, result.output
    record = TimeRecord.objects.get()
    assert record.link.url == "https://example.com/issues/right"
    assert "https://example.com/issues/right" in result.output


def test_continue_fails_without_any_previous_time_entry():
    result = runner.invoke(app, ["continue"])

    assert result.exit_code != 0
    assert "No previous time entry" in result.output


def test_continue_reports_instead_of_failing_while_a_time_entry_is_running():
    runner.invoke(app, ["mou", "add", "nlnet-2026"])
    runner.invoke(app, ["task", "select", "10a"])
    runner.invoke(app, ["start", "https://example.com/issues/1"])

    result = runner.invoke(app, ["continue"])

    assert result.exit_code == 0, result.output
    assert "Already running" in result.output
    assert "https://example.com/issues/1" in result.output
    assert TimeRecord.objects.count() == 1


def test_continue_creates_a_new_time_entry_for_the_same_link():
    runner.invoke(app, ["mou", "add", "nlnet-2026"])
    runner.invoke(app, ["task", "select", "10a"])
    runner.invoke(app, ["start", "https://example.com/issues/1"])
    runner.invoke(app, ["stop"])
    stopped_pk = TimeRecord.objects.get().pk

    result = runner.invoke(app, ["continue"])

    assert result.exit_code == 0, result.output
    assert TimeRecord.objects.count() == 2
    new_record = TimeRecord.objects.exclude(pk=stopped_pk).get()
    assert new_record.link.url == "https://example.com/issues/1"
    assert new_record.is_running is True
    old_record = TimeRecord.objects.get(pk=stopped_pk)
    assert old_record.is_running is False
    assert "https://example.com/issues/1" in result.output


def test_continue_shows_the_tasks_description_if_present():
    runner.invoke(app, ["mou", "add", "nlnet-2026"])
    runner.invoke(app, ["task", "select", "10a"])
    task = Task.objects.get(name="10a")
    task.description = "Do the thing"
    task.save(update_fields=["description"])
    runner.invoke(app, ["start", "https://example.com/issues/1"])
    runner.invoke(app, ["stop"])

    result = runner.invoke(app, ["continue"])

    assert result.exit_code == 0, result.output
    assert "Do the thing" in result.output


def test_edit_without_any_time_entries_fails():
    result = runner.invoke(app, ["edit", "https://example.com/issues/1"])

    assert result.exit_code != 0


def test_edit_replaces_the_last_entrys_link():
    runner.invoke(app, ["mou", "add", "nlnet-2026"])
    runner.invoke(app, ["task", "select", "10a"])
    runner.invoke(app, ["start", "https://example.com/issues/wrong"])
    runner.invoke(app, ["stop"])

    result = runner.invoke(app, ["edit", "https://example.com/issues/right"])

    assert result.exit_code == 0, result.output
    record = TimeRecord.objects.get()
    assert record.link.url == "https://example.com/issues/right"


def test_edit_adds_tags_without_changing_the_link():
    runner.invoke(app, ["mou", "add", "nlnet-2026"])
    runner.invoke(app, ["task", "select", "10a"])
    runner.invoke(app, ["start", "https://example.com/issues/1"])

    result = runner.invoke(app, ["edit", "--tags", "review"])

    assert result.exit_code == 0, result.output
    record = TimeRecord.objects.get()
    assert record.link.url == "https://example.com/issues/1"
    assert {tag.name for tag in record.link.tags.all()} == {"implementation", "review"}


def test_edit_edits_the_most_recent_entry_even_if_stopped():
    runner.invoke(app, ["mou", "add", "nlnet-2026"])
    runner.invoke(app, ["task", "select", "10a"])
    runner.invoke(app, ["start", "https://example.com/issues/1"])
    runner.invoke(app, ["stop"])

    result = runner.invoke(app, ["edit", "https://example.com/issues/2"])

    assert result.exit_code == 0, result.output
    record = TimeRecord.objects.get()
    assert record.link.url == "https://example.com/issues/2"
    assert record.is_running is False


def _time_record_pk() -> int:
    return TimeRecord.objects.get().pk


def test_timesheet_show_reports_when_there_are_none():
    result = runner.invoke(app, ["timesheet", "show"])

    assert result.exit_code == 0, result.output
    assert "No time entries" in result.output


def test_timesheet_show_prints_one_line_per_entry():
    runner.invoke(app, ["mou", "add", "nlnet-2026"])
    runner.invoke(app, ["task", "select", "10a"])
    runner.invoke(app, ["start", "https://example.com/issues/1"])
    runner.invoke(app, ["stop"])
    pk = _time_record_pk()

    result = runner.invoke(app, ["timesheet", "show"])

    assert result.exit_code == 0, result.output
    line = result.output.strip()
    assert line.startswith(f"{pk} nlnet-2026 10a ")
    assert "https://example.com/issues/1" in line
    assert "implementation" in line
    start_field = line.split()[3]
    assert "." not in start_field  # no seconds/microseconds in the start timestamp
    assert start_field.count(":") == 1  # HH:MM only, no seconds


def test_timesheet_show_uses_a_dash_for_an_entry_without_a_link():
    TimeRecord.objects.create(start_time=timezone.now(), end_time=timezone.now())

    result = runner.invoke(app, ["timesheet", "show"])

    assert result.exit_code == 0, result.output
    assert result.output.strip().endswith(" -")


def test_timesheet_export_to_stdout_is_valid_csv():
    runner.invoke(app, ["mou", "add", "nlnet-2026"])
    runner.invoke(app, ["task", "select", "10a"])
    runner.invoke(app, ["start", "https://example.com/issues/1"])
    runner.invoke(app, ["stop"])

    result = runner.invoke(app, ["timesheet", "export"])

    assert result.exit_code == 0, result.output
    assert "pk,mou,task,start,duration,link,tags" in result.output
    assert "https://example.com/issues/1" in result.output


def test_timesheet_export_to_a_file(tmp_path):
    runner.invoke(app, ["mou", "add", "nlnet-2026"])
    runner.invoke(app, ["task", "select", "10a"])
    runner.invoke(app, ["start", "https://example.com/issues/1"])
    runner.invoke(app, ["stop"])
    path = tmp_path / "timesheet.csv"

    result = runner.invoke(app, ["timesheet", "export", str(path)])

    assert result.exit_code == 0, result.output
    assert "https://example.com/issues/1" in path.read_text()


def test_timesheet_import_creates_entries_from_csv(tmp_path):
    runner.invoke(app, ["mou", "add", "nlnet-2026"])
    runner.invoke(app, ["task", "select", "10a"])
    path = tmp_path / "timesheet.csv"
    path.write_text(
        "pk,mou,task,start,duration,link,tags\n"
        ",nlnet-2026,10a,2026-09-04T09:00:00,01:00:00,"
        "https://example.com/issues/1,implementation\n"
    )

    result = _invoke_timesheet_import(str(path))

    assert result.exit_code == 0, result.output
    record = TimeRecord.objects.get()
    assert record.link.url == "https://example.com/issues/1"


def test_timesheet_import_creates_entries_with_an_explicit_new_pk(tmp_path):
    runner.invoke(app, ["mou", "add", "nlnet-2026"])
    runner.invoke(app, ["task", "select", "10a"])
    path = tmp_path / "timesheet.csv"
    path.write_text(
        "pk,mou,task,start,duration,link,tags\n"
        "1000,nlnet-2026,10a,2026-09-04T09:00:00,01:00:00,"
        "https://example.com/issues/1,implementation\n"
    )

    result = _invoke_timesheet_import(str(path))

    assert result.exit_code == 0, result.output
    record = TimeRecord.objects.get(pk=1000)
    assert record.link.url == "https://example.com/issues/1"


def test_timesheet_import_aborts_on_a_duplicate_pk(tmp_path):
    runner.invoke(app, ["mou", "add", "nlnet-2026"])
    runner.invoke(app, ["task", "select", "10a"])
    path = tmp_path / "timesheet.csv"
    path.write_text(
        "pk,mou,task,start,duration,link,tags\n"
        "1000,nlnet-2026,10a,2026-09-04T09:00:00,01:00:00,"
        "https://example.com/issues/1,implementation\n"
        "1000,nlnet-2026,10a,2026-09-04T10:00:00,01:00:00,"
        "https://example.com/issues/2,implementation\n"
    )

    result = _invoke_timesheet_import(str(path))

    assert result.exit_code != 0
    assert "Duplicate pk" in result.output
    assert "1000" in result.output
    assert TimeRecord.objects.count() == 0


def test_timesheet_import_aborts_before_backing_up_on_a_duplicate_pk(tmp_path):
    runner.invoke(app, ["mou", "add", "nlnet-2026"])
    runner.invoke(app, ["task", "select", "10a"])
    path = tmp_path / "timesheet.csv"
    path.write_text(
        "pk,mou,task,start,duration,link,tags\n"
        "1000,nlnet-2026,10a,2026-09-04T09:00:00,01:00:00,"
        "https://example.com/issues/1,implementation\n"
        "1000,nlnet-2026,10a,2026-09-04T10:00:00,01:00:00,"
        "https://example.com/issues/2,implementation\n"
    )

    result = _invoke_timesheet_import(str(path))

    assert result.exit_code != 0
    assert "Backed up" not in result.output


def test_timesheet_import_allows_multiple_blank_pks(tmp_path):
    runner.invoke(app, ["mou", "add", "nlnet-2026"])
    runner.invoke(app, ["task", "select", "10a"])
    path = tmp_path / "timesheet.csv"
    path.write_text(
        "pk,mou,task,start,duration,link,tags\n"
        ",nlnet-2026,10a,2026-09-04T09:00:00,01:00:00,"
        "https://example.com/issues/1,implementation\n"
        ",nlnet-2026,10a,2026-09-04T10:00:00,01:00:00,"
        "https://example.com/issues/2,implementation\n"
    )

    result = _invoke_timesheet_import(str(path))

    assert result.exit_code == 0, result.output
    assert TimeRecord.objects.count() == 2


def test_timesheet_import_prompts_to_delete_missing_entries_and_deletes_on_yes(
    tmp_path,
):
    runner.invoke(app, ["mou", "add", "nlnet-2026"])
    runner.invoke(app, ["task", "select", "10a"])
    runner.invoke(app, ["start", "https://example.com/issues/1"])
    runner.invoke(app, ["stop"])
    path = tmp_path / "timesheet.csv"
    path.write_text("pk,mou,task,start,duration,link,tags\n")

    result = _invoke_timesheet_import(str(path), input="y\n")

    assert result.exit_code == 0, result.output
    assert "Delete them?" in result.output
    assert TimeRecord.objects.count() == 0


def test_timesheet_import_prompts_to_delete_missing_entries_and_keeps_on_no(tmp_path):
    runner.invoke(app, ["mou", "add", "nlnet-2026"])
    runner.invoke(app, ["task", "select", "10a"])
    runner.invoke(app, ["start", "https://example.com/issues/1"])
    runner.invoke(app, ["stop"])
    path = tmp_path / "timesheet.csv"
    path.write_text("pk,mou,task,start,duration,link,tags\n")

    result = _invoke_timesheet_import(str(path), input="n\n")

    assert result.exit_code == 0, result.output
    assert TimeRecord.objects.count() == 1


def test_timesheet_import_does_not_prompt_when_nothing_is_missing(tmp_path):
    runner.invoke(app, ["mou", "add", "nlnet-2026"])
    runner.invoke(app, ["task", "select", "10a"])
    runner.invoke(app, ["start", "https://example.com/issues/1"])
    runner.invoke(app, ["stop"])
    pk = _time_record_pk()
    path = tmp_path / "timesheet.csv"
    path.write_text(
        "pk,mou,task,start,duration,link,tags\n"
        f"{pk},nlnet-2026,10a,2026-09-04T09:00:00,01:00:00,"
        "https://example.com/issues/1,implementation\n"
    )

    result = _invoke_timesheet_import(str(path))

    assert result.exit_code == 0, result.output
    assert "Delete them?" not in result.output
    assert TimeRecord.objects.count() == 1


def test_import_edit_import_then_export_round_trips_all_fields(tmp_path):
    mou_a = MoU.objects.create(name="mou-a")
    Task.objects.create(mou=mou_a, name="10a")
    mou_b = MoU.objects.create(name="mou-b")
    Task.objects.create(mou=mou_b, name="20b")

    initial_csv = (
        "pk,mou,task,start,duration,link,tags\n"
        ",mou-a,10a,2026-01-01T09:00:00,01:00:00,https://example.com/1,implementation\n"
    )
    initial_path = tmp_path / "initial.csv"
    initial_path.write_text(initial_csv)
    result = _invoke_timesheet_import(str(initial_path))
    assert result.exit_code == 0, result.output

    pk = TimeRecord.objects.get().pk
    # Change every field: mou, task, start, duration, link, and tags.
    edited_csv = (
        "pk,mou,task,start,duration,link,tags\n"
        f"{pk},mou-b,20b,2026-02-02T10:30:00,02:15:00,https://example.com/2,review\n"
    )
    edited_path = tmp_path / "edited.csv"
    edited_path.write_text(edited_csv)
    result = _invoke_timesheet_import(str(edited_path))
    assert result.exit_code == 0, result.output

    from nlnet_rfp_recorder.timesheet import read_csv

    export_result = runner.invoke(app, ["timesheet", "export"])

    assert export_result.exit_code == 0, export_result.output
    assert read_csv(export_result.output) == read_csv(edited_csv)


def test_timesheet_import_fails_for_an_unknown_mou(tmp_path):
    path = tmp_path / "timesheet.csv"
    path.write_text(
        "pk,mou,task,start,duration,link,tags\n"
        ",does-not-exist,10a,2026-09-04T09:00:00,01:00:00,https://example.com/1,\n"
    )

    result = _invoke_timesheet_import(str(path))

    assert result.exit_code != 0
    assert "No such MoU" in result.output


def test_timesheet_remove_deletes_it():
    runner.invoke(app, ["mou", "add", "nlnet-2026"])
    runner.invoke(app, ["task", "select", "10a"])
    runner.invoke(app, ["start", "https://example.com/issues/1"])
    runner.invoke(app, ["stop"])
    pk = _time_record_pk()

    result = runner.invoke(app, ["timesheet", "remove", str(pk)])

    assert result.exit_code == 0, result.output
    assert TimeRecord.objects.count() == 0


def test_timesheet_remove_fails_for_an_unknown_pk():
    result = runner.invoke(app, ["timesheet", "remove", "999"])

    assert result.exit_code != 0


def test_timesheet_edit_replaces_all_fields():
    runner.invoke(app, ["mou", "add", "nlnet-2026"])
    runner.invoke(app, ["task", "select", "10a"])
    runner.invoke(app, ["start", "https://example.com/issues/1"])
    runner.invoke(app, ["stop"])
    pk = _time_record_pk()

    result = runner.invoke(
        app,
        [
            "timesheet",
            "edit",
            str(pk),
            "nlnet-2026",
            "10a",
            "2026-09-04T09:00:00",
            "02:00:00",
            "https://example.com/issues/2",
            "review",
        ],
    )

    assert result.exit_code == 0, result.output
    record = TimeRecord.objects.get(pk=pk)
    assert record.link.url == "https://example.com/issues/2"
    assert record.duration == timedelta(hours=2)
    # A new link was created for the new URL, so it only carries the tag
    # given in this edit - the old link's "implementation" tag stays there.
    assert {tag.name for tag in record.link.tags.all()} == {"review"}


def test_timesheet_edit_fails_for_an_unknown_pk():
    runner.invoke(app, ["mou", "add", "nlnet-2026"])
    runner.invoke(app, ["task", "select", "10a"])

    result = runner.invoke(
        app,
        [
            "timesheet",
            "edit",
            "999",
            "nlnet-2026",
            "10a",
            "2026-09-04T09:00:00",
            "01:00:00",
            "https://example.com/issues/1",
        ],
    )

    assert result.exit_code != 0


def test_mou_add_rejects_a_name_with_spaces():
    result = runner.invoke(app, ["mou", "add", "nlnet 2026"])

    assert result.exit_code != 0
    assert "must not contain spaces" in result.output
    assert MoU.objects.count() == 0


def test_report_fails_without_rfp_euros(settings):
    settings.RFP_EUROS_PER_HOUR = None

    result = runner.invoke(app, ["report", "create"])

    assert result.exit_code != 0


def test_report_fails_without_a_selected_mou(settings):
    settings.RFP_EUROS_PER_HOUR = 20.0

    result = runner.invoke(app, ["report", "create"])

    assert result.exit_code != 0
    assert "No MoU selected" in result.output


def test_report_only_includes_tasks_from_the_selected_mou(settings):
    settings.RFP_EUROS_PER_HOUR = 20.0
    other_mou = MoU.objects.create(name="other-mou", selected=False)
    other_task = Task.objects.create(mou=other_mou, name="99z")
    other_link = Link.objects.create(task=other_task, url="https://example.com/other")
    TimeRecord.objects.create(
        link=other_link,
        start_time=timezone.now() - timedelta(minutes=20),
        end_time=timezone.now(),
    )
    mou = MoU.objects.create(name="nlnet-2026", selected=True)
    task = Task.objects.create(mou=mou, name="10a")
    link = Link.objects.create(task=task, url="https://example.com/mine")
    TimeRecord.objects.create(
        link=link,
        start_time=timezone.now() - timedelta(minutes=20),
        end_time=timezone.now(),
    )

    runner.invoke(app, ["report", "create"])
    result = runner.invoke(app, ["report", "print", "nlnet-2026-1"])

    assert result.exit_code == 0, result.output
    assert "MoU: nlnet-2026" in result.output
    assert "10a" in result.output
    assert "99z" not in result.output
    assert "https://example.com/other" not in result.output
    assert "Total: 10€" in result.output


def test_report_prints_budget_issues_and_pull_requests(settings):
    settings.RFP_EUROS_PER_HOUR = 20.0
    mou = MoU.objects.create(name="nlnet-2026", selected=True)
    task = Task.objects.create(mou=mou, name="10a")
    now = timezone.now()
    issue_link = Link.objects.create(
        task=task, url="https://github.com/nlnet/rfp-recorder/issues/1"
    )
    pr_link = Link.objects.create(
        task=task, url="https://github.com/nlnet/rfp-recorder/pull/2"
    )
    other_link = Link.objects.create(task=task, url="https://example.com/docs/design")
    for link in (issue_link, pr_link, other_link):
        TimeRecord.objects.create(
            link=link,
            start_time=now - timedelta(minutes=20),
            end_time=now,
        )

    with _mock_github_session("closed"):
        runner.invoke(app, ["report", "create"])
        result = runner.invoke(app, ["report", "print", "nlnet-2026-1"])

    assert result.exit_code == 0, result.output
    assert "10a: 30€" in result.output
    assert "Issues:" in result.output
    assert "- https://github.com/nlnet/rfp-recorder/issues/1" in result.output
    assert "Pull Requests:" in result.output
    assert "- https://github.com/nlnet/rfp-recorder/pull/2" in result.output
    assert "Links:" in result.output
    assert "- https://example.com/docs/design" in result.output
    assert "Total: 30€" in result.output


def test_report_includes_open_issues_but_excludes_open_prs(settings):
    settings.RFP_EUROS_PER_HOUR = 20.0
    mou = MoU.objects.create(name="nlnet-2026", selected=True)
    task = Task.objects.create(mou=mou, name="10a")
    now = timezone.now()
    issue_link = Link.objects.create(
        task=task, url="https://github.com/nlnet/rfp-recorder/issues/1"
    )
    pr_link = Link.objects.create(
        task=task, url="https://github.com/nlnet/rfp-recorder/pull/2"
    )
    for link in (issue_link, pr_link):
        TimeRecord.objects.create(
            link=link,
            start_time=now - timedelta(minutes=20),
            end_time=now,
        )

    with _mock_github_session("open"):
        create_result = runner.invoke(app, ["report", "create"])
        result = runner.invoke(app, ["report", "print", "nlnet-2026-1"])

    assert result.exit_code == 0, result.output
    assert "Issues:" in result.output
    assert "- https://github.com/nlnet/rfp-recorder/issues/1" in result.output
    assert "Pull Requests:" not in result.output
    # only the issue's 20 minutes count, not the open PR's
    assert "10a: 10€" in result.output
    # the open PR isn't shown when printing the persisted report - that
    # info was only ever surfaced once, at create time (never re-checked)
    assert "Excluded Pull Requests" not in result.output
    assert "Excluded Pull Requests (not merged):" in create_result.output
    assert "- https://github.com/nlnet/rfp-recorder/pull/2" in create_result.output


def test_report_create_fails_cleanly_when_github_rejects_a_status_check(settings):
    settings.RFP_EUROS_PER_HOUR = 20.0
    mou = MoU.objects.create(name="nlnet-2026", selected=True)
    task = Task.objects.create(mou=mou, name="10a")
    link = Link.objects.create(
        task=task, url="https://github.com/collective/icalendar/pull/1559"
    )
    now = timezone.now()
    TimeRecord.objects.create(
        link=link, start_time=now - timedelta(minutes=20), end_time=now
    )

    with _mock_github_error_session():
        result = runner.invoke(app, ["report", "create"])

    assert result.exit_code != 0
    assert "Could not check GitHub PR status" in result.output
    assert "the organization forbids this token" in result.output
    # the report created before the failing status check must be rolled back
    assert Report.objects.count() == 0


def test_report_print_preview_fails_cleanly_when_github_rejects_a_status_check(
    settings,
):
    settings.RFP_EUROS_PER_HOUR = 20.0
    mou = MoU.objects.create(name="nlnet-2026", selected=True)
    task = Task.objects.create(mou=mou, name="10a")
    link = Link.objects.create(
        task=task, url="https://github.com/collective/icalendar/pull/1559"
    )
    now = timezone.now()
    TimeRecord.objects.create(
        link=link, start_time=now - timedelta(minutes=20), end_time=now
    )

    with _mock_github_error_session():
        result = runner.invoke(app, ["report", "print"])

    assert result.exit_code != 0
    assert "Could not check GitHub PR status" in result.output
    assert "the organization forbids this token" in result.output


def test_report_create_and_print_fetch_statuses_once_for_many_tasks(settings):
    # Report.create batches every task's PR status check into a single
    # GitHub round trip, and generate_report (used by `report print`) never
    # hits the network at all - so across many tasks and PRs, the whole
    # create+print flow should call fetch_statuses_async exactly once.
    settings.RFP_EUROS_PER_HOUR = 20.0
    mou = MoU.objects.create(name="nlnet-2026", selected=True)
    now = timezone.now()
    for task_index in range(3):
        task = Task.objects.create(mou=mou, name=f"{10 + task_index}a")
        for pr_index in range(4):
            link = Link.objects.create(
                task=task,
                url=(
                    "https://github.com/nlnet/rfp-recorder/pull/"
                    f"{task_index * 10 + pr_index}"
                ),
            )
            TimeRecord.objects.create(
                link=link,
                start_time=now - timedelta(minutes=30),
                end_time=now,
            )

    async def fake_fetch_statuses_async(references, token=None):
        return [Status.CLOSED for _ in references]

    with patch(
        "nlnet_rfp_recorder.github.fetch_statuses_async",
        AsyncMock(side_effect=fake_fetch_statuses_async),
    ) as mock_fetch:
        create_result = runner.invoke(app, ["report", "create"])
        result = runner.invoke(app, ["report", "print", "nlnet-2026-1"])

    assert create_result.exit_code == 0, create_result.output
    assert result.exit_code == 0, result.output
    assert mock_fetch.call_count == 1
    for task_index in range(3):
        assert f"{10 + task_index}a: 40€" in result.output
    assert "Total: 120€" in result.output


def test_report_fails_while_something_is_running(settings):
    settings.RFP_EUROS_PER_HOUR = 20.0
    mou = MoU.objects.create(name="nlnet-2026", selected=True)
    task = Task.objects.create(mou=mou, name="10a")
    link = Link.objects.create(task=task, url="https://example.com/issues/1")
    TimeRecord.objects.create(link=link, start_time=timezone.now())

    result = runner.invoke(app, ["report", "create"])

    assert result.exit_code != 0
    assert "https://example.com/issues/1" in result.output
    assert "still running" in result.output.lower()


def test_report_shows_review_tag_suffix(settings):
    settings.RFP_EUROS_PER_HOUR = 20.0
    mou = MoU.objects.create(name="nlnet-2026", selected=True)
    task = Task.objects.create(mou=mou, name="10a")
    link = Link.objects.create(task=task, url="https://example.com/docs/design")
    link.add_tag("review")
    TimeRecord.objects.create(
        link=link,
        start_time=timezone.now() - timedelta(minutes=20),
        end_time=timezone.now(),
    )

    runner.invoke(app, ["report", "create"])
    result = runner.invoke(app, ["report", "print", "nlnet-2026-1"])

    assert result.exit_code == 0, result.output
    assert "- https://example.com/docs/design - 10€ (review)" in result.output


def test_report_create_persists_a_report(settings):
    settings.RFP_EUROS_PER_HOUR = 20.0
    mou = MoU.objects.create(name="nlnet-2026", selected=True)
    task = Task.objects.create(mou=mou, name="10a")
    link = Link.objects.create(task=task, url="https://example.com/issues/1")
    TimeRecord.objects.create(
        link=link,
        start_time=timezone.now() - timedelta(minutes=20),
        end_time=timezone.now(),
    )

    result = runner.invoke(app, ["report", "create"])

    assert result.exit_code == 0, result.output
    report = Report.objects.get()
    assert report.id == "nlnet-2026-1"
    assert f"rfp report print {report.id}" in result.output


def test_report_create_twice_fails_the_second_time_with_nothing_new(settings):
    settings.RFP_EUROS_PER_HOUR = 20.0
    mou = MoU.objects.create(name="nlnet-2026", selected=True)
    task = Task.objects.create(mou=mou, name="10a")
    link = Link.objects.create(task=task, url="https://example.com/issues/1")
    TimeRecord.objects.create(
        link=link,
        start_time=timezone.now() - timedelta(minutes=20),
        end_time=timezone.now(),
    )
    runner.invoke(app, ["report", "create"])

    result = runner.invoke(app, ["report", "create"])

    assert result.exit_code != 0
    assert "Nothing to report" in result.output
    # the empty second report must not be left behind
    assert not Report.objects.filter(id="nlnet-2026-2").exists()


def test_report_create_fails_with_no_time_records(settings):
    settings.RFP_EUROS_PER_HOUR = 20.0
    MoU.objects.create(name="nlnet-2026", selected=True)

    result = runner.invoke(app, ["report", "create"])

    assert result.exit_code != 0
    assert "Nothing to report" in result.output
    assert Report.objects.count() == 0


def test_report_remove_deletes_it(settings):
    settings.RFP_EUROS_PER_HOUR = 20.0
    mou = MoU.objects.create(name="nlnet-2026", selected=True)
    task = Task.objects.create(mou=mou, name="10a")
    link = Link.objects.create(task=task, url="https://example.com/issues/1")
    TimeRecord.objects.create(
        link=link,
        start_time=timezone.now() - timedelta(minutes=20),
        end_time=timezone.now(),
    )
    runner.invoke(app, ["report", "create"])

    result = runner.invoke(app, ["report", "remove", "nlnet-2026-1"])

    assert result.exit_code == 0, result.output
    assert Report.objects.count() == 0


def test_report_print_with_an_explicit_id(settings):
    settings.RFP_EUROS_PER_HOUR = 20.0
    mou = MoU.objects.create(name="nlnet-2026", selected=True)
    task = Task.objects.create(mou=mou, name="10a")
    link = Link.objects.create(task=task, url="https://example.com/issues/1")
    TimeRecord.objects.create(
        link=link,
        start_time=timezone.now() - timedelta(minutes=20),
        end_time=timezone.now(),
    )
    runner.invoke(app, ["report", "create"])

    result = runner.invoke(app, ["report", "print", "nlnet-2026-1"])

    assert result.exit_code == 0, result.output
    assert "Report: nlnet-2026-1" in result.output
    assert "10a" in result.output


def test_report_print_without_an_id_previews_unreported_entries(settings):
    settings.RFP_EUROS_PER_HOUR = 20.0
    mou = MoU.objects.create(name="nlnet-2026", selected=True)
    task = Task.objects.create(mou=mou, name="10a")
    link_a = Link.objects.create(task=task, url="https://example.com/a")
    TimeRecord.objects.create(
        link=link_a,
        start_time=timezone.now() - timedelta(minutes=20),
        end_time=timezone.now(),
    )
    runner.invoke(app, ["report", "create"])
    link_b = Link.objects.create(task=task, url="https://example.com/b")
    TimeRecord.objects.create(
        link=link_b,
        start_time=timezone.now() - timedelta(minutes=20),
        end_time=timezone.now(),
    )

    result = runner.invoke(app, ["report", "print"])

    assert result.exit_code == 0, result.output
    assert "Report: (unreported)" in result.output
    assert "https://example.com/b" in result.output
    assert "https://example.com/a" not in result.output
    assert Report.objects.filter(id__startswith="nlnet-2026").count() == 1


def test_report_print_without_an_id_is_empty_with_nothing_unreported(settings):
    settings.RFP_EUROS_PER_HOUR = 20.0
    MoU.objects.create(name="nlnet-2026", selected=True)

    result = runner.invoke(app, ["report", "print"])

    assert result.exit_code == 0, result.output
    assert "Report: (unreported)" in result.output
    assert "Total: 0€" in result.output


def test_report_print_without_an_id_fails_without_a_selected_mou():
    result = runner.invoke(app, ["report", "print"])

    assert result.exit_code != 0
    assert "No MoU selected" in result.output


def test_report_print_fails_for_an_unknown_id():
    result = runner.invoke(app, ["report", "print", "does-not-exist"])

    assert result.exit_code != 0
    assert "No such report" in result.output


def test_report_list_shows_id_date_and_budget_used(settings):
    settings.RFP_EUROS_PER_HOUR = 20.0
    mou = MoU.objects.create(name="nlnet-2026", selected=True)
    task = Task.objects.create(mou=mou, name="10a")
    link = Link.objects.create(task=task, url="https://example.com/issues/1")
    TimeRecord.objects.create(
        link=link,
        start_time=timezone.now() - timedelta(hours=1),
        end_time=timezone.now(),
    )
    runner.invoke(app, ["report", "create"])
    report = Report.objects.get()
    today = report.created.date().isoformat()

    result = runner.invoke(app, ["report", "list"])

    assert result.exit_code == 0, result.output
    assert f"{report.id} {today} 20€" in result.output


def test_report_list_only_includes_the_selected_mous_reports(settings):
    settings.RFP_EUROS_PER_HOUR = 20.0
    mou_a = MoU.objects.create(name="mou-a", selected=False)
    task_a = Task.objects.create(mou=mou_a, name="10a")
    link_a = Link.objects.create(task=task_a, url="https://example.com/a")
    TimeRecord.objects.create(
        link=link_a,
        start_time=timezone.now() - timedelta(minutes=20),
        end_time=timezone.now(),
    )
    Report.create(mou_a).add_unreported_time_records()

    mou_b = MoU.objects.create(name="mou-b", selected=True)
    task_b = Task.objects.create(mou=mou_b, name="20b")
    link_b = Link.objects.create(task=task_b, url="https://example.com/b")
    TimeRecord.objects.create(
        link=link_b,
        start_time=timezone.now() - timedelta(minutes=20),
        end_time=timezone.now(),
    )
    runner.invoke(app, ["report", "create"])

    result = runner.invoke(app, ["report", "list"])

    assert result.exit_code == 0, result.output
    assert "mou-a-1" not in result.output
    assert "mou-b-1" in result.output


def test_report_list_fails_without_a_selected_mou():
    result = runner.invoke(app, ["report", "list"])

    assert result.exit_code != 0
    assert "No MoU selected" in result.output


def test_report_list_reports_when_there_are_none():
    MoU.objects.create(name="nlnet-2026", selected=True)

    result = runner.invoke(app, ["report", "list"])

    assert result.exit_code == 0, result.output
    assert "No reports" in result.output


def test_report_remove_fails_for_an_unknown_report():
    result = runner.invoke(app, ["report", "remove", "does-not-exist"])

    assert result.exit_code != 0


def test_report_remove_orphans_but_keeps_its_time_records(settings):
    settings.RFP_EUROS_PER_HOUR = 20.0
    mou = MoU.objects.create(name="nlnet-2026", selected=True)
    task = Task.objects.create(mou=mou, name="10a")
    link = Link.objects.create(task=task, url="https://example.com/issues/1")
    TimeRecord.objects.create(
        link=link,
        start_time=timezone.now() - timedelta(minutes=20),
        end_time=timezone.now(),
    )
    runner.invoke(app, ["report", "create"])
    record = TimeRecord.objects.get()
    assert record.report_line.report_id == "nlnet-2026-1"

    runner.invoke(app, ["report", "remove", "nlnet-2026-1"])

    record.refresh_from_db()
    assert record.report_line_id is None


def _report_csv(*rows: dict) -> str:
    buffer = io.StringIO()
    fieldnames = ["task", "link", "budget", "tags", "records"]
    writer = csv.DictWriter(buffer, fieldnames=fieldnames)
    writer.writeheader()
    for row in rows:
        writer.writerow(row)
    return buffer.getvalue()


def test_report_export_lists_report_lines_as_csv(tmp_path, settings):
    settings.RFP_EUROS_PER_HOUR = 20.0
    mou = MoU.objects.create(name="nlnet-2026", selected=True)
    task = Task.objects.create(mou=mou, name="10a")
    link = Link.objects.create(task=task, url="https://example.com/issues/1")
    link.add_tag("review")
    now = timezone.now()
    TimeRecord.objects.create(
        link=link, start_time=now - timedelta(minutes=30), end_time=now
    )
    runner.invoke(app, ["report", "create"])
    record = TimeRecord.objects.get()
    path = tmp_path / "report.csv"

    result = runner.invoke(app, ["report", "export", "nlnet-2026-1", str(path)])

    assert result.exit_code == 0, result.output
    rows = list(csv.DictReader(io.StringIO(path.read_text())))
    assert rows == [
        {
            "task": "10a",
            "link": "https://example.com/issues/1",
            "title": "",
            "budget": "10.0",
            "tags": "review",
            "records": str(record.pk),
        }
    ]


def test_report_export_fetches_and_caches_the_issue_title(tmp_path, settings):
    settings.RFP_EUROS_PER_HOUR = 20.0
    mou = MoU.objects.create(name="nlnet-2026", selected=True)
    task = Task.objects.create(mou=mou, name="10a")
    link = Link.objects.create(
        task=task, url="https://github.com/nlnet/rfp-recorder/issues/1"
    )
    now = timezone.now()
    TimeRecord.objects.create(
        link=link, start_time=now - timedelta(minutes=30), end_time=now
    )
    runner.invoke(app, ["report", "create"])
    path = tmp_path / "report.csv"

    with _mock_github_title_session("Fix the thing"):
        result = runner.invoke(app, ["report", "export", "nlnet-2026-1", str(path)])

    assert result.exit_code == 0, result.output
    rows = list(csv.DictReader(io.StringIO(path.read_text())))
    assert rows[0]["title"] == "Fix the thing"
    link.refresh_from_db()
    assert link.title == "Fix the thing"


def test_report_export_fails_when_github_is_unreachable(settings):
    settings.RFP_EUROS_PER_HOUR = 20.0
    mou = MoU.objects.create(name="nlnet-2026", selected=True)
    task = Task.objects.create(mou=mou, name="10a")
    link = Link.objects.create(
        task=task, url="https://github.com/nlnet/rfp-recorder/issues/1"
    )
    now = timezone.now()
    TimeRecord.objects.create(
        link=link, start_time=now - timedelta(minutes=30), end_time=now
    )
    runner.invoke(app, ["report", "create"])

    session = MagicMock()
    session.get = AsyncMock(
        side_effect=niquests.exceptions.ConnectionError("no network")
    )
    session.__aenter__ = AsyncMock(return_value=session)
    session.__aexit__ = AsyncMock(return_value=False)
    with patch("nlnet_rfp_recorder.github.niquests.AsyncSession", return_value=session):
        result = runner.invoke(app, ["report", "export", "nlnet-2026-1"])

    assert result.exit_code != 0
    assert "Could not fetch issue/PR titles" in result.output


def test_report_export_fails_for_an_unknown_report():
    result = runner.invoke(app, ["report", "export", "does-not-exist"])

    assert result.exit_code != 0


def test_report_import_removes_a_line_not_in_the_file(tmp_path, settings):
    settings.RFP_EUROS_PER_HOUR = 20.0
    mou = MoU.objects.create(name="nlnet-2026", selected=True)
    task = Task.objects.create(mou=mou, name="10a")
    link = Link.objects.create(task=task, url="https://example.com/issues/1")
    TimeRecord.objects.create(
        link=link,
        start_time=timezone.now() - timedelta(minutes=20),
        end_time=timezone.now(),
    )
    runner.invoke(app, ["report", "create"])
    record = TimeRecord.objects.get()
    path = tmp_path / "report.csv"
    path.write_text(_report_csv())

    result = _invoke_report_import("nlnet-2026-1", str(path), input="1\n")

    assert result.exit_code == 0, result.output
    assert "No longer in the imported file" in result.output
    record.refresh_from_db()
    assert record.report_line is None
    link.refresh_from_db()
    assert link.excluded_from_reports is False


def test_report_import_remove_choice_2_excludes_the_link_permanently(
    tmp_path, settings
):
    settings.RFP_EUROS_PER_HOUR = 20.0
    mou = MoU.objects.create(name="nlnet-2026", selected=True)
    task = Task.objects.create(mou=mou, name="10a")
    link = Link.objects.create(task=task, url="https://example.com/issues/1")
    TimeRecord.objects.create(
        link=link,
        start_time=timezone.now() - timedelta(minutes=20),
        end_time=timezone.now(),
    )
    runner.invoke(app, ["report", "create"])
    record = TimeRecord.objects.get()
    path = tmp_path / "report.csv"
    path.write_text(_report_csv())

    result = _invoke_report_import("nlnet-2026-1", str(path), input="2\n")

    assert result.exit_code == 0, result.output
    record.refresh_from_db()
    assert record.report_line is None
    link.refresh_from_db()
    assert link.excluded_from_reports is True


def test_report_import_remove_choice_3_keeps_the_line(tmp_path, settings):
    settings.RFP_EUROS_PER_HOUR = 20.0
    mou = MoU.objects.create(name="nlnet-2026", selected=True)
    task = Task.objects.create(mou=mou, name="10a")
    link = Link.objects.create(task=task, url="https://example.com/issues/1")
    TimeRecord.objects.create(
        link=link,
        start_time=timezone.now() - timedelta(minutes=20),
        end_time=timezone.now(),
    )
    runner.invoke(app, ["report", "create"])
    record = TimeRecord.objects.get()
    path = tmp_path / "report.csv"
    path.write_text(_report_csv())

    result = _invoke_report_import("nlnet-2026-1", str(path), input="3\n")

    assert result.exit_code == 0, result.output
    record.refresh_from_db()
    assert record.report_line is not None
    assert record.report_line.report_id == "nlnet-2026-1"


def test_report_import_removed_link_prompt_shows_the_github_title(tmp_path, settings):
    settings.RFP_EUROS_PER_HOUR = 20.0
    mou = MoU.objects.create(name="nlnet-2026", selected=True)
    task = Task.objects.create(mou=mou, name="10a")
    link = Link.objects.create(
        task=task, url="https://github.com/collective/icalendar/issues/1"
    )
    TimeRecord.objects.create(
        link=link,
        start_time=timezone.now() - timedelta(minutes=20),
        end_time=timezone.now(),
    )
    runner.invoke(app, ["report", "create"])
    path = tmp_path / "report.csv"
    path.write_text(_report_csv())

    with patch(
        "nlnet_rfp_recorder.github.fetch_title", return_value="Fix the thing"
    ) as fetch_title:
        result = _invoke_report_import("nlnet-2026-1", str(path), input="1\n")

    fetch_title.assert_called_once_with("collective", "icalendar", 1, token=None)
    assert result.exit_code == 0, result.output
    assert "Fix the thing" in result.output


def test_report_import_reprompts_on_an_invalid_choice(tmp_path, settings):
    settings.RFP_EUROS_PER_HOUR = 20.0
    mou = MoU.objects.create(name="nlnet-2026", selected=True)
    task = Task.objects.create(mou=mou, name="10a")
    link = Link.objects.create(task=task, url="https://example.com/issues/1")
    TimeRecord.objects.create(
        link=link,
        start_time=timezone.now() - timedelta(minutes=20),
        end_time=timezone.now(),
    )
    runner.invoke(app, ["report", "create"])
    path = tmp_path / "report.csv"
    path.write_text(_report_csv())

    result = _invoke_report_import("nlnet-2026-1", str(path), input="bogus\n1\n")

    assert result.exit_code == 0, result.output
    assert "Please enter 1, 2, or 3." in result.output


def test_report_import_adds_a_record_from_the_file(tmp_path, settings):
    settings.RFP_EUROS_PER_HOUR = 20.0
    mou = MoU.objects.create(name="nlnet-2026", selected=True)
    task = Task.objects.create(mou=mou, name="10a")
    link = Link.objects.create(task=task, url="https://example.com/issues/1")
    record = TimeRecord.objects.create(
        link=link,
        start_time=timezone.now() - timedelta(minutes=20),
        end_time=timezone.now(),
    )
    report = Report.create(mou)
    path = tmp_path / "report.csv"
    path.write_text(
        _report_csv(
            {
                "task": "10a",
                "link": link.url,
                "budget": "100",
                "tags": "review",
                "records": str(record.pk),
            }
        )
    )

    result = _invoke_report_import(report.id, str(path))

    assert result.exit_code == 0, result.output
    record.refresh_from_db()
    assert record.report_line.report_id == report.id
    assert record.report_line.budget == 100.0
    assert record.report_line.tags == "review"


def test_report_import_fails_for_a_link_of_a_different_mou(tmp_path, settings):
    settings.RFP_EUROS_PER_HOUR = 20.0
    mou_a = MoU.objects.create(name="mou-a")
    mou_b = MoU.objects.create(name="mou-b", selected=True)
    task_b = Task.objects.create(mou=mou_b, name="10a")
    link = Link.objects.create(task=task_b, url="https://example.com/issues/1")
    TimeRecord.objects.create(
        link=link,
        start_time=timezone.now() - timedelta(minutes=20),
        end_time=timezone.now(),
    )
    report_a = Report.create(mou_a)
    path = tmp_path / "report.csv"
    path.write_text(
        _report_csv(
            {
                "task": "10a",
                "link": link.url,
                "budget": "100",
                "tags": "",
                "records": "",
            }
        )
    )

    result = _invoke_report_import(report_a.id, str(path))

    assert result.exit_code != 0
    assert "does not belong to MoU" in result.output


def test_report_import_creates_a_new_link_for_an_unknown_url(tmp_path, settings):
    # An unknown link is no longer a hard failure - it's created fresh,
    # with no time records attached (there's nothing to unambiguously
    # attach it to).
    mou = MoU.objects.create(name="nlnet-2026", selected=True)
    task = Task.objects.create(mou=mou, name="10a")
    report = Report.create(mou)
    path = tmp_path / "report.csv"
    path.write_text(
        _report_csv(
            {
                "task": "10a",
                "link": "https://example.com/issues/999",
                "budget": "50",
                "tags": "",
                "records": "",
            }
        )
    )

    result = _invoke_report_import(report.id, str(path))

    assert result.exit_code == 0, result.output
    link = Link.objects.get(url="https://example.com/issues/999")
    assert link.task == task
    report_line = ReportLine.objects.get(report=report, link=link)
    assert report_line.budget == 50.0
    assert report_line.time_records.count() == 0


def test_report_review_prints_each_task_and_defaults_small_tasks_to_excluded(
    settings,
):
    settings.RFP_EUROS_PER_HOUR = 20.0
    mou = MoU.objects.create(name="nlnet-2026", selected=True)
    big_task = Task.objects.create(mou=mou, name="10a")
    small_task = Task.objects.create(mou=mou, name="10b")
    big_link = Link.objects.create(task=big_task, url="https://example.com/issues/1")
    small_link = Link.objects.create(
        task=small_task, url="https://example.com/issues/2"
    )
    now = timezone.now()
    # 3 hours @ 20€/h = 60€ - at/above 50, defaults to included.
    TimeRecord.objects.create(
        link=big_link, start_time=now - timedelta(hours=3), end_time=now
    )
    # 6 minutes @ 20€/h = 2€ - under 50, defaults to excluded.
    TimeRecord.objects.create(
        link=small_link, start_time=now - timedelta(minutes=6), end_time=now
    )
    runner.invoke(app, ["report", "create"])
    small_record = TimeRecord.objects.get(link=small_link)

    # Two task prompts (both blank, accepting their defaults), then an
    # explicit "y" to the final write confirmation (whose own default is
    # No, to make actually writing changes an opt-in step).
    result = runner.invoke(app, ["report", "review", "nlnet-2026-1"], input="\n\ny\n")

    assert result.exit_code == 0, result.output
    assert "10a: 60€" in result.output
    assert "10b: 10€" in result.output
    assert "Included: 10a" in result.output
    assert "Excluded: 10b" in result.output
    assert ReportLine.objects.filter(report__id="nlnet-2026-1", link=big_link).exists()
    assert not ReportLine.objects.filter(
        report__id="nlnet-2026-1", link=small_link
    ).exists()
    small_record.refresh_from_db()
    assert small_record.report_line is None


def test_report_review_leaves_the_report_unchanged_when_cancelled(settings):
    settings.RFP_EUROS_PER_HOUR = 20.0
    mou = MoU.objects.create(name="nlnet-2026", selected=True)
    task = Task.objects.create(mou=mou, name="10a")
    link = Link.objects.create(task=task, url="https://example.com/issues/1")
    now = timezone.now()
    # 6 minutes @ 20€/h = 2€ - defaults to excluded, but we cancel anyway.
    TimeRecord.objects.create(
        link=link, start_time=now - timedelta(minutes=6), end_time=now
    )
    runner.invoke(app, ["report", "create"])
    record = TimeRecord.objects.get()

    result = runner.invoke(app, ["report", "review", "nlnet-2026-1"], input="\nn\n")

    assert result.exit_code == 0, result.output
    assert "Cancelled" in result.output
    assert ReportLine.objects.filter(report__id="nlnet-2026-1", link=link).exists()
    record.refresh_from_db()
    assert record.report_line is not None


def test_report_review_with_nothing_excluded_skips_the_write_prompt(settings):
    settings.RFP_EUROS_PER_HOUR = 20.0
    mou = MoU.objects.create(name="nlnet-2026", selected=True)
    task = Task.objects.create(mou=mou, name="10a")
    link = Link.objects.create(task=task, url="https://example.com/issues/1")
    now = timezone.now()
    # 3 hours @ 20€/h = 60€ - defaults to included, so nothing to write.
    TimeRecord.objects.create(
        link=link, start_time=now - timedelta(hours=3), end_time=now
    )
    runner.invoke(app, ["report", "create"])

    result = runner.invoke(app, ["report", "review", "nlnet-2026-1"], input="\n")

    assert result.exit_code == 0, result.output
    assert "Nothing to change" in result.output
    assert ReportLine.objects.filter(report__id="nlnet-2026-1", link=link).exists()


def test_report_review_fails_for_an_unknown_report():
    result = runner.invoke(app, ["report", "review", "does-not-exist"])

    assert result.exit_code != 0


def test_report_review_reports_nothing_to_review_for_an_empty_report(settings):
    mou = MoU.objects.create(name="nlnet-2026", selected=True)
    report = Report.create(mou)

    result = runner.invoke(app, ["report", "review", report.id])

    assert result.exit_code == 0, result.output
    assert "no lines to review" in result.output


def test_mou_add_selects_a_mou():
    result = runner.invoke(app, ["mou", "add", "nlnet-2026"])

    assert result.exit_code == 0, result.output
    mou = MoU.objects.get()
    assert mou.name == "nlnet-2026"
    assert mou.selected is True


def test_mou_status_fails_without_a_selected_mou():
    result = runner.invoke(app, ["mou", "status"])

    assert result.exit_code != 0


def test_mou_status_shows_no_budget_line_when_no_tasks_have_a_max_budget():
    runner.invoke(app, ["mou", "add", "nlnet-2026"])

    result = runner.invoke(app, ["mou", "status"])

    assert result.exit_code == 0, result.output
    assert "MoU: nlnet-2026" in result.output
    assert "Budget" not in result.output


def test_mou_status_sums_budget_across_tasks(tmp_path, settings):
    settings.RFP_EUROS_PER_HOUR = 20.0
    runner.invoke(app, ["mou", "add", "nlnet-2026"])
    budget_file = tmp_path / "budget.txt"
    budget_file.write_text("(DONE) 10a. Already done\t€ 300\n10b. Still open\t€ 200\n")
    _invoke_mou_import(str(budget_file))

    result = runner.invoke(app, ["mou", "status"])

    assert result.exit_code == 0, result.output
    assert "MoU: nlnet-2026" in result.output
    # 10a is fully used (300), 10b has no tracked time yet (0): 300/500
    assert "300€/500€" in result.output


def test_mou_list_reports_when_there_are_none():
    result = runner.invoke(app, ["mou", "list"])

    assert result.exit_code == 0, result.output
    assert "No MoUs" in result.output


def test_mou_list_marks_the_selected_mou():
    runner.invoke(app, ["mou", "add", "nlnet-2025"])
    runner.invoke(app, ["mou", "add", "nlnet-2026"])

    result = runner.invoke(app, ["mou", "list"])

    assert result.exit_code == 0, result.output
    assert "  nlnet-2025" in result.output
    assert "* nlnet-2026" in result.output


def test_mou_remove_deletes_it():
    runner.invoke(app, ["mou", "add", "nlnet-2026"])

    result = runner.invoke(app, ["mou", "remove", "nlnet-2026"])

    assert result.exit_code == 0, result.output
    assert MoU.objects.count() == 0


def test_task_status_survives_its_mou_being_removed():
    runner.invoke(app, ["mou", "add", "nlnet-2026"])
    runner.invoke(app, ["task", "select", "10a"])

    runner.invoke(app, ["mou", "remove", "nlnet-2026"])
    result = runner.invoke(app, ["task"])

    assert result.exit_code == 0, result.output
    assert "MoU: none" in result.output
    assert "Selected task: 10a" in result.output


def test_mou_remove_fails_for_an_unknown_mou():
    result = runner.invoke(app, ["mou", "remove", "does-not-exist"])

    assert result.exit_code != 0


def test_mou_select_fails_for_an_unknown_mou():
    result = runner.invoke(app, ["mou", "select", "does-not-exist"])

    assert result.exit_code != 0


def test_mou_select_switches_to_an_existing_mou_without_creating():
    runner.invoke(app, ["mou", "add", "nlnet-2025"])
    runner.invoke(app, ["mou", "add", "nlnet-2026"])

    result = runner.invoke(app, ["mou", "select", "nlnet-2025"])

    assert result.exit_code == 0, result.output
    assert MoU.objects.count() == 2
    assert MoU.get_selected().name == "nlnet-2025"


def test_mou_import_prints_the_current_mou(tmp_path):
    runner.invoke(app, ["mou", "add", "nlnet-2026"])
    budget_file = tmp_path / "budget.txt"
    budget_file.write_text("10a. Do the thing\t€ 500\n")

    result = _invoke_mou_import(str(budget_file))

    assert result.exit_code == 0, result.output
    assert "Current MoU: nlnet-2026" in result.output


def test_mou_import_fails_without_a_selected_mou(tmp_path):
    budget_file = tmp_path / "budget.txt"
    budget_file.write_text("10a. Do the thing\t€ 500\n")

    result = _invoke_mou_import(str(budget_file))

    assert result.exit_code != 0


def test_mou_import_caps_matching_tasks(tmp_path):
    runner.invoke(app, ["mou", "add", "nlnet-2026"])
    budget_file = tmp_path / "budget.txt"
    budget_file.write_text("10a. Do the thing\t€ 500\n")

    result = _invoke_mou_import(str(budget_file))

    assert result.exit_code == 0, result.output
    mou = MoU.objects.get()
    assert "10a. Do the thing" in mou.budget
    task = Task.objects.get(name="10a")
    assert task.max_budget == 500.0
    assert task.mou == mou


def test_mou_import_with_mou_option_creates_and_selects_it(tmp_path):
    budget_file = tmp_path / "budget.txt"
    budget_file.write_text("10a. Do the thing\t€ 500\n")

    result = _invoke_mou_import(str(budget_file), "--mou", "nlnet-2026")

    assert result.exit_code == 0, result.output
    mou = MoU.objects.get()
    assert mou.name == "nlnet-2026"
    assert mou.selected is True
    task = Task.objects.get(name="10a")
    assert task.max_budget == 500.0
    assert task.mou == mou


def test_mou_import_with_mou_option_selects_an_existing_mou(tmp_path):
    runner.invoke(app, ["mou", "add", "nlnet-2025"])
    runner.invoke(app, ["mou", "add", "nlnet-2026"])
    budget_file = tmp_path / "budget.txt"
    budget_file.write_text("10a. Do the thing\t€ 500\n")

    result = _invoke_mou_import(str(budget_file), "--mou", "nlnet-2025")

    assert result.exit_code == 0, result.output
    assert MoU.objects.count() == 2
    assert MoU.get_selected().name == "nlnet-2025"


def test_mou_import_from_stdin_notifies_when_parsing_starts():
    runner.invoke(app, ["mou", "add", "nlnet-2026"])

    result = _invoke_mou_import(input="10a. Do the thing\t€ 500\n\n\n\n")

    assert result.exit_code == 0, result.output
    assert "stop typing" in result.stderr.lower()
    task = Task.objects.get(name="10a")
    assert task.max_budget == 500.0


def test_mou_export_prints_the_raw_imported_text(tmp_path):
    runner.invoke(app, ["mou", "add", "nlnet-2026"])
    budget_file = tmp_path / "budget.txt"
    budget_file.write_text("10a. Do the thing\t€ 500\n")
    _invoke_mou_import(str(budget_file))

    result = runner.invoke(app, ["mou", "export"])

    assert result.exit_code == 0, result.output
    assert result.output == "10a. Do the thing\t€ 500\n"


def test_mou_export_fails_without_a_selected_mou():
    result = runner.invoke(app, ["mou", "export"])

    assert result.exit_code != 0
    assert "No MoU selected" in result.output


def test_mou_export_is_empty_before_anything_was_imported():
    runner.invoke(app, ["mou", "add", "nlnet-2026"])

    result = runner.invoke(app, ["mou", "export"])

    assert result.exit_code == 0, result.output
    assert result.output == ""


def test_mou_export_with_a_name_exports_a_non_selected_mou(tmp_path):
    runner.invoke(app, ["mou", "add", "nlnet-2025"])
    budget_file = tmp_path / "budget.txt"
    budget_file.write_text("10a. Do the thing\t€ 500\n")
    _invoke_mou_import(str(budget_file))
    runner.invoke(app, ["mou", "add", "nlnet-2026"])

    result = runner.invoke(app, ["mou", "export", "nlnet-2025"])

    assert result.exit_code == 0, result.output
    assert result.output == "10a. Do the thing\t€ 500\n"
    # exporting a MoU by name must not change which one is selected
    assert MoU.get_selected().name == "nlnet-2026"


def test_mou_export_fails_for_an_unknown_name():
    result = runner.invoke(app, ["mou", "export", "does-not-exist"])

    assert result.exit_code != 0
    assert "No such MoU" in result.output


def test_mou_export_resolves_an_alias(tmp_path):
    runner.invoke(app, ["mou", "add", "nlnet-2025"])
    budget_file = tmp_path / "budget.txt"
    budget_file.write_text("10a. Do the thing\t€ 500\n")
    _invoke_mou_import(str(budget_file))
    runner.invoke(app, ["alias", "set", "mou", "nlnet-2025", "og"])

    result = runner.invoke(app, ["mou", "export", "og"])

    assert result.exit_code == 0, result.output
    assert result.output == "10a. Do the thing\t€ 500\n"


def test_start_tags_default_to_implementation():
    Task.objects.create(name="10a")

    result = runner.invoke(app, ["start", "https://example.com/issues/1"])

    assert result.exit_code == 0, result.output
    link = Link.objects.get()
    assert [tag.name for tag in link.tags.all()] == ["implementation"]


def test_start_accepts_explicit_tags():
    Task.objects.create(name="10a")

    result = runner.invoke(
        app, ["start", "https://example.com/issues/1", "--tags", "review"]
    )

    assert result.exit_code == 0, result.output
    link = Link.objects.get()
    assert [tag.name for tag in link.tags.all()] == ["review"]


def test_review_starts_a_time_entry_tagged_review():
    Task.objects.create(name="10a")

    result = runner.invoke(app, ["review", "https://example.com/issues/1"])

    assert result.exit_code == 0, result.output
    record = TimeRecord.objects.get()
    assert record.is_running is True
    link = Link.objects.get()
    assert [tag.name for tag in link.tags.all()] == ["review"]
    assert "Started time entry for task 10a" in result.output


def test_review_without_a_task_fails():
    result = runner.invoke(app, ["review", "https://example.com/issues/1"])

    assert result.exit_code != 0


def test_review_stops_a_previously_running_entry():
    Task.objects.create(name="10a")
    runner.invoke(app, ["start", "https://example.com/issues/1"])

    result = runner.invoke(app, ["review", "https://example.com/issues/2"])

    assert result.exit_code == 0, result.output
    assert "Stopped 10a" in result.output


def test_review_with_an_explicit_task_selects_it_first():
    runner.invoke(app, ["mou", "add", "nlnet-2026"])
    runner.invoke(app, ["task", "select", "10a"])

    result = runner.invoke(app, ["review", "10b", "https://example.com/issues/1"])

    assert result.exit_code == 0, result.output
    record = TimeRecord.objects.get()
    assert record.link.task.name == "10b"
    assert [tag.name for tag in record.link.tags.all()] == ["review"]
    assert Task.objects.get(name="10b").selected is True


def test_review_with_too_many_positional_arguments_fails():
    Task.objects.create(name="10a")

    result = runner.invoke(app, ["review", "10a", "extra", "https://example.com/1"])

    assert result.exit_code != 0
    assert "Usage: rfp review" in result.output


def test_review_prints_the_tasks_description_if_present():
    runner.invoke(app, ["mou", "add", "nlnet-2026"])
    Task.objects.create(mou=MoU.objects.get(), name="10a", description="Do the thing")

    result = runner.invoke(app, ["review", "10a", "https://example.com/issues/1"])

    assert result.exit_code == 0, result.output
    assert "Do the thing" in result.output


def test_implement_starts_a_time_entry_tagged_implementation():
    Task.objects.create(name="10a")

    result = runner.invoke(app, ["implement", "https://example.com/issues/1"])

    assert result.exit_code == 0, result.output
    record = TimeRecord.objects.get()
    assert record.is_running is True
    link = Link.objects.get()
    assert [tag.name for tag in link.tags.all()] == ["implementation"]
    assert "Started time entry for task 10a" in result.output


def test_implement_without_a_task_fails():
    result = runner.invoke(app, ["implement", "https://example.com/issues/1"])

    assert result.exit_code != 0


def test_implement_with_an_explicit_task_selects_it_first():
    runner.invoke(app, ["mou", "add", "nlnet-2026"])
    runner.invoke(app, ["task", "select", "10a"])

    result = runner.invoke(app, ["implement", "10b", "https://example.com/issues/1"])

    assert result.exit_code == 0, result.output
    record = TimeRecord.objects.get()
    assert record.link.task.name == "10b"


def _mock_github_get(responses: dict[str, dict]):
    """Patch niquests.get to return canned JSON per URL suffix."""

    def fake_get(url, **kwargs):
        for suffix, data in responses.items():
            if url.endswith(suffix):
                return MagicMock(status_code=200, json=MagicMock(return_value=data))
        raise AssertionError(f"Unexpected GitHub request: {url}")

    return patch("nlnet_rfp_recorder.github.niquests.get", side_effect=fake_get)


def test_start_defaults_to_implementation_when_i_authored_the_issue():
    Task.objects.create(name="10a")
    GitHubToken.set("secret")

    with _mock_github_get(
        {
            "/user": {"login": "octocat"},
            "/issues/1782": {"user": {"login": "octocat"}},
        }
    ):
        result = runner.invoke(
            app, ["start", "https://github.com/nlnet/rfp-recorder/issues/1782"]
        )

    assert result.exit_code == 0, result.output
    link = Link.objects.get()
    assert [tag.name for tag in link.tags.all()] == ["implementation"]


def test_start_defaults_to_review_when_someone_else_authored_the_issue():
    Task.objects.create(name="10a")
    GitHubToken.set("secret")

    with _mock_github_get(
        {
            "/user": {"login": "octocat"},
            "/issues/1782": {"user": {"login": "someone-else"}},
        }
    ):
        result = runner.invoke(
            app, ["start", "https://github.com/nlnet/rfp-recorder/issues/1782"]
        )

    assert result.exit_code == 0, result.output
    link = Link.objects.get()
    assert [tag.name for tag in link.tags.all()] == ["review"]


def test_start_defaults_to_implementation_without_a_saved_token():
    Task.objects.create(name="10a")

    with patch("nlnet_rfp_recorder.github.niquests.get") as get:
        result = runner.invoke(
            app, ["start", "https://github.com/nlnet/rfp-recorder/issues/1782"]
        )

    assert result.exit_code == 0, result.output
    get.assert_not_called()
    link = Link.objects.get()
    assert [tag.name for tag in link.tags.all()] == ["implementation"]


def test_start_defaults_to_implementation_when_github_is_unreachable():
    Task.objects.create(name="10a")
    GitHubToken.set("secret")

    with patch(
        "nlnet_rfp_recorder.github.niquests.get",
        side_effect=niquests.exceptions.ConnectionError("no network"),
    ):
        result = runner.invoke(
            app, ["start", "https://github.com/nlnet/rfp-recorder/issues/1782"]
        )

    assert result.exit_code == 0, result.output
    link = Link.objects.get()
    assert [tag.name for tag in link.tags.all()] == ["implementation"]


def test_start_defaults_to_implementation_for_a_plain_link_even_with_a_token():
    # No issue/PR to check authorship against, so no GitHub call is made
    # at all - the previous, unconditional default just applies.
    Task.objects.create(name="10a")
    GitHubToken.set("secret")

    with patch("nlnet_rfp_recorder.github.niquests.get") as get:
        result = runner.invoke(app, ["start", "https://example.com/issues/1"])

    assert result.exit_code == 0, result.output
    get.assert_not_called()
    link = Link.objects.get()
    assert [tag.name for tag in link.tags.all()] == ["implementation"]


def test_start_asks_before_changing_an_existing_tag():
    Task.objects.create(name="10a")
    runner.invoke(app, ["start", "https://example.com/issues/1"])
    runner.invoke(app, ["stop"])

    result = runner.invoke(
        app,
        ["start", "https://example.com/issues/1", "--tags", "review"],
        input="y\n",
    )

    assert result.exit_code == 0, result.output
    assert "is tagged 'implementation', but this looks like 'review'" in result.output
    link = Link.objects.get()
    assert [tag.name for tag in link.tags.all()] == ["review"]


def test_start_declining_the_tag_change_leaves_the_old_tag():
    Task.objects.create(name="10a")
    runner.invoke(app, ["start", "https://example.com/issues/1"])
    runner.invoke(app, ["stop"])

    result = runner.invoke(
        app,
        ["start", "https://example.com/issues/1", "--tags", "review"],
        input="n\n",
    )

    assert result.exit_code == 0, result.output
    link = Link.objects.get()
    assert [tag.name for tag in link.tags.all()] == ["implementation"]


def test_review_on_an_implementation_tagged_link_asks_before_changing_it():
    Task.objects.create(name="10a")
    runner.invoke(app, ["implement", "https://example.com/issues/1"])
    runner.invoke(app, ["stop"])

    result = runner.invoke(app, ["review", "https://example.com/issues/1"], input="y\n")

    assert result.exit_code == 0, result.output
    assert "is tagged 'implementation', but this looks like 'review'" in result.output
    link = Link.objects.get()
    assert [tag.name for tag in link.tags.all()] == ["review"]


def test_start_does_not_ask_when_the_tag_already_matches():
    Task.objects.create(name="10a")
    runner.invoke(app, ["start", "https://example.com/issues/1"])
    runner.invoke(app, ["stop"])

    result = runner.invoke(app, ["start", "https://example.com/issues/1"])

    assert result.exit_code == 0, result.output
    assert "is tagged" not in result.output
    link = Link.objects.get()
    assert [tag.name for tag in link.tags.all()] == ["implementation"]


def test_start_asks_before_moving_a_link_to_a_different_task():
    runner.invoke(app, ["mou", "add", "nlnet-2026"])
    runner.invoke(app, ["task", "select", "10a"])
    runner.invoke(app, ["start", "https://example.com/issues/1"])
    runner.invoke(app, ["stop"])

    result = runner.invoke(
        app, ["start", "10b", "https://example.com/issues/1"], input="y\n"
    )

    assert result.exit_code == 0, result.output
    assert "is under task 10a, but this looks like 10b" in result.output
    link = Link.objects.get()
    assert link.task.name == "10b"


def test_start_defaults_to_yes_when_moving_a_link_to_a_different_task():
    # Blank input accepts the confirm's default - which must be yes here,
    # unlike the tag question: typing a different task is usually a
    # deliberate correction, not something to be wary of by default.
    runner.invoke(app, ["mou", "add", "nlnet-2026"])
    runner.invoke(app, ["task", "select", "10a"])
    runner.invoke(app, ["start", "https://example.com/issues/1"])
    runner.invoke(app, ["stop"])

    result = runner.invoke(
        app, ["start", "10b", "https://example.com/issues/1"], input="\n"
    )

    assert result.exit_code == 0, result.output
    link = Link.objects.get()
    assert link.task.name == "10b"


def test_start_declining_the_task_change_leaves_the_old_task():
    runner.invoke(app, ["mou", "add", "nlnet-2026"])
    runner.invoke(app, ["task", "select", "10a"])
    runner.invoke(app, ["start", "https://example.com/issues/1"])
    runner.invoke(app, ["stop"])

    result = runner.invoke(
        app, ["start", "10b", "https://example.com/issues/1"], input="n\n"
    )

    assert result.exit_code == 0, result.output
    link = Link.objects.get()
    assert link.task.name == "10a"


def test_start_does_not_ask_when_the_task_already_matches():
    runner.invoke(app, ["mou", "add", "nlnet-2026"])
    runner.invoke(app, ["task", "select", "10a"])
    runner.invoke(app, ["start", "https://example.com/issues/1"])
    runner.invoke(app, ["stop"])

    result = runner.invoke(app, ["start", "10a", "https://example.com/issues/1"])

    assert result.exit_code == 0, result.output
    assert "is under task" not in result.output


def test_start_assigns_a_taskless_link_without_asking():
    task = Task.objects.create(name="10a")
    Link.objects.create(url="https://example.com/issues/1")

    result = runner.invoke(app, ["start", "https://example.com/issues/1"])

    assert result.exit_code == 0, result.output
    assert "is under task" not in result.output
    link = Link.objects.get()
    assert link.task == task


def test_implement_on_a_different_task_asks_before_moving_it():
    runner.invoke(app, ["mou", "add", "nlnet-2026"])
    runner.invoke(app, ["task", "select", "10a"])
    runner.invoke(app, ["implement", "https://example.com/issues/1"])
    runner.invoke(app, ["stop"])

    result = runner.invoke(
        app, ["implement", "10b", "https://example.com/issues/1"], input="y\n"
    )

    assert result.exit_code == 0, result.output
    link = Link.objects.get()
    assert link.task.name == "10b"


def test_token_without_args_prints_instructions_and_saves_nothing():
    result = runner.invoke(app, ["token"])

    assert result.exit_code == 0, result.output
    assert "personal-access-tokens" in result.output
    assert GitHubToken.objects.count() == 0


def test_token_with_a_value_saves_it():
    response = MagicMock(status_code=200)
    response.json = MagicMock(return_value={"login": "octocat"})

    with patch("nlnet_rfp_recorder.github.niquests.get", return_value=response) as get:
        result = runner.invoke(app, ["token", "secret"])

    assert result.exit_code == 0, result.output
    assert "Saved GitHub token for octocat." in result.output
    assert GitHubToken.get() == "secret"
    get.assert_called_once_with(
        "https://api.github.com/user",
        headers={"Authorization": "Bearer secret"},
        timeout=TIMEOUT_SECONDS,
    )


def test_token_rejects_a_token_github_does_not_recognize():
    response = MagicMock(status_code=401)

    with patch("nlnet_rfp_recorder.github.niquests.get", return_value=response):
        result = runner.invoke(app, ["token", "bad-token"])

    assert result.exit_code != 0
    assert "401" in result.output
    assert GitHubToken.objects.count() == 0


def test_token_fails_when_github_is_unreachable():
    with patch(
        "nlnet_rfp_recorder.github.niquests.get",
        side_effect=niquests.exceptions.ConnectionError("boom"),
    ):
        result = runner.invoke(app, ["token", "secret"])

    assert result.exit_code != 0
    assert "Could not verify the GitHub token" in result.output
    assert GitHubToken.objects.count() == 0


def test_complete_mou_name_matches_by_prefix():
    from nlnet_rfp_recorder.cli import _complete_mou_name

    MoU.objects.create(name="nlnet-2025")
    MoU.objects.create(name="nlnet-2026")
    MoU.objects.create(name="other-grant")

    assert set(_complete_mou_name("nlnet")) == {"nlnet-2025", "nlnet-2026"}
    assert _complete_mou_name("other") == ["other-grant"]
    assert _complete_mou_name("nope") == []


def test_complete_task_name_matches_by_prefix():
    from nlnet_rfp_recorder.cli import _complete_task_name

    mou = MoU.objects.create(name="nlnet-2026")
    Task.objects.create(mou=mou, name="10a")
    Task.objects.create(mou=mou, name="10b")
    Task.objects.create(mou=mou, name="11a")

    assert set(_complete_task_name("10")) == {"10a", "10b"}
    assert _complete_task_name("11") == ["11a"]
    assert _complete_task_name("99") == []


def test_status_with_nothing_selected():
    result = runner.invoke(app, ["status"])

    assert result.exit_code == 0, result.output
    assert "MoU: none selected" in result.output
    assert "Task: none selected" in result.output
    assert "Not running." in result.output


def test_status_shows_task_status_when_selected():
    runner.invoke(app, ["mou", "add", "nlnet-2026"])
    runner.invoke(app, ["task", "select", "10a"])

    result = runner.invoke(app, ["status"])

    assert result.exit_code == 0, result.output
    assert "MoU: nlnet-2026" in result.output
    assert "Selected task: 10a" in result.output
    assert "Not running." in result.output


def test_status_shows_running_time_entry_and_duration():
    runner.invoke(app, ["mou", "add", "nlnet-2026"])
    runner.invoke(app, ["task", "select", "10a"])
    runner.invoke(app, ["start", "https://example.com/issues/1"])

    result = runner.invoke(app, ["status"])

    assert result.exit_code == 0, result.output
    assert "Running: https://example.com/issues/1" in result.output


def test_stats_today_reports_no_time_when_nothing_tracked():
    result = runner.invoke(app, ["stats", "today"])

    assert result.exit_code == 0, result.output
    assert "No time tracked in this period." in result.output


def test_stats_today_shows_per_task_time_and_budget(settings):
    settings.RFP_EUROS_PER_HOUR = 20.0
    mou = MoU.objects.create(name="nlnet-2026")
    task = Task.objects.create(mou=mou, name="10a")
    link = Link.objects.create(task=task, url="https://example.com/issues/1")
    now = timezone.now()
    TimeRecord.objects.create(
        link=link,
        start_time=now - timedelta(hours=1),
        end_time=now,
    )

    result = runner.invoke(app, ["stats", "today"])

    assert result.exit_code == 0, result.output
    assert "10a" in result.output
    assert "1:00" in result.output
    assert "20€" in result.output


def test_stats_hides_the_total_row_with_only_one_task(settings):
    settings.RFP_EUROS_PER_HOUR = 20.0
    mou = MoU.objects.create(name="nlnet-2026")
    task = Task.objects.create(mou=mou, name="10a")
    link = Link.objects.create(task=task, url="https://example.com/issues/1")
    now = timezone.now()
    TimeRecord.objects.create(
        link=link, start_time=now - timedelta(hours=1), end_time=now
    )

    result = runner.invoke(app, ["stats", "today"])

    assert result.exit_code == 0, result.output
    assert "Total" not in result.output
    assert len(result.output.splitlines()) == 2


def test_stats_today_excludes_records_from_before_today(settings):
    settings.RFP_EUROS_PER_HOUR = 20.0
    mou = MoU.objects.create(name="nlnet-2026")
    task = Task.objects.create(mou=mou, name="10a")
    link = Link.objects.create(task=task, url="https://example.com/issues/1")
    yesterday = timezone.now() - timedelta(days=1)
    TimeRecord.objects.create(
        link=link,
        start_time=yesterday - timedelta(hours=1),
        end_time=yesterday,
    )

    result = runner.invoke(app, ["stats", "today"])

    assert result.exit_code == 0, result.output
    assert "No time tracked in this period." in result.output


def test_stats_days_includes_records_within_the_window(settings):
    settings.RFP_EUROS_PER_HOUR = 20.0
    mou = MoU.objects.create(name="nlnet-2026")
    task = Task.objects.create(mou=mou, name="10a")
    link = Link.objects.create(task=task, url="https://example.com/issues/1")
    two_days_ago = timezone.now() - timedelta(days=2)
    TimeRecord.objects.create(
        link=link,
        start_time=two_days_ago,
        end_time=two_days_ago + timedelta(hours=1),
    )

    result = runner.invoke(app, ["stats", "days", "3"])

    assert result.exit_code == 0, result.output
    assert "10a" in result.output
    assert "1:00" in result.output


def test_stats_days_excludes_records_outside_the_window(settings):
    settings.RFP_EUROS_PER_HOUR = 20.0
    mou = MoU.objects.create(name="nlnet-2026")
    task = Task.objects.create(mou=mou, name="10a")
    link = Link.objects.create(task=task, url="https://example.com/issues/1")
    ten_days_ago = timezone.now() - timedelta(days=10)
    TimeRecord.objects.create(
        link=link,
        start_time=ten_days_ago,
        end_time=ten_days_ago + timedelta(hours=1),
    )

    result = runner.invoke(app, ["stats", "days", "3"])

    assert result.exit_code == 0, result.output
    assert "No time tracked in this period." in result.output


def test_stats_days_rejects_a_non_positive_n():
    result = runner.invoke(app, ["stats", "days", "0"])

    assert result.exit_code != 0
    assert "must be positive" in result.output


def test_stats_total_reports_no_time_when_nothing_tracked():
    result = runner.invoke(app, ["stats", "total"])

    assert result.exit_code == 0, result.output
    assert "No time tracked in this period." in result.output


def test_stats_total_includes_records_regardless_of_age(settings):
    settings.RFP_EUROS_PER_HOUR = 20.0
    mou = MoU.objects.create(name="nlnet-2026")
    task = Task.objects.create(mou=mou, name="10a")
    link = Link.objects.create(task=task, url="https://example.com/issues/1")
    long_ago = timezone.now() - timedelta(days=3650)
    TimeRecord.objects.create(
        link=link, start_time=long_ago, end_time=long_ago + timedelta(hours=1)
    )

    result = runner.invoke(app, ["stats", "total"])

    assert result.exit_code == 0, result.output
    assert "10a" in result.output
    assert "1:00" in result.output
    assert "20€" in result.output


def test_stats_total_sums_records_across_different_days(settings):
    settings.RFP_EUROS_PER_HOUR = 20.0
    mou = MoU.objects.create(name="nlnet-2026")
    task = Task.objects.create(mou=mou, name="10a")
    link_a = Link.objects.create(task=task, url="https://example.com/issues/1")
    link_b = Link.objects.create(task=task, url="https://example.com/issues/2")
    now = timezone.now()
    TimeRecord.objects.create(
        link=link_a,
        start_time=now - timedelta(days=30),
        end_time=now - timedelta(days=30) + timedelta(hours=1),
    )
    TimeRecord.objects.create(
        link=link_b, start_time=now - timedelta(hours=1), end_time=now
    )

    result = runner.invoke(app, ["stats", "total"])

    assert result.exit_code == 0, result.output
    assert "2:00" in result.output


def test_stats_sums_multiple_records_per_task(settings):
    settings.RFP_EUROS_PER_HOUR = 20.0
    mou = MoU.objects.create(name="nlnet-2026")
    task = Task.objects.create(mou=mou, name="10a")
    link_a = Link.objects.create(task=task, url="https://example.com/issues/1")
    link_b = Link.objects.create(task=task, url="https://example.com/issues/2")
    now = timezone.now()
    TimeRecord.objects.create(
        link=link_a, start_time=now - timedelta(hours=1), end_time=now
    )
    TimeRecord.objects.create(
        link=link_b,
        start_time=now - timedelta(minutes=30),
        end_time=now,
    )

    result = runner.invoke(app, ["stats", "today"])

    assert result.exit_code == 0, result.output
    assert "1:30" in result.output


def test_stats_groups_two_tasks_separately_with_a_total(settings):
    settings.RFP_EUROS_PER_HOUR = 20.0
    mou = MoU.objects.create(name="nlnet-2026")
    task_a = Task.objects.create(mou=mou, name="10a")
    task_b = Task.objects.create(mou=mou, name="11b")
    link_a = Link.objects.create(task=task_a, url="https://example.com/issues/1")
    link_b = Link.objects.create(task=task_b, url="https://example.com/issues/2")
    now = timezone.now()
    TimeRecord.objects.create(
        link=link_a, start_time=now - timedelta(hours=1), end_time=now
    )
    TimeRecord.objects.create(
        link=link_b, start_time=now - timedelta(hours=1), end_time=now
    )

    result = runner.invoke(app, ["stats", "today"])

    assert result.exit_code == 0, result.output
    assert "10a" in result.output
    assert "11b" in result.output
    assert "Total" in result.output
    assert "2:00" in result.output
    assert "40€" in result.output


def test_stats_groups_time_with_no_task_under_a_question_mark(settings):
    settings.RFP_EUROS_PER_HOUR = 20.0
    link = Link.objects.create(url="https://example.com/issues/1")
    now = timezone.now()
    TimeRecord.objects.create(
        link=link, start_time=now - timedelta(hours=1), end_time=now
    )

    result = runner.invoke(app, ["stats", "today"])

    assert result.exit_code == 0, result.output
    assert "?" in result.output


def test_stats_hides_budget_when_rfp_euros_per_hour_is_unset(settings):
    settings.RFP_EUROS_PER_HOUR = None
    mou = MoU.objects.create(name="nlnet-2026")
    task = Task.objects.create(mou=mou, name="10a")
    link = Link.objects.create(task=task, url="https://example.com/issues/1")
    now = timezone.now()
    TimeRecord.objects.create(
        link=link, start_time=now - timedelta(hours=1), end_time=now
    )

    result = runner.invoke(app, ["stats", "today"])

    assert result.exit_code == 0, result.output
    assert "1:00" in result.output
    assert "€" not in result.output


def test_stats_has_a_header_row_with_id_time_euro_new(settings):
    settings.RFP_EUROS_PER_HOUR = 20.0
    mou = MoU.objects.create(name="nlnet-2026")
    task = Task.objects.create(mou=mou, name="10a")
    link = Link.objects.create(task=task, url="https://example.com/issues/1")
    now = timezone.now()
    TimeRecord.objects.create(
        link=link, start_time=now - timedelta(hours=1), end_time=now
    )

    result = runner.invoke(app, ["stats", "today"])

    assert result.exit_code == 0, result.output
    header = result.output.splitlines()[0].split()
    assert header == ["ID", "time", "Euro", "new", "50+"]


def test_stats_header_omits_euro_and_new_when_rate_is_unset(settings):
    settings.RFP_EUROS_PER_HOUR = None
    mou = MoU.objects.create(name="nlnet-2026")
    task = Task.objects.create(mou=mou, name="10a")
    link = Link.objects.create(task=task, url="https://example.com/issues/1")
    now = timezone.now()
    TimeRecord.objects.create(
        link=link, start_time=now - timedelta(hours=1), end_time=now
    )

    result = runner.invoke(app, ["stats", "today"])

    assert result.exit_code == 0, result.output
    header = result.output.splitlines()[0].split()
    assert header == ["ID", "time"]


def test_stats_shows_an_alias_column_when_a_task_has_one(settings):
    settings.RFP_EUROS_PER_HOUR = 20.0
    mou = MoU.objects.create(name="nlnet-2026")
    task = Task.objects.create(mou=mou, name="10a")
    Alias.create("task", "10a", "foo", mou=mou)
    link = Link.objects.create(task=task, url="https://example.com/issues/1")
    now = timezone.now()
    TimeRecord.objects.create(
        link=link, start_time=now - timedelta(hours=1), end_time=now
    )

    result = runner.invoke(app, ["stats", "today"])

    assert result.exit_code == 0, result.output
    lines = result.output.splitlines()
    assert lines[0].split() == ["ID", "alias", "time", "Euro", "new", "50+"]
    assert "foo" in lines[1]


def test_stats_omits_the_alias_column_when_no_task_has_one(settings):
    settings.RFP_EUROS_PER_HOUR = 20.0
    mou = MoU.objects.create(name="nlnet-2026")
    task = Task.objects.create(mou=mou, name="10a")
    link = Link.objects.create(task=task, url="https://example.com/issues/1")
    now = timezone.now()
    TimeRecord.objects.create(
        link=link, start_time=now - timedelta(hours=1), end_time=now
    )

    result = runner.invoke(app, ["stats", "today"])

    assert result.exit_code == 0, result.output
    header = result.output.splitlines()[0].split()
    assert "alias" not in header


def test_stats_new_column_shows_unclaimed_budget(settings):
    settings.RFP_EUROS_PER_HOUR = 20.0
    mou = MoU.objects.create(name="nlnet-2026")
    task = Task.objects.create(mou=mou, name="10a")
    link = Link.objects.create(task=task, url="https://example.com/issues/1")
    now = timezone.now()
    TimeRecord.objects.create(
        link=link, start_time=now - timedelta(hours=1), end_time=now
    )

    result = runner.invoke(app, ["stats", "today"])

    assert result.exit_code == 0, result.output
    lines = result.output.splitlines()
    assert lines[1].split() == ["10a", "1:00", "20€", "20€"]


def test_stats_new_column_is_blank_once_claimed_by_a_report(settings):
    settings.RFP_EUROS_PER_HOUR = 20.0
    mou = MoU.objects.create(name="nlnet-2026", selected=True)
    task = Task.objects.create(mou=mou, name="10a")
    link = Link.objects.create(task=task, url="https://example.com/issues/1")
    now = timezone.now()
    record = TimeRecord.objects.create(
        link=link, start_time=now - timedelta(hours=1), end_time=now
    )
    report = Report.create(mou)
    report.add_time_record(record)

    result = runner.invoke(app, ["stats", "today"])

    assert result.exit_code == 0, result.output
    lines = result.output.splitlines()
    assert lines[1].split() == ["10a", "1:00", "20€"]


def test_stats_threshold_column_shows_new_budget_at_or_above_the_threshold(settings):
    settings.RFP_EUROS_PER_HOUR = 20.0
    settings.REVIEW_DEFAULT_EXCLUDE_BELOW = 50
    mou = MoU.objects.create(name="nlnet-2026")
    task = Task.objects.create(mou=mou, name="10a")
    link = Link.objects.create(task=task, url="https://example.com/issues/1")
    now = timezone.now()
    TimeRecord.objects.create(
        link=link, start_time=now - timedelta(hours=3), end_time=now
    )

    result = runner.invoke(app, ["stats", "today"])

    assert result.exit_code == 0, result.output
    lines = result.output.splitlines()
    assert lines[0].split()[-1] == "50+"
    assert lines[1].split() == ["10a", "3:00", "60€", "60€", "60€"]


def test_stats_threshold_column_is_blank_below_the_threshold(settings):
    settings.RFP_EUROS_PER_HOUR = 20.0
    settings.REVIEW_DEFAULT_EXCLUDE_BELOW = 50
    mou = MoU.objects.create(name="nlnet-2026")
    task = Task.objects.create(mou=mou, name="10a")
    link = Link.objects.create(task=task, url="https://example.com/issues/1")
    now = timezone.now()
    TimeRecord.objects.create(
        link=link, start_time=now - timedelta(hours=1), end_time=now
    )

    result = runner.invoke(app, ["stats", "today"])

    assert result.exit_code == 0, result.output
    lines = result.output.splitlines()
    assert lines[1].split() == ["10a", "1:00", "20€", "20€"]


def test_stats_threshold_column_totals_only_qualifying_tasks(settings):
    settings.RFP_EUROS_PER_HOUR = 20.0
    settings.REVIEW_DEFAULT_EXCLUDE_BELOW = 50
    mou = MoU.objects.create(name="nlnet-2026")
    task_a = Task.objects.create(mou=mou, name="10a")
    task_b = Task.objects.create(mou=mou, name="11b")
    link_a = Link.objects.create(task=task_a, url="https://example.com/issues/1")
    link_b = Link.objects.create(task=task_b, url="https://example.com/issues/2")
    now = timezone.now()
    # 10a: 3h = 60€ (qualifies); 11b: 1h = 20€ (does not).
    TimeRecord.objects.create(
        link=link_a, start_time=now - timedelta(hours=3), end_time=now
    )
    TimeRecord.objects.create(
        link=link_b, start_time=now - timedelta(hours=1), end_time=now
    )

    result = runner.invoke(app, ["stats", "today"])

    assert result.exit_code == 0, result.output
    lines = result.output.splitlines()
    assert lines[1].split() == ["10a", "3:00", "60€", "60€", "60€"]
    assert lines[2].split() == ["11b", "1:00", "20€", "20€"]
    assert lines[3].split() == ["Total", "4:00", "80€", "80€", "60€"]


def test_complete_link_url_matches_by_prefix():
    from nlnet_rfp_recorder.cli import _complete_link_url

    Link.objects.create(url="https://example.com/issues/1")
    Link.objects.create(url="https://example.com/issues/2")
    Link.objects.create(url="https://other.example/pull/1")

    assert set(_complete_link_url("https://example.com")) == {
        "https://example.com/issues/1",
        "https://example.com/issues/2",
    }
    assert _complete_link_url("https://other") == ["https://other.example/pull/1"]
    assert _complete_link_url("https://nope") == []


def test_complete_backup_name_matches_by_prefix(tmp_path, settings):
    db_path = tmp_path / "custom.sqlite3"
    db_path.write_bytes(b"")
    settings.DATABASES["default"]["NAME"] = str(db_path)
    (tmp_path / "custom-20260101T090000.sqlite3").write_bytes(b"")
    (tmp_path / "custom-20260102T090000.sqlite3").write_bytes(b"")
    (tmp_path / "other-20260101T090000.sqlite3").write_bytes(b"")

    from nlnet_rfp_recorder.cli import _complete_backup_name

    assert set(_complete_backup_name("custom-2026010")) == {
        "custom-20260101T090000",
        "custom-20260102T090000",
    }
    assert _complete_backup_name("nope") == []


def test_restore_fails_for_an_unknown_backup_name():
    result = runner.invoke(app, ["restore", "does-not-exist"])

    assert result.exit_code != 0
    assert "No such backup" in result.output


BACKUP_NAME_RE = re.compile(r"\d{4}-\d{2}-\d{2}_\d{2}-\d{2}_([a-z-]+)$")


def _backup_action(output: str) -> str:
    """Pull the action label out of a "Backed up ... to ...-<action>" line."""
    match = re.search(r"Backed up .*? to (\S+)", output)
    assert match, output
    name_match = BACKUP_NAME_RE.search(Path(match.group(1)).stem)
    assert name_match, match.group(1)
    return name_match.group(1)


def test_backup_labels_the_backup_as_backup():
    result = runner.invoke(app, ["backup"])

    assert result.exit_code == 0, result.output
    assert _backup_action(result.output) == "backup"


def test_restore_labels_its_safety_backup_as_restore():
    runner.invoke(app, ["mou", "add", "nlnet-2026"])
    backup_result = runner.invoke(app, ["backup"])
    backup_name = re.search(r"to (\S+)", backup_result.output).group(1)

    result = runner.invoke(app, ["restore", Path(backup_name).stem])

    assert result.exit_code == 0, result.output
    assert _backup_action(result.output) == "restore"


def test_timesheet_import_labels_its_backup_as_timesheet_import(tmp_path):
    path = tmp_path / "timesheet.csv"
    path.write_text("pk,mou,task,start,duration,link,tags\n")

    result = _invoke_timesheet_import(str(path))

    assert result.exit_code == 0, result.output
    assert _backup_action(result.output) == "timesheet-import"


def test_task_import_labels_its_backup_as_task_import(tmp_path):
    MoU.objects.create(name="nlnet-2026", selected=True)
    path = tmp_path / "tasks.csv"
    path.write_text(
        "id,alias,personal_budget,max_budget,used_budget,description\n10a,,,,,\n"
    )

    result = _invoke_task_import(str(path))

    assert result.exit_code == 0, result.output
    assert _backup_action(result.output) == "task-import"


def test_mou_import_labels_its_backup_as_mou_import(tmp_path):
    MoU.objects.create(name="nlnet-2026", selected=True)
    budget_file = tmp_path / "budget.txt"
    budget_file.write_text("10a. Do the thing\t€ 500\n")

    result = _invoke_mou_import(str(budget_file))

    assert result.exit_code == 0, result.output
    assert _backup_action(result.output) == "mou-import"


def test_report_import_labels_its_backup_as_report_import(tmp_path):
    mou = MoU.objects.create(name="nlnet-2026", selected=True)
    Task.objects.create(mou=mou, name="10a")
    report = Report.create(mou)
    path = tmp_path / "report.csv"
    path.write_text(_report_csv())

    result = _invoke_report_import(report.id, str(path))

    assert result.exit_code == 0, result.output
    assert _backup_action(result.output) == "report-import"


def _subcommand_names(*group_path: str) -> list[str]:
    # Ask Click's own list_commands() - the exact hook it uses to order the
    # rendered --help panel - instead of parsing rendered/wrapped Rich
    # output, which is sensitive to terminal width and Rich version and
    # made these tests flaky in CI (though not reproducible locally).
    from typer.main import get_command

    group = get_command(app)
    ctx = typer.Context(group)
    for name in group_path:
        group = group.commands[name]
        ctx = typer.Context(group, parent=ctx)
    return group.list_commands(ctx)


def test_help_lists_commands_alphabetically():
    result = runner.invoke(app, ["--help"])

    assert result.exit_code == 0, result.output
    assert _subcommand_names() == [
        "alias",
        "backup",
        "continue",
        "edit",
        "implement",
        "migrate",
        "mou",
        "report",
        "restore",
        "review",
        "start",
        "stats",
        "status",
        "stop",
        "task",
        "timesheet",
        "token",
        "version",
    ]


def test_timesheet_help_lists_subcommands_alphabetically():
    result = runner.invoke(app, ["timesheet", "--help"])

    assert result.exit_code == 0, result.output
    assert _subcommand_names("timesheet") == [
        "edit",
        "export",
        "import",
        "remove",
        "show",
    ]


def test_stats_help_lists_subcommands_alphabetically():
    result = runner.invoke(app, ["stats", "--help"])

    assert result.exit_code == 0, result.output
    assert _subcommand_names("stats") == ["days", "today", "total"]


def test_report_help_lists_subcommands_alphabetically():
    result = runner.invoke(app, ["report", "--help"])

    assert result.exit_code == 0, result.output
    assert _subcommand_names("report") == [
        "create",
        "export",
        "import",
        "list",
        "print",
        "remove",
        "review",
    ]


def test_mou_help_lists_subcommands_alphabetically():
    result = runner.invoke(app, ["mou", "--help"])

    assert result.exit_code == 0, result.output
    assert _subcommand_names("mou") == [
        "add",
        "export",
        "import",
        "list",
        "remove",
        "select",
        "status",
    ]


def test_task_help_lists_subcommands_alphabetically():
    result = runner.invoke(app, ["task", "--help"])

    assert result.exit_code == 0, result.output
    assert _subcommand_names("task") == [
        "export",
        "import",
        "list",
        "remove",
        "select",
        "set",
        "status",
    ]


def test_alias_help_lists_subcommands_alphabetically():
    result = runner.invoke(app, ["alias", "--help"])

    assert result.exit_code == 0, result.output
    assert _subcommand_names("alias") == ["list", "remove", "rename", "set"]


def test_alias_set_creates_a_mou_alias():
    runner.invoke(app, ["mou", "add", "nlnet-2026"])

    result = runner.invoke(app, ["alias", "set", "mou", "nlnet-2026", "og"])

    assert result.exit_code == 0, result.output
    assert Alias.objects.get(item_type="mou", alias="og").target == "nlnet-2026"


def test_alias_set_creates_a_task_alias_scoped_to_the_selected_mou():
    runner.invoke(app, ["mou", "add", "nlnet-2026"])
    runner.invoke(app, ["task", "select", "10a"])

    result = runner.invoke(app, ["alias", "set", "task", "10a", "lib"])

    assert result.exit_code == 0, result.output
    task_alias = Alias.objects.get(item_type="task", alias="lib")
    assert task_alias.target == "10a"
    assert task_alias.mou == MoU.get_selected()


def test_alias_set_creates_a_url_alias():
    result = runner.invoke(
        app,
        [
            "alias",
            "set",
            "url",
            "https://github.com/collective/icalendar",
            "ical",
        ],
    )

    assert result.exit_code == 0, result.output
    assert (
        Alias.objects.get(item_type="url", alias="ical").target
        == "https://github.com/collective/icalendar"
    )


def test_alias_set_fails_cleanly_on_a_validation_error():
    result = runner.invoke(app, ["alias", "set", "mou", "does-not-exist", "og"])

    assert result.exit_code != 0
    assert "No such MoU" in result.output


def test_alias_set_rejects_an_unknown_item_type():
    result = runner.invoke(app, ["alias", "set", "bogus", "x", "y"])

    assert result.exit_code != 0
    assert "mou" in result.output
    assert "task" in result.output
    assert "url" in result.output


def test_alias_set_help_shows_the_item_choices():
    result = runner.invoke(app, ["alias", "set", "--help"])

    assert result.exit_code == 0, result.output
    assert "mou|task|url" in result.output


def test_alias_remove_by_alias_name():
    runner.invoke(app, ["mou", "add", "nlnet-2026"])
    runner.invoke(app, ["alias", "set", "mou", "nlnet-2026", "og"])

    result = runner.invoke(app, ["alias", "remove", "mou", "og"])

    assert result.exit_code == 0, result.output
    assert Alias.objects.count() == 0


def test_alias_remove_by_underlying_id():
    runner.invoke(app, ["mou", "add", "nlnet-2026"])
    runner.invoke(app, ["alias", "set", "mou", "nlnet-2026", "og"])

    result = runner.invoke(app, ["alias", "remove", "mou", "nlnet-2026"])

    assert result.exit_code == 0, result.output
    assert Alias.objects.count() == 0


def test_alias_remove_fails_for_an_unknown_alias():
    result = runner.invoke(app, ["alias", "remove", "mou", "does-not-exist"])

    assert result.exit_code != 0
    assert "No mou alias found" in result.output


def test_alias_rename_updates_the_alias():
    runner.invoke(app, ["mou", "add", "nlnet-2026"])
    runner.invoke(app, ["alias", "set", "mou", "nlnet-2026", "og"])

    result = runner.invoke(app, ["alias", "rename", "mou", "og", "newalias"])

    assert result.exit_code == 0, result.output
    assert Alias.objects.filter(alias="og").exists() is False
    renamed = Alias.objects.get(item_type="mou", alias="newalias")
    assert renamed.target == "nlnet-2026"


def test_alias_rename_fails_for_an_unknown_alias():
    result = runner.invoke(app, ["alias", "rename", "mou", "does-not-exist", "new"])

    assert result.exit_code != 0
    assert "No mou alias" in result.output


def test_alias_rename_steals_a_collision_with_the_new_name():
    runner.invoke(app, ["mou", "add", "nlnet-2026"])
    runner.invoke(app, ["mou", "add", "nlnet-2027"])
    runner.invoke(app, ["alias", "set", "mou", "nlnet-2026", "og"])
    runner.invoke(app, ["alias", "set", "mou", "nlnet-2027", "new"])

    result = runner.invoke(app, ["alias", "rename", "mou", "og", "new"])

    assert result.exit_code == 0, result.output
    assert "Replaced alias 'new' (was for mou 'nlnet-2027')" in result.output
    assert Alias.objects.filter(item_type="mou", alias="og").exists() is False
    renamed = Alias.objects.get(item_type="mou", alias="new")
    assert renamed.target == "nlnet-2026"


def test_alias_set_prints_when_it_steals_an_alias_from_another_target():
    runner.invoke(app, ["mou", "add", "nlnet-2026"])
    runner.invoke(app, ["mou", "add", "nlnet-2027"])
    runner.invoke(app, ["alias", "set", "mou", "nlnet-2026", "og"])

    result = runner.invoke(app, ["alias", "set", "mou", "nlnet-2027", "og"])

    assert result.exit_code == 0, result.output
    assert "Set alias 'og' for mou 'nlnet-2027'." in result.output
    assert "Replaced alias 'og' (was for mou 'nlnet-2026')" in result.output
    assert Alias.objects.get(item_type="mou", alias="og").target == "nlnet-2027"


def test_alias_set_prints_when_it_replaces_the_targets_existing_alias():
    runner.invoke(app, ["mou", "add", "nlnet-2026"])
    runner.invoke(app, ["alias", "set", "mou", "nlnet-2026", "old"])

    result = runner.invoke(app, ["alias", "set", "mou", "nlnet-2026", "new"])

    assert result.exit_code == 0, result.output
    assert "Set alias 'new' for mou 'nlnet-2026'." in result.output
    assert "Replaced alias 'old' (was for mou 'nlnet-2026')" in result.output
    assert Alias.objects.filter(item_type="mou", alias="old").exists() is False


def test_alias_set_prints_nothing_extra_when_nothing_was_replaced():
    runner.invoke(app, ["mou", "add", "nlnet-2026"])

    result = runner.invoke(app, ["alias", "set", "mou", "nlnet-2026", "og"])

    assert result.exit_code == 0, result.output
    assert "Replaced" not in result.output


def test_alias_list_shows_everything_by_default():
    runner.invoke(app, ["mou", "add", "nlnet-2026"])
    runner.invoke(app, ["alias", "set", "mou", "nlnet-2026", "og"])
    runner.invoke(
        app,
        ["alias", "set", "url", "https://github.com/collective/icalendar", "ical"],
    )

    result = runner.invoke(app, ["alias", "list"])

    assert result.exit_code == 0, result.output
    assert "mou  nlnet-2026 -> og" in result.output
    assert "url  https://github.com/collective/icalendar -> ical" in result.output


def test_alias_list_filters_by_item_type():
    runner.invoke(app, ["mou", "add", "nlnet-2026"])
    runner.invoke(app, ["alias", "set", "mou", "nlnet-2026", "og"])
    runner.invoke(
        app,
        ["alias", "set", "url", "https://github.com/collective/icalendar", "ical"],
    )

    result = runner.invoke(app, ["alias", "list", "mou"])

    assert result.exit_code == 0, result.output
    assert "og" in result.output
    assert "ical" not in result.output


def test_alias_list_reports_when_there_are_none():
    result = runner.invoke(app, ["alias", "list"])

    assert result.exit_code == 0, result.output
    assert "No aliases yet" in result.output


def test_mou_select_resolves_an_alias():
    runner.invoke(app, ["mou", "add", "nlnet-2025"])
    runner.invoke(app, ["mou", "add", "nlnet-2026"])
    runner.invoke(app, ["alias", "set", "mou", "nlnet-2025", "og"])

    result = runner.invoke(app, ["mou", "select", "og"])

    assert result.exit_code == 0, result.output
    assert MoU.get_selected().name == "nlnet-2025"


def test_mou_remove_resolves_an_alias():
    runner.invoke(app, ["mou", "add", "nlnet-2026"])
    runner.invoke(app, ["alias", "set", "mou", "nlnet-2026", "og"])

    result = runner.invoke(app, ["mou", "remove", "og"])

    assert result.exit_code == 0, result.output
    assert MoU.objects.count() == 0


def test_task_select_resolves_an_alias():
    runner.invoke(app, ["mou", "add", "nlnet-2026"])
    runner.invoke(app, ["task", "select", "10a"])
    runner.invoke(app, ["alias", "set", "task", "10a", "lib"])
    runner.invoke(app, ["task", "select", "11b"])

    result = runner.invoke(app, ["task", "select", "lib"])

    assert result.exit_code == 0, result.output
    assert Task.get_selected().name == "10a"


def test_task_set_resolves_an_alias():
    runner.invoke(app, ["mou", "add", "nlnet-2026"])
    runner.invoke(app, ["task", "select", "10a"])
    runner.invoke(app, ["alias", "set", "task", "10a", "lib"])

    result = runner.invoke(app, ["task", "set", "lib", "--budget", "500"])

    assert result.exit_code == 0, result.output
    assert Task.objects.get(name="10a").max_budget == 500.0


def test_task_remove_resolves_an_alias():
    runner.invoke(app, ["mou", "add", "nlnet-2026"])
    runner.invoke(app, ["task", "select", "10a"])
    runner.invoke(app, ["alias", "set", "task", "10a", "lib"])

    result = runner.invoke(app, ["task", "remove", "lib"])

    assert result.exit_code == 0, result.output
    assert Task.objects.filter(name="10a").exists() is False


def test_start_resolves_a_url_alias(settings):
    settings.RFP_EUROS_PER_HOUR = 20.0
    runner.invoke(app, ["mou", "add", "nlnet-2026"])
    runner.invoke(app, ["task", "select", "10a"])
    runner.invoke(
        app,
        ["alias", "set", "url", "https://github.com/collective/icalendar", "ical"],
    )

    with patch(
        "nlnet_rfp_recorder.timetracking.models.alias.classify_issue_or_pr",
        return_value="pull",
    ):
        result = runner.invoke(app, ["start", "ical/1782"])

    assert result.exit_code == 0, result.output
    link = Link.objects.get()
    assert link.url == "https://github.com/collective/icalendar/pull/1782"


def test_start_fails_cleanly_for_an_unregistered_url_alias(settings):
    settings.RFP_EUROS_PER_HOUR = 20.0
    runner.invoke(app, ["mou", "add", "nlnet-2026"])
    runner.invoke(app, ["task", "select", "10a"])

    result = runner.invoke(app, ["start", "ical/1782"])

    assert result.exit_code != 0
    assert "No alias 'ical' for url" in result.output


def test_mou_status_shows_the_alias_in_parens():
    runner.invoke(app, ["mou", "add", "nlnet-2026"])
    runner.invoke(app, ["alias", "set", "mou", "nlnet-2026", "og"])

    result = runner.invoke(app, ["mou", "status"])

    assert result.exit_code == 0, result.output
    assert "MoU: nlnet-2026 (og)" in result.output


def test_mou_list_shows_the_alias_in_parens():
    runner.invoke(app, ["mou", "add", "nlnet-2026"])
    runner.invoke(app, ["alias", "set", "mou", "nlnet-2026", "og"])

    result = runner.invoke(app, ["mou", "list"])

    assert result.exit_code == 0, result.output
    assert "* nlnet-2026 (og)" in result.output


def test_task_status_shows_the_alias_in_parens():
    runner.invoke(app, ["mou", "add", "nlnet-2026"])
    runner.invoke(app, ["task", "select", "10a"])
    runner.invoke(app, ["alias", "set", "task", "10a", "lib"])

    result = runner.invoke(app, ["task", "status"])

    assert result.exit_code == 0, result.output
    assert "Selected task: 10a (lib)" in result.output


def test_task_list_shows_the_alias_in_parens():
    runner.invoke(app, ["mou", "add", "nlnet-2026"])
    runner.invoke(app, ["task", "select", "10a"])
    runner.invoke(app, ["alias", "set", "task", "10a", "lib"])

    result = runner.invoke(app, ["task", "list"])

    assert result.exit_code == 0, result.output
    assert "* 10a (lib)" in result.output


def test_start_shows_the_task_alias_in_confirmation(settings):
    settings.RFP_EUROS_PER_HOUR = 20.0
    runner.invoke(app, ["mou", "add", "nlnet-2026"])
    runner.invoke(app, ["task", "select", "10a"])
    runner.invoke(app, ["alias", "set", "task", "10a", "lib"])

    result = runner.invoke(app, ["start", "https://example.com/issues/1"])

    assert result.exit_code == 0, result.output
    assert "Started time entry for task 10a (lib)" in result.output


def test_report_print_preview_shows_no_aliases(settings):
    settings.RFP_EUROS_PER_HOUR = 20.0
    mou = MoU.objects.create(name="nlnet-2026", selected=True)
    task = Task.objects.create(mou=mou, name="10a")
    link = Link.objects.create(task=task, url="https://example.com/issues/1")
    now = timezone.now()
    TimeRecord.objects.create(
        link=link, start_time=now - timedelta(minutes=30), end_time=now
    )
    runner.invoke(app, ["alias", "set", "mou", "nlnet-2026", "og"])
    runner.invoke(app, ["alias", "set", "task", "10a", "lib"])

    result = runner.invoke(app, ["report", "print"])

    assert result.exit_code == 0, result.output
    assert "MoU: nlnet-2026" in result.output
    assert "10a: 10€" in result.output
    assert "og" not in result.output
    assert "lib" not in result.output
