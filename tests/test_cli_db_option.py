import os
import subprocess
import sys
from pathlib import Path

RFP = Path(sys.executable).with_name("rfp")


def run_rfp(*args: str, env: dict | None = None) -> subprocess.CompletedProcess:
    # A real subprocess is required here: Django settings (and the --db
    # option / RFP_DB env var that feed them) are only ever read once per
    # process, so this is the only way to verify --db actually routes to a
    # given file.
    return subprocess.run(
        [str(RFP), *args],
        capture_output=True,
        text=True,
        env={**os.environ, **(env or {})},
    )


def test_db_option_before_the_subcommand_is_used(tmp_path):
    db_path = tmp_path / "custom.sqlite3"
    result = run_rfp("--db", str(db_path), "migrate")

    assert result.returncode == 0, result.stderr
    assert db_path.is_file()


def test_db_option_after_the_subcommand_is_used(tmp_path):
    db_path = tmp_path / "custom.sqlite3"
    result = run_rfp("migrate", "--db", str(db_path))

    assert result.returncode == 0, result.stderr
    assert db_path.is_file()


def test_rfp_db_env_var_is_used_when_no_option_given(tmp_path):
    db_path = tmp_path / "custom.sqlite3"
    result = run_rfp("migrate", env={"RFP_DB": str(db_path)})

    assert result.returncode == 0, result.stderr
    assert db_path.is_file()
