from datetime import timedelta
from unittest.mock import AsyncMock, MagicMock, patch

import niquests
import pytest
import typer
from django.utils import timezone
from typer.testing import CliRunner

from nlnet_rfp_recorder.cli import app
from nlnet_rfp_recorder.github import TIMEOUT_SECONDS, Status
from nlnet_rfp_recorder.timetracking.models import (
    GitHubToken,
    Link,
    MoU,
    Report,
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
    settings.RFP_EUROS = None
    runner.invoke(app, ["mou", "add", "nlnet-2026"])
    budget_file = tmp_path / "budget.txt"
    budget_file.write_text("10a. Do the thing\t€ 500\n")
    runner.invoke(app, ["mou", "budget", str(budget_file)])

    result = runner.invoke(app, ["task", "select", "10a"])

    assert result.exit_code == 0, result.output
    assert "Budget 0€/500€" in result.output
    assert "left" not in result.output


def test_task_output_shows_budget_and_time_left(tmp_path, settings):
    settings.RFP_EUROS = 20.0
    runner.invoke(app, ["mou", "add", "nlnet-2026"])
    budget_file = tmp_path / "budget.txt"
    budget_file.write_text("10a. Do the thing\t€ 500\n")
    runner.invoke(app, ["mou", "budget", str(budget_file)])
    task = Task.objects.get(name="10a")
    link = Link.objects.create(task=task, url="https://example.com/issues/1")
    now = timezone.now()
    TimeRecord.objects.create(
        link=link, start_time=now - timedelta(hours=1), end_time=now
    )

    result = runner.invoke(app, ["task", "select", "10a"])

    assert result.exit_code == 0, result.output
    assert "Budget 20€/500€ - 24:00 left" in result.output


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
    assert "Budget 100€/500€" in result.output


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
    assert "  10a  Budget 100€/500€" in result.output
    assert "* 11b" in result.output


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
    settings.RFP_EUROS = None

    result = runner.invoke(app, ["report", "create"])

    assert result.exit_code != 0


def test_report_fails_without_a_selected_mou(settings):
    settings.RFP_EUROS = 20.0

    result = runner.invoke(app, ["report", "create"])

    assert result.exit_code != 0
    assert "No MoU selected" in result.output


def test_report_only_includes_tasks_from_the_selected_mou(settings):
    settings.RFP_EUROS = 20.0
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
    settings.RFP_EUROS = 20.0
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
    assert "10a: 20€" in result.output
    assert "Issues:" in result.output
    assert "- https://github.com/nlnet/rfp-recorder/issues/1" in result.output
    assert "Pull Requests:" in result.output
    assert "- https://github.com/nlnet/rfp-recorder/pull/2" in result.output
    assert "Links:" in result.output
    assert "- https://example.com/docs/design" in result.output
    assert "Total: 20€" in result.output


def test_report_includes_open_issues_but_excludes_open_prs(settings):
    settings.RFP_EUROS = 20.0
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
    settings.RFP_EUROS = 20.0
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
    settings.RFP_EUROS = 20.0
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
    settings.RFP_EUROS = 20.0
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
    settings.RFP_EUROS = 20.0
    mou = MoU.objects.create(name="nlnet-2026", selected=True)
    task = Task.objects.create(mou=mou, name="10a")
    link = Link.objects.create(task=task, url="https://example.com/issues/1")
    TimeRecord.objects.create(link=link, start_time=timezone.now())

    result = runner.invoke(app, ["report", "create"])

    assert result.exit_code != 0
    assert "https://example.com/issues/1" in result.output
    assert "still running" in result.output.lower()


def test_report_shows_review_tag_suffix(settings):
    settings.RFP_EUROS = 20.0
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
    assert "- https://example.com/docs/design (review)" in result.output


def test_report_create_persists_a_report(settings):
    settings.RFP_EUROS = 20.0
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
    settings.RFP_EUROS = 20.0
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
    settings.RFP_EUROS = 20.0
    MoU.objects.create(name="nlnet-2026", selected=True)

    result = runner.invoke(app, ["report", "create"])

    assert result.exit_code != 0
    assert "Nothing to report" in result.output
    assert Report.objects.count() == 0


def test_report_remove_deletes_it(settings):
    settings.RFP_EUROS = 20.0
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
    settings.RFP_EUROS = 20.0
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
    settings.RFP_EUROS = 20.0
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
    settings.RFP_EUROS = 20.0
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
    settings.RFP_EUROS = 20.0
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
    settings.RFP_EUROS = 20.0
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
    settings.RFP_EUROS = 20.0
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
    assert record.report_id == "nlnet-2026-1"

    runner.invoke(app, ["report", "remove", "nlnet-2026-1"])

    record.refresh_from_db()
    assert record.report_id is None


def test_report_export_lists_time_record_pks(tmp_path, settings):
    settings.RFP_EUROS = 20.0
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
    path = tmp_path / "report.txt"

    result = runner.invoke(app, ["report", "export", "nlnet-2026-1", str(path)])

    assert result.exit_code == 0, result.output
    assert path.read_text() == f"{record.pk}\n"


def test_report_export_fails_for_an_unknown_report():
    result = runner.invoke(app, ["report", "export", "does-not-exist"])

    assert result.exit_code != 0


def test_report_import_removes_a_pk_not_in_the_file(tmp_path, settings):
    settings.RFP_EUROS = 20.0
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
    path = tmp_path / "report.txt"
    path.write_text("")

    result = runner.invoke(app, ["report", "import", "nlnet-2026-1", str(path)])

    assert result.exit_code == 0, result.output
    record.refresh_from_db()
    assert record.report_id is None


def test_report_import_adds_a_pk_from_the_file(tmp_path, settings):
    settings.RFP_EUROS = 20.0
    mou = MoU.objects.create(name="nlnet-2026", selected=True)
    task = Task.objects.create(mou=mou, name="10a")
    link = Link.objects.create(task=task, url="https://example.com/issues/1")
    record = TimeRecord.objects.create(
        link=link,
        start_time=timezone.now() - timedelta(minutes=20),
        end_time=timezone.now(),
    )
    report = Report.create(mou)
    path = tmp_path / "report.txt"
    path.write_text(f"{record.pk}\n")

    result = runner.invoke(app, ["report", "import", report.id, str(path)])

    assert result.exit_code == 0, result.output
    record.refresh_from_db()
    assert record.report_id == report.id


def test_report_import_fails_for_a_pk_of_a_different_mou(tmp_path, settings):
    settings.RFP_EUROS = 20.0
    mou_a = MoU.objects.create(name="mou-a")
    mou_b = MoU.objects.create(name="mou-b", selected=True)
    task_b = Task.objects.create(mou=mou_b, name="10a")
    link = Link.objects.create(task=task_b, url="https://example.com/issues/1")
    record = TimeRecord.objects.create(
        link=link,
        start_time=timezone.now() - timedelta(minutes=20),
        end_time=timezone.now(),
    )
    report_a = Report.create(mou_a)
    path = tmp_path / "report.txt"
    path.write_text(f"{record.pk}\n")

    result = runner.invoke(app, ["report", "import", report_a.id, str(path)])

    assert result.exit_code != 0
    assert "does not belong to MoU" in result.output


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
    settings.RFP_EUROS = 20.0
    runner.invoke(app, ["mou", "add", "nlnet-2026"])
    budget_file = tmp_path / "budget.txt"
    budget_file.write_text("(DONE) 10a. Already done\t€ 300\n10b. Still open\t€ 200\n")
    runner.invoke(app, ["mou", "budget", str(budget_file)])

    result = runner.invoke(app, ["mou", "status"])

    assert result.exit_code == 0, result.output
    assert "MoU: nlnet-2026" in result.output
    # 10a is fully used (300), 10b has no tracked time yet (0): 300/500
    assert "Budget 300€/500€" in result.output


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


def test_mou_budget_prints_the_current_mou(tmp_path):
    runner.invoke(app, ["mou", "add", "nlnet-2026"])
    budget_file = tmp_path / "budget.txt"
    budget_file.write_text("10a. Do the thing\t€ 500\n")

    result = runner.invoke(app, ["mou", "budget", str(budget_file)])

    assert result.exit_code == 0, result.output
    assert "Current MoU: nlnet-2026" in result.output


def test_mou_budget_fails_without_a_selected_mou(tmp_path):
    budget_file = tmp_path / "budget.txt"
    budget_file.write_text("10a. Do the thing\t€ 500\n")

    result = runner.invoke(app, ["mou", "budget", str(budget_file)])

    assert result.exit_code != 0


def test_mou_budget_caps_matching_tasks(tmp_path):
    runner.invoke(app, ["mou", "add", "nlnet-2026"])
    budget_file = tmp_path / "budget.txt"
    budget_file.write_text("10a. Do the thing\t€ 500\n")

    result = runner.invoke(app, ["mou", "budget", str(budget_file)])

    assert result.exit_code == 0, result.output
    mou = MoU.objects.get()
    assert "10a. Do the thing" in mou.budget
    task = Task.objects.get(name="10a")
    assert task.max_budget == 500.0
    assert task.mou == mou


def test_mou_budget_with_mou_option_creates_and_selects_it(tmp_path):
    budget_file = tmp_path / "budget.txt"
    budget_file.write_text("10a. Do the thing\t€ 500\n")

    result = runner.invoke(
        app, ["mou", "budget", str(budget_file), "--mou", "nlnet-2026"]
    )

    assert result.exit_code == 0, result.output
    mou = MoU.objects.get()
    assert mou.name == "nlnet-2026"
    assert mou.selected is True
    task = Task.objects.get(name="10a")
    assert task.max_budget == 500.0
    assert task.mou == mou


def test_mou_budget_with_mou_option_selects_an_existing_mou(tmp_path):
    runner.invoke(app, ["mou", "add", "nlnet-2025"])
    runner.invoke(app, ["mou", "add", "nlnet-2026"])
    budget_file = tmp_path / "budget.txt"
    budget_file.write_text("10a. Do the thing\t€ 500\n")

    result = runner.invoke(
        app, ["mou", "budget", str(budget_file), "--mou", "nlnet-2025"]
    )

    assert result.exit_code == 0, result.output
    assert MoU.objects.count() == 2
    assert MoU.get_selected().name == "nlnet-2025"


def test_mou_budget_from_stdin_notifies_when_parsing_starts():
    runner.invoke(app, ["mou", "add", "nlnet-2026"])

    result = runner.invoke(
        app, ["mou", "budget"], input="10a. Do the thing\t€ 500\n\n\n\n"
    )

    assert result.exit_code == 0, result.output
    assert "stop typing" in result.stderr.lower()
    task = Task.objects.get(name="10a")
    assert task.max_budget == 500.0


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
        "backup",
        "edit",
        "migrate",
        "mou",
        "report",
        "restore",
        "review",
        "start",
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
    ]


def test_mou_help_lists_subcommands_alphabetically():
    result = runner.invoke(app, ["mou", "--help"])

    assert result.exit_code == 0, result.output
    assert _subcommand_names("mou") == [
        "add",
        "budget",
        "list",
        "remove",
        "select",
        "status",
    ]


def test_task_help_lists_subcommands_alphabetically():
    result = runner.invoke(app, ["task", "--help"])

    assert result.exit_code == 0, result.output
    assert _subcommand_names("task") == ["list", "remove", "select", "set", "status"]
