import os
import platform
from pathlib import Path

import pytest

from nlnet_rfp_recorder.lock_diagnostics import find_processes_with_file_open

pytestmark = pytest.mark.skipif(
    platform.system() != "Linux", reason="reads /proc, Linux only"
)


def test_find_processes_with_file_open_finds_the_current_process(tmp_path):
    target = tmp_path / "locked.sqlite3"
    with open(target, "w"):
        holders = find_processes_with_file_open(target)

    assert any(holder.pid == os.getpid() for holder in holders)
    self_holder = next(holder for holder in holders if holder.pid == os.getpid())
    assert self_holder.comm  # some process name was read
    assert self_holder.mode in ("write", "read/write")


def test_find_processes_with_file_open_reports_the_access_mode():
    # Read this test file itself, read-only, from the current process.
    with open(__file__):
        holders = find_processes_with_file_open(Path(__file__))

    self_holder = next(holder for holder in holders if holder.pid == os.getpid())
    assert self_holder.mode == "read"


def test_find_processes_with_file_open_returns_empty_for_an_unopened_file(tmp_path):
    target = tmp_path / "never-opened.sqlite3"
    target.write_text("")

    assert find_processes_with_file_open(target) == []


def test_find_processes_with_file_open_matches_a_relative_path_to_the_same_file(
    tmp_path, monkeypatch
):
    target = tmp_path / "locked.sqlite3"
    monkeypatch.chdir(tmp_path)
    with open(target, "w"):
        holders = find_processes_with_file_open(Path("locked.sqlite3"))

    assert any(holder.pid == os.getpid() for holder in holders)


def test_find_processes_with_file_open_handles_a_nonexistent_path():
    assert find_processes_with_file_open(Path("/no/such/path/at/all")) == []


def test_find_processes_with_file_open_includes_the_full_cmdline(tmp_path):
    target = tmp_path / "locked.sqlite3"
    with open(target, "w"):
        holders = find_processes_with_file_open(target)

    self_holder = next(holder for holder in holders if holder.pid == os.getpid())
    assert self_holder.cmdline
    assert self_holder.user is not None
