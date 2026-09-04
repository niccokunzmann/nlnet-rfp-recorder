import os
import sqlite3
import subprocess
import sys
import tempfile
from pathlib import Path

import pytest

RFP = Path(sys.executable).with_name("rfp")
TEST_DB_FILE = Path(tempfile.gettempdir()) / "rfp-test.sqlite3"


@pytest.fixture(autouse=True)
def _clean_test_db():
    TEST_DB_FILE.unlink(missing_ok=True)
    yield
    TEST_DB_FILE.unlink(missing_ok=True)


def run_rfp(*args: str, env: dict | None = None) -> subprocess.CompletedProcess:
    # A real subprocess is required here for the same reason as --db: Django
    # settings are only ever read once per process.
    return subprocess.run(
        [str(RFP), *args],
        capture_output=True,
        text=True,
        env={**os.environ, **(env or {})},
    )


def _task_names() -> set[str]:
    connection = sqlite3.connect(TEST_DB_FILE)
    names = {row[0] for row in connection.execute("SELECT name FROM timetracking_task")}
    connection.close()
    return names


def test_test_option_before_the_subcommand_seeds_fixtures():
    # Deliberately avoids `report`: it checks live GitHub status for every
    # issue/PR link, which is both slow and subject to rate limiting. This
    # only asserts on what our own code guarantees: the fixture tasks exist.
    result = run_rfp("--test", "task", "select", "12c")

    assert result.returncode == 0, result.stderr
    assert TEST_DB_FILE.is_file()
    assert {"10a", "11b", "12c"} <= _task_names()


def test_test_option_after_the_subcommand_seeds_fixtures():
    result = run_rfp("task", "select", "12c", "--test")

    assert result.returncode == 0, result.stderr
    assert TEST_DB_FILE.is_file()
    assert {"10a", "11b", "12c"} <= _task_names()


def test_test_option_persists_across_invocations():
    run_rfp("--test", "task", "select", "12c")
    result = run_rfp("--test", "task", "select", "13d")

    assert result.returncode == 0, result.stderr
    # the fixture tasks and the previously added one are all still there
    assert {"10a", "11b", "12c", "13d"} <= _task_names()
