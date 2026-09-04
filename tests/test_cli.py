import sqlite3
import subprocess
import sys
from pathlib import Path

from typer.testing import CliRunner

from nlnet_rfp_recorder.cli import app

runner = CliRunner()

RFP = Path(sys.executable).with_name("rfp")


def run_rfp(db_path: Path, *args: str) -> subprocess.CompletedProcess:
    return subprocess.run(
        [str(RFP), "--db", str(db_path), *args],
        capture_output=True,
        text=True,
    )


def test_no_args_shows_help():
    result = runner.invoke(app, [])
    assert "Usage" in result.output


def test_version_command():
    result = runner.invoke(app, ["version"])
    assert result.exit_code == 0
    assert result.output.strip()


def test_migrate_creates_database_at_given_path(tmp_path):
    # Run in a fresh process: Django settings (and the --db override that
    # feeds them) are only ever read once per process.
    db_path = tmp_path / "custom.sqlite3"
    result = run_rfp(db_path, "migrate")

    assert result.returncode == 0, result.stderr
    assert db_path.is_file()


def test_task_selects_a_task(tmp_path):
    db_path = tmp_path / "custom.sqlite3"
    run_rfp(db_path, "migrate")
    result = run_rfp(db_path, "task", "10a")

    assert result.returncode == 0, result.stderr
    connection = sqlite3.connect(db_path)
    rows = connection.execute("SELECT name, selected FROM timetracking_task").fetchall()
    connection.close()
    assert rows == [("10a", 1)]


def test_task_rejects_an_invalid_name(tmp_path):
    db_path = tmp_path / "custom.sqlite3"
    run_rfp(db_path, "migrate")
    result = run_rfp(db_path, "task", "Write the RfP")

    assert result.returncode != 0


def test_start_without_a_task_fails(tmp_path):
    db_path = tmp_path / "custom.sqlite3"
    run_rfp(db_path, "migrate")
    result = run_rfp(db_path, "start", "https://example.com/issues/1")

    assert result.returncode != 0


def test_start_creates_a_time_entry_for_the_selected_task(tmp_path):
    db_path = tmp_path / "custom.sqlite3"
    run_rfp(db_path, "migrate")
    run_rfp(db_path, "task", "10a")
    result = run_rfp(db_path, "start", "https://example.com/issues/1")

    assert result.returncode == 0, result.stderr
    connection = sqlite3.connect(db_path)
    records = connection.execute(
        "SELECT task_id, end_time FROM timetracking_timerecord"
    ).fetchall()
    links = connection.execute("SELECT url FROM timetracking_link").fetchall()
    connection.close()
    assert records == [(1, None)]
    assert links == [("https://example.com/issues/1",)]


def test_stop_without_a_running_entry_succeeds(tmp_path):
    db_path = tmp_path / "custom.sqlite3"
    run_rfp(db_path, "migrate")
    result = run_rfp(db_path, "stop")

    assert result.returncode == 0, result.stderr


def test_stop_ends_the_running_time_entry(tmp_path):
    db_path = tmp_path / "custom.sqlite3"
    run_rfp(db_path, "migrate")
    run_rfp(db_path, "task", "10a")
    run_rfp(db_path, "start", "https://example.com/issues/1")
    result = run_rfp(db_path, "stop")

    assert result.returncode == 0, result.stderr
    connection = sqlite3.connect(db_path)
    (end_time,) = connection.execute(
        "SELECT end_time FROM timetracking_timerecord"
    ).fetchone()
    connection.close()
    assert end_time is not None
