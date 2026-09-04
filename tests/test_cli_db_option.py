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


def test_mou_migrates_a_fresh_database_without_an_explicit_migrate_call(tmp_path):
    db_path = tmp_path / "custom.sqlite3"
    result = run_rfp("--db", str(db_path), "mou", "list")

    assert result.returncode == 0, result.stderr
    assert db_path.is_file()


def test_backup_creates_a_timestamped_copy_next_to_the_database(tmp_path):
    db_path = tmp_path / "custom.sqlite3"
    run_rfp("--db", str(db_path), "migrate")

    result = run_rfp("--db", str(db_path), "backup")

    assert result.returncode == 0, result.stderr
    backups = [p for p in tmp_path.iterdir() if p != db_path]
    assert len(backups) == 1
    backup_path = backups[0]
    assert backup_path.name.startswith("custom-")
    assert backup_path.suffix == ".sqlite3"
    assert " " not in backup_path.name
    assert backup_path.read_bytes() == db_path.read_bytes()


def test_restore_copies_the_backup_back_over_the_database(tmp_path):
    db_path = tmp_path / "custom.sqlite3"
    run_rfp("--db", str(db_path), "mou", "add", "nlnet-2026")
    run_rfp("--db", str(db_path), "backup")
    backups = [p for p in tmp_path.iterdir() if p != db_path]
    assert len(backups) == 1
    backup_name = backups[0].stem

    run_rfp("--db", str(db_path), "mou", "add", "nlnet-2027")
    result = run_rfp("--db", str(db_path), "restore", backup_name)

    assert result.returncode == 0, result.stderr
    assert "Restored" in result.stdout
    list_result = run_rfp("--db", str(db_path), "mou", "list")
    assert "nlnet-2027" not in list_result.stdout
    assert "nlnet-2026" in list_result.stdout


def test_restore_accepts_the_full_backup_filename(tmp_path):
    db_path = tmp_path / "custom.sqlite3"
    run_rfp("--db", str(db_path), "mou", "add", "nlnet-2026")
    run_rfp("--db", str(db_path), "backup")
    backups = [p for p in tmp_path.iterdir() if p != db_path]
    backup_full_name = backups[0].name

    result = run_rfp("--db", str(db_path), "restore", backup_full_name)

    assert result.returncode == 0, result.stderr


def test_restore_fails_for_an_unknown_backup(tmp_path):
    db_path = tmp_path / "custom.sqlite3"
    run_rfp("--db", str(db_path), "migrate")

    result = run_rfp("--db", str(db_path), "restore", "does-not-exist")

    assert result.returncode != 0
    assert "No such backup" in result.stderr


def test_restore_itself_backs_up_the_pre_restore_database(tmp_path):
    db_path = tmp_path / "custom.sqlite3"
    run_rfp("--db", str(db_path), "mou", "add", "nlnet-2026")
    run_rfp("--db", str(db_path), "backup")
    backups_before = {p for p in tmp_path.iterdir() if p != db_path}
    backup_name = next(iter(backups_before)).stem

    result = run_rfp("--db", str(db_path), "restore", backup_name)

    assert result.returncode == 0, result.stderr
    backups_after = {p for p in tmp_path.iterdir() if p != db_path}
    assert len(backups_after) == 2
    assert backups_before < backups_after


def test_timesheet_import_backs_up_the_database_first(tmp_path):
    db_path = tmp_path / "custom.sqlite3"
    run_rfp("--db", str(db_path), "migrate")
    csv_path = tmp_path / "timesheet.csv"
    csv_path.write_text("pk,mou,task,start,duration,link,tags\n")

    result = run_rfp("--db", str(db_path), "timesheet", "import", str(csv_path))

    assert result.returncode == 0, result.stderr
    assert "Backed up database to" in result.stdout
    backups = [p for p in tmp_path.iterdir() if p not in (db_path, csv_path)]
    assert len(backups) == 1
    assert backups[0].name.startswith("custom-")
