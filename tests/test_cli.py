from datetime import timedelta
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from django.utils import timezone
from typer.testing import CliRunner

from nlnet_rfp_recorder.cli import app
from nlnet_rfp_recorder.timetracking.models import (
    GitHubToken,
    Link,
    MoU,
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


pytestmark = pytest.mark.django_db

runner = CliRunner()


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


def test_report_fails_without_rfp_euros(settings):
    settings.RFP_EUROS = None

    result = runner.invoke(app, ["report"])

    assert result.exit_code != 0


def test_report_prints_budget_issues_and_pull_requests(settings):
    settings.RFP_EUROS = 20.0
    task = Task.objects.create(name="10a")
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
        result = runner.invoke(app, ["report"])

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
    task = Task.objects.create(name="10a")
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
        result = runner.invoke(app, ["report"])

    assert result.exit_code == 0, result.output
    assert "Issues:" in result.output
    assert "- https://github.com/nlnet/rfp-recorder/issues/1" in result.output
    assert "Pull Requests:" not in result.output
    assert "- https://github.com/nlnet/rfp-recorder/pull/2" not in result.output
    # only the issue's 20 minutes count, not the open PR's
    assert "10a: 10€" in result.output


def test_report_fails_while_something_is_running(settings):
    settings.RFP_EUROS = 20.0
    task = Task.objects.create(name="10a")
    link = Link.objects.create(task=task, url="https://example.com/issues/1")
    TimeRecord.objects.create(link=link, start_time=timezone.now())

    result = runner.invoke(app, ["report"])

    assert result.exit_code != 0
    assert "https://example.com/issues/1" in result.output
    assert "still running" in result.output.lower()


def test_report_shows_review_tag_suffix(settings):
    settings.RFP_EUROS = 20.0
    task = Task.objects.create(name="10a")
    link = Link.objects.create(task=task, url="https://example.com/docs/design")
    link.add_tag("review")
    TimeRecord.objects.create(
        link=link,
        start_time=timezone.now() - timedelta(minutes=20),
        end_time=timezone.now(),
    )

    result = runner.invoke(app, ["report"])

    assert result.exit_code == 0, result.output
    assert "- https://example.com/docs/design (review)" in result.output


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
    result = runner.invoke(app, ["token", "secret"])

    assert result.exit_code == 0, result.output
    assert GitHubToken.get() == "secret"


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


def test_help_lists_commands_alphabetically():
    result = runner.invoke(app, ["--help"])

    assert result.exit_code == 0, result.output
    names = [
        "migrate",
        "mou",
        "report",
        "review",
        "start",
        "status",
        "stop",
        "task",
        "token",
        "version",
    ]
    positions = [result.output.index(f"│ {name} ") for name in names]
    assert positions == sorted(positions)


def test_mou_help_lists_subcommands_alphabetically():
    result = runner.invoke(app, ["mou", "--help"])

    assert result.exit_code == 0, result.output
    names = ["add", "budget", "list", "remove", "select", "status"]
    positions = [result.output.index(f"│ {name} ") for name in names]
    assert positions == sorted(positions)


def test_task_help_lists_subcommands_alphabetically():
    result = runner.invoke(app, ["task", "--help"])

    assert result.exit_code == 0, result.output
    names = ["list", "remove", "select", "set", "status"]
    positions = [result.output.index(f"│ {name} ") for name in names]
    assert positions == sorted(positions)
