import pytest
from typer.testing import CliRunner

from nlnet_rfp_recorder.cli import app
from nlnet_rfp_recorder.timetracking.models import Link, Task, TimeRecord

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


def test_task_selects_a_task():
    result = runner.invoke(app, ["task", "10a"])

    assert result.exit_code == 0, result.output
    task = Task.objects.get()
    assert task.name == "10a"
    assert task.selected is True


def test_task_rejects_an_invalid_name():
    result = runner.invoke(app, ["task", "Write the RfP"])

    assert result.exit_code != 0


def test_start_without_a_task_fails():
    result = runner.invoke(app, ["start", "https://example.com/issues/1"])

    assert result.exit_code != 0


def test_start_creates_a_time_entry_for_the_selected_task():
    runner.invoke(app, ["task", "10a"])
    result = runner.invoke(app, ["start", "https://example.com/issues/1"])

    assert result.exit_code == 0, result.output
    record = TimeRecord.objects.get()
    assert record.task.name == "10a"
    assert record.is_running is True
    assert [link.url for link in Link.objects.all()] == ["https://example.com/issues/1"]


def test_start_prints_the_previous_entry_being_stopped():
    runner.invoke(app, ["task", "10a"])
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
    runner.invoke(app, ["task", "10a"])
    runner.invoke(app, ["start", "https://example.com/issues/1"])
    result = runner.invoke(app, ["stop"])

    assert result.exit_code == 0, result.output
    record = TimeRecord.objects.get()
    assert record.is_running is False
