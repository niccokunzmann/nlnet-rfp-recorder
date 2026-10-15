import csv
import io
import os
import re
from datetime import date, datetime, timedelta
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import niquests
import pytest
import typer
from django.utils import timezone
from typer.testing import CliRunner

from nlnet_rfp_recorder.cli import app
from nlnet_rfp_recorder.github import TIMEOUT_SECONDS, GitHubState, Status
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
    response.json = MagicMock(return_value={"state": "open", "title": title})
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


@pytest.fixture(autouse=True)
def background_log(tmp_path):
    """Keep `rfp background-task` from logging into the real data dir."""
    path = tmp_path / "rfp-background-task.log"
    with patch("nlnet_rfp_recorder.cli._background_log_path", return_value=path):
        yield path


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

    runner.invoke(app, ["task", "set", "max", "10a", "500"])
    result = runner.invoke(app, ["task", "set", "used", "10a", "100"])

    assert result.exit_code == 0, result.output
    task = Task.objects.get(name="10a")
    assert task.max_budget == 500.0
    assert task.used_budget == 100.0
    assert "100€/500€" in result.output


def test_task_set_fails_for_an_unknown_task():
    runner.invoke(app, ["mou", "add", "nlnet-2026"])

    result = runner.invoke(app, ["task", "set", "max", "10a", "500"])

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
    runner.invoke(app, ["task", "set", "max", "10a", "500"])
    runner.invoke(app, ["task", "set", "used", "10a", "100"])
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
    runner.invoke(app, ["task", "set", "max", "10a", "500"])

    result = runner.invoke(app, ["task", "set", "budget", "10a", "200"])

    assert result.exit_code == 0, result.output
    task = Task.objects.get(name="10a")
    assert task.personal_budget == 200.0
    assert "0€/200€" in result.output
    assert "4:00 left" in result.output


def test_task_status_budget_line_falls_back_to_max_budget():
    runner.invoke(app, ["mou", "add", "nlnet-2026"])
    runner.invoke(app, ["task", "select", "10a"])
    runner.invoke(app, ["task", "set", "max", "10a", "500"])

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


def test_parse_budget_value_reads_a_plain_amount():
    from nlnet_rfp_recorder.cli import _parse_budget_value

    assert _parse_budget_value("150") == (150.0, False)


def test_parse_budget_value_reads_a_percentage():
    from nlnet_rfp_recorder.cli import _parse_budget_value

    assert _parse_budget_value("50%") == (0.5, True)


def test_parse_budget_value_strips_surrounding_whitespace():
    from nlnet_rfp_recorder.cli import _parse_budget_value

    assert _parse_budget_value(" 50 % ") == (0.5, True)


def test_parse_budget_value_rejects_invalid_text():
    from nlnet_rfp_recorder.cli import _parse_budget_value

    with pytest.raises(ValueError, match="Invalid amount"):
        _parse_budget_value("abc")


def test_parse_budget_value_rejects_invalid_percentage_text():
    from nlnet_rfp_recorder.cli import _parse_budget_value

    with pytest.raises(ValueError, match="Invalid percentage"):
        _parse_budget_value("abc%")


def test_task_set_max_sets_an_absolute_amount():
    runner.invoke(app, ["mou", "add", "nlnet-2026"])
    runner.invoke(app, ["task", "select", "10a"])

    result = runner.invoke(app, ["task", "set", "max", "10a", "500"])

    assert result.exit_code == 0, result.output
    assert Task.objects.get(name="10a").max_budget == 500.0


def test_task_set_max_rejects_a_negative_amount():
    runner.invoke(app, ["mou", "add", "nlnet-2026"])
    runner.invoke(app, ["task", "select", "10a"])

    result = runner.invoke(app, ["task", "set", "max", "10a", "--", "-1"])

    assert result.exit_code != 0
    assert "cannot be negative" in result.output


def test_task_set_max_scales_by_a_percentage():
    runner.invoke(app, ["mou", "add", "nlnet-2026"])
    runner.invoke(app, ["task", "select", "10a"])
    runner.invoke(app, ["task", "set", "max", "10a", "200"])

    result = runner.invoke(app, ["task", "set", "max", "10a", "150%"])

    assert result.exit_code == 0, result.output
    assert Task.objects.get(name="10a").max_budget == 300.0


def test_task_set_budget_requires_a_maximum_first():
    runner.invoke(app, ["mou", "add", "nlnet-2026"])
    runner.invoke(app, ["task", "select", "10a"])

    result = runner.invoke(app, ["task", "set", "budget", "10a", "100"])

    assert result.exit_code != 0
    assert "no maximum budget" in result.output


def test_task_set_budget_by_percentage():
    runner.invoke(app, ["mou", "add", "nlnet-2026"])
    runner.invoke(app, ["task", "select", "10a"])
    runner.invoke(app, ["task", "set", "max", "10a", "200"])

    result = runner.invoke(app, ["task", "set", "budget", "10a", "50%"])

    assert result.exit_code == 0, result.output
    assert Task.objects.get(name="10a").personal_budget == 100.0


def test_task_set_budget_by_percentage_can_be_run_again():
    # A later % must land relative to the (unchanged) maximum, not to
    # whatever budget the previous call set - so re-running with a
    # different percentage doesn't compound on top of it.
    runner.invoke(app, ["mou", "add", "nlnet-2026"])
    runner.invoke(app, ["task", "select", "10a"])
    runner.invoke(app, ["task", "set", "max", "10a", "200"])
    runner.invoke(app, ["task", "set", "budget", "10a", "50%"])

    result = runner.invoke(app, ["task", "set", "budget", "10a", "30%"])

    assert result.exit_code == 0, result.output
    assert Task.objects.get(name="10a").personal_budget == 60.0


def test_task_set_budget_percentage_over_100_fails():
    runner.invoke(app, ["mou", "add", "nlnet-2026"])
    runner.invoke(app, ["task", "select", "10a"])
    runner.invoke(app, ["task", "set", "max", "10a", "200"])

    result = runner.invoke(app, ["task", "set", "budget", "10a", "150%"])

    assert result.exit_code != 0
    assert "between 0% and 100%" in result.output


def test_task_set_used_by_percentage_above_100_is_allowed():
    runner.invoke(app, ["mou", "add", "nlnet-2026"])
    runner.invoke(app, ["task", "select", "10a"])
    runner.invoke(app, ["task", "set", "max", "10a", "200"])

    result = runner.invoke(app, ["task", "set", "used", "10a", "150%"])

    assert result.exit_code == 0, result.output
    assert Task.objects.get(name="10a").used_budget == 300.0


def test_task_set_accepts_a_range_across_several_tasks():
    runner.invoke(app, ["mou", "add", "nlnet-2026"])
    for name in ["1a", "1b", "1c"]:
        runner.invoke(app, ["task", "select", name])

    result = runner.invoke(app, ["task", "set", "max", "1a-1c", "500"])

    assert result.exit_code == 0, result.output
    assert "Updated 3 tasks:" in result.output
    for name in ["1a", "1b", "1c"]:
        assert Task.objects.get(name=name).max_budget == 500.0


def test_task_set_accepts_a_comma_separated_list_of_ranges_and_names():
    runner.invoke(app, ["mou", "add", "nlnet-2026"])
    for name in ["1a", "1b", "1c", "2f", "2h"]:
        runner.invoke(app, ["task", "select", name])

    result = runner.invoke(app, ["task", "set", "max", "1a-1c,2f,2h", "0"])

    assert result.exit_code == 0, result.output
    for name in ["1a", "1b", "1c", "2f", "2h"]:
        assert Task.objects.get(name=name).max_budget == 0.0


def test_task_set_bulk_percentage_is_relative_to_each_tasks_own_maximum():
    # One "50%" applied to a range/list must land on a different euro
    # figure per task - each relative to its own max_budget, not to a
    # single shared amount computed once for the whole batch.
    runner.invoke(app, ["mou", "add", "nlnet-2026"])
    for name in ["1a", "1b", "1c"]:
        runner.invoke(app, ["task", "select", name])
    runner.invoke(app, ["task", "set", "max", "1a", "200"])
    runner.invoke(app, ["task", "set", "max", "1b", "400"])
    runner.invoke(app, ["task", "set", "max", "1c", "100"])

    result = runner.invoke(app, ["task", "set", "budget", "1a-1c", "50%"])

    assert result.exit_code == 0, result.output
    assert Task.objects.get(name="1a").personal_budget == 100.0
    assert Task.objects.get(name="1b").personal_budget == 200.0
    assert Task.objects.get(name="1c").personal_budget == 50.0
    budgets = {
        Task.objects.get(name=name).personal_budget for name in ["1a", "1b", "1c"]
    }
    assert len(budgets) == 3


def test_task_set_accepts_a_bare_number_matching_only_that_group():
    # "4" must match exactly the "4" group (4a, 4b) - not 40a or 14a,
    # even though both also contain "4" as a string.
    runner.invoke(app, ["mou", "add", "nlnet-2026"])
    for name in ["4a", "4b", "40a", "14a"]:
        runner.invoke(app, ["task", "select", name])

    result = runner.invoke(app, ["task", "set", "max", "4", "0"])

    assert result.exit_code == 0, result.output
    for name in ["4a", "4b"]:
        assert Task.objects.get(name=name).max_budget == 0.0
    assert Task.objects.get(name="40a").max_budget is None
    assert Task.objects.get(name="14a").max_budget is None


def test_task_set_accepts_a_range_of_numbered_groups():
    runner.invoke(app, ["mou", "add", "nlnet-2026"])
    for name in ["9a", "10a", "10b", "11a", "14a", "15a"]:
        runner.invoke(app, ["task", "select", name])

    result = runner.invoke(app, ["task", "set", "max", "10-14", "0"])

    assert result.exit_code == 0, result.output
    for name in ["10a", "10b", "11a", "14a"]:
        assert Task.objects.get(name=name).max_budget == 0.0
    assert Task.objects.get(name="9a").max_budget is None
    assert Task.objects.get(name="15a").max_budget is None


def test_task_set_fails_for_an_unknown_task_or_prefix():
    runner.invoke(app, ["mou", "add", "nlnet-2026"])

    result = runner.invoke(app, ["task", "set", "max", "9z", "500"])

    assert result.exit_code != 0
    assert "No such task: 9z" in result.output


def test_task_set_a_range_with_end_before_start_fails():
    runner.invoke(app, ["mou", "add", "nlnet-2026"])
    runner.invoke(app, ["task", "select", "1a"])
    runner.invoke(app, ["task", "select", "1c"])

    result = runner.invoke(app, ["task", "set", "max", "1c-1a", "500"])

    assert result.exit_code != 0
    assert "end before start" in result.output


def test_task_set_bulk_update_is_all_or_nothing():
    # 1a already has a maximum, 1b doesn't - the whole batch must fail
    # (and leave 1a untouched) rather than partially applying.
    runner.invoke(app, ["mou", "add", "nlnet-2026"])
    runner.invoke(app, ["task", "select", "1a"])
    runner.invoke(app, ["task", "set", "max", "1a", "500"])
    runner.invoke(app, ["task", "select", "1b"])

    result = runner.invoke(app, ["task", "set", "budget", "1a-1b", "100"])

    assert result.exit_code != 0
    assert Task.objects.get(name="1a").personal_budget is None


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


def test_start_without_a_task_creates_a_taskless_entry():
    # Also covers "no MoU selected": without one, `rfp task` (and thus any
    # task at all) can never succeed - starting still isn't held up by
    # that, so the clock runs and the task can be assigned later.
    result = runner.invoke(app, ["start", "https://example.com/issues/1"])

    assert result.exit_code == 0, result.output
    assert "No MoU selected" in result.output
    record = TimeRecord.objects.get()
    assert record.is_running is True
    assert record.link.task is None


def test_start_asks_which_task_when_none_is_selected():
    runner.invoke(app, ["mou", "add", "nlnet-2026"])
    runner.invoke(app, ["task", "select", "10a"])
    runner.invoke(app, ["task", "select", "10b"])
    runner.invoke(app, ["task", "select"])  # deselect - nothing selected now

    result = runner.invoke(
        app, ["start", "https://example.com/issues/1"], input="10a\n"
    )

    assert result.exit_code == 0, result.output
    assert "Which task should this be assigned to?" in result.output
    record = TimeRecord.objects.get()
    assert record.link.task.name == "10a"
    assert Task.objects.get(name="10a").selected is True


def test_start_prompt_defaults_to_the_last_worked_task():
    runner.invoke(app, ["mou", "add", "nlnet-2026"])
    runner.invoke(app, ["task", "select", "10a"])
    runner.invoke(app, ["start", "10a", "https://example.com/issues/1"])
    runner.invoke(app, ["stop"])
    runner.invoke(app, ["task", "select"])  # deselect

    result = runner.invoke(app, ["start", "https://example.com/issues/2"], input="\n")

    assert result.exit_code == 0, result.output
    assert "[10a]" in result.output
    record = TimeRecord.objects.get(link__url="https://example.com/issues/2")
    assert record.link.task.name == "10a"


def test_start_prompt_default_uses_the_bare_name_not_the_alias():
    # An aliased task's display_name is "10a (ical-lang)" - not itself a
    # valid answer, so the default *submitted* on blank Enter must be
    # the plain name - but it's still shown with its alias in the
    # prompt text itself (test_start_prompt_shows_the_alias_in_brackets
    # covers the display side).
    runner.invoke(app, ["mou", "add", "nlnet-2026"])
    runner.invoke(app, ["task", "select", "10a"])
    runner.invoke(app, ["alias", "set", "task", "10a", "ical-lang"])
    runner.invoke(app, ["start", "10a", "https://example.com/issues/1"])
    runner.invoke(app, ["stop"])
    runner.invoke(app, ["task", "select"])

    result = runner.invoke(app, ["start", "https://example.com/issues/2"], input="\n")

    assert result.exit_code == 0, result.output
    record = TimeRecord.objects.get(link__url="https://example.com/issues/2")
    assert record.link.task.name == "10a"


def test_start_prompt_shows_the_alias_in_brackets():
    runner.invoke(app, ["mou", "add", "nlnet-2026"])
    runner.invoke(app, ["task", "select", "10a"])
    runner.invoke(app, ["alias", "set", "task", "10a", "ical-lang"])
    runner.invoke(app, ["start", "10a", "https://example.com/issues/1"])
    runner.invoke(app, ["stop"])
    runner.invoke(app, ["task", "select"])

    result = runner.invoke(app, ["start", "https://example.com/issues/2"], input="\n")

    assert result.exit_code == 0, result.output
    assert "[10a (ical-lang)]" in result.output


def test_start_prompt_puts_the_input_on_its_own_line():
    runner.invoke(app, ["mou", "add", "nlnet-2026"])
    task = Task.objects.create(mou=MoU.objects.get(), name="10a")
    Link.objects.create(url="https://example.com/issues/1")

    result = runner.invoke(app, ["start", "https://example.com/issues/1"], input="\n")

    assert result.exit_code == 0, result.output
    assert "tasks) [10a]\n> " in result.output
    link = Link.objects.get()
    assert link.task == task


def test_start_rejects_an_unknown_task_and_asks_again():
    runner.invoke(app, ["mou", "add", "nlnet-2026"])
    runner.invoke(app, ["task", "select", "10a"])
    runner.invoke(app, ["task", "select"])

    result = runner.invoke(
        app, ["start", "https://example.com/issues/1"], input="99z\n10a\n"
    )

    assert result.exit_code == 0, result.output
    assert "No such task: '99z'" in result.output
    record = TimeRecord.objects.get()
    assert record.link.task.name == "10a"


def test_start_question_mark_lists_tasks_then_asks_again():
    runner.invoke(app, ["mou", "add", "nlnet-2026"])
    Task.objects.create(mou=MoU.objects.get(), name="10a", description="Do the thing")
    runner.invoke(app, ["task", "select"])  # deselect the just-created task

    result = runner.invoke(
        app, ["start", "https://example.com/issues/1"], input="?\n10a\n"
    )

    assert result.exit_code == 0, result.output
    assert "Do the thing" in result.output
    record = TimeRecord.objects.get()
    assert record.link.task.name == "10a"


@pytest.mark.parametrize("command1", ["start", "review", "implement"])
@pytest.mark.parametrize("command2", ["start", "review", "implement"])
def test_two_new_links_in_a_row_both_ask_for_a_task(command1, command2):
    runner.invoke(app, ["mou", "add", "nlnet-2026"])
    runner.invoke(app, ["task", "select", "10a"])
    runner.invoke(app, ["task", "select", "10b"])
    runner.invoke(app, ["task", "select"])  # deselect - nothing selected now

    result1 = runner.invoke(
        app, [command1, "https://example.com/issues/101"], input="10a\n"
    )
    result2 = runner.invoke(
        app, [command2, "https://example.com/issues/102"], input="10b\n"
    )

    assert result1.exit_code == 0, result1.output
    assert "Which task should this be assigned to?" in result1.output
    assert result2.exit_code == 0, result2.output
    assert "Which task should this be assigned to?" in result2.output


def test_start_creates_a_time_entry_for_the_selected_task():
    # A blank Enter accepts the selected task, offered as the default -
    # it's still asked about, since a taskless link always is (see
    # test_two_new_links_in_a_row_both_ask_for_a_task).
    runner.invoke(app, ["mou", "add", "nlnet-2026"])
    runner.invoke(app, ["task", "select", "10a"])
    result = runner.invoke(app, ["start", "https://example.com/issues/1"], input="\n")

    assert result.exit_code == 0, result.output
    record = TimeRecord.objects.get()
    assert record.link.task.name == "10a"
    assert record.is_running is True
    assert [link.url for link in Link.objects.all()] == ["https://example.com/issues/1"]


def test_start_prints_the_previous_entry_being_stopped():
    runner.invoke(app, ["mou", "add", "nlnet-2026"])
    runner.invoke(app, ["task", "select", "10a"])
    runner.invoke(app, ["start", "10a", "https://example.com/issues/1"])
    result = runner.invoke(app, ["start", "https://example.com/issues/2"], input="\n")

    assert result.exit_code == 0, result.output
    assert "Stopped 10a" in result.output
    assert "https://example.com/issues/1" in result.output
    # The task isn't known yet when "Started..." prints - it's still
    # taskless at that point, resolved only once the prompt is answered.
    assert "Started time entry for task ?: https://example.com/issues/2" in (
        result.output
    )
    record = TimeRecord.objects.get(link__url="https://example.com/issues/2")
    assert record.link.task.name == "10a"


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
    runner.invoke(app, ["start", "10a", "https://example.com/issues/1"])
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


def test_start_with_a_single_bare_task_alias_uses_an_empty_url():
    # No "/" or ":" - can't be a link, so it's a task name/alias started
    # with an empty url instead of the currently selected task's link.
    runner.invoke(app, ["mou", "add", "nlnet-2026"])
    runner.invoke(app, ["task", "select", "10a"])
    runner.invoke(app, ["alias", "set", "task", "10a", "ical-lang"])

    result = runner.invoke(app, ["start", "ical-lang"])

    assert result.exit_code == 0, result.output
    assert "'ical-lang' isn't a link - starting it with an empty url." in result.output
    record = TimeRecord.objects.get()
    assert record.link.task.name == "10a"
    assert record.link.url == ""


def _setup_task_only_start():
    runner.invoke(app, ["mou", "add", "nlnet-2026"])
    Task.objects.create(mou=MoU.objects.get(), name="10a")
    Task.objects.create(mou=MoU.objects.get(), name="10b")


def test_start_task_only_with_no_records_does_not_ask_to_change():
    _setup_task_only_start()

    result = runner.invoke(app, ["start", "10a"])

    assert result.exit_code == 0, result.output
    assert "Change it to" not in result.output
    assert TimeRecord.get_running().link.task.name == "10a"


def test_start_task_only_with_nothing_running_does_not_ask_to_change():
    _setup_task_only_start()
    runner.invoke(app, ["start", "10a"])
    runner.invoke(app, ["stop"])

    result = runner.invoke(app, ["start", "10b"])

    assert result.exit_code == 0, result.output
    assert "Change it to" not in result.output
    assert TimeRecord.get_running().link.task.name == "10b"


def test_start_task_only_while_another_link_runs_does_not_ask_to_change():
    _setup_task_only_start()
    runner.invoke(app, ["start", "10a", "https://example.com/issues/1"])

    result = runner.invoke(app, ["start", "10b"])

    assert result.exit_code == 0, result.output
    assert "Change it to" not in result.output
    assert TimeRecord.get_running().link.task.name == "10b"


def test_start_task_only_with_the_same_task_running_does_not_ask_to_change():
    _setup_task_only_start()
    runner.invoke(app, ["start", "10a"])

    result = runner.invoke(app, ["start", "10a"])

    assert result.exit_code == 0, result.output
    assert "Change it to" not in result.output
    assert TimeRecord.get_running().link.task.name == "10a"


def test_start_task_only_with_a_different_task_running_does_not_ask_to_change():
    _setup_task_only_start()
    runner.invoke(app, ["start", "10a"])

    result = runner.invoke(app, ["start", "10b"])

    assert result.exit_code == 0, result.output
    assert "Change it to" not in result.output
    assert TimeRecord.get_running().link.task.name == "10b"


@pytest.mark.parametrize("command", ["review", "implement"])
@pytest.mark.parametrize("other", ["none", "same", "different", "other-link"])
def test_review_and_implement_task_only_do_not_ask_to_change(command, other):
    _setup_task_only_start()
    if other == "same":
        runner.invoke(app, ["start", "10b"])
    elif other == "different":
        runner.invoke(app, ["start", "10a"])
    elif other == "other-link":
        runner.invoke(app, ["start", "10a", "https://example.com/issues/1"])

    result = runner.invoke(app, [command, "10b"])

    assert result.exit_code == 0, result.output
    assert "Change it to" not in result.output
    assert TimeRecord.get_running().link.task.name == "10b"


def test_task_only_entries_keep_their_own_tags_and_suggest_fixing_the_url():
    _setup_task_only_start()
    runner.invoke(app, ["review", "10a"])

    result = runner.invoke(app, ["implement", "10b"])

    assert result.exit_code == 0, result.output
    first, second = TimeRecord.objects.order_by("start_time")
    assert [t.name for t in first.link.tags.all()] == ["review"]
    assert first.link.task.name == "10a"
    assert [t.name for t in second.link.tags.all()] == ["implementation"]
    assert second.link.task.name == "10b"
    assert "Still without a proper url" in result.output
    assert f"rfp edit {first.pk} --url" in result.output


def test_task_only_start_resumes_the_same_entry_for_same_task_and_tag():
    _setup_task_only_start()
    runner.invoke(app, ["review", "10a"])

    runner.invoke(app, ["review", "10a"])

    assert TimeRecord.objects.count() == 1


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
    # An explicit task, not the selected-task fallback, so this doesn't
    # also trigger the task prompt (see _prompt_for_task) - irrelevant
    # noise for what this test checks.
    runner.invoke(app, ["mou", "add", "nlnet-2026"])
    runner.invoke(app, ["task", "select", "10a"])

    result = runner.invoke(app, ["start", "10a", "https://example.com/issues/1"])

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
    runner.invoke(app, ["start", "10a", "https://example.com/issues/wrong"])

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


def test_continue_selects_the_links_task():
    # Same invariant `_start` keeps: whatever task the continued entry's
    # link belongs to is the task just worked on, so it must end up
    # selected even if a different task was selected in the meantime.
    runner.invoke(app, ["mou", "add", "nlnet-2026"])
    runner.invoke(app, ["task", "select", "10a"])
    runner.invoke(app, ["start", "10a", "https://example.com/issues/1"])
    runner.invoke(app, ["stop"])
    runner.invoke(app, ["task", "select", "10b"])

    result = runner.invoke(app, ["continue"])

    assert result.exit_code == 0, result.output
    assert Task.objects.get(name="10a").selected is True
    assert Task.objects.get(name="10b").selected is False


def test_continue_shows_the_tasks_description_if_present():
    runner.invoke(app, ["mou", "add", "nlnet-2026"])
    runner.invoke(app, ["task", "select", "10a"])
    task = Task.objects.get(name="10a")
    task.description = "Do the thing"
    task.save(update_fields=["description"])
    runner.invoke(app, ["start", "10a", "https://example.com/issues/1"])
    runner.invoke(app, ["stop"])

    result = runner.invoke(app, ["continue"])

    assert result.exit_code == 0, result.output
    assert "Do the thing" in result.output


def test_continue_with_entry_id_continues_that_entry():
    runner.invoke(app, ["mou", "add", "nlnet-2026"])
    runner.invoke(app, ["start", "10a", "https://example.com/issues/1"])
    runner.invoke(app, ["start", "10a", "https://example.com/issues/2"])
    runner.invoke(app, ["stop"])
    first = TimeRecord.objects.get(link__url="https://example.com/issues/1")

    result = runner.invoke(app, ["continue", str(first.pk)])

    assert result.exit_code == 0, result.output
    running = TimeRecord.get_running()
    assert running.link.url == "https://example.com/issues/1"
    assert running.pk != first.pk


def test_continue_with_unknown_entry_id_fails():
    result = runner.invoke(app, ["continue", "9999"])

    assert result.exit_code != 0
    assert "No time entry 9999" in result.output


def test_continue_with_unknown_url_refers_to_start():
    runner.invoke(app, ["mou", "add", "nlnet-2026"])

    result = runner.invoke(app, ["continue", "https://example.com/issues/7"])

    assert result.exit_code != 0
    assert "rfp start https://example.com/issues/7" in result.output
    assert TimeRecord.objects.count() == 0


def test_continue_with_url_switches_mou():
    runner.invoke(app, ["mou", "add", "nlnet-2025"])
    runner.invoke(app, ["start", "10a", "https://example.com/issues/1"])
    runner.invoke(app, ["stop"])
    runner.invoke(app, ["mou", "add", "nlnet-2026"])

    result = runner.invoke(app, ["continue", "https://example.com/issues/1"])

    assert result.exit_code == 0, result.output
    assert MoU.get_selected().name == "nlnet-2025"
    assert TimeRecord.get_running().link.url == "https://example.com/issues/1"


def test_continue_with_task_uses_its_last_entry():
    runner.invoke(app, ["mou", "add", "nlnet-2026"])
    runner.invoke(app, ["start", "10a", "https://example.com/issues/1"])
    runner.invoke(app, ["start", "10b", "https://example.com/issues/2"])
    runner.invoke(app, ["stop"])

    result = runner.invoke(app, ["continue", "10a"])

    assert result.exit_code == 0, result.output
    assert TimeRecord.get_running().link.url == "https://example.com/issues/1"
    assert Task.objects.get(name="10a").selected is True


def test_edit_without_any_time_entries_fails():
    result = runner.invoke(app, ["edit", "--url", "https://example.com/issues/1"])

    assert result.exit_code != 0


def test_edit_replaces_the_last_entrys_link():
    runner.invoke(app, ["mou", "add", "nlnet-2026"])
    runner.invoke(app, ["task", "select", "10a"])
    runner.invoke(app, ["start", "10a", "https://example.com/issues/wrong"])
    runner.invoke(app, ["stop"])

    result = runner.invoke(app, ["edit", "--url", "https://example.com/issues/right"])

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
    runner.invoke(app, ["start", "10a", "https://example.com/issues/1"])
    runner.invoke(app, ["stop"])

    result = runner.invoke(app, ["edit", "--url", "https://example.com/issues/2"])

    assert result.exit_code == 0, result.output
    record = TimeRecord.objects.get()
    assert record.link.url == "https://example.com/issues/2"
    assert record.is_running is False


def _start_and_stop_three_entries() -> list[int]:
    runner.invoke(app, ["mou", "add", "nlnet-2026"])
    runner.invoke(app, ["task", "select", "10a"])
    for n in (1, 2, 3):
        runner.invoke(app, ["start", "10a", f"https://example.com/issues/{n}"])
        runner.invoke(app, ["stop"])
    return list(TimeRecord.objects.order_by("pk").values_list("pk", flat=True))


def test_edit_by_positive_pk_edits_that_entry():
    pks = _start_and_stop_three_entries()

    result = runner.invoke(app, ["edit", str(pks[0]), "--tags", "review"])

    assert result.exit_code == 0, result.output
    record = TimeRecord.objects.get(pk=pks[0])
    assert "review" in {tag.name for tag in record.link.tags.all()}
    other = TimeRecord.objects.get(pk=pks[2])
    assert "review" not in {tag.name for tag in other.link.tags.all()}


def test_edit_by_negative_one_edits_the_most_recent_entry():
    pks = _start_and_stop_three_entries()

    result = runner.invoke(app, ["edit", "-1", "--tags", "review"])

    assert result.exit_code == 0, result.output
    record = TimeRecord.objects.get(pk=pks[2])
    assert "review" in {tag.name for tag in record.link.tags.all()}


def test_edit_by_negative_index_counts_back_from_the_most_recent_entry():
    pks = _start_and_stop_three_entries()

    result = runner.invoke(app, ["edit", "-3", "--tags", "review"])

    assert result.exit_code == 0, result.output
    record = TimeRecord.objects.get(pk=pks[0])
    assert "review" in {tag.name for tag in record.link.tags.all()}


def test_edit_negative_pk_works_before_or_after_options():
    pks = _start_and_stop_three_entries()

    result = runner.invoke(app, ["edit", "--tags", "review", "-1"])

    assert result.exit_code == 0, result.output
    record = TimeRecord.objects.get(pk=pks[2])
    assert "review" in {tag.name for tag in record.link.tags.all()}


def test_edit_rejects_pk_zero():
    _start_and_stop_three_entries()

    result = runner.invoke(app, ["edit", "0", "--tags", "review"])

    assert result.exit_code != 0
    assert "Invalid time entry: 0" in result.output


def test_edit_fails_for_an_unknown_positive_pk():
    _start_and_stop_three_entries()

    result = runner.invoke(app, ["edit", "999", "--tags", "review"])

    assert result.exit_code != 0
    assert "No such time entry: 999" in result.output


def test_edit_fails_for_a_negative_index_out_of_range():
    _start_and_stop_three_entries()

    result = runner.invoke(app, ["edit", "-99", "--tags", "review"])

    assert result.exit_code != 0
    assert "No time entry -99 entries back from the most recent" in result.output


def test_edit_rejects_a_single_dash_typo_for_an_option():
    # ignore_unknown_options (needed so a bare "-1" isn't rejected as an
    # unrecognized option before it ever reaches TIMESHEET_PK) must not
    # let a misspelled option quietly do nothing or get misread as
    # something else - it still fails, just via TIMESHEET_PK's own int
    # conversion rejecting the leftover token.
    runner.invoke(app, ["mou", "add", "nlnet-2026"])
    runner.invoke(app, ["task", "select", "10a"])
    runner.invoke(app, ["start", "https://example.com/issues/1"])

    result = runner.invoke(app, ["edit", "-url", "https://example.com/issues/2"])

    assert result.exit_code != 0
    assert "not a valid int" in result.output
    record = TimeRecord.objects.get()
    assert record.link.url == "https://example.com/issues/1"


def test_edit_rejects_a_misspelled_long_option():
    runner.invoke(app, ["mou", "add", "nlnet-2026"])
    runner.invoke(app, ["task", "select", "10a"])
    runner.invoke(app, ["start", "https://example.com/issues/1"])

    result = runner.invoke(app, ["edit", "--durtaion", "50"])

    assert result.exit_code != 0
    assert "not a valid int" in result.output


def test_edit_rejects_two_positional_pks():
    pks = _start_and_stop_three_entries()

    result = runner.invoke(app, ["edit", "-1", str(pks[0])])

    assert result.exit_code != 0
    assert "unexpected extra argument" in result.output.lower()


def test_edit_reassigns_the_last_entrys_task():
    runner.invoke(app, ["mou", "add", "nlnet-2026"])
    runner.invoke(app, ["task", "select", "10a"])
    runner.invoke(app, ["start", "https://example.com/issues/1"])
    runner.invoke(app, ["stop"])
    runner.invoke(app, ["task", "select", "11b"])

    result = runner.invoke(app, ["edit", "--task", "11b"])

    assert result.exit_code == 0, result.output
    record = TimeRecord.objects.get()
    assert record.link.task.name == "11b"
    assert "11b" in result.output


def test_edit_reassigns_the_task_by_alias():
    runner.invoke(app, ["mou", "add", "nlnet-2026"])
    runner.invoke(app, ["task", "select", "10a"])
    runner.invoke(app, ["start", "https://example.com/issues/1"])
    runner.invoke(app, ["task", "select", "11b"])
    runner.invoke(app, ["alias", "set", "task", "11b", "bee"])

    result = runner.invoke(app, ["edit", "--task", "bee"])

    assert result.exit_code == 0, result.output
    record = TimeRecord.objects.get()
    assert record.link.task.name == "11b"


def test_edit_task_selects_the_new_task():
    runner.invoke(app, ["mou", "add", "nlnet-2026"])
    runner.invoke(app, ["task", "select", "10a"])
    runner.invoke(app, ["start", "https://example.com/issues/1"])
    runner.invoke(app, ["task", "select", "11b"])
    runner.invoke(app, ["task", "select", "10a"])

    result = runner.invoke(app, ["edit", "--task", "11b"])

    assert result.exit_code == 0, result.output
    assert Task.objects.get(name="11b").selected is True
    assert Task.objects.get(name="10a").selected is False


def test_edit_task_fails_for_an_unknown_task():
    runner.invoke(app, ["mou", "add", "nlnet-2026"])
    runner.invoke(app, ["task", "select", "10a"])
    runner.invoke(app, ["start", "10a", "https://example.com/issues/1"])

    result = runner.invoke(app, ["edit", "--task", "does-not-exist"])

    assert result.exit_code != 0
    assert "No such task" in result.output
    record = TimeRecord.objects.get()
    assert record.link.task.name == "10a"


def test_edit_task_fails_without_a_link():
    runner.invoke(app, ["mou", "add", "nlnet-2026"])
    runner.invoke(app, ["task", "select", "10a"])
    TimeRecord.objects.create(start_time=timezone.now())

    result = runner.invoke(app, ["edit", "--task", "10a"])

    assert result.exit_code != 0
    assert "no link" in result.output


def test_edit_changes_link_tags_and_task_together():
    runner.invoke(app, ["mou", "add", "nlnet-2026"])
    runner.invoke(app, ["task", "select", "10a"])
    runner.invoke(app, ["start", "10a", "https://example.com/issues/1"])
    runner.invoke(app, ["task", "select", "11b"])

    result = runner.invoke(
        app,
        [
            "edit",
            "--url",
            "https://example.com/issues/2",
            "--tags",
            "review",
            "--task",
            "11b",
        ],
    )

    assert result.exit_code == 0, result.output
    record = TimeRecord.objects.get()
    assert record.link.url == "https://example.com/issues/2"
    assert record.link.task.name == "11b"
    assert "review" in {tag.name for tag in record.link.tags.all()}


def test_edit_duration_moves_the_start_keeping_the_end_fixed():
    runner.invoke(app, ["mou", "add", "nlnet-2026"])
    runner.invoke(app, ["task", "select", "10a"])
    runner.invoke(app, ["start", "https://example.com/issues/1"])
    runner.invoke(app, ["stop"])
    original_end = TimeRecord.objects.get().end_time

    result = runner.invoke(app, ["edit", "--duration", "50"])

    assert result.exit_code == 0, result.output
    assert "New duration: 0:50" in result.output
    record = TimeRecord.objects.get()
    assert record.end_time == original_end
    assert record.start_time == original_end - timedelta(minutes=50)
    assert record.duration == timedelta(minutes=50)


def test_edit_duration_accepts_hmm():
    runner.invoke(app, ["mou", "add", "nlnet-2026"])
    runner.invoke(app, ["task", "select", "10a"])
    runner.invoke(app, ["start", "https://example.com/issues/1"])
    runner.invoke(app, ["stop"])

    result = runner.invoke(app, ["edit", "--duration", "1:20"])

    assert result.exit_code == 0, result.output
    record = TimeRecord.objects.get()
    assert record.duration == timedelta(hours=1, minutes=20)


def test_edit_duration_plain_minutes_matches_equivalent_hmm():
    runner.invoke(app, ["mou", "add", "nlnet-2026"])
    runner.invoke(app, ["task", "select", "10a"])
    runner.invoke(app, ["start", "https://example.com/issues/1"])
    runner.invoke(app, ["stop"])

    result = runner.invoke(app, ["edit", "--duration", "80"])

    assert result.exit_code == 0, result.output
    record = TimeRecord.objects.get()
    assert record.duration == timedelta(hours=1, minutes=20)


def test_edit_duration_plus_prefix_adds_to_the_current_duration():
    runner.invoke(app, ["mou", "add", "nlnet-2026"])
    runner.invoke(app, ["task", "select", "10a"])
    runner.invoke(app, ["start", "https://example.com/issues/1"])
    runner.invoke(app, ["stop"])
    runner.invoke(app, ["edit", "--duration", "50"])

    result = runner.invoke(app, ["edit", "--duration", "+15"])

    assert result.exit_code == 0, result.output
    assert "New duration: 1:05" in result.output
    record = TimeRecord.objects.get()
    assert record.duration == timedelta(minutes=65)


def test_edit_duration_minus_prefix_subtracts_from_the_current_duration():
    runner.invoke(app, ["mou", "add", "nlnet-2026"])
    runner.invoke(app, ["task", "select", "10a"])
    runner.invoke(app, ["start", "https://example.com/issues/1"])
    runner.invoke(app, ["stop"])
    runner.invoke(app, ["edit", "--duration", "50"])

    result = runner.invoke(app, ["edit", "--duration", "-15"])

    assert result.exit_code == 0, result.output
    assert "New duration: 0:35" in result.output
    record = TimeRecord.objects.get()
    assert record.duration == timedelta(minutes=35)


def test_edit_duration_minus_prefix_accepts_hmm():
    runner.invoke(app, ["mou", "add", "nlnet-2026"])
    runner.invoke(app, ["task", "select", "10a"])
    runner.invoke(app, ["start", "https://example.com/issues/1"])
    runner.invoke(app, ["stop"])
    runner.invoke(app, ["edit", "--duration", "2:00"])

    result = runner.invoke(app, ["edit", "--duration", "-1:20"])

    assert result.exit_code == 0, result.output
    record = TimeRecord.objects.get()
    assert record.duration == timedelta(minutes=40)


def test_edit_duration_subtracting_too_much_clamps_to_zero():
    runner.invoke(app, ["mou", "add", "nlnet-2026"])
    runner.invoke(app, ["task", "select", "10a"])
    runner.invoke(app, ["start", "https://example.com/issues/1"])
    runner.invoke(app, ["stop"])
    runner.invoke(app, ["edit", "--duration", "30"])

    result = runner.invoke(app, ["edit", "--duration", "-1:00"])

    assert result.exit_code == 0, result.output
    assert "New duration: 0:00" in result.output
    record = TimeRecord.objects.get()
    assert record.duration == timedelta(0)
    assert record.start_time == record.end_time


def test_edit_duration_on_a_running_entry_moves_the_start_and_keeps_it_running():
    # The whole point of --duration: fixing an entry left running because
    # `rfp stop` was forgotten. The elapsed time is measured against
    # "now" (there's no end_time yet), and only the start moves - the
    # entry is still running afterwards, unlike a plain `rfp stop`.
    runner.invoke(app, ["mou", "add", "nlnet-2026"])
    runner.invoke(app, ["task", "select", "10a"])
    started_at = timezone.now()
    with patch("django.utils.timezone.now", return_value=started_at):
        runner.invoke(app, ["start", "https://example.com/issues/1"])

    edited_at = started_at + timedelta(hours=3)
    with patch("django.utils.timezone.now", return_value=edited_at):
        result = runner.invoke(app, ["edit", "--duration", "50"])
        record = TimeRecord.objects.get()
        duration_now = record.duration

    assert result.exit_code == 0, result.output
    assert record.is_running is True
    assert record.end_time is None
    assert record.start_time == edited_at - timedelta(minutes=50)
    assert duration_now == timedelta(minutes=50)


def test_edit_duration_rejects_garbage():
    runner.invoke(app, ["mou", "add", "nlnet-2026"])
    runner.invoke(app, ["task", "select", "10a"])
    runner.invoke(app, ["start", "https://example.com/issues/1"])

    result = runner.invoke(app, ["edit", "--duration", "not-a-duration"])

    assert result.exit_code != 0
    assert "Invalid duration" in result.output
    record = TimeRecord.objects.get()
    assert record.is_running is True


def test_edit_duration_rejects_non_numeric_hmm_parts():
    runner.invoke(app, ["mou", "add", "nlnet-2026"])
    runner.invoke(app, ["task", "select", "10a"])
    runner.invoke(app, ["start", "https://example.com/issues/1"])

    result = runner.invoke(app, ["edit", "--duration", "1:xx"])

    assert result.exit_code != 0
    assert "Invalid duration" in result.output


def _time_record_pk() -> int:
    return TimeRecord.objects.get().pk


def test_timesheet_list_reports_when_there_are_none():
    result = runner.invoke(app, ["timesheet", "list"])

    assert result.exit_code == 0, result.output
    assert "No time entries" in result.output


def test_timesheet_list_prints_one_line_per_entry():
    runner.invoke(app, ["mou", "add", "nlnet-2026"])
    runner.invoke(app, ["task", "select", "10a"])
    runner.invoke(app, ["start", "10a", "https://example.com/issues/1"])
    runner.invoke(app, ["stop"])
    pk = _time_record_pk()

    result = runner.invoke(app, ["timesheet", "list"])

    assert result.exit_code == 0, result.output
    line = result.output.strip()
    assert line.startswith(f"{pk} nlnet-2026 10a ")
    assert "https://example.com/issues/1" in line
    assert "implementation" in line
    start_field = line.split()[3]
    assert "." not in start_field  # no seconds/microseconds in the start timestamp
    assert start_field.count(":") == 1  # HH:MM only, no seconds


def test_timesheet_list_uses_a_dash_for_an_entry_without_a_link():
    TimeRecord.objects.create(start_time=timezone.now(), end_time=timezone.now())

    result = runner.invoke(app, ["timesheet", "list"])

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


def test_report_create_shows_the_task_alias_for_excluded_prs(settings):
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
            link=link, start_time=now - timedelta(minutes=20), end_time=now
        )
    runner.invoke(app, ["alias", "set", "task", "10a", "lib"])

    with _mock_github_session("open"):
        result = runner.invoke(app, ["report", "create"])

    assert result.exit_code == 0, result.output
    assert "Excluded Pull Requests (not merged):" in result.output
    assert "10a (lib):" in result.output


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
    # create+print flow should call fetch_states_async exactly once.
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

    async def fake_fetch_states_async(references, token=None):
        return [GitHubState(Status.CLOSED, None, "title") for _ in references]

    with patch(
        "nlnet_rfp_recorder.github.fetch_states_async",
        AsyncMock(side_effect=fake_fetch_states_async),
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


def test_report_review_shows_the_task_alias_and_description(settings):
    # Unlike the report text itself (test_report_print_preview_shows_no_
    # aliases), review is operator-only feedback - showing the alias and
    # description here helps decide what to keep, same reasoning as
    # format_excluded_links showing the alias for excluded PRs.
    settings.RFP_EUROS_PER_HOUR = 20.0
    mou = MoU.objects.create(name="nlnet-2026", selected=True)
    task = Task.objects.create(
        mou=mou, name="10a", description="Improve calendar parsing"
    )
    Alias.create("task", "10a", "lib", mou=mou)
    link = Link.objects.create(task=task, url="https://example.com/issues/1")
    now = timezone.now()
    TimeRecord.objects.create(
        link=link, start_time=now - timedelta(hours=3), end_time=now
    )
    runner.invoke(app, ["report", "create"])

    result = runner.invoke(app, ["report", "review", "nlnet-2026-1"], input="\n")

    assert result.exit_code == 0, result.output
    assert "10a (lib): 60€" in result.output
    assert "Improve calendar parsing" in result.output


def test_report_review_asks_about_an_open_implementation_issue_and_keeps_it(settings):
    settings.RFP_EUROS_PER_HOUR = 20.0
    mou = MoU.objects.create(name="nlnet-2026", selected=True)
    task = Task.objects.create(mou=mou, name="10a")
    link = Link.objects.create(
        task=task, url="https://github.com/nlnet/rfp-recorder/issues/1"
    )
    link.add_tag("implementation")
    now = timezone.now()
    # 3 hours @ 20€/h = 60€ - at/above 50, task question defaults to included.
    TimeRecord.objects.create(
        link=link, start_time=now - timedelta(hours=3), end_time=now
    )
    runner.invoke(app, ["report", "create"])

    with _mock_github_session("open"):
        # Blank "keep it as is" for the open issue, blank "use as is" for
        # the task - nothing ends up changed, so no final write prompt.
        result = runner.invoke(app, ["report", "review", "nlnet-2026-1"], input="\n\n")

    assert result.exit_code == 0, result.output
    assert "https://github.com/nlnet/rfp-recorder/issues/1 is open and tagged" in (
        result.output
    )
    assert "Kept as is: 1" in result.output
    assert "Nothing to change." in result.output
    assert ReportLine.objects.filter(report__id="nlnet-2026-1", link=link).exists()


def test_report_review_excludes_an_open_implementation_issue_when_declined(settings):
    settings.RFP_EUROS_PER_HOUR = 20.0
    mou = MoU.objects.create(name="nlnet-2026", selected=True)
    task = Task.objects.create(mou=mou, name="10a")
    link = Link.objects.create(
        task=task, url="https://github.com/nlnet/rfp-recorder/issues/1"
    )
    link.add_tag("implementation")
    now = timezone.now()
    TimeRecord.objects.create(
        link=link, start_time=now - timedelta(hours=3), end_time=now
    )
    runner.invoke(app, ["report", "create"])
    record = TimeRecord.objects.get()

    with _mock_github_session("open"):
        result = runner.invoke(
            app, ["report", "review", "nlnet-2026-1"], input="n\n\ny\n"
        )

    assert result.exit_code == 0, result.output
    assert "Excluded: 1" in result.output
    assert not ReportLine.objects.filter(report__id="nlnet-2026-1", link=link).exists()
    record.refresh_from_db()
    assert record.report_line is None


def test_report_review_replaces_the_link_for_an_open_implementation_issue(settings):
    settings.RFP_EUROS_PER_HOUR = 20.0
    mou = MoU.objects.create(name="nlnet-2026", selected=True)
    task = Task.objects.create(mou=mou, name="10a")
    old_link = Link.objects.create(
        task=task, url="https://github.com/nlnet/rfp-recorder/issues/1"
    )
    old_link.add_tag("implementation")
    now = timezone.now()
    TimeRecord.objects.create(
        link=old_link, start_time=now - timedelta(hours=3), end_time=now
    )
    runner.invoke(app, ["report", "create"])
    new_url = "https://github.com/nlnet/rfp-recorder/issues/2"

    with _mock_github_session("open"):
        result = runner.invoke(
            app, ["report", "review", "nlnet-2026-1"], input=f"{new_url}\n\ny\n"
        )

    assert result.exit_code == 0, result.output
    assert "Replaced: 1" in result.output
    assert not ReportLine.objects.filter(
        report__id="nlnet-2026-1", link=old_link
    ).exists()
    new_line = ReportLine.objects.get(report__id="nlnet-2026-1", link__url=new_url)
    assert new_line.link.task == task


def test_report_review_does_not_ask_about_a_closed_implementation_issue(settings):
    settings.RFP_EUROS_PER_HOUR = 20.0
    mou = MoU.objects.create(name="nlnet-2026", selected=True)
    task = Task.objects.create(mou=mou, name="10a")
    link = Link.objects.create(
        task=task, url="https://github.com/nlnet/rfp-recorder/issues/1"
    )
    link.add_tag("implementation")
    now = timezone.now()
    TimeRecord.objects.create(
        link=link, start_time=now - timedelta(hours=3), end_time=now
    )
    runner.invoke(app, ["report", "create"])

    with _mock_github_session("closed"):
        result = runner.invoke(app, ["report", "review", "nlnet-2026-1"], input="\n")

    assert result.exit_code == 0, result.output
    assert "is open and tagged" not in result.output
    assert "Open issues:" not in result.output


def test_report_review_does_not_ask_about_a_review_tagged_issue(settings):
    settings.RFP_EUROS_PER_HOUR = 20.0
    mou = MoU.objects.create(name="nlnet-2026", selected=True)
    task = Task.objects.create(mou=mou, name="10a")
    link = Link.objects.create(
        task=task, url="https://github.com/nlnet/rfp-recorder/issues/1"
    )
    link.add_tag("review")
    now = timezone.now()
    TimeRecord.objects.create(
        link=link, start_time=now - timedelta(hours=3), end_time=now
    )
    runner.invoke(app, ["report", "create"])

    with _mock_github_session("open"):
        result = runner.invoke(app, ["report", "review", "nlnet-2026-1"], input="\n")

    assert result.exit_code == 0, result.output
    assert "is open and tagged" not in result.output
    assert "Open issues:" not in result.output


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


def _create_two_link_report(settings) -> tuple[Task, Link, Link]:
    settings.RFP_EUROS_PER_HOUR = 20.0
    mou = MoU.objects.create(name="nlnet-2026", selected=True)
    task = Task.objects.create(mou=mou, name="10a", description="Improve parsing")
    link_a = Link.objects.create(task=task, url="https://example.com/issues/1")
    link_b = Link.objects.create(task=task, url="https://example.com/issues/2")
    now = timezone.now()
    for link in (link_a, link_b):
        TimeRecord.objects.create(
            link=link, start_time=now - timedelta(hours=1), end_time=now
        )
    runner.invoke(app, ["report", "create"])
    return task, link_a, link_b


def test_report_edit_prints_the_task_description_and_each_link(settings):
    task, link_a, link_b = _create_two_link_report(settings)

    result = runner.invoke(app, ["report", "edit", "nlnet-2026-1"], input="\n\n")

    assert result.exit_code == 0, result.output
    assert "10a:" in result.output
    assert "Improve parsing" in result.output
    assert link_a.url in result.output
    assert link_b.url in result.output
    assert "Nothing to change." in result.output
    assert ReportLine.objects.filter(report__id="nlnet-2026-1", link=link_a).exists()
    assert ReportLine.objects.filter(report__id="nlnet-2026-1", link=link_b).exists()


def test_report_edit_excludes_a_link_when_declined(settings):
    task, link_a, link_b = _create_two_link_report(settings)
    record_a = TimeRecord.objects.get(link=link_a)

    result = runner.invoke(app, ["report", "edit", "nlnet-2026-1"], input="n\n\ny\n")

    assert result.exit_code == 0, result.output
    assert "Excluded: 1" in result.output
    assert not ReportLine.objects.filter(
        report__id="nlnet-2026-1", link=link_a
    ).exists()
    assert ReportLine.objects.filter(report__id="nlnet-2026-1", link=link_b).exists()
    record_a.refresh_from_db()
    assert record_a.report_line is None


def test_report_edit_replaces_a_links_url(settings):
    task, link_a, link_b = _create_two_link_report(settings)
    new_url = "https://example.com/issues/99"

    result = runner.invoke(
        app, ["report", "edit", "nlnet-2026-1"], input=f"{new_url}\n\ny\n"
    )

    assert result.exit_code == 0, result.output
    assert "Replaced: 1" in result.output
    assert not ReportLine.objects.filter(
        report__id="nlnet-2026-1", link=link_a
    ).exists()
    new_line = ReportLine.objects.get(report__id="nlnet-2026-1", link__url=new_url)
    assert new_line.link.task == task


def test_report_edit_restricts_to_the_given_task_range(settings):
    settings.RFP_EUROS_PER_HOUR = 20.0
    mou = MoU.objects.create(name="nlnet-2026", selected=True)
    task_a = Task.objects.create(mou=mou, name="10a")
    task_b = Task.objects.create(mou=mou, name="11b")
    link_a = Link.objects.create(task=task_a, url="https://example.com/issues/1")
    link_b = Link.objects.create(task=task_b, url="https://example.com/issues/2")
    now = timezone.now()
    for link in (link_a, link_b):
        TimeRecord.objects.create(
            link=link, start_time=now - timedelta(hours=1), end_time=now
        )
    runner.invoke(app, ["report", "create"])

    result = runner.invoke(app, ["report", "edit", "nlnet-2026-1", "10a"], input="\n")

    assert result.exit_code == 0, result.output
    assert "10a:" in result.output
    assert "11b:" not in result.output
    assert link_b.url not in result.output


def test_report_edit_fails_for_an_unknown_report():
    result = runner.invoke(app, ["report", "edit", "does-not-exist"])

    assert result.exit_code != 0


def test_report_edit_reports_nothing_to_edit_for_an_empty_report(settings):
    mou = MoU.objects.create(name="nlnet-2026", selected=True)
    report = Report.create(mou)

    result = runner.invoke(app, ["report", "edit", report.id])

    assert result.exit_code == 0, result.output
    assert "no matching lines to edit" in result.output


def test_report_edit_leaves_the_report_unchanged_when_cancelled(settings):
    task, link_a, link_b = _create_two_link_report(settings)

    result = runner.invoke(app, ["report", "edit", "nlnet-2026-1"], input="n\n\nn\n")

    assert result.exit_code == 0, result.output
    assert "Cancelled" in result.output
    assert ReportLine.objects.filter(report__id="nlnet-2026-1", link=link_a).exists()


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
    runner.invoke(app, ["mou", "add", "nlnet-2026"])
    Task.objects.create(mou=MoU.objects.get(), name="10a")

    result = runner.invoke(app, ["review", "https://example.com/issues/1"], input="\n")

    assert result.exit_code == 0, result.output
    record = TimeRecord.objects.get()
    assert record.is_running is True
    assert record.link.task.name == "10a"
    link = Link.objects.get()
    assert [tag.name for tag in link.tags.all()] == ["review"]


def test_review_without_a_task_creates_a_taskless_entry():
    result = runner.invoke(app, ["review", "https://example.com/issues/1"])

    assert result.exit_code == 0, result.output
    record = TimeRecord.objects.get()
    assert record.is_running is True
    assert record.link.task is None


def test_review_stops_a_previously_running_entry():
    runner.invoke(app, ["mou", "add", "nlnet-2026"])
    Task.objects.create(mou=MoU.objects.get(), name="10a")
    runner.invoke(app, ["start", "10a", "https://example.com/issues/1"])

    result = runner.invoke(app, ["review", "https://example.com/issues/2"], input="\n")

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
    runner.invoke(app, ["mou", "add", "nlnet-2026"])
    Task.objects.create(mou=MoU.objects.get(), name="10a")

    result = runner.invoke(
        app, ["implement", "https://example.com/issues/1"], input="\n"
    )

    assert result.exit_code == 0, result.output
    record = TimeRecord.objects.get()
    assert record.is_running is True
    assert record.link.task.name == "10a"
    link = Link.objects.get()
    assert [tag.name for tag in link.tags.all()] == ["implementation"]


def test_implement_without_a_task_creates_a_taskless_entry():
    result = runner.invoke(app, ["implement", "https://example.com/issues/1"])

    assert result.exit_code == 0, result.output
    record = TimeRecord.objects.get()
    assert record.is_running is True
    assert record.link.task is None


def test_implement_with_a_single_bare_task_alias_uses_an_empty_url():
    runner.invoke(app, ["mou", "add", "nlnet-2026"])
    runner.invoke(app, ["task", "select", "10a"])
    runner.invoke(app, ["alias", "set", "task", "10a", "ical-lang"])

    result = runner.invoke(app, ["implement", "ical-lang"])

    assert result.exit_code == 0, result.output
    assert "'ical-lang' isn't a link - starting it with an empty url." in result.output
    record = TimeRecord.objects.get()
    assert record.link.task.name == "10a"
    assert record.link.url == ""


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
    runner.invoke(app, ["start", "10a", "https://example.com/issues/1"])
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
    runner.invoke(app, ["start", "10a", "https://example.com/issues/1"])
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
    runner.invoke(app, ["start", "10a", "https://example.com/issues/1"])
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
    runner.invoke(app, ["start", "10a", "https://example.com/issues/1"])
    runner.invoke(app, ["stop"])

    result = runner.invoke(app, ["start", "10a", "https://example.com/issues/1"])

    assert result.exit_code == 0, result.output
    assert "is under task" not in result.output


def test_start_asks_before_assigning_a_taskless_link_defaulting_to_the_selected_task():
    # A taskless link is never auto-assigned to the selected task
    # silently any more - it's always asked about, just like any other
    # taskless link (test_two_new_links_in_a_row_both_ask_for_a_task) -
    # but the selected task is offered as the default, so accepting it
    # is a single Enter away.
    runner.invoke(app, ["mou", "add", "nlnet-2026"])
    task = Task.objects.create(mou=MoU.objects.get(), name="10a")
    Link.objects.create(url="https://example.com/issues/1")

    result = runner.invoke(app, ["start", "https://example.com/issues/1"], input="\n")

    assert result.exit_code == 0, result.output
    assert "is under task" not in result.output
    assert "Which task should this be assigned to?" in result.output
    assert "[10a]" in result.output
    link = Link.objects.get()
    assert link.task == task


def test_start_without_a_task_keeps_the_links_existing_task():
    # No task was typed, so the selected task is only a fallback filling
    # in for a missing argument - an existing assignment wins without a
    # question, unlike typing a task explicitly (see
    # test_start_asks_before_moving_a_link_to_a_different_task).
    runner.invoke(app, ["mou", "add", "nlnet-2026"])
    runner.invoke(app, ["task", "select", "10a"])
    runner.invoke(app, ["start", "10b", "https://example.com/issues/1"])
    runner.invoke(app, ["stop"])
    runner.invoke(app, ["task", "select", "10a"])

    result = runner.invoke(app, ["start", "https://example.com/issues/1"])

    assert result.exit_code == 0, result.output
    assert "but this looks like" not in result.output
    assert "is under task 10b" in result.output
    assert "rfp start 10a https://example.com/issues/1" in result.output
    link = Link.objects.get()
    assert link.task.name == "10b"
    assert Task.objects.get(name="10b").selected is True
    assert Task.objects.get(name="10a").selected is False


def test_start_declining_the_task_change_reselects_the_old_task():
    # An explicit task name selects it right away (test_start_with_an_
    # explicit_task_selects_it_first), but declining to actually move the
    # link means 10a - not 10b - is the task that was really worked on,
    # so it must end up selected again, not 10b.
    runner.invoke(app, ["mou", "add", "nlnet-2026"])
    runner.invoke(app, ["task", "select", "10a"])
    runner.invoke(app, ["start", "10a", "https://example.com/issues/1"])
    runner.invoke(app, ["stop"])

    result = runner.invoke(
        app, ["start", "10b", "https://example.com/issues/1"], input="n\n"
    )

    assert result.exit_code == 0, result.output
    assert Task.objects.get(name="10a").selected is True
    assert Task.objects.get(name="10b").selected is False


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


def test_complete_alias_id_delegates_by_item_type():
    from types import SimpleNamespace

    from nlnet_rfp_recorder.cli import _complete_alias_id

    mou = MoU.objects.create(name="nlnet-2026")
    Task.objects.create(mou=mou, name="10a")
    Link.objects.create(url="https://example.com/issues/1")

    assert _complete_alias_id(SimpleNamespace(params={"item": "mou"}), "nlnet") == [
        "nlnet-2026"
    ]
    assert _complete_alias_id(SimpleNamespace(params={"item": "task"}), "10") == ["10a"]
    assert _complete_alias_id(
        SimpleNamespace(params={"item": "url"}), "https://example"
    ) == ["https://example.com/issues/1"]
    assert _complete_alias_id(SimpleNamespace(params={}), "any") == []


def test_complete_report_edit_tasks_scopes_to_the_reports_own_tasks(settings):
    from types import SimpleNamespace

    from nlnet_rfp_recorder.cli import _complete_report_edit_tasks

    settings.RFP_EUROS_PER_HOUR = 20.0
    mou = MoU.objects.create(name="nlnet-2026", selected=True)
    task_in_report = Task.objects.create(mou=mou, name="10a")
    Alias.create("task", "10a", "lib", mou=mou)
    # Exists in the MoU, but never tracked/reported - must not be suggested.
    Task.objects.create(mou=mou, name="10b")
    link = Link.objects.create(task=task_in_report, url="https://example.com/issues/1")
    TimeRecord.objects.create(
        link=link,
        start_time=timezone.now() - timedelta(hours=1),
        end_time=timezone.now(),
    )
    runner.invoke(app, ["report", "create"])

    ctx = SimpleNamespace(params={"report_id": "nlnet-2026-1"})
    assert _complete_report_edit_tasks(ctx, "1") == ["10a"]
    assert _complete_report_edit_tasks(ctx, "l") == ["lib"]
    assert _complete_report_edit_tasks(ctx, "10b") == []
    assert _complete_report_edit_tasks(SimpleNamespace(params={}), "1") == []


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
    MoU.objects.create(name="nlnet-2026", selected=True)
    result = runner.invoke(app, ["stats", "today"])

    assert result.exit_code == 0, result.output
    assert "No time tracked in this period." in result.output


def test_stats_today_shows_per_task_time_and_budget(settings):
    settings.RFP_EUROS_PER_HOUR = 20.0
    mou = MoU.objects.create(name="nlnet-2026", selected=True)
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
    mou = MoU.objects.create(name="nlnet-2026", selected=True)
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
    mou = MoU.objects.create(name="nlnet-2026", selected=True)
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
    mou = MoU.objects.create(name="nlnet-2026", selected=True)
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
    mou = MoU.objects.create(name="nlnet-2026", selected=True)
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
    MoU.objects.create(name="nlnet-2026", selected=True)
    result = runner.invoke(app, ["stats", "days", "0"])

    assert result.exit_code != 0
    assert "must be positive" in result.output


def test_stats_total_reports_no_time_when_nothing_tracked():
    MoU.objects.create(name="nlnet-2026", selected=True)
    result = runner.invoke(app, ["stats", "total"])

    assert result.exit_code == 0, result.output
    assert "No time tracked in this period." in result.output


def test_stats_total_includes_records_regardless_of_age(settings):
    settings.RFP_EUROS_PER_HOUR = 20.0
    mou = MoU.objects.create(name="nlnet-2026", selected=True)
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
    mou = MoU.objects.create(name="nlnet-2026", selected=True)
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
    mou = MoU.objects.create(name="nlnet-2026", selected=True)
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
    mou = MoU.objects.create(name="nlnet-2026", selected=True)
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


def test_stats_ignores_other_mous_and_untracked_time(settings):
    settings.RFP_EUROS_PER_HOUR = 20.0
    MoU.objects.create(name="nlnet-2026", selected=True)
    other = MoU.objects.create(name="nlnet-2025")
    task = Task.objects.create(mou=other, name="10a")
    now = timezone.now()
    for link in (
        Link.objects.create(task=task, url="https://example.com/issues/1"),
        Link.objects.create(url="https://example.com/issues/2"),
    ):
        TimeRecord.objects.create(
            link=link, start_time=now - timedelta(hours=1), end_time=now
        )

    result = runner.invoke(app, ["stats", "today"])

    assert result.exit_code == 0, result.output
    assert "No time tracked in this period." in result.output


def test_stats_fails_without_a_selected_mou():
    result = runner.invoke(app, ["stats", "today"])

    assert result.exit_code != 0
    assert "No MoU selected" in result.output


def test_stats_hides_budget_when_rfp_euros_per_hour_is_unset(settings):
    settings.RFP_EUROS_PER_HOUR = None
    mou = MoU.objects.create(name="nlnet-2026", selected=True)
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
    mou = MoU.objects.create(name="nlnet-2026", selected=True)
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
    mou = MoU.objects.create(name="nlnet-2026", selected=True)
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
    mou = MoU.objects.create(name="nlnet-2026", selected=True)
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
    mou = MoU.objects.create(name="nlnet-2026", selected=True)
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
    mou = MoU.objects.create(name="nlnet-2026", selected=True)
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
    mou = MoU.objects.create(name="nlnet-2026", selected=True)
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
    mou = MoU.objects.create(name="nlnet-2026", selected=True)
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
    mou = MoU.objects.create(name="nlnet-2026", selected=True)
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


@pytest.mark.parametrize("args", [["stats", "--help"], ["stats", "today", "--help"]])
def test_stats_help_explains_the_columns(args):
    result = runner.invoke(app, args)

    assert result.exit_code == 0, result.output
    for column in ("ID", "alias", "time", "Euro", "new"):
        assert column in result.output
    assert "REVIEW_DEFAULT_EXCLUDE_BELOW" in result.output
    assert "Total" in result.output


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


def test_complete_csv_path_lists_csv_files_and_directories(tmp_path):
    from nlnet_rfp_recorder.cli import _complete_csv_path

    (tmp_path / "report.csv").write_text("")
    (tmp_path / "notes.txt").write_text("")
    (tmp_path / "reports").mkdir()

    matches = _complete_csv_path(f"{tmp_path}/")

    assert f"{tmp_path}/report.csv" in matches
    assert f"{tmp_path}/reports/" in matches
    assert not any(match.endswith("notes.txt") for match in matches)


def test_complete_csv_path_filters_by_prefix(tmp_path):
    from nlnet_rfp_recorder.cli import _complete_csv_path

    (tmp_path / "report-a.csv").write_text("")
    (tmp_path / "report-b.csv").write_text("")
    (tmp_path / "other.csv").write_text("")

    matches = _complete_csv_path(f"{tmp_path}/report")

    assert set(matches) == {f"{tmp_path}/report-a.csv", f"{tmp_path}/report-b.csv"}


def test_complete_csv_path_returns_nothing_for_a_missing_directory(tmp_path):
    from nlnet_rfp_recorder.cli import _complete_csv_path

    assert _complete_csv_path(f"{tmp_path}/does-not-exist/x") == []


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
        "background-task",
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
        "list",
        "remove",
    ]


def test_stats_help_lists_subcommands_alphabetically():
    result = runner.invoke(app, ["stats", "--help"])

    assert result.exit_code == 0, result.output
    assert _subcommand_names("stats") == [
        "days",
        "implementation",
        "review",
        "today",
        "total",
    ]


def test_report_help_lists_subcommands_alphabetically():
    result = runner.invoke(app, ["report", "--help"])

    assert result.exit_code == 0, result.output
    assert _subcommand_names("report") == [
        "create",
        "edit",
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
        "deadline",
        "export",
        "import",
        "list",
        "plot",
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
        "plot",
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

    result = runner.invoke(app, ["task", "set", "max", "lib", "500"])

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
        result = runner.invoke(app, ["start", "ical/1782"], input="\n")

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

    # An explicit task, so the "Started..." line - printed before the
    # taskless-link prompt would even run - already reflects it.
    result = runner.invoke(app, ["start", "10a", "https://example.com/issues/1"])

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


def test_main_reports_database_locked_diagnostics(capsys):
    from django.db.utils import OperationalError

    from nlnet_rfp_recorder.cli import main
    from nlnet_rfp_recorder.lock_diagnostics import FileHolder

    holder = FileHolder(
        pid=1234,
        comm="sqlitebro",
        cmdline="/usr/bin/sqlitebro rfp.db",
        user="alice",
        mode="read/write",
    )
    with (
        patch(
            "nlnet_rfp_recorder.cli.app",
            side_effect=OperationalError("database is locked"),
        ),
        patch(
            "nlnet_rfp_recorder.lock_diagnostics.find_processes_with_file_open",
            return_value=[holder],
        ),
    ):
        with pytest.raises(SystemExit) as exc_info:
            main()

    assert exc_info.value.code == 1
    captured = capsys.readouterr()
    assert "Database is locked" in captured.err
    assert "PID 1234" in captured.err
    assert "sqlitebro" in captured.err
    assert "alice" in captured.err


def test_main_reports_no_holder_found_gracefully(capsys):
    from django.db.utils import OperationalError

    from nlnet_rfp_recorder.cli import main

    with (
        patch(
            "nlnet_rfp_recorder.cli.app",
            side_effect=OperationalError("database is locked"),
        ),
        patch(
            "nlnet_rfp_recorder.lock_diagnostics.find_processes_with_file_open",
            return_value=[],
        ),
    ):
        with pytest.raises(SystemExit) as exc_info:
            main()

    assert exc_info.value.code == 1
    captured = capsys.readouterr()
    assert "No process currently has it open" in captured.err


def test_main_reraises_other_operational_errors():
    from django.db.utils import OperationalError

    from nlnet_rfp_recorder.cli import main

    with patch(
        "nlnet_rfp_recorder.cli.app",
        side_effect=OperationalError("no such table: foo"),
    ):
        with pytest.raises(OperationalError):
            main()


def test_main_skips_process_listing_on_non_linux(capsys):
    from django.db.utils import OperationalError

    from nlnet_rfp_recorder.cli import main

    with (
        patch(
            "nlnet_rfp_recorder.cli.app",
            side_effect=OperationalError("database is locked"),
        ),
        patch("platform.system", return_value="Darwin"),
    ):
        with pytest.raises(SystemExit):
            main()

    captured = capsys.readouterr()
    assert "only supported on Linux" in captured.err


def test_review_batch_runs_review_for_each_line():
    _setup_task_only_start()

    result = runner.invoke(
        app,
        ["review", "10a", "--batch"],
        input="https://example.com/issues/1\n\nhttps://example.com/issues/2\n",
    )

    assert result.exit_code == 0, result.output
    records = TimeRecord.objects.order_by("start_time")
    assert [r.link.url for r in records] == [
        "https://example.com/issues/1",
        "https://example.com/issues/2",
    ]
    assert all(r.link.task.name == "10a" for r in records)
    assert all([t.name for t in r.link.tags.all()] == ["review"] for r in records)


def test_review_batch_without_task_asks_for_each_url():
    _setup_task_only_start()

    result = runner.invoke(
        app,
        ["review", "--batch"],
        input="https://example.com/issues/1\n10b\nhttps://example.com/issues/2\n10a\n",
    )

    assert result.exit_code == 0, result.output
    assert result.output.count("Which task should this be assigned to?") == 2
    records = TimeRecord.objects.order_by("start_time")
    assert [r.link.task.name for r in records] == ["10b", "10a"]


def test_mou_import_leaves_no_task_selected(tmp_path):
    runner.invoke(app, ["mou", "add", "nlnet-2026"])
    budget_file = tmp_path / "budget.txt"
    budget_file.write_text("10a. Do the thing\t€ 500\n10b. Other thing\t€ 300\n")

    result = _invoke_mou_import(str(budget_file))

    assert result.exit_code == 0, result.output
    assert Task.objects.count() == 2
    assert Task.get_selected() is None


def test_mou_import_stops_the_running_time_entry(tmp_path):
    runner.invoke(app, ["mou", "add", "nlnet-2026"])
    runner.invoke(app, ["start", "10a", "https://example.com/issues/1"])
    assert TimeRecord.get_running() is not None
    budget_file = tmp_path / "budget.txt"
    budget_file.write_text("10a. Do the thing\t€ 500\n")

    result = _invoke_mou_import(str(budget_file))

    assert result.exit_code == 0, result.output
    assert "Stopped" in result.output
    assert TimeRecord.get_running() is None


def _stopped_record(start_hm: tuple[int, int], minutes: int) -> TimeRecord:
    """A stopped entry today starting at `start_hm`, `minutes` long."""

    runner.invoke(app, ["mou", "add", "nlnet-2026"])
    runner.invoke(app, ["task", "select", "10a"])
    runner.invoke(app, ["start", "https://example.com/issues/1"])
    runner.invoke(app, ["stop"])
    record = TimeRecord.objects.get()
    record.start_time = datetime.now().replace(
        hour=start_hm[0], minute=start_hm[1], second=0, microsecond=0
    )
    record.end_time = record.start_time + timedelta(minutes=minutes)
    record.save()
    return record


def test_edit_start_changes_the_duration_by_default():
    record = _stopped_record((1, 0), 120)
    end = record.end_time

    result = runner.invoke(app, ["edit", "--start", "0:30"], input="\n")

    assert result.exit_code == 0, result.output
    record.refresh_from_db()
    assert (record.start_time.hour, record.start_time.minute) == (0, 30)
    assert record.end_time == end
    assert record.duration == timedelta(minutes=150)


def test_edit_start_can_move_the_entry_instead():
    record = _stopped_record((1, 0), 120)

    result = runner.invoke(app, ["edit", "--start", "0:30"], input="m\n")

    assert result.exit_code == 0, result.output
    record.refresh_from_db()
    assert (record.start_time.hour, record.start_time.minute) == (0, 30)
    assert record.duration == timedelta(minutes=120)


def test_edit_end_changes_the_duration_by_default():
    record = _stopped_record((0, 0), 60)
    start = record.start_time

    result = runner.invoke(app, ["edit", "--end", "0:30"], input="\n")

    assert result.exit_code == 0, result.output
    record.refresh_from_db()
    assert record.start_time == start
    assert record.duration == timedelta(minutes=30)


def test_edit_end_can_move_the_entry_instead():
    record = _stopped_record((0, 0), 60)

    result = runner.invoke(app, ["edit", "--end", "0:30"], input="m\n")

    assert result.exit_code == 0, result.output
    record.refresh_from_db()
    assert (record.end_time.hour, record.end_time.minute) == (0, 30)
    assert record.duration == timedelta(minutes=60)
    assert (record.start_time.hour, record.start_time.minute) == (23, 30)


def test_edit_start_later_than_now_asks_about_yesterday():
    record = _stopped_record((0, 0), 30)
    today = record.start_time.date()

    result = runner.invoke(app, ["edit", "--start", "23:59"], input="y\nm\n")

    assert result.exit_code == 0, result.output
    assert "yesterday" in result.output
    record.refresh_from_db()
    assert record.start_time.date() == today - timedelta(days=1)


def test_edit_start_rejects_invalid_time():
    _stopped_record((1, 0), 30)

    result = runner.invoke(app, ["edit", "--start", "nine"])

    assert result.exit_code == 1
    assert "Invalid time" in result.output


def test_edit_end_of_running_entry_fails():
    runner.invoke(app, ["mou", "add", "nlnet-2026"])
    runner.invoke(app, ["task", "select", "10a"])
    runner.invoke(app, ["start", "https://example.com/issues/1"])

    result = runner.invoke(app, ["edit", "--end", "0:00"])

    assert result.exit_code == 1
    assert "still running" in result.output


def _start_running_entry() -> TimeRecord:
    runner.invoke(app, ["mou", "add", "nlnet-2026"])
    runner.invoke(app, ["task", "select", "10a"])
    runner.invoke(app, ["start", "https://example.com/issues/1"])
    return TimeRecord.objects.get()


def _fake_clock(*times):

    iterator = iter(times)
    return lambda: datetime(2026, 10, 6) + timedelta(seconds=next(iterator))


def test_background_task_stops_the_entry_at_the_time_before_suspend():

    from nlnet_rfp_recorder.timetracking.models import BackgroundTask

    record = _start_running_entry()
    record.start_time = datetime(2026, 10, 5)
    record.save()
    task = BackgroundTask.register()

    # start 0, ticks at 1, 2, then a 1h jump (suspend)
    task.watch(sleep=lambda s: None, now=_fake_clock(0, 1, 2, 3602), ticks=3)

    record.refresh_from_db()
    assert record.end_time == datetime(2026, 10, 6) + timedelta(seconds=2)


def test_background_task_leaves_the_entry_running_without_a_gap():
    from nlnet_rfp_recorder.timetracking.models import BackgroundTask

    record = _start_running_entry()
    task = BackgroundTask.register()

    task.watch(sleep=lambda s: None, now=_fake_clock(0, 1, 2), ticks=2)

    record.refresh_from_db()
    assert record.is_running


def test_background_task_updates_the_db_entry_every_minute():

    from nlnet_rfp_recorder.timetracking.models import BackgroundTask

    task = BackgroundTask.register()

    task.watch(sleep=lambda s: None, now=_fake_clock(0, 30, 61, 90), ticks=3)

    task.refresh_from_db()
    assert task.last_seen == datetime(2026, 10, 6) + timedelta(seconds=61)


def test_background_task_check_reports_status():
    from nlnet_rfp_recorder.timetracking.models import BackgroundTask

    result = runner.invoke(app, ["background-task", "--check"])
    assert result.exit_code == 1
    assert "never run" in result.output

    task = BackgroundTask.register()
    result = runner.invoke(app, ["background-task", "--check"])
    assert result.exit_code == 0
    assert f"pid {os.getpid()}" in result.output
    assert "Last check 0 seconds ago" in result.output

    task.unregister()
    result = runner.invoke(app, ["background-task", "--check"])
    assert result.exit_code == 1
    assert "shut down cleanly" in result.output
    assert "Awaiting recovery" not in result.output


def test_background_task_refuses_to_start_twice(background_log):
    from nlnet_rfp_recorder.timetracking.models import BackgroundTask

    BackgroundTask.register()

    result = runner.invoke(app, ["background-task"])

    assert result.exit_code == 1
    assert "already running" in background_log.read_text()


def test_background_task_with_a_dead_pid_is_not_alive():
    from nlnet_rfp_recorder.timetracking.models import BackgroundTask

    task = BackgroundTask.register()
    task.pid = 2**22 + 12345  # beyond the default pid_max
    task.save()

    assert not BackgroundTask.is_alive()
    result = runner.invoke(app, ["background-task", "--check"])
    assert result.exit_code == 1
    assert "Not running" in result.output


def test_background_task_reports_the_suspend_and_the_stopped_entry(caplog):

    from nlnet_rfp_recorder.cli import _report_suspend
    from nlnet_rfp_recorder.timetracking.models import BackgroundTask

    record = _start_running_entry()
    record.start_time = datetime(2026, 10, 5)
    record.save()
    task = BackgroundTask.register()

    task.watch(
        on_suspend=_report_suspend,
        sleep=lambda s: None,
        now=_fake_clock(0, 1, 7201),
        ticks=2,
    )

    output = caplog.text
    assert "Suspend detected at 00:00:01: 2 hours until 02:00:01." in output
    assert "Stopped " in output
    assert "https://example.com/issues/1" in output


def test_background_task_reports_a_suspend_with_nothing_running(caplog):
    from nlnet_rfp_recorder.cli import _report_suspend
    from nlnet_rfp_recorder.timetracking.models import BackgroundTask

    task = BackgroundTask.register()

    task.watch(
        on_suspend=_report_suspend,
        sleep=lambda s: None,
        now=_fake_clock(0, 1, 301),
        ticks=2,
    )

    output = caplog.text
    assert "Suspend detected" in output
    assert "5 minutes" in output
    assert "nothing to stop" in output


def test_background_task_watch_returns_true_when_the_schema_changed():
    from django.db import connection
    from django.db.migrations.recorder import MigrationRecorder

    from nlnet_rfp_recorder.timetracking.models import BackgroundTask

    task = BackgroundTask.register()

    def run():
        return task.watch(
            sleep=lambda s: None, now=_fake_clock(0, 30, 61, 91, 121), ticks=4
        )

    assert run() is False
    MigrationRecorder(connection).record_applied("timetracking", "9999_from_the_future")

    assert run() is True


def test_background_task_restarts_itself_when_the_schema_changed(background_log):
    from nlnet_rfp_recorder.timetracking.models import BackgroundTask

    with (
        patch.object(BackgroundTask, "watch", return_value=True),
        patch("nlnet_rfp_recorder.cli.os.execv") as execv,
    ):
        result = runner.invoke(app, ["background-task"])

    assert result.exit_code == 0, result.output
    assert "Restarting" in background_log.read_text()
    execv.assert_called_once()
    assert not BackgroundTask.is_alive()


def test_background_task_survives_a_query_failing_against_a_newer_schema():

    from django.db import connection
    from django.db.migrations.recorder import MigrationRecorder

    from nlnet_rfp_recorder.timetracking.models import BackgroundTask

    record = _start_running_entry()
    record.start_time = datetime(2026, 10, 5)
    record.save()
    task = BackgroundTask.register()
    # What a newer version's migration does to this process's models:
    # TimeRecord.end_time no longer exists under that name.
    with connection.cursor() as cursor:
        cursor.execute(
            "ALTER TABLE timetracking_timerecord RENAME COLUMN end_time TO stopped_at"
        )
    MigrationRecorder(connection).record_applied("timetracking", "9999_rename")

    # A suspend makes watch() query TimeRecord straight away -> OperationalError.
    result = task.watch(sleep=lambda s: None, now=_fake_clock(0, 1, 7201), ticks=2)

    assert result is True


def test_background_task_reraises_an_operational_error_without_a_schema_change():
    from django.db import OperationalError, connection

    from nlnet_rfp_recorder.timetracking.models import BackgroundTask

    task = BackgroundTask.register()
    with connection.cursor() as cursor:
        cursor.execute(
            "ALTER TABLE timetracking_timerecord RENAME COLUMN end_time TO stopped_at"
        )

    with pytest.raises(OperationalError):
        task.watch(sleep=lambda s: None, now=_fake_clock(0, 1, 7201), ticks=2)


def test_background_task_logs_to_a_file_and_prints(background_log):
    from nlnet_rfp_recorder.timetracking.models import BackgroundTask

    with patch.object(BackgroundTask, "watch", return_value=False):
        result = runner.invoke(app, ["background-task"])

    assert result.exit_code == 0, result.output
    assert "Watching for suspend" in result.output
    assert "Watching for suspend" in background_log.read_text()


def test_background_task_log_prints_the_log_without_starting_a_task(background_log):
    from nlnet_rfp_recorder.timetracking.models import BackgroundTask

    background_log.write_text("2026-10-06 20:00:00 INFO hello\n")

    result = runner.invoke(app, ["background-task", "--log"])

    assert result.exit_code == 0, result.output
    assert "INFO hello" in result.output
    assert BackgroundTask.get() is None


def test_background_task_log_without_a_log_fails(background_log):
    result = runner.invoke(app, ["background-task", "--log"])

    assert result.exit_code == 1
    assert "No log yet" in result.output


def _dead_background_task(last_seen):
    from nlnet_rfp_recorder.timetracking.models import BackgroundTask

    return BackgroundTask.objects.create(
        started=last_seen - timedelta(hours=1),
        last_seen=last_seen,
        pid=2**22 + 12345,  # beyond the default pid_max: no such process
        is_running=True,
    )


def test_background_task_recovers_after_an_unclean_shutdown(background_log):

    from nlnet_rfp_recorder.timetracking.models import BackgroundTask

    record = _start_running_entry()
    record.start_time = datetime(2026, 10, 5, 8)
    record.save()
    last_seen = datetime(2026, 10, 5, 18, 30)
    _dead_background_task(last_seen)

    with patch.object(BackgroundTask, "watch", return_value=False):
        result = runner.invoke(app, ["background-task"])

    assert result.exit_code == 0, result.output
    record.refresh_from_db()
    assert record.end_time == last_seen
    log = background_log.read_text()
    assert "did not shut down cleanly" in log
    assert "Stopped" in log
    assert BackgroundTask.objects.count() == 1


def test_background_task_recovery_leaves_a_newer_entry_running(background_log):

    from nlnet_rfp_recorder.timetracking.models import BackgroundTask

    record = _start_running_entry()
    record.start_time = datetime(2026, 10, 6, 9)
    record.save()
    _dead_background_task(datetime(2026, 10, 5, 18, 30))

    with patch.object(BackgroundTask, "watch", return_value=False):
        runner.invoke(app, ["background-task"])

    record.refresh_from_db()
    assert record.is_running
    log = background_log.read_text()
    assert "after the last check-in" in log
    assert "Left running" in log
    assert "nothing to stop" not in log


def test_background_task_no_recovery_after_a_clean_exit(background_log):

    from nlnet_rfp_recorder.timetracking.models import BackgroundTask

    record = _start_running_entry()
    task = _dead_background_task(datetime(2026, 10, 5, 18, 30))
    task.unregister()

    with patch.object(BackgroundTask, "watch", return_value=False):
        runner.invoke(app, ["background-task"])

    record.refresh_from_db()
    assert record.is_running
    assert "did not shut down cleanly" not in background_log.read_text()


def test_background_task_check_reports_a_killed_task_awaiting_recovery():

    _dead_background_task(datetime(2026, 10, 5, 18, 30))

    result = runner.invoke(app, ["background-task", "--check"])

    assert result.exit_code == 1
    assert "did not shut down cleanly" in result.output
    assert "Awaiting recovery" in result.output
    assert "2026-10-05 18:30:00" in result.output
    assert "rfp background-task --recover" in result.output


def test_background_task_recover_recovers_and_loops_once(background_log):
    from unittest.mock import ANY

    from nlnet_rfp_recorder.timetracking.models import BackgroundTask

    record = _start_running_entry()
    record.start_time = datetime(2026, 10, 5, 8)
    record.save()
    _dead_background_task(datetime(2026, 10, 5, 18, 30))

    with patch.object(BackgroundTask, "watch", return_value=False) as watch:
        result = runner.invoke(app, ["background-task", "--recover"])

    assert result.exit_code == 0, result.output
    watch.assert_called_once_with(on_suspend=ANY, ticks=1)
    record.refresh_from_db()
    assert record.end_time == datetime(2026, 10, 5, 18, 30)
    assert "did not shut down cleanly" in result.output
    assert not BackgroundTask.is_alive()
    assert not BackgroundTask.get().is_running
    assert "RECOVER: " in result.output
    assert "RECOVER: The previous background task" in background_log.read_text()


def test_background_task_recover_aborts_while_a_task_is_running(background_log):
    from nlnet_rfp_recorder.timetracking.models import BackgroundTask

    record = _start_running_entry()
    BackgroundTask.register()

    result = runner.invoke(app, ["background-task", "--recover"])

    assert result.exit_code == 1
    assert "already running" in result.output
    record.refresh_from_db()
    assert record.is_running
    assert BackgroundTask.is_alive()
    assert "RECOVER: A background task is already running" in (
        background_log.read_text()
    )


def test_background_task_recovery_logs_when_no_entry_is_running(background_log):

    from nlnet_rfp_recorder.timetracking.models import BackgroundTask

    _dead_background_task(datetime(2026, 10, 5, 18, 30))

    with patch.object(BackgroundTask, "watch", return_value=False):
        runner.invoke(app, ["background-task"])

    assert "No time entry is running, nothing to stop." in background_log.read_text()


def test_stop_reason_is_user_by_default_and_after_rfp_stop():
    record = _start_running_entry()
    assert record.stop_reason == "user"

    runner.invoke(app, ["stop"])

    record.refresh_from_db()
    assert record.end_time is not None
    assert record.stop_reason == "user"


def test_stop_reason_is_user_when_starting_another_entry():
    record = _start_running_entry()

    runner.invoke(app, ["start", "https://example.com/issues/2"])

    record.refresh_from_db()
    assert record.end_time is not None
    assert record.stop_reason == "user"


def test_stop_reason_is_edit_after_rfp_edit_end_and_start_move():
    record = _stopped_record((1, 0), 120)

    runner.invoke(app, ["edit", "--end", "1:30"], input="\n")
    record.refresh_from_db()
    assert record.stop_reason == "edit"

    record.stop_reason = "user"
    record.save()
    runner.invoke(app, ["edit", "--start", "0:30"], input="m\n")
    record.refresh_from_db()
    assert record.stop_reason == "edit"


def test_stop_reason_stays_user_when_edit_only_changes_the_start():
    record = _stopped_record((1, 0), 120)

    runner.invoke(app, ["edit", "--start", "0:30"], input="\n")

    record.refresh_from_db()
    assert record.stop_reason == "user"


def test_stop_reason_is_edit_when_a_duration_edit_moves_the_end():
    record = _stopped_record((1, 0), 120)

    record.set_duration(timedelta(minutes=30), fix="start")

    record.refresh_from_db()
    assert record.stop_reason == "edit"


def test_stop_reason_is_suspend_for_a_background_task_suspend():

    from nlnet_rfp_recorder.timetracking.models import BackgroundTask

    record = _start_running_entry()
    record.start_time = datetime(2026, 10, 5)
    record.save()
    task = BackgroundTask.register()

    task.watch(sleep=lambda s: None, now=_fake_clock(0, 1, 7201), ticks=2)

    record.refresh_from_db()
    assert record.stop_reason == "suspend"


def test_stop_reason_is_restart_after_recovering_from_a_shutdown(background_log):

    from nlnet_rfp_recorder.timetracking.models import BackgroundTask

    record = _start_running_entry()
    record.start_time = datetime(2026, 10, 5, 8)
    record.save()
    _dead_background_task(datetime(2026, 10, 5, 18, 30))

    with patch.object(BackgroundTask, "watch", return_value=False):
        runner.invoke(app, ["background-task"])

    record.refresh_from_db()
    assert record.stop_reason == "restart"


def test_stop_reason_resets_to_user_when_an_entry_is_resumed():
    record = _start_running_entry()
    TimeRecord.stop(reason="suspend")

    runner.invoke(app, ["start", "https://example.com/issues/1"])

    record.refresh_from_db()
    assert record.is_running
    assert record.stop_reason == "user"


def test_timesheet_list_lists_background_stop_reasons_in_the_last_column():
    runner.invoke(app, ["mou", "add", "nlnet-2026"])
    runner.invoke(app, ["task", "select", "10a"])
    runner.invoke(app, ["start", "https://example.com/issues/1"])
    TimeRecord.stop(reason="suspend")
    runner.invoke(app, ["start", "https://example.com/issues/2"])
    TimeRecord.stop(reason="restart")
    runner.invoke(app, ["start", "https://example.com/issues/3"])
    runner.invoke(app, ["stop"])
    runner.invoke(app, ["start", "https://example.com/issues/4"])
    TimeRecord.stop(reason="edit")

    result = runner.invoke(app, ["timesheet", "list"])

    lines = result.output.splitlines()
    assert lines[0].endswith("implementation suspend")
    assert lines[1].endswith("implementation restart")
    assert lines[2].endswith("implementation")
    assert lines[3].endswith("implementation")


def test_status_says_not_running_after_a_background_stop():
    runner.invoke(app, ["mou", "add", "nlnet-2026"])
    runner.invoke(app, ["task", "select", "10a"])
    runner.invoke(app, ["start", "https://example.com/issues/1"])

    for reason, expected in (
        ("suspend", "Not running after suspend."),
        ("user", "Not running."),
    ):
        TimeRecord.stop(reason=reason)
        result = runner.invoke(app, ["status"])
        assert expected in result.output
        assert result.output.count("Not running") == 1
        runner.invoke(app, ["start", "https://example.com/issues/1"])

    TimeRecord.stop(reason="restart")
    assert "Not running after restart." in runner.invoke(app, ["status"]).output
    TimeRecord.objects.update(stop_reason="edit")
    assert "Not running." in runner.invoke(app, ["status"]).output


def test_mou_deadline_can_be_set_shown_and_cleared():
    runner.invoke(app, ["mou", "add", "nlnet-2026"])

    assert "No deadline" in runner.invoke(app, ["mou", "deadline"]).output
    result = runner.invoke(app, ["mou", "deadline", "2099-01-31"])
    assert result.exit_code == 0, result.output
    assert "Deadline: 2099-01-31" in result.output
    assert "Deadline: 2099-01-31" in runner.invoke(app, ["mou", "status"]).output

    runner.invoke(app, ["mou", "deadline", "--clear"])
    assert "Deadline" not in runner.invoke(app, ["mou", "status"]).output


def test_mou_deadline_rejects_an_invalid_date():
    runner.invoke(app, ["mou", "add", "nlnet-2026"])

    result = runner.invoke(app, ["mou", "deadline", "next week"])

    assert result.exit_code != 0


def test_plot_calendar_fails_without_time_records():
    runner.invoke(app, ["mou", "add", "nlnet-2026"])

    result = runner.invoke(app, ["mou", "plot", "calendar"])

    assert result.exit_code != 0


def _record_an_hour_for_plot():
    mou = MoU.objects.create(name="nlnet-2026")
    task = Task.objects.create(mou=mou, name="10a")
    link = Link.objects.create(task=task, url="https://example.com/issues/1")
    now = timezone.now()
    TimeRecord.objects.create(
        link=link, start_time=now - timedelta(hours=1), end_time=now
    )


def test_plot_calendar_shows_a_heat_map_of_worked_time():
    _record_an_hour_for_plot()

    result = runner.invoke(app, ["mou", "plot", "calendar"])

    assert result.exit_code == 0, result.output
    assert "Hours worked on nlnet-2026" in result.output
    assert "Mon" in result.output


def test_split_bar_always_adds_up_to_the_width():
    from nlnet_rfp_recorder.cli import _split_bar

    assert _split_bar([1, 1, 1], 10) == [4, 3, 3]
    assert sum(_split_bar([7.3, 0.2, 99.1], 77)) == 77
    assert _split_bar([0, 0, 0], 10) == [0, 0, 0]


def test_task_plot_complete_shows_stacked_bars(settings):
    settings.RFP_EUROS_PER_HOUR = 20.0
    mou = MoU.objects.create(name="nlnet-2026")
    task = Task.objects.create(mou=mou, name="10a", max_budget=300.0)
    task.set_budget(200.0)

    result = runner.invoke(app, ["task", "plot", "complete"])

    assert result.exit_code == 0, result.output
    assert "10a" in result.output
    assert "worked" in result.output
    assert "max" in result.output


def test_task_plot_complete_fails_without_tasks():
    MoU.objects.create(name="nlnet-2026")

    result = runner.invoke(app, ["task", "plot", "complete"])

    assert result.exit_code != 0


def test_task_plot_complete_bars_align_and_skip_tasks_without_budget():
    mou = MoU.objects.create(name="nlnet-2026")
    Task.objects.create(mou=mou, name="10a", max_budget=300.0).set_budget(100.0)
    Task.objects.create(mou=mou, name="10b", max_budget=30.0)
    Task.objects.create(mou=mou, name="10c", max_budget=50.0).set_budget(0.0)

    result = runner.invoke(app, ["task", "plot", "complete"])

    assert result.exit_code == 0, result.output
    bars = {
        line[:3]: line.count("█")
        for line in result.output.splitlines()
        if line[:3] in ("10a", "10b", "10c")
    }
    assert bars == {"10a": 50, "10b": 50}


def test_task_plot_complete_separates_task_groups_with_a_blank_line():
    mou = MoU.objects.create(name="nlnet-2026")
    for name in ("5f", "5g", "6a"):
        Task.objects.create(mou=mou, name=name, max_budget=100.0)

    result = runner.invoke(app, ["task", "plot", "complete"])

    assert result.exit_code == 0, result.output
    lines = [line[:2].strip() for line in result.output.splitlines()[2:]]
    assert lines == ["5", "5f", "5g", "", "6", "6a"]


def test_task_plot_complete_fits_the_terminal_width(monkeypatch):
    mou = MoU.objects.create(name="nlnet-2026")
    task = Task.objects.create(mou=mou, name="1i", max_budget=300.0)
    Alias.create("task", "1i", "ical-rfc-compat", mou=mou)
    task.set_budget(200.0)
    monkeypatch.setenv("COLUMNS", "60")

    result = runner.invoke(app, ["task", "plot", "complete"])

    assert result.exit_code == 0, result.output
    lines = result.output.splitlines()
    assert max(len(line) for line in lines) <= 60
    assert any(line.count("█") >= 15 for line in lines)


def test_plot_calendar_is_cut_to_the_terminal_width(monkeypatch):
    _record_an_hour_for_plot()
    monkeypatch.setenv("COLUMNS", "30")

    result = runner.invoke(app, ["mou", "plot", "calendar"])

    assert result.exit_code == 0, result.output
    mon = next(line for line in result.output.splitlines() if line.startswith("Mon"))
    assert len(re.sub(r"\x1b\[[0-9;]*m", "", mon)) <= 30


def _mou_for_plot_complete(settings, started_days_ago, deadline_in_days):
    settings.RFP_EUROS_PER_HOUR = 20.0
    mou = MoU.objects.create(
        name="nlnet-2026", deadline=date.today() + timedelta(days=deadline_in_days)
    )
    task = Task.objects.create(mou=mou, name="10a", max_budget=1000.0, used_budget=100)
    link = Link.objects.create(task=task, url="https://example.com/issues/1")
    start = timezone.now() - timedelta(days=started_days_ago)
    TimeRecord.objects.create(
        link=link, start_time=start, end_time=start + timedelta(hours=5)
    )


def test_mou_plot_complete_says_how_far_behind_schedule_you_are(settings):
    # Half the time is over, so 500€ should be used; 100€ + 5h * 20€ = 200€ are.
    _mou_for_plot_complete(settings, 10, 10)

    result = runner.invoke(app, ["mou", "plot", "complete"])

    assert result.exit_code == 0, result.output
    assert "1000€" in result.output
    assert "submitted 100€" in result.output
    assert "worked 100€" in result.output
    assert "15:00 hours behind schedule." in result.output
    # 5h tracked over 11 days; 400€ = 20h left over the days until the deadline.
    assert "Average total" in result.output
    assert "Average 30 days" in result.output
    assert "daily work (Mo-Fr)" in result.output
    assert "weekly work" in result.output
    assert "Required for completion" in result.output


def test_mou_plot_complete_says_when_you_are_in_front(settings):
    _mou_for_plot_complete(settings, 10, 10000)

    result = runner.invoke(app, ["mou", "plot", "complete"])

    assert result.exit_code == 0, result.output
    assert "in front of schedule." in result.output


def test_mou_plot_complete_asks_for_a_deadline(settings):
    _mou_for_plot_complete(settings, 10, 10)
    MoU.objects.update(deadline=None)

    result = runner.invoke(app, ["mou", "plot", "complete"])

    assert result.exit_code == 0, result.output
    assert "deadline" in result.output


def test_task_plot_complete_shows_submitted_and_worked_separately(settings):
    settings.RFP_EUROS_PER_HOUR = 20.0
    mou = MoU.objects.create(name="nlnet-2026")
    task = Task.objects.create(mou=mou, name="10a", max_budget=400.0, used_budget=100)
    link = Link.objects.create(task=task, url="https://example.com/issues/1")
    now = timezone.now()
    TimeRecord.objects.create(
        link=link, start_time=now - timedelta(hours=5), end_time=now
    )

    result = runner.invoke(app, ["task", "plot", "complete"])

    assert result.exit_code == 0, result.output
    assert "submitted" in result.output
    assert "worked" in result.output


def test_mou_plot_weeks_shows_required_and_recent_weeks(settings):
    # Started last Sunday, so the MoU spans this week and last week only,
    # whatever weekday the test runs on.
    _mou_for_plot_complete(settings, date.today().weekday() + 1, 10)

    result = runner.invoke(app, ["mou", "plot", "weeks"])

    assert result.exit_code == 0, result.output
    lines = result.output.splitlines()
    assert lines[0] == "Weeks since the start:"
    assert "required for completion" in lines[1]
    assert "this week (actual)" in lines[2]
    assert "this week (projected)" in lines[3]
    assert "last week (" in lines[4]
    assert "2 weeks ago (" not in result.output
    assert len(lines) == 5


def test_mou_plot_weeks_marks_missing_hours_red_after_yellow(settings, monkeypatch):
    _mou_for_plot_complete(settings, 10, 10)
    monkeypatch.setenv("COLUMNS", "80")

    result = runner.invoke(app, ["mou", "plot", "weeks"], color=True)

    assert result.exit_code == 0, result.output
    row = next(line for line in result.output.splitlines() if "last week" in line)
    assert row.index("\x1b[33m") < row.index("\x1b[31m")


def test_mou_plot_weeks_fails_without_rate(settings):
    _mou_for_plot_complete(settings, 10, 10)
    settings.RFP_EUROS_PER_HOUR = None

    result = runner.invoke(app, ["mou", "plot", "weeks"])

    assert result.exit_code != 0


def test_task_plot_complete_shows_right_aligned_budget_amounts():
    mou = MoU.objects.create(name="nlnet-2026")
    Task.objects.create(mou=mou, name="1a", max_budget=1200.0, used_budget=50)
    Task.objects.create(mou=mou, name="1b", max_budget=300.0, used_budget=5)

    result = runner.invoke(app, ["task", "plot", "complete"])

    assert result.exit_code == 0, result.output
    rows = [line for line in result.output.splitlines() if line.startswith("1")]
    # The group line "1" first, with the totals of 1a and 1b.
    assert "55€ / 1500€ :" in rows[0]
    assert "  50€ / 1200€ :" in rows[1]
    assert "   5€ /  300€ :" in rows[2]
    assert rows[1].index("€") == rows[2].index("€")


def test_task_plot_complete_hours_replaces_euros_with_time(settings):
    settings.RFP_EUROS_PER_HOUR = 20.0
    mou = MoU.objects.create(name="nlnet-2026")
    Task.objects.create(mou=mou, name="1a", max_budget=1200.0, used_budget=50)
    Task.objects.create(mou=mou, name="1b", max_budget=300.0, used_budget=5)

    result = runner.invoke(app, ["task", "plot", "complete", "--hours"])

    assert result.exit_code == 0, result.output
    assert "€" not in result.output
    rows = [line for line in result.output.splitlines() if line.startswith("1")]
    assert " 2:30 / 60:00 :" in rows[1]
    assert " 0:15 / 15:00 :" in rows[2]


def test_mou_plot_weeks_aligns_bars_and_pads_dates_with_spaces(settings):
    _mou_for_plot_complete(settings, 10, 10)

    result = runner.invoke(app, ["mou", "plot", "weeks"])

    lines = result.output.splitlines()[1:]
    assert len({line.index(" : ") for line in lines}) == 1
    assert any(re.search(r"\( ?\d+\. ?\d+\.\d{4} - ", line) for line in lines)


def test_mou_plot_weeks_shows_actual_and_projected_this_week(settings, monkeypatch):
    import datetime as dt

    import nlnet_rfp_recorder.cli as cli

    settings.RFP_EUROS_PER_HOUR = 20.0
    monday = dt.datetime(2026, 10, 5)
    thursday_noon = monday + dt.timedelta(days=3.5)

    class FrozenDatetime(dt.datetime):
        @classmethod
        def now(cls, tz=None):
            return thursday_noon

    class FrozenDate(dt.date):
        @classmethod
        def today(cls):
            return thursday_noon.date()

    monkeypatch.setattr(cli, "datetime", FrozenDatetime)
    monkeypatch.setattr(cli, "date", FrozenDate)
    mou = MoU.objects.create(name="nlnet-2026", deadline=dt.date(2026, 12, 1))
    task = Task.objects.create(mou=mou, name="10a", max_budget=1000.0)
    link = Link.objects.create(task=task, url="https://example.com/issues/1")
    # 6h worked in the first 3.5 days of the week -> 12h projected for 7 days.
    TimeRecord.objects.create(
        link=link, start_time=monday, end_time=monday + dt.timedelta(hours=6)
    )

    result = runner.invoke(app, ["mou", "plot", "weeks"])

    assert result.exit_code == 0, result.output
    lines = result.output.splitlines()
    assert "6:00  this week (actual)" in lines[2]
    assert "12:00  this week (projected)" in lines[3]


def test_complete_task_name_only_offers_the_selected_mous_ids_and_aliases():
    from nlnet_rfp_recorder.cli import _complete_task_name

    other = MoU.objects.create(name="nlnet-2025")
    Task.objects.create(mou=other, name="10z")
    Alias.create("task", "10z", "old-thing", mou=other)
    mou = MoU.select("nlnet-2026")
    Task.objects.create(mou=mou, name="10a")
    Alias.create("task", "10a", "vcard", mou=mou)

    assert _complete_task_name("10") == ["10a"]
    assert _complete_task_name("v") == ["vcard"]
    assert _complete_task_name("old") == []


def test_complete_task_name_offers_nothing_without_a_selected_mou():
    from nlnet_rfp_recorder.cli import _complete_task_name

    mou = MoU.objects.create(name="nlnet-2026", selected=False)
    Task.objects.create(mou=mou, name="10a")

    assert _complete_task_name("10") == []


def test_task_plot_complete_marks_spending_over_the_budget(settings):
    settings.RFP_EUROS_PER_HOUR = 20.0
    mou = MoU.objects.create(name="nlnet-2026")
    Task.objects.create(
        mou=mou, name="10a", max_budget=400.0, used_budget=150
    ).set_budget(100.0)

    result = runner.invoke(app, ["task", "plot", "complete"], color=True)

    assert result.exit_code == 0, result.output
    assert "over budget" in result.output
    row = next(line for line in result.output.splitlines() if line.startswith("10a"))
    assert "\x1b[93m" in row  # light yellow: the 50€ over the 100€ budget


def test_task_plot_complete_amount_is_bright_green_when_all_is_submitted(settings):
    settings.RFP_EUROS_PER_HOUR = 20.0
    mou = MoU.objects.create(name="nlnet-2026")
    submitted = Task.objects.create(
        mou=mou, name="1a", max_budget=400.0, used_budget=80
    )
    submitted.set_budget(200.0)
    worked = Task.objects.create(mou=mou, name="2a", max_budget=400.0)
    worked.set_budget(200.0)
    link = Link.objects.create(task=worked, url="https://example.com/issues/1")
    now = timezone.now()
    TimeRecord.objects.create(
        link=link, start_time=now - timedelta(hours=2), end_time=now
    )

    result = runner.invoke(app, ["task", "plot", "complete"], color=True)

    assert result.exit_code == 0, result.output
    row_1a = next(line for line in result.output.splitlines() if line.startswith("1a"))
    row_2a = next(line for line in result.output.splitlines() if line.startswith("2a"))
    assert "\x1b[92m80€" in row_1a
    assert "\x1b[38;5;28m" in row_2a


def test_task_plot_complete_budget_amount_matches_used_when_none_is_left(settings):
    settings.RFP_EUROS_PER_HOUR = 20.0
    mou = MoU.objects.create(name="nlnet-2026")
    Task.objects.create(
        mou=mou, name="1a", max_budget=400.0, used_budget=200
    ).set_budget(200.0)
    Task.objects.create(
        mou=mou, name="2a", max_budget=400.0, used_budget=100
    ).set_budget(200.0)

    result = runner.invoke(app, ["task", "plot", "complete"], color=True)

    rows = {
        line[:2]: line
        for line in result.output.splitlines()
        if line[:2] in ("1a", "2a")
    }
    assert "\x1b[92m200€\x1b[0m / \x1b[92m200€" in rows["1a"]  # nothing left
    assert "\x1b[93m200€" not in rows["1a"]
    assert "/ \x1b[33m200€" in rows["2a"]  # 100€ left


def test_task_plot_complete_amounts_turn_yellow_and_red_when_over_budget(settings):
    settings.RFP_EUROS_PER_HOUR = 20.0
    mou = MoU.objects.create(name="nlnet-2026")
    over = Task.objects.create(mou=mou, name="1a", max_budget=400.0, used_budget=300)
    over.set_budget(200.0)

    result = runner.invoke(app, ["task", "plot", "complete"], color=True)

    row = next(line for line in result.output.splitlines() if line.startswith("1a"))
    assert "\x1b[93m300€\x1b[0m / \x1b[31m200€" in row


def test_task_plot_complete_percent_shows_percent_of_the_maximum():
    mou = MoU.objects.create(name="nlnet-2026")
    Task.objects.create(
        mou=mou, name="1a", max_budget=400.0, used_budget=100
    ).set_budget(200.0)

    result = runner.invoke(app, ["task", "plot", "complete", "--percent"])

    assert result.exit_code == 0, result.output
    assert "€" not in result.output
    rows = [line for line in result.output.splitlines() if line.startswith("1")]
    assert "25% / 50% :" in rows[1]


def test_task_plot_complete_rejects_hours_together_with_percent():
    MoU.objects.create(name="nlnet-2026")

    result = runner.invoke(app, ["task", "plot", "complete", "--hours", "--percent"])

    assert result.exit_code != 0


def test_stats_review_histograms_closed_issues_and_pull_requests():
    mou = MoU.objects.create(name="nlnet-2026", selected=True)
    task = Task.objects.create(mou=mou, name="10a")
    now = timezone.now()
    statuses = {}
    for url, minutes, status in [
        ("https://github.com/o/r/issues/1", 5, Status.CLOSED),
        ("https://github.com/o/r/issues/2", 45, Status.CLOSED),
        ("https://github.com/o/r/issues/3", 25, Status.OPEN),
        ("https://github.com/o/r/pull/4", 12, Status.CLOSED),
    ]:
        link = Link.objects.create(task=task, url=url)
        link.add_tag("review")
        statuses[url] = status
        TimeRecord.objects.create(
            link=link, start_time=now - timedelta(minutes=minutes), end_time=now
        )

    def fake_fetch(references, token=None):
        return [
            GitHubState(statuses[reference.url], None, "title")
            for reference in references
        ]

    with patch("nlnet_rfp_recorder.timetracking.models.link.fetch_states", fake_fetch):
        result = runner.invoke(app, ["stats", "review"])

    assert result.exit_code == 0, result.output
    assert "Closed issues (review) (2" in result.output
    assert "  0-10 min | █ 1" in result.output
    assert "..." in result.output
    assert "40-50 min | █ 1" in result.output
    assert "Closed pull requests (review) (1" in result.output
    assert "median: 0:25  average: 0:25" in result.output
    assert Link.objects.get(url="https://github.com/o/r/issues/1").closed_at
    assert Link.objects.get(url="https://github.com/o/r/issues/3").closed_at is None

    # Closed links are not asked about again.
    asked = []

    def spy(references, token=None):
        asked.extend(reference.url for reference in references)
        return [GitHubState(Status.OPEN, None, None) for _ in references]

    with patch("nlnet_rfp_recorder.timetracking.models.link.fetch_states", spy):
        again = runner.invoke(app, ["stats", "review"])
    assert again.exit_code == 0, again.output
    assert asked == ["https://github.com/o/r/issues/3"]
    assert "median: 0:25" in again.output
