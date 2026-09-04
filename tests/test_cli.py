import subprocess
import sys
from pathlib import Path

from typer.testing import CliRunner

from nlnet_rfp_recorder.cli import app

runner = CliRunner()


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
    rfp = Path(sys.executable).with_name("rfp")
    result = subprocess.run(
        [str(rfp), "--db", str(db_path), "migrate"],
        capture_output=True,
        text=True,
    )

    assert result.returncode == 0, result.stderr
    assert db_path.is_file()
