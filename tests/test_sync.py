import json
import subprocess
import time
import uuid
from datetime import datetime, timedelta
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest
from django.db.utils import OperationalError
from typer.testing import CliRunner

from nlnet_rfp_recorder import cli
from nlnet_rfp_recorder.cli import app
from nlnet_rfp_recorder.sync import (
    LINK,
    TASK,
    TIME_RECORD,
    Remote,
    Sync,
    apply_entries,
    export_entries,
    read_file,
    serialize_link,
    write_file,
)
from nlnet_rfp_recorder.timetracking.models import (
    Alias,
    Link,
    MoU,
    Report,
    ReportTask,
    SyncDeletion,
    SyncSettings,
    Tag,
    Task,
    TimeRecord,
    sync_now,
)
from nlnet_rfp_recorder.timetracking.models.task import task_uuid

pytestmark = pytest.mark.django_db

runner = CliRunner()

EARLIER = "2000-01-01T00:00:00.000Z"
MIDDLE = "2020-01-01T00:00:00.000Z"
LATER = "2026-01-01T00:00:00.000Z"
OTHER = "other-computer"


@pytest.fixture(autouse=True)
def isolated(monkeypatch):
    """Keep the user's git config out, and reset what one invocation
    leaves for main() so tests don't leak into each other."""
    monkeypatch.setenv("GIT_CONFIG_GLOBAL", "/dev/null")
    monkeypatch.setenv("GIT_CONFIG_NOSYSTEM", "1")
    yield
    cli._sync_pending = False
    cli._no_sync = False


@pytest.fixture(autouse=True)
def berlin(monkeypatch):
    """This computer is in Berlin: UTC+1 in November."""
    monkeypatch.setenv("TZ", "Europe/Berlin")
    time.tzset()
    yield
    monkeypatch.undo()
    time.tzset()


@pytest.fixture
def machine():
    return SyncSettings.get().machine


@pytest.fixture
def repository(tmp_path):
    """A bare repository to sync with; the clone goes to tmp_path/work."""
    remote = tmp_path / "remote.git"
    subprocess.run(["git", "init", "--quiet", "--bare", str(remote)], check=True)
    settings = SyncSettings.get()
    settings.url = str(remote)
    settings.name = "Test"
    settings.email = "test@example.org"
    settings.save()
    with patch(
        "nlnet_rfp_recorder.sync.repository_path", return_value=tmp_path / "work"
    ):
        yield remote


def _record(url="https://github.com/o/r/issues/1", minutes=30) -> TimeRecord:
    link = Link.objects.create(url=url)
    start = datetime(2026, 11, 1, 9, 0)
    return TimeRecord.objects.create(
        link=link, start_time=start, end_time=start + timedelta(minutes=minutes)
    )


def _set_version(obj, modified: str, modified_by: str = "here") -> None:
    type(obj).objects.filter(pk=obj.pk).update(
        modified=modified, modified_by=modified_by
    )
    obj.refresh_from_db()


def _link_entry(uuid_text: str, modified: str, **data) -> dict:
    return {
        "model": LINK,
        "uuid": uuid_text,
        "modified": modified,
        "modified_by": OTHER,
        "deleted": False,
        "url": "https://github.com/o/r/issues/2",
        "mou": None,
        "task": None,
        "tags": [],
        "excluded_from_reports": False,
        "title": "",
        "closed_at": None,
        **data,
    }


def _record_entry(uuid_text: str, link_uuid: str | None, modified: str, **data):
    return {
        "model": TIME_RECORD,
        "uuid": uuid_text,
        "modified": modified,
        "modified_by": OTHER,
        "deleted": False,
        "link": link_uuid,
        "start_time": "2026-11-02T10:00:00+01:00",
        "end_time": "2026-11-02T11:00:00+01:00",
        "stop_reason": "user",
        "computer": OTHER,
        **data,
    }


def _deleted(model: str, uuid_text: str, modified: str) -> dict:
    return {
        "model": model,
        "uuid": uuid_text,
        "modified": modified,
        "modified_by": OTHER,
        "deleted": True,
    }


def _new_uuid() -> str:
    return str(uuid.uuid4())


def _by_uuid(entries: list[dict]) -> dict[str, dict]:
    return {entry["uuid"]: entry for entry in entries}


# --- the triggers stamp every change ---------------------------------------


def test_new_rows_are_marked_with_this_computer(machine):
    record = _record()
    record.refresh_from_db()

    assert record.modified_by == machine
    assert record.computer == machine
    assert record.link.modified_by == machine


def _save_stop_reason(record):
    record.stop_reason = "edit"
    record.save(update_fields=["stop_reason"])


def _bulk_update_start(record):
    record.start_time = datetime(2026, 11, 1, 8)
    TimeRecord.objects.bulk_update([record], ["start_time"])


@pytest.mark.parametrize(
    "change",
    [
        lambda r: TimeRecord.objects.filter(pk=r.pk).update(stop_reason="edit"),
        lambda r: TimeRecord.objects.filter(pk=r.pk).update(
            end_time=datetime(2026, 11, 1, 12, 0)
        ),
        lambda r: TimeRecord.objects.filter(pk=r.pk).update(link=None),
        _save_stop_reason,
        _bulk_update_start,
    ],
)
def test_every_way_of_changing_a_time_record_stamps_it(change, machine):
    record = _record()
    _set_version(record, EARLIER, "someone")

    change(record)

    record.refresh_from_db()
    assert record.modified > EARLIER
    assert record.modified_by == machine


def test_saving_without_a_change_does_not_stamp():
    record = _record()
    _set_version(record, EARLIER, "someone")

    record.save()

    record.refresh_from_db()
    assert (record.modified, record.modified_by) == (EARLIER, "someone")


def test_a_report_is_not_an_edit_of_its_time_records():
    record = _record()
    _set_version(record, EARLIER, "someone")

    Report.create(MoU.objects.create(name="nlnet-2026"))
    from nlnet_rfp_recorder.timetracking.models import ReportLine

    line = ReportLine.objects.create(report=Report.objects.get(), link=record.link)
    TimeRecord.objects.filter(pk=record.pk).update(report_line=line)

    record.refresh_from_db()
    assert record.modified == EARLIER


def test_tags_and_task_changes_stamp_a_link(machine):
    link = _record().link
    _set_version(link, EARLIER, "someone")
    link.tags.add(Tag.objects.get_or_create(name="review")[0])
    link.refresh_from_db()
    assert link.modified > EARLIER
    assert link.modified_by == machine

    _set_version(link, EARLIER, "someone")
    link.tags.clear()
    link.refresh_from_db()
    assert link.modified > EARLIER

    _set_version(link, EARLIER, "someone")
    mou = MoU.objects.create(name="nlnet-2026")
    Link.objects.filter(pk=link.pk).update(task=Task.objects.create(mou=mou, name="1a"))
    link.refresh_from_db()
    assert link.modified > EARLIER


def test_a_cached_github_title_is_not_an_edit():
    link = _record().link
    _set_version(link, EARLIER, "someone")

    Link.objects.filter(pk=link.pk).update(
        title="A title", closed_at=datetime(2026, 1, 1)
    )

    link.refresh_from_db()
    assert link.modified == EARLIER


def test_deleting_notes_the_deletion(machine):
    record = _record()

    record.delete()

    deletion = SyncDeletion.objects.get(model=TIME_RECORD)
    assert deletion.uuid == record.uuid.hex
    assert deletion.deleted_by == machine


def test_deleting_a_link_stamps_its_records():
    record = _record()
    _set_version(record, EARLIER, "someone")

    record.link.delete()

    record.refresh_from_db()
    assert record.link is None
    assert record.modified > EARLIER


def test_triggers_come_back_after_a_table_rebuild():
    from django.core.management import call_command
    from django.db import connection

    with connection.cursor() as cursor:
        cursor.execute("DROP TRIGGER rfp_sync_time_record_modified")
    call_command("migrate", verbosity=0)
    with connection.cursor() as cursor:
        cursor.execute(
            "SELECT name FROM sqlite_master WHERE name = %s",
            ["rfp_sync_time_record_modified"],
        )
        assert cursor.fetchone() is not None


# --- export ----------------------------------------------------------------


def test_export_has_the_version_and_the_computer(machine):
    record = _record()

    entries = export_entries(Remote())

    assert [entry["model"] for entry in entries] == [LINK, TIME_RECORD]
    assert entries[1]["uuid"] == str(record.uuid)
    assert entries[1]["modified_by"] == machine
    assert entries[1]["computer"] == machine
    assert entries[1]["start_time"] == "2026-11-01T09:00:00+01:00"
    assert entries[1]["deleted"] is False


def test_export_has_deletions_from_here_and_from_the_files():
    record = _record()
    record_uuid = str(record.uuid)
    record.delete()
    elsewhere = _deleted(TIME_RECORD, _new_uuid(), EARLIER)

    entries = _by_uuid(export_entries(Remote.of(elsewhere)))

    assert entries[record_uuid]["deleted"] is True
    assert entries[elsewhere["uuid"]] == elsewhere


def test_export_leaves_out_a_deletion_of_what_lives_here():
    record = _record()
    deleted = _deleted(TIME_RECORD, str(record.uuid), EARLIER)

    entries = _by_uuid(export_entries(Remote.of(deleted)))

    assert entries[str(record.uuid)]["deleted"] is False


# --- import ----------------------------------------------------------------


def test_remote_objects_are_created_with_their_version():
    link_uuid, record_uuid = _new_uuid(), _new_uuid()

    result = apply_entries(
        Remote.of(
            _link_entry(link_uuid, LATER, mou="nlnet-2026", task="1a", tags=["review"]),
            _record_entry(record_uuid, link_uuid, LATER),
        )
    )

    assert (result.changed, result.warnings) == (2, [])
    record = TimeRecord.objects.get(uuid=record_uuid)
    assert (record.modified, record.modified_by, record.computer) == (
        LATER,
        OTHER,
        OTHER,
    )
    assert (record.link.modified, record.link.modified_by) == (LATER, OTHER)
    # A MoU and task unknown here are made by name.
    assert (record.link.task.mou.name, record.link.task.name) == ("nlnet-2026", "1a")
    assert [tag.name for tag in record.link.tags.all()] == ["review"]
    assert record.end_time - record.start_time == timedelta(hours=1)
    # Importing again changes nothing.
    again = apply_entries(Remote.of(_record_entry(record_uuid, link_uuid, LATER)))
    assert again.total == 0


def test_the_newest_version_wins():
    record = _record()
    _set_version(record, MIDDLE)
    link_uuid = str(record.link.uuid)

    older = _record_entry(str(record.uuid), link_uuid, EARLIER, stop_reason="edit")
    apply_entries(Remote.of(older))
    record.refresh_from_db()
    assert record.stop_reason == "user"

    newer = _record_entry(str(record.uuid), link_uuid, LATER, stop_reason="edit")
    apply_entries(Remote.of(newer))
    record.refresh_from_db()
    assert record.stop_reason == "edit"
    assert record.modified == LATER


def test_an_edit_wins_by_when_it_was_made_not_when_it_was_synced():
    """The laptop edited at MIDDLE while offline and syncs now; the
    desktop edited at LATER. The desktop's edit wins on both."""
    record = _record()
    _set_version(record, MIDDLE)  # the laptop's own, older edit
    newer = _record_entry(
        str(record.uuid), str(record.link.uuid), LATER, stop_reason="edit"
    )

    apply_entries(Remote.of(newer))

    record.refresh_from_db()
    assert record.stop_reason == "edit"
    assert (
        _by_uuid(export_entries(Remote.of(newer)))[str(record.uuid)]["modified"]
        == LATER
    )


def test_the_same_time_is_decided_by_the_computer_id():
    record = _record()
    _set_version(record, LATER, "bbb")
    entry = _record_entry(str(record.uuid), str(record.link.uuid), LATER)

    apply_entries(Remote.of({**entry, "modified_by": "aaa", "stop_reason": "edit"}))
    record.refresh_from_db()
    assert record.stop_reason == "user"

    apply_entries(Remote.of({**entry, "modified_by": "zzz", "stop_reason": "edit"}))
    record.refresh_from_db()
    assert record.stop_reason == "edit"


def test_a_newer_deletion_deletes_and_an_older_one_does_not():
    record = _record()
    _set_version(record, MIDDLE)

    apply_entries(Remote.of(_deleted(TIME_RECORD, str(record.uuid), EARLIER)))
    assert TimeRecord.objects.filter(pk=record.pk).exists()

    result = apply_entries(Remote.of(_deleted(TIME_RECORD, str(record.uuid), LATER)))
    assert result.deleted == 1
    assert not TimeRecord.objects.filter(pk=record.pk).exists()
    # The deletion came from the files: it is not noted as one made here.
    assert not SyncDeletion.objects.exists()


def test_a_deletion_here_wins_over_an_older_remote_version():
    record = _record()
    record_uuid, link_uuid = str(record.uuid), str(record.link.uuid)
    record.delete()

    apply_entries(Remote.of(_record_entry(record_uuid, link_uuid, EARLIER)))

    assert not TimeRecord.objects.filter(uuid=record_uuid).exists()


def test_a_newer_edit_elsewhere_brings_back_what_was_deleted_here():
    record = _record()
    record_uuid, link_uuid = str(record.uuid), str(record.link.uuid)
    record.delete()
    SyncDeletion.objects.update(deleted_at=MIDDLE)

    apply_entries(Remote.of(_record_entry(record_uuid, link_uuid, LATER)))

    assert TimeRecord.objects.filter(uuid=record_uuid).exists()


def test_a_link_deleted_elsewhere_comes_back_when_time_was_added_here(machine):
    record = _record()
    link = record.link
    _set_version(link, EARLIER)
    deletion = _deleted(LINK, str(link.uuid), LATER)

    result = apply_entries(Remote.of(deletion))

    assert result.restored == 1
    link.refresh_from_db()
    assert link.modified > LATER
    assert link.modified_by == machine
    record.refresh_from_db()
    assert record.link == link
    # Its new version wins over the deletion on the other computers.
    exported = _by_uuid(export_entries(Remote.of(deletion)))
    assert exported[str(link.uuid)]["deleted"] is False


def test_a_link_without_time_records_is_deleted():
    link = Link.objects.create(url="https://x")
    _set_version(link, EARLIER)

    result = apply_entries(Remote.of(_deleted(LINK, str(link.uuid), LATER)))

    assert result.deleted == 1
    assert not Link.objects.exists()


def test_a_time_record_restores_its_link_deleted_here():
    """Edge case 5: a link deleted on this computer while the other one
    added time to it - the link comes back with the new time."""
    record = _record()
    link_entry = _link_entry(str(record.link.uuid), EARLIER, url=record.link.url)
    record.link.delete()
    record_entry = _record_entry(_new_uuid(), link_entry["uuid"], LATER)

    result = apply_entries(Remote.of(link_entry, record_entry))

    new_record = TimeRecord.objects.get(uuid=record_entry["uuid"])
    assert new_record.link is not None
    assert str(new_record.link.uuid) == link_entry["uuid"]
    assert result.restored == 1
    assert not SyncDeletion.objects.filter(model=LINK).exists()
    exported = _by_uuid(
        export_entries(Remote.of(_deleted(LINK, link_entry["uuid"], MIDDLE)))
    )
    assert exported[link_entry["uuid"]]["deleted"] is False


def test_a_time_record_restores_its_link_deleted_elsewhere():
    """Edge case 5, the other way round: the files say the link is deleted
    (newest version), but a time record pointing to it is new."""
    link_uuid = _new_uuid()
    remote = Remote.of(
        _link_entry(link_uuid, EARLIER),
        _deleted(LINK, link_uuid, MIDDLE),
        _record_entry(_new_uuid(), link_uuid, LATER),
    )

    result = apply_entries(remote)

    assert Link.objects.filter(uuid=link_uuid).exists()
    assert TimeRecord.objects.get().link.uuid == uuid.UUID(link_uuid)
    assert result.restored == 1


def test_a_time_record_with_a_link_in_no_file_is_skipped_not_lost():
    record_entry = _record_entry(_new_uuid(), _new_uuid(), LATER)

    result = apply_entries(Remote.of(record_entry))

    assert result.changed == 0
    assert "link" in result.warnings[0]
    # Nothing here is marked as seen: the next sync tries again.
    assert apply_entries(Remote.of(record_entry)).warnings


def test_a_reported_time_record_is_not_deleted_by_another_computer():
    from nlnet_rfp_recorder.timetracking.models import ReportLine

    record = _record()
    report = Report.create(MoU.objects.create(name="nlnet-2026"))
    line = ReportLine.objects.create(report=report, link=record.link)
    TimeRecord.objects.filter(pk=record.pk).update(report_line=line)
    _set_version(record, EARLIER)

    result = apply_entries(Remote.of(_deleted(TIME_RECORD, str(record.uuid), LATER)))

    assert TimeRecord.objects.filter(pk=record.pk).exists()
    assert result.restored == 1
    assert report.id in result.warnings[0]
    record.refresh_from_db()
    assert record.modified > LATER


def test_the_same_url_tracked_twice_becomes_one_link():
    record = _record(url="https://github.com/o/r/issues/2")
    local_uuid = str(record.link.uuid)
    smaller = "00000000-0000-4000-8000-000000000000"
    larger = "ffffffff-ffff-4fff-8fff-ffffffffffff"
    remote_record = _new_uuid()

    apply_entries(
        Remote.of(
            _link_entry(larger, LATER), _record_entry(remote_record, larger, LATER)
        )
    )
    assert Link.objects.count() == 1
    assert str(Link.objects.get().uuid) == local_uuid
    assert TimeRecord.objects.get(uuid=remote_record).link == record.link

    apply_entries(Remote.of(_link_entry(smaller, LATER)))
    assert Link.objects.count() == 1
    assert str(Link.objects.get().uuid) == smaller


def test_renaming_a_link_to_a_url_in_use_keeps_both():
    """Edge case 4: another computer renamed a link to a URL that is a
    link of its own here."""
    first = _record(url="https://github.com/o/r/issues/1").link
    second = _record(url="https://github.com/o/r/issues/2").link
    _set_version(first, EARLIER)

    result = apply_entries(
        Remote.of(_link_entry(str(first.uuid), LATER, url=second.url, title="T"))
    )

    first.refresh_from_db()
    assert first.url == "https://github.com/o/r/issues/1"
    assert first.title == "T"
    assert Link.objects.count() == 2
    assert "renamed" in result.warnings[0]


def test_the_same_entry_from_a_copied_database_is_kept_once():
    record = _record()  # 2026-11-01 9:00-9:30
    link_uuid = str(record.link.uuid)
    copy = {
        "start_time": "2026-11-01T09:00:00+01:00",
        "end_time": "2026-11-01T09:30:00+01:00",
    }
    smaller = "00000000-0000-4000-8000-000000000000"
    larger = "ffffffff-ffff-4fff-8fff-ffffffffffff"

    apply_entries(Remote.of(_record_entry(larger, link_uuid, LATER, **copy)))
    assert TimeRecord.objects.count() == 1
    assert TimeRecord.objects.get().uuid == record.uuid

    apply_entries(Remote.of(_record_entry(smaller, link_uuid, LATER, **copy)))
    assert TimeRecord.objects.count() == 1
    assert str(TimeRecord.objects.get().uuid) == smaller
    # Taking the other uuid is no deletion.
    assert not SyncDeletion.objects.exists()


def test_entries_that_differ_by_a_microsecond_are_both_kept():
    record = _record()

    apply_entries(
        Remote.of(
            _record_entry(
                _new_uuid(),
                str(record.link.uuid),
                LATER,
                start_time="2026-11-01T09:00:00.000001+01:00",
                end_time="2026-11-01T09:30:00+01:00",
            )
        )
    )

    assert TimeRecord.objects.count() == 2


@pytest.mark.parametrize(
    "broken",
    [
        {"start_time": None},
        {"start_time": "yesterday"},
        {"end_time": 5},
        {"link": "not-a-uuid"},
    ],
)
def test_a_broken_entry_is_skipped_and_the_rest_synced(broken):
    good = _record_entry(_new_uuid(), None, LATER)
    bad = _record_entry(_new_uuid(), None, LATER, **broken)

    result = apply_entries(Remote.of(bad, good))

    assert TimeRecord.objects.filter(uuid=good["uuid"]).exists()
    assert not TimeRecord.objects.filter(uuid=bad["uuid"]).exists()
    assert "could not be read" in result.warnings[0]


def test_unreadable_lines_are_left_out(tmp_path):
    good = _record_entry(_new_uuid(), None, LATER)
    path = tmp_path / "x.jsonl"
    path.write_text(
        "\n".join(
            [
                "not json",
                "[]",
                json.dumps({"model": "report"}),
                json.dumps({**good, "uuid": "nope"}),
                json.dumps({**good, "modified": 5}),
                json.dumps({k: v for k, v in good.items() if k != "modified"}),
                json.dumps(good),
            ]
        )
    )

    assert [entry["uuid"] for entry in read_file(path)] == [good["uuid"]]


def test_a_clock_ahead_is_warned_about():
    result = apply_entries(
        Remote.of(_record_entry(_new_uuid(), None, "2999-01-01T00:00:00.000Z"))
    )

    assert "clock of other-computer is ahead" in result.warnings[0]


def test_timers_running_on_two_computers_are_warned_about():
    TimeRecord.objects.create(start_time=datetime(2026, 11, 2, 9))

    result = apply_entries(
        Remote.of(_record_entry(_new_uuid(), None, LATER, end_time=None))
    )

    assert "several computers" in result.warnings[-1]


def test_the_background_task_leaves_another_computers_timer_running():
    apply_entries(Remote.of(_record_entry(_new_uuid(), None, LATER, end_time=None)))

    assert TimeRecord.get_running() is not None
    assert TimeRecord.get_running(here_only=True) is None
    assert TimeRecord.stop(reason="suspend", here_only=True) is None


def test_times_from_another_time_zone_are_converted():
    record_uuid, naive_uuid = _new_uuid(), _new_uuid()
    result = apply_entries(
        Remote.of(
            _record_entry(
                record_uuid,
                None,
                LATER,
                start_time="2026-11-02T10:00:00+00:00",  # London
                end_time="2026-11-02T05:30:00-05:00",  # New York
            ),
            _record_entry(
                naive_uuid,
                None,
                LATER,
                start_time="2026-11-03T10:00:00",  # no offset: local
                end_time=None,
            ),
        )
    )

    assert result.warnings == []
    record = TimeRecord.objects.get(uuid=record_uuid)
    assert record.start_time == datetime(2026, 11, 2, 11, 0)
    assert record.end_time == datetime(2026, 11, 2, 11, 30)
    assert TimeRecord.objects.get(uuid=naive_uuid).start_time == datetime(
        2026, 11, 3, 10, 0
    )


def test_newest_and_last_live_version_across_files(tmp_path):
    key = _new_uuid()
    write_file(tmp_path / "a.jsonl", [_link_entry(key, EARLIER, title="old")])
    write_file(tmp_path / "b.jsonl", [_deleted(LINK, key, LATER)])
    write_file(tmp_path / "c.jsonl", [_link_entry(key, MIDDLE, title="middle")])

    remote = Remote.read(sorted(tmp_path.glob("*.jsonl")))

    assert remote.newest[(LINK, key)]["deleted"] is True
    assert remote.live[(LINK, key)]["title"] == "middle"


def test_link_serialization_uses_names():
    mou = MoU.objects.create(name="nlnet-2026")
    link = Link.objects.create(
        url="https://x", task=Task.objects.create(mou=mou, name="1a")
    )
    link.tags.add(Tag.objects.get_or_create(name="review")[0])

    assert serialize_link(link)["mou"] == "nlnet-2026"
    assert serialize_link(link)["task"] == "1a"
    assert serialize_link(link)["tags"] == ["review"]


# --- git -------------------------------------------------------------------


def _git(*args: str) -> str:
    return subprocess.run(
        ["git", *args], capture_output=True, text=True, check=True
    ).stdout


def _other_computer_pushes(remote: Path, tmp_path: Path, entries: list[dict]) -> None:
    clone = tmp_path / f"other-{uuid.uuid4().hex[:6]}"
    subprocess.run(["git", "clone", "--quiet", str(remote), str(clone)], check=True)
    git = ["git", "-C", str(clone), "-c", "user.name=O", "-c", "user.email=o@o"]
    # The bare repository's HEAD may name another branch than rfp's main.
    subprocess.run(
        [*git, "checkout", "--quiet", "-B", "main", "origin/main"],
        capture_output=True,
    )
    write_file(clone / f"{OTHER}.jsonl", entries)
    subprocess.run([*git, "add", "."], check=True)
    subprocess.run([*git, "commit", "--quiet", "-m", "other"], check=True)
    subprocess.run([*git, "push", "--quiet", "origin", "HEAD:main"], check=True)


def test_push_and_pull_through_git(repository, tmp_path):
    record = _record()
    sync = Sync()

    assert sync.push() is True
    assert sync.push() is False  # nothing changed

    pushed = _git("-C", str(repository), "show", f"main:{sync.file_name}")
    assert str(record.uuid) in pushed
    author = _git("-C", str(repository), "log", "-1", "--format=%an <%ae>", "main")
    assert author.strip() == "Test <test@example.org>"

    link_uuid, record_uuid = _new_uuid(), _new_uuid()
    _other_computer_pushes(
        repository,
        tmp_path,
        [
            _link_entry(link_uuid, sync_now()),
            _record_entry(record_uuid, link_uuid, sync_now()),
            _deleted(TIME_RECORD, str(record.uuid), sync_now()),
        ],
    )

    result = Sync().pull()

    assert (result.changed, result.deleted) == (2, 1)
    assert TimeRecord.objects.filter(uuid=record_uuid).exists()
    assert not TimeRecord.objects.filter(pk=record.pk).exists()


def _pushed_deletions(sync: Sync) -> set[str]:
    path = sync.git.path / sync.file_name
    return {
        entry["uuid"]
        for entry in map(json.loads, path.read_text().splitlines())
        if entry["deleted"]
    }


def test_deletions_wait_in_the_database_until_pushed(repository):
    record = _record()
    Sync().push()
    record.delete()
    assert SyncDeletion.objects.count() == 1

    Sync().push()

    assert not SyncDeletion.objects.exists()
    assert str(record.uuid) in _pushed_deletions(Sync())
    # ...and stay in the file from then on.
    Sync().pull()
    _record(url="https://another")
    Sync().push()
    assert str(record.uuid) in _pushed_deletions(Sync())


def test_a_push_race_is_retried(repository, tmp_path):
    """Edge case 1: another computer pushes between this one's fetch and
    push. The push is rejected, the other's changes are taken, and the
    push is tried again - both files end up in the repository."""
    _record()
    sync = Sync()
    sync.pull()
    link_uuid = _new_uuid()
    _other_computer_pushes(repository, tmp_path, [_link_entry(link_uuid, sync_now())])

    assert sync.push() is True

    files = _git("-C", str(repository), "ls-tree", "--name-only", "main").split()
    assert sorted(files) == sorted([sync.file_name, f"{OTHER}.jsonl"])
    assert Link.objects.filter(uuid=link_uuid).exists()


def test_a_push_that_keeps_being_rejected_gives_up_and_loses_nothing(repository):
    from nlnet_rfp_recorder.sync import SyncError

    record = _record()
    with (
        patch("nlnet_rfp_recorder.sync.Git.push", return_value=False),
        pytest.raises(SyncError, match="rejected"),
    ):
        Sync().push()

    assert Sync().push() is True
    pushed = _git("-C", str(repository), "show", f"main:{Sync().file_name}")
    assert str(record.uuid) in pushed


def test_a_new_url_is_cloned_anew(repository, tmp_path):
    Sync().pull()
    other = tmp_path / "other.git"
    subprocess.run(["git", "init", "--quiet", "--bare", str(other)], check=True)
    settings = SyncSettings.get()
    settings.url = str(other)
    settings.save()

    sync = Sync()
    sync.pull()

    remote_url = _git("-C", str(sync.git.path), "remote", "get-url", "origin")
    assert remote_url.strip() == str(other)


def test_a_broken_clone_is_cloned_anew(repository, tmp_path):
    (tmp_path / "work").mkdir()
    (tmp_path / "work" / "junk").write_text("half a clone")

    Sync().pull()

    assert (tmp_path / "work" / ".git").is_dir()
    assert not (tmp_path / "work" / "junk").exists()


def test_an_empty_database_gets_everything(repository, tmp_path):
    link_uuid, record_uuid = _new_uuid(), _new_uuid()
    _other_computer_pushes(
        repository,
        tmp_path,
        [
            _link_entry(link_uuid, LATER, mou="nlnet-2026", task="1a"),
            _record_entry(record_uuid, link_uuid, LATER),
        ],
    )

    result = Sync().pull()

    assert (result.changed, result.warnings) == (2, [])
    assert TimeRecord.objects.get(uuid=record_uuid).link.task.name == "1a"


# --- the command -----------------------------------------------------------


def test_sync_settings():
    with patch("nlnet_rfp_recorder.cli._public_github_repository", return_value=None):
        result = runner.invoke(
            app,
            [
                "sync",
                "--set-url",
                "git@example.org:me/rfp.git",
                "--set-name",
                "Me",
                "--set-email",
                "me@example.org",
                "--set-machine",
                "laptop",
                "--auto",
                "no",
            ],
        )

    assert result.exit_code == 0, result.output
    settings = SyncSettings.get()
    assert (settings.url, settings.name, settings.email) == (
        "git@example.org:me/rfp.git",
        "Me",
        "me@example.org",
    )
    assert (settings.machine, settings.auto) == ("laptop", False)
    assert "This computer: laptop" in result.output


def test_auto_sync_is_on_by_default():
    assert SyncSettings.get().auto is True


def test_auto_takes_only_yes_or_no():
    result = runner.invoke(app, ["sync", "--auto", "maybe"])

    assert result.exit_code == 1
    assert SyncSettings.get().auto is True


def test_set_machine_keeps_this_computers_timers():
    record = TimeRecord.objects.create(start_time=datetime(2026, 11, 2, 9))

    result = runner.invoke(app, ["sync", "--set-machine", "desk"])

    assert result.exit_code == 0, result.output
    record.refresh_from_db()
    assert record.computer == "desk"
    assert TimeRecord.get_running(here_only=True) == record


@pytest.mark.parametrize("name", ["", "-x", "a/b", "a b", "x" * 65])
def test_set_machine_refuses_bad_ids(name):
    result = runner.invoke(app, ["sync", "--set-machine", name])

    assert result.exit_code == 1


def test_set_machine_refuses_another_computers_id(repository, tmp_path):
    _other_computer_pushes(repository, tmp_path, [])
    Sync().pull()

    result = runner.invoke(app, ["sync", "--set-machine", OTHER])

    assert result.exit_code == 1
    assert "Another computer" in result.output


def test_sync_lists_the_other_computers(repository, tmp_path):
    _other_computer_pushes(repository, tmp_path, [])
    Sync().pull()

    result = runner.invoke(app, ["sync", "--auto", "yes"])

    assert f"Other computers: {OTHER}" in result.output


def test_a_copied_database_gets_its_own_id():
    settings = SyncSettings.get()
    settings.machine, settings.host = "laptop", "laptop-host"
    settings.save()

    with patch("socket.gethostname", return_value="desktop-host"):
        copied = SyncSettings.get()

    assert copied.machine.startswith("desktop-host-")


def test_sync_refuses_a_public_github_repository():
    response = MagicMock(status_code=200)
    response.json = MagicMock(return_value={"private": False})
    with patch("niquests.get", return_value=response):
        result = runner.invoke(app, ["sync", "--set-url", "https://github.com/me/rfp"])

    assert result.exit_code == 1
    assert "public" in result.output
    assert SyncSettings.get().url == ""


def test_sync_without_repository_explains_set_url():
    result = runner.invoke(app, ["sync"])

    assert result.exit_code == 1
    assert "--set-url" in result.output


def test_sync_runs_mou_update_and_git(repository):
    _record()
    mou = MoU.objects.create(name="nlnet-2026", selected=True)
    mou.rfp_project, mou.rfp_secret = "project", "secret"
    mou.save()

    with (
        patch("nlnet_rfp_recorder.cli._update_from_rfp", return_value={}) as update,
        patch("nlnet_rfp_recorder.cli.shutil.copy2"),
    ):
        result = runner.invoke(app, ["sync"])

    assert result.exit_code == 0, result.output
    update.assert_called_once()
    assert "Pushed this computer's changes." in result.output


def test_every_command_pulls_and_main_pushes(repository, tmp_path):
    link_uuid, record_uuid = _new_uuid(), _new_uuid()
    _other_computer_pushes(
        repository,
        tmp_path,
        [_link_entry(link_uuid, LATER), _record_entry(record_uuid, link_uuid, LATER)],
    )

    result = runner.invoke(app, ["timesheet", "list"])

    assert result.exit_code == 0, result.output
    assert "issues/2" in result.output
    assert cli._sync_pending
    cli._push_after_command()
    assert not cli._sync_pending
    log = _git("-C", str(repository), "log", "--format=%s", "main")
    assert log.startswith("Sync ")


@pytest.mark.parametrize(
    "args",
    [
        ["timesheet", "list", "--no-sync"],
        ["timesheet", "--no-sync", "list"],
        ["--no-sync", "timesheet", "list"],
        ["status", "--no-sync"],
    ],
)
def test_no_sync_skips_the_sync(repository, args):
    with patch.object(Sync, "pull") as pull:
        result = runner.invoke(app, args)

    assert result.exit_code == 0, result.output
    pull.assert_not_called()
    assert not cli._sync_pending


@pytest.mark.parametrize(
    "args",
    [
        ["version"],
        ["token"],
        ["mou", "list"],
        ["mou", "add", "nlnet-2026"],
        ["mou", "export"],
        ["alias", "list", "url"],
        ["alias", "set", "url", "https://github.com/o/r", "r"],
        ["alias", "set", "mou", "nlnet-2026", "n"],
        ["alias", "set", "task", "1a", "api", "--no-sync"],
        ["migrate"],
    ],
)
def test_commands_without_synced_data_do_not_sync(repository, args):
    """Only links, time records and tasks are synced: commands that neither
    read nor change them have nothing to sync."""
    _task("1a", mou_name="nlnet-2026")
    MoU.objects.filter(name="nlnet-2026").update(selected=True)
    with patch.object(Sync, "pull") as pull:
        result = runner.invoke(app, args)

    assert result.exit_code == 0, result.output
    pull.assert_not_called()
    assert not cli._sync_pending


@pytest.mark.parametrize(
    "args",
    [
        ["alias", "list"],
        ["alias", "list", "task"],
        ["alias", "set", "task", "1a", "api"],
    ],
)
def test_commands_on_task_aliases_sync(repository, args):
    _task("1a", mou_name="nlnet-2026")
    MoU.objects.filter(name="nlnet-2026").update(selected=True)
    with patch.object(Sync, "pull", return_value=MagicMock(total=0, warnings=[])):
        with patch.object(Sync, "fetch"):
            result = runner.invoke(app, args)

    assert result.exit_code == 0, result.output
    assert cli._sync_pending


def test_no_sync_lasts_one_invocation(repository):
    runner.invoke(app, ["--no-sync", "timesheet", "list"])
    with patch.object(Sync, "pull") as pull:
        runner.invoke(app, ["timesheet", "list"])

    pull.assert_called_once()


def test_auto_no_turns_the_sync_around_commands_off(repository):
    runner.invoke(app, ["sync", "--auto", "no"])

    with patch.object(Sync, "pull") as pull:
        result = runner.invoke(app, ["timesheet", "list"])

    assert result.exit_code == 0, result.output
    pull.assert_not_called()


def test_ctrl_c_skips_the_pull_not_the_command(repository):
    with patch.object(Sync, "pull", side_effect=KeyboardInterrupt):
        result = runner.invoke(app, ["timesheet", "list"])

    assert result.exit_code == 0, result.output
    assert "Sync interrupted." in result.output


def test_ctrl_c_during_the_push_after_a_command(repository, capsys):
    """Edge case 2: Ctrl+C while main() pushes after the command."""
    cli._sync_pending = True
    with patch.object(Sync, "push", side_effect=KeyboardInterrupt):
        cli._push_after_command()

    assert "Sync interrupted." in capsys.readouterr().err
    assert not cli._sync_pending


def test_main_pushes_even_when_the_command_fails(repository):
    with (
        patch.object(Sync, "pull"),
        patch.object(Sync, "push") as push,
        patch("sys.argv", ["rfp", "timesheet", "remove", "999"]),
        pytest.raises(SystemExit),
    ):
        cli.main()

    push.assert_called_once()


def test_a_locked_database_skips_the_sync(repository):
    """Edge case 3: the background task holds the database."""
    with patch.object(Sync, "pull", side_effect=OperationalError("database is locked")):
        result = runner.invoke(app, ["timesheet", "list"])

    assert result.exit_code == 0, result.output
    assert "Sync skipped: database is locked" in result.output


def test_an_unreachable_repository_skips_the_sync(tmp_path):
    settings = SyncSettings.get()
    settings.url = str(tmp_path / "missing.git")
    settings.save()

    with patch(
        "nlnet_rfp_recorder.sync.repository_path", return_value=tmp_path / "work"
    ):
        result = runner.invoke(app, ["timesheet", "list"])

    assert result.exit_code == 0, result.output
    assert "Sync skipped:" in result.output
    assert not (tmp_path / "work").exists()


# --- tasks and their budgets -------------------------------------------------


def _task(name="1a", mou_name="nlnet-2026", **values) -> Task:
    mou, _ = MoU.objects.get_or_create(name=mou_name)
    return Task.objects.create(mou=mou, name=name, selected=False, **values)


def _set_field_versions(task: Task, **versions) -> None:
    """Give values a version as [when, computer] - None removes it."""
    field_versions = dict(task.field_versions)
    for name, value in versions.items():
        if value is None:
            field_versions.pop(name, None)
        else:
            field_versions[name] = list(value)
    Task.objects.filter(pk=task.pk).update(field_versions=field_versions)
    task.refresh_from_db()


def _task_entry(
    modified: str, name="1a", mou_name="nlnet-2026", versions=None, **data
) -> dict:
    """A task from the other computer; every value given in `data` has
    the version `modified` unless `versions` says otherwise."""
    if versions is None:
        versions = {field: [modified, OTHER] for field in data}
    return {
        "model": TASK,
        "uuid": str(task_uuid(mou_name, name)),
        "modified": modified,
        "modified_by": OTHER,
        "deleted": False,
        "mou": mou_name,
        "task": name,
        "max_budget": None,
        "remaining_budget": None,
        "used_budget": 0.0,
        "personal_budget": None,
        "description": "",
        "versions": versions,
        **data,
    }


def _get_task(name="1a", mou_name="nlnet-2026") -> Task:
    return Task.objects.get(mou__name=mou_name, name=name)


def test_the_same_task_has_the_same_uuid_on_every_computer():
    assert _task().uuid == task_uuid("nlnet-2026", "1a")
    assert _task("1b").uuid != task_uuid("nlnet-2026", "1a")
    assert _task(mou_name="other").uuid != task_uuid("nlnet-2026", "1a")


# The triggers


def test_a_new_task_dates_the_values_it_has(machine):
    task = _task(max_budget=500.0, description="Do it")
    task.refresh_from_db()

    assert set(task.field_versions) == {"max_budget", "description"}
    assert task.field_versions["max_budget"][1] == machine
    assert task.modified_by == machine


def test_a_new_task_without_values_has_no_versions():
    """Defaults - no budget, 0 used, no description - are not known
    values: they must never win over a budget from elsewhere."""
    task = _task()
    task.refresh_from_db()

    assert task.field_versions == {}


def _update_personal_budget(task):
    Task.objects.filter(pk=task.pk).update(personal_budget=100)


def _save_personal_budget(task):
    task.personal_budget = 100
    task.save()


def _set_budget(task):
    task.set_budget(100)


@pytest.mark.parametrize(
    "change", [_update_personal_budget, _save_personal_budget, _set_budget]
)
def test_every_way_of_changing_a_value_dates_it(change, machine):
    task = _task(max_budget=500.0)
    _set_version(task, EARLIER, "someone")
    _set_field_versions(task, max_budget=(EARLIER, "someone"))

    change(task)

    task.refresh_from_db()
    assert task.personal_budget == 100
    assert task.field_versions["personal_budget"][0] > EARLIER
    assert task.field_versions["personal_budget"][1] == machine
    assert task.field_versions["max_budget"] == [EARLIER, "someone"]
    assert task.modified > EARLIER


def test_clearing_a_value_dates_it():
    task = _task(personal_budget=300.0)
    _set_field_versions(task, personal_budget=(EARLIER, "someone"))

    Task.objects.filter(pk=task.pk).update(personal_budget=None)

    task.refresh_from_db()
    assert task.field_versions["personal_budget"][0] > EARLIER


def test_selecting_a_task_is_not_a_change():
    task = _task(max_budget=500.0)
    _set_version(task, EARLIER, "someone")
    _set_field_versions(task, max_budget=(EARLIER, "someone"))

    Task.objects.filter(pk=task.pk).update(selected=True)
    task.selected = False
    task.save()

    task.refresh_from_db()
    assert task.modified == EARLIER
    assert task.field_versions == {"max_budget": [EARLIER, "someone"]}


def test_saving_a_task_loaded_before_does_not_lose_versions():
    """A task object from before a change still has the old versions in
    memory: saving it must not write them back."""
    task = _task()
    stale = Task.objects.get(pk=task.pk)
    Task.objects.filter(pk=task.pk).update(personal_budget=100)

    stale.description = "Do it"
    stale.save()

    task.refresh_from_db()
    assert set(task.field_versions) == {"personal_budget", "description"}


def test_removing_the_mou_notes_its_tasks_as_deleted():
    """A task without MoU is not synced: the others must not think it
    is still there - and this computer must not take it back."""
    task = _task(max_budget=500.0)

    MoU.objects.all().delete()

    assert SyncDeletion.objects.filter(model=TASK, uuid=task.uuid.hex).exists()


def test_deleting_a_task_is_noted_for_the_others():
    task = _task()

    task.delete()

    assert SyncDeletion.objects.filter(model=TASK, uuid=task.uuid.hex).exists()


def test_deleting_a_task_without_mou_is_not_noted():
    task = _task()
    MoU.objects.all().delete()
    SyncDeletion.objects.all().delete()

    Task.objects.all().delete()

    assert not SyncDeletion.objects.filter(model=TASK, uuid=task.uuid.hex).exists()


# Export


def test_tasks_are_exported_with_their_values_and_versions():
    task = _task(max_budget=500.0, personal_budget=300.0, description="Do it")
    task.refresh_from_db()

    entry = _by_uuid(export_entries(Remote()))[str(task.uuid)]

    assert entry["model"] == TASK
    assert (entry["mou"], entry["task"]) == ("nlnet-2026", "1a")
    assert entry["max_budget"] == 500.0
    assert entry["personal_budget"] == 300.0
    assert entry["description"] == "Do it"
    assert entry["remaining_budget"] is None
    assert set(entry["versions"]) == {"max_budget", "personal_budget", "description"}


def test_a_task_without_mou_is_exported_as_deleted():
    task = _task()
    MoU.objects.all().delete()

    assert _by_uuid(export_entries(Remote()))[str(task.uuid)]["deleted"]


# Merging


def test_a_new_task_comes_with_its_values():
    result = apply_entries(
        Remote.of(_task_entry(LATER, max_budget=500.0, personal_budget=300.0))
    )

    task = _get_task()
    assert (task.max_budget, task.personal_budget) == (500.0, 300.0)
    assert task.field_versions["max_budget"] == [LATER, OTHER]
    assert not task.selected
    assert (task.modified, task.modified_by) == (LATER, OTHER)
    assert result.changed == 1


def test_a_newer_value_wins():
    task = _task(personal_budget=100.0)
    _set_field_versions(task, personal_budget=(EARLIER, "here"))

    apply_entries(Remote.of(_task_entry(LATER, personal_budget=200.0)))

    task.refresh_from_db()
    assert task.personal_budget == 200.0
    assert task.field_versions["personal_budget"] == [LATER, OTHER]


def test_an_older_value_loses():
    task = _task(personal_budget=100.0)
    _set_field_versions(task, personal_budget=(LATER, "here"))

    result = apply_entries(Remote.of(_task_entry(EARLIER, personal_budget=200.0)))

    task.refresh_from_db()
    assert task.personal_budget == 100.0
    assert task.field_versions["personal_budget"] == [LATER, "here"]
    assert result.changed == 0


def test_a_value_never_set_there_does_not_wipe_it_here():
    """The other computer never had the budget - its task is newer, but
    without a version for the budget it does not touch the one here."""
    task = _task(max_budget=500.0, personal_budget=300.0, used_budget=50.0)
    _set_version(task, EARLIER)
    _set_field_versions(
        task,
        max_budget=(EARLIER, "here"),
        personal_budget=(EARLIER, "here"),
        used_budget=(EARLIER, "here"),
    )

    apply_entries(Remote.of(_task_entry(LATER, description="From NLnet")))

    task.refresh_from_db()
    assert (task.max_budget, task.personal_budget, task.used_budget) == (
        500.0,
        300.0,
        50.0,
    )
    assert task.description == "From NLnet"


def test_values_are_merged_one_by_one():
    task = _task(max_budget=500.0, personal_budget=300.0)
    _set_field_versions(
        task, max_budget=(EARLIER, "here"), personal_budget=(LATER, "here")
    )

    apply_entries(
        Remote.of(
            _task_entry(
                MIDDLE,
                max_budget=600.0,
                personal_budget=100.0,
            )
        )
    )

    task.refresh_from_db()
    assert (task.max_budget, task.personal_budget) == (600.0, 300.0)


def test_a_value_cleared_there_later_is_cleared_here():
    task = _task(personal_budget=300.0)
    _set_field_versions(task, personal_budget=(EARLIER, "here"))

    apply_entries(Remote.of(_task_entry(LATER, personal_budget=None)))

    task.refresh_from_db()
    assert task.personal_budget is None


def test_a_value_cleared_here_later_stays_cleared():
    task = _task()
    _set_field_versions(task, personal_budget=(LATER, "here"))

    apply_entries(Remote.of(_task_entry(EARLIER, personal_budget=300.0)))

    task.refresh_from_db()
    assert task.personal_budget is None


def test_a_used_budget_of_0_from_nlnet_counts():
    task = _task(used_budget=50.0)
    _set_field_versions(task, used_budget=(EARLIER, "here"))

    apply_entries(Remote.of(_task_entry(LATER, used_budget=0.0)))

    task.refresh_from_db()
    assert task.used_budget == 0.0


def test_equal_times_are_decided_by_the_computer():
    task = _task(personal_budget=100.0)
    _set_field_versions(task, personal_budget=(LATER, "a-computer"))

    apply_entries(
        Remote.of(
            _task_entry(
                LATER,
                personal_budget=200.0,
                versions={"personal_budget": [LATER, "z-computer"]},
            )
        )
    )

    task.refresh_from_db()
    assert task.personal_budget == 200.0


def test_merging_twice_changes_nothing_the_second_time():
    remote = Remote.of(_task_entry(LATER, max_budget=500.0))
    apply_entries(remote)

    assert apply_entries(remote).changed == 0


def test_versions_that_are_none_are_ignored():
    task = _task(personal_budget=100.0)
    _set_field_versions(task, personal_budget=(EARLIER, "here"))

    result = apply_entries(
        Remote.of(
            _task_entry(
                LATER,
                personal_budget=200.0,
                versions={"personal_budget": "yesterday", "max_budget": [1, 2]},
            ),
        )
    )

    task.refresh_from_db()
    assert task.personal_budget == 100.0
    assert not result.warnings


def test_a_value_that_is_no_number_is_skipped_with_a_warning():
    task = _task(personal_budget=100.0)
    _set_field_versions(task, personal_budget=(EARLIER, "here"))

    result = apply_entries(Remote.of(_task_entry(LATER, personal_budget="lots")))

    task.refresh_from_db()
    assert task.personal_budget == 100.0
    assert result.warnings


def test_an_entry_without_versions_changes_no_value():
    """An entry that comes without versions does not know any value."""
    task = _task(personal_budget=100.0)
    entry = _task_entry(LATER, personal_budget=200.0)
    del entry["versions"]

    apply_entries(Remote.of(entry))

    task.refresh_from_db()
    assert task.personal_budget == 100.0


def test_a_task_made_by_a_synced_link_takes_the_budget():
    """Links make a task by name - with no budget. That task must not
    win over the budget of the computer that has it."""
    apply_entries(
        Remote.of(
            _link_entry(_new_uuid(), LATER, mou="nlnet-2026", task="1a"),
            _task_entry(EARLIER, personal_budget=300.0),
        )
    )

    task = _get_task()
    assert task.personal_budget == 300.0
    assert not task.selected


def test_a_task_made_by_a_link_here_takes_the_budget():
    _record()
    link = Link.objects.get()
    link.task = _task()
    link.save()

    apply_entries(Remote.of(_task_entry(EARLIER, max_budget=500.0)))

    assert _get_task().max_budget == 500.0


def test_a_task_deleted_here_is_not_made_again():
    task = _task(max_budget=500.0)
    task.delete()

    apply_entries(Remote.of(_task_entry(EARLIER, max_budget=500.0)))

    assert not Task.objects.exists()


def test_a_task_deleted_here_comes_back_if_changed_there_later():
    task = _task(max_budget=500.0)
    task.delete()

    apply_entries(Remote.of(_task_entry("2999-01-01T00:00:00.000Z", max_budget=1.0)))

    assert _get_task().max_budget == 1.0


def test_a_task_deleted_there_is_not_made_here():
    entry = _task_entry(EARLIER, max_budget=500.0)

    result = apply_entries(Remote.of(entry, _deleted(TASK, entry["uuid"], LATER)))

    assert not Task.objects.exists()
    assert result.total == 0


def test_the_mou_of_a_task_removed_here_is_not_made_again():
    _task(max_budget=500.0)
    MoU.objects.all().delete()

    apply_entries(Remote.of(_task_entry(EARLIER, max_budget=500.0)))

    assert not MoU.objects.exists()


# Deleting


def test_a_deleted_task_is_deleted():
    task = _task()
    _set_version(task, EARLIER)

    result = apply_entries(Remote.of(_deleted(TASK, str(task.uuid), LATER)))

    assert not Task.objects.exists()
    assert result.deleted == 1
    assert not SyncDeletion.objects.exists()


def test_a_task_changed_here_after_its_deletion_is_kept():
    task = _task()
    _set_version(task, LATER)

    apply_entries(Remote.of(_deleted(TASK, str(task.uuid), EARLIER)))

    assert Task.objects.exists()


def test_a_deleted_task_with_links_here_is_kept():
    task = _task()
    Link.objects.create(url="https://github.com/o/r/issues/1", task=task)
    _set_version(task, EARLIER)

    result = apply_entries(Remote.of(_deleted(TASK, str(task.uuid), LATER)))

    task.refresh_from_db()
    assert task.modified > LATER
    assert result.restored == 1


def test_a_deleted_task_in_a_report_here_is_kept():
    task = _task()
    report = Report.objects.create(id="report-1", mou=task.mou)
    ReportTask.objects.create(report=report, task=task, budget=10)
    _set_version(task, EARLIER)

    apply_entries(Remote.of(_deleted(TASK, str(task.uuid), LATER)))

    assert Task.objects.filter(pk=task.pk).exists()


def test_a_deleted_task_keeps_the_values_of_a_later_version_there():
    """Deleted at MIDDLE, but its budget changed before - the values
    are merged first, then the deletion only applies if it is newer."""
    task = _task()
    _set_version(task, EARLIER)
    entry = _task_entry(LATER, max_budget=500.0)

    apply_entries(Remote.of(entry, _deleted(TASK, entry["uuid"], MIDDLE)))

    task.refresh_from_db()
    assert task.max_budget == 500.0


# Through git


def test_task_budgets_go_from_one_computer_to_the_other(repository, tmp_path):
    """This computer has the budget, the other one made the task later
    without it: after the sync, both have the budget."""
    task = _task(max_budget=500.0, personal_budget=300.0)
    _set_version(task, EARLIER, "computer-a")
    _other_computer_pushes(
        repository, tmp_path, [_task_entry(LATER, description="From NLnet")]
    )

    sync = Sync(interactive=False)
    sync.pull()
    sync.push()

    task.refresh_from_db()
    assert (task.max_budget, task.personal_budget) == (500.0, 300.0)
    assert task.description == "From NLnet"
    pushed = _by_uuid(read_file(sync.git.path / sync.file_name))[str(task.uuid)]
    assert (pushed["max_budget"], pushed["personal_budget"]) == (500.0, 300.0)
    assert pushed["description"] == "From NLnet"
    assert pushed["versions"]["description"] == [LATER, OTHER]


def test_a_task_from_another_computer_is_not_pushed_back_changed(repository, tmp_path):
    entry = _task_entry(LATER, max_budget=500.0)
    _other_computer_pushes(repository, tmp_path, [entry])

    sync = Sync(interactive=False)
    sync.pull()
    sync.push()

    pushed = _by_uuid(read_file(sync.git.path / sync.file_name))[entry["uuid"]]
    assert pushed["versions"] == entry["versions"]
    assert (pushed["modified"], pushed["modified_by"]) == (LATER, OTHER)


# --- task aliases ------------------------------------------------------------


def _alias(task: Task, alias: str) -> Alias:
    return Alias.create("task", task.name, alias, mou=task.mou)


def _aliases(mou_name="nlnet-2026") -> dict[str, str]:
    """task name -> alias"""
    return dict(
        Alias.objects.filter(item_type="task", mou__name=mou_name).values_list(
            "target", "alias"
        )
    )


def test_giving_a_task_an_alias_dates_it(machine):
    task = _task()
    _set_version(task, EARLIER, "someone")

    _alias(task, "api")

    task.refresh_from_db()
    assert task.field_versions["alias"][1] == machine
    assert task.modified > EARLIER


@pytest.mark.parametrize(
    "change",
    [
        lambda: Alias.objects.filter(alias="api").delete(),
        lambda: Alias.objects.filter(alias="api").update(alias="web"),
    ],
)
def test_removing_or_renaming_an_alias_dates_it(change):
    task = _task()
    _alias(task, "api")
    _set_field_versions(task, alias=(EARLIER, "someone"))

    change()

    task.refresh_from_db()
    assert task.field_versions["alias"][0] > EARLIER


def test_moving_an_alias_dates_both_tasks():
    first, second = _task("1a"), _task("1b")
    _alias(first, "api")
    _set_field_versions(first, alias=(EARLIER, "someone"))

    _alias(second, "api")

    first.refresh_from_db()
    second.refresh_from_db()
    assert first.field_versions["alias"][0] > EARLIER
    assert "alias" in second.field_versions


def test_setting_the_same_alias_again_is_no_change():
    """`rfp task import` sets every alias in the file again."""
    task = _task()
    _alias(task, "api")
    _set_field_versions(task, alias=(EARLIER, "someone"))

    _alias(task, "api")

    task.refresh_from_db()
    assert task.field_versions["alias"] == [EARLIER, "someone"]


def test_mou_and_url_aliases_date_no_task():
    task = _task()
    _set_version(task, EARLIER, "someone")

    Alias.create("mou", "nlnet-2026", "n")
    Alias.create("url", "https://github.com/o/r", "r")

    task.refresh_from_db()
    assert "alias" not in task.field_versions
    assert task.modified == EARLIER


def test_a_task_is_exported_with_its_alias():
    task = _task()
    _alias(task, "api")
    task.refresh_from_db()

    entry = _by_uuid(export_entries(Remote()))[str(task.uuid)]

    assert entry["alias"] == "api"
    assert entry["versions"]["alias"] == task.field_versions["alias"]


def test_a_task_without_alias_exports_none_without_version():
    task = _task()

    entry = _by_uuid(export_entries(Remote()))[str(task.uuid)]

    assert entry["alias"] is None
    assert "alias" not in entry["versions"]


def test_a_new_task_comes_with_its_alias():
    result = apply_entries(Remote.of(_task_entry(LATER, alias="api")))

    task = _get_task()
    assert task.alias == "api"
    assert task.field_versions["alias"] == [LATER, OTHER]
    assert (task.modified, task.modified_by) == (LATER, OTHER)
    assert result.changed == 1


def test_a_newer_alias_wins():
    task = _task()
    _alias(task, "api")
    _set_version(task, EARLIER)
    _set_field_versions(task, alias=(EARLIER, "here"))

    apply_entries(Remote.of(_task_entry(LATER, alias="web")))

    task.refresh_from_db()
    assert _aliases() == {"1a": "web"}
    assert task.field_versions["alias"] == [LATER, OTHER]
    assert (task.modified, task.modified_by) == (LATER, OTHER)


def test_an_older_alias_loses():
    task = _task()
    _alias(task, "api")
    _set_field_versions(task, alias=(LATER, "here"))

    result = apply_entries(Remote.of(_task_entry(EARLIER, alias="web")))

    assert _aliases() == {"1a": "api"}
    assert result.changed == 0


def test_an_alias_removed_there_later_is_removed_here():
    task = _task()
    _alias(task, "api")
    _set_field_versions(task, alias=(EARLIER, "here"))

    apply_entries(Remote.of(_task_entry(LATER, alias=None)))

    task.refresh_from_db()
    assert _aliases() == {}
    assert task.field_versions["alias"] == [LATER, OTHER]


def test_an_alias_never_set_there_is_kept_here():
    task = _task()
    _alias(task, "api")
    _set_version(task, EARLIER)

    apply_entries(Remote.of(_task_entry(LATER, description="From NLnet")))

    assert _aliases() == {"1a": "api"}


def test_an_entry_from_before_aliases_were_synced_keeps_the_alias():
    task = _task()
    _alias(task, "api")
    entry = _task_entry(LATER, description="From NLnet")
    assert "alias" not in entry

    apply_entries(Remote.of(entry))

    assert task.alias == "api"


@pytest.mark.parametrize("alias", ["has space", "", "1x", 42])
def test_an_alias_that_cannot_be_one_is_skipped_with_a_warning(alias):
    task = _task()
    _alias(task, "api")
    _set_field_versions(task, alias=(EARLIER, "here"))

    result = apply_entries(
        Remote.of(_task_entry(LATER, alias=alias, description="From NLnet"))
    )

    task.refresh_from_db()
    assert _aliases() == {"1a": "api"}
    assert task.field_versions["alias"] == [EARLIER, "here"]
    assert task.description == "From NLnet"
    assert result.warnings


def test_the_same_alias_merged_twice_changes_nothing_the_second_time():
    remote = Remote.of(_task_entry(LATER, alias="api"))
    apply_entries(remote)

    assert apply_entries(remote).changed == 0
    assert _get_task().field_versions["alias"] == [LATER, OTHER]


def test_an_alias_given_to_another_task_later_moves_there():
    first = _task("1a")
    _task("1b")
    _alias(first, "api")
    _set_version(first, EARLIER)
    _set_field_versions(first, alias=(EARLIER, "here"))

    apply_entries(Remote.of(_task_entry(LATER, name="1b", alias="api")))

    first.refresh_from_db()
    assert _aliases() == {"1b": "api"}
    # The first task lost it to a newer alias, not by a change here.
    assert first.field_versions["alias"] == [EARLIER, "here"]
    assert first.modified == EARLIER


def test_an_alias_given_to_another_task_here_later_stays_here():
    first = _task("1a")
    _task("1b")
    _alias(first, "api")
    _set_field_versions(first, alias=(LATER, "here"))

    apply_entries(Remote.of(_task_entry(EARLIER, name="1b", alias="api")))

    assert _aliases() == {"1a": "api"}
    assert _get_task("1b").field_versions["alias"] == [EARLIER, OTHER]


@pytest.mark.parametrize("order", [("1a", "1b"), ("1b", "1a")])
def test_two_tasks_with_the_same_alias_agree_whatever_comes_first(order):
    """Both computers gave the alias to a different task: the newer
    one keeps it, on every computer."""
    _task("1a")
    _task("1b")
    times = {"1a": EARLIER, "1b": LATER}

    for name in order:
        apply_entries(Remote.of(_task_entry(times[name], name=name, alias="api")))

    assert _aliases() == {"1b": "api"}


def test_aliases_of_the_same_name_in_two_mous_are_kept():
    task = _task(mou_name="other")
    _alias(task, "api")

    apply_entries(Remote.of(_task_entry(LATER, alias="api")))

    assert _aliases() == {"1a": "api"}
    assert _aliases("other") == {"1a": "api"}


def test_task_aliases_go_from_one_computer_to_the_other(repository, tmp_path):
    task = _task("1a")
    _alias(task, "api")
    _other_computer_pushes(
        repository, tmp_path, [_task_entry(LATER, name="1b", alias="web")]
    )

    sync = Sync(interactive=False)
    sync.pull()
    sync.push()

    assert _aliases() == {"1a": "api", "1b": "web"}
    pushed = _by_uuid(read_file(sync.git.path / sync.file_name))
    assert pushed[str(task.uuid)]["alias"] == "api"
    other = pushed[str(task_uuid("nlnet-2026", "1b"))]
    assert other["alias"] == "web"
    assert other["versions"]["alias"] == [LATER, OTHER]


def test_migrating_dates_the_aliases_there_are(machine):
    from importlib import import_module

    from django.apps import apps

    migration = import_module(
        "nlnet_rfp_recorder.timetracking.migrations.0031_task_alias_versions"
    )
    aliased, plain = _task("1a"), _task("1b")
    _alias(aliased, "api")
    _set_field_versions(aliased, alias=None)
    _set_version(aliased, EARLIER)

    migration.date_task_aliases(apps, None)

    aliased.refresh_from_db()
    plain.refresh_from_db()
    assert aliased.field_versions["alias"][1] == machine
    assert aliased.modified > EARLIER
    assert "alias" not in plain.field_versions


# --- time reported on another computer ---------------------------------------


def _reported_task(rate_settings, hours=1.0):
    """A task with `hours` of tracked time, as synced to this computer."""
    rate_settings.RFP_EUROS_PER_HOUR = 20.0
    task = _task(max_budget=500.0)
    link = Link.objects.create(url="https://github.com/o/r/issues/1", task=task)
    start = datetime(2026, 11, 1, 9, 0)
    record = TimeRecord.objects.create(
        link=link, start_time=start, end_time=start + timedelta(hours=hours)
    )
    return task, record


def _nlnet_report(task, request="req-1", budget=20, **status) -> Report:
    """A request as `rfp mou update` brings it in: no lines, just what
    it claims per task."""
    report = Report.objects.create(
        id=f"nlnet-2026-{request}", mou=task.mou, rfp_request=request, **status
    )
    ReportTask.objects.create(report=report, task=task, budget=budget, requested=True)
    return report


def test_time_in_a_report_gets_its_request(settings):
    task, record = _reported_task(settings)
    report = Report.create(task.mou)
    report.add_time_record(record)
    record.refresh_from_db()
    assert record.rfp_request == ""
    _set_version(record, EARLIER)

    Report.objects.filter(pk=report.pk).update(rfp_request="req-1")

    record.refresh_from_db()
    assert record.rfp_request == "req-1"
    assert record.modified > EARLIER  # so the other computers learn it


def test_time_put_in_a_report_with_a_request_gets_it(settings):
    task, record = _reported_task(settings)
    report = Report.create(task.mou)
    Report.objects.filter(pk=report.pk).update(rfp_request="req-1")
    report.refresh_from_db()

    report.add_time_record(record)

    record.refresh_from_db()
    assert record.rfp_request == "req-1"


def test_time_taken_out_of_a_report_loses_its_request(settings):
    task, record = _reported_task(settings)
    report = Report.create(task.mou)
    report.add_time_record(record)
    Report.objects.filter(pk=report.pk).update(rfp_request="req-1")
    record.refresh_from_db()

    report.remove_time_record(record)

    record.refresh_from_db()
    assert record.rfp_request == ""


def test_time_of_a_removed_report_loses_its_request(settings):
    task, record = _reported_task(settings)
    report = Report.create(task.mou)
    report.add_time_record(record)
    Report.objects.filter(pk=report.pk).update(rfp_request="req-1")

    Report.objects.filter(pk=report.pk).delete()

    record.refresh_from_db()
    assert record.rfp_request == ""


def test_time_keeps_its_request_when_the_report_is_renamed(settings):
    task, record = _reported_task(settings)
    report = Report.create(task.mou)
    report.add_time_record(record)
    Report.objects.filter(pk=report.pk).update(rfp_request="req-1")
    report.refresh_from_db()

    report.rename("nlnet-2026-2026-11-01-me-1")

    record.refresh_from_db()
    assert record.rfp_request == "req-1"


def test_the_request_of_time_is_synced(settings):
    task, record = _reported_task(settings)
    TimeRecord.objects.filter(pk=record.pk).update(rfp_request="req-1")

    entry = _by_uuid(export_entries(Remote()))[str(record.uuid)]

    assert entry["rfp_request"] == "req-1"


def test_time_reported_on_another_computer_is_reported_here(settings):
    task, record = _reported_task(settings)
    _set_version(record, EARLIER)
    entry = _record_entry(str(record.uuid), str(record.link.uuid), LATER)
    entry.update(
        start_time="2026-11-01T09:00:00+01:00",
        end_time="2026-11-01T10:00:00+01:00",
        rfp_request="req-1",
    )

    apply_entries(Remote.of(entry))

    record.refresh_from_db()
    assert record.rfp_request == "req-1"
    assert record.modified == LATER


def test_the_report_here_decides_the_request_of_its_time(settings):
    """This computer has the report: an edit from elsewhere that does not
    know the request does not take it - and the others learn it."""
    task, record = _reported_task(settings)
    report = Report.create(task.mou)
    report.add_time_record(record)
    Report.objects.filter(pk=report.pk).update(rfp_request="req-1")
    entry = _record_entry(str(record.uuid), str(record.link.uuid), LATER)
    entry.update(
        start_time="2026-11-01T09:00:00+01:00",
        end_time="2026-11-01T10:00:00+01:00",
        rfp_request="",
    )

    apply_entries(Remote.of(entry))

    record.refresh_from_db()
    assert record.rfp_request == "req-1"
    assert record.modified > LATER


def test_an_entry_without_request_keeps_the_one_here(settings):
    """From a computer that does not sync the request yet."""
    task, record = _reported_task(settings)
    TimeRecord.objects.filter(pk=record.pk).update(rfp_request="req-1")
    _set_version(record, EARLIER)
    entry = _record_entry(str(record.uuid), str(record.link.uuid), LATER)
    entry.update(
        start_time="2026-11-01T09:00:00+01:00",
        end_time="2026-11-01T10:00:00+01:00",
    )

    apply_entries(Remote.of(entry))

    record.refresh_from_db()
    assert record.rfp_request == "req-1"
    assert record.modified == LATER


def test_my_request_counts_as_reported_without_report_lines(settings):
    task, record = _reported_task(settings)
    TimeRecord.objects.filter(pk=record.pk).update(rfp_request="req-1")
    _nlnet_report(task, budget=25)

    assert task.reported_budget == 25
    assert list(task.tracked_time_records) == []
    assert task.unreported_budget == 0
    assert task.budget == 25


def test_my_request_is_not_used_by_others(settings):
    task, record = _reported_task(settings)
    TimeRecord.objects.filter(pk=record.pk).update(rfp_request="req-1")
    _nlnet_report(task, budget=25)
    Task.objects.filter(pk=task.pk).update(used_budget=25)
    task.refresh_from_db()

    assert task.used_by_others == 0


def test_the_request_of_someone_else_is_used_by_others(settings):
    task, record = _reported_task(settings)
    TimeRecord.objects.filter(pk=record.pk).update(rfp_request="req-1")
    _nlnet_report(task, budget=25)
    _nlnet_report(task, request="req-2", budget=100)
    Task.objects.filter(pk=task.pk).update(used_budget=125)
    task.refresh_from_db()

    assert task.reported_budget == 25
    assert task.used_by_others == 100


def test_time_of_a_declined_request_counts_as_not_reported(settings):
    task, record = _reported_task(settings)
    TimeRecord.objects.filter(pk=record.pk).update(rfp_request="req-1")
    _nlnet_report(task, budget=25, declined=True)

    assert task.reported_budget == 0
    assert list(task.tracked_time_records) == [record]
    assert task.unreported_budget == 20


def test_time_of_a_request_this_computer_does_not_know_yet_counts_once(settings):
    """Before `rfp mou update` here, the time is counted as worked."""
    task, record = _reported_task(settings)
    TimeRecord.objects.filter(pk=record.pk).update(rfp_request="req-1")

    assert task.reported_budget == 0
    assert task.unreported_budget == 20


def test_time_reported_on_another_computer_is_not_reported_again(settings):
    task, record = _reported_task(settings)
    TimeRecord.objects.filter(pk=record.pk).update(rfp_request="req-1")
    _nlnet_report(task)

    report = Report.create(task.mou)
    report.add_unreported_time_records()

    assert not report.lines.exists()


def test_the_triggers_are_made_anew(settings):
    """An older version of a trigger is replaced, not kept."""
    from django.db import connection

    from nlnet_rfp_recorder.timetracking import triggers

    with connection.cursor() as cursor:
        cursor.execute("DROP TRIGGER rfp_sync_time_record_modified")
        cursor.execute(
            "CREATE TRIGGER rfp_sync_time_record_modified AFTER UPDATE ON "
            "timetracking_timerecord BEGIN SELECT 1; END"
        )
    triggers.install(connection)

    task, record = _reported_task(settings)
    _set_version(record, EARLIER)
    TimeRecord.objects.filter(pk=record.pk).update(rfp_request="req-1")
    record.refresh_from_db()
    assert record.modified > EARLIER
