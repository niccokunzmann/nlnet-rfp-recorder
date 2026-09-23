"""Linux-only diagnostics for a SQLite "database is locked" error.

sqlite3's own OperationalError names no culprit at all, so when it's
raised on Linux, cli.main() calls find_processes_with_file_open() to
scan /proc directly for every process with the database file open -
no dependency on lsof (or any other external tool) being installed.
"""

from __future__ import annotations

import pwd
from dataclasses import dataclass
from pathlib import Path


@dataclass
class FileHolder:
    """One process that has the database file open, as much detail
    about it as /proc exposes to this user.
    """

    pid: int
    comm: str
    cmdline: str
    user: str | None
    mode: str


def find_processes_with_file_open(path: Path) -> list[FileHolder]:
    """Every process (visible to this user) with `path` open right now.

    Resolves `path` once and compares each candidate fd's resolved
    target against it, so a different path string pointing at the same
    file (relative vs. absolute, a symlink, ...) still matches.
    Processes this user can't introspect (owned by someone else,
    without root) are silently skipped rather than reported as unknown
    - /proc simply denies access to their fd directory, the same way
    `ps`/`lsof` would.
    """
    try:
        target = path.resolve()
    except OSError:
        return []

    proc_dir = Path("/proc")
    try:
        pid_names = [entry.name for entry in proc_dir.iterdir() if entry.name.isdigit()]
    except OSError:
        return []

    holders: list[FileHolder] = []
    for pid_name in pid_names:
        fd_dir = proc_dir / pid_name / "fd"
        try:
            fd_entries = list(fd_dir.iterdir())
        except OSError:
            continue
        for fd_entry in fd_entries:
            try:
                resolved = fd_entry.resolve()
            except OSError:
                continue
            if resolved == target:
                holders.append(_describe_process(int(pid_name), fd_entry.name))
                break
    return holders


def _describe_process(pid: int, fd: str) -> FileHolder:
    proc_dir = Path("/proc") / str(pid)

    comm = "?"
    try:
        comm = (proc_dir / "comm").read_text().strip()
    except OSError:
        pass

    cmdline = comm
    try:
        raw = (proc_dir / "cmdline").read_bytes()
        parts = [part.decode(errors="replace") for part in raw.split(b"\0") if part]
        if parts:
            cmdline = " ".join(parts)
    except OSError:
        pass

    user = None
    try:
        user = pwd.getpwuid(proc_dir.stat().st_uid).pw_name
    except OSError, KeyError:
        pass

    # /proc/<pid>/fdinfo/<fd>'s "flags:" line is the fd's open() flags in
    # octal - the low 2 bits (O_ACCMODE) say read/write/both, same
    # encoding the C library itself uses.
    mode = "?"
    try:
        for line in (proc_dir / "fdinfo" / fd).read_text().splitlines():
            if line.startswith("flags:"):
                access = int(line.split(":", 1)[1].strip(), 8) & 0o3
                mode = {0: "read", 1: "write", 2: "read/write"}.get(access, "?")
                break
    except OSError:
        pass

    return FileHolder(pid=pid, comm=comm, cmdline=cmdline, user=user, mode=mode)
