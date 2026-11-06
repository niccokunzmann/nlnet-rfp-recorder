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
    Link,
    MoU,
    Report,
    SyncDeletion,
    SyncSettings,
    Tag,
    Task,
    TimeRecord,
    sync_now,
)

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
        ["task", "export"],
        ["alias", "list"],
        ["migrate"],
    ],
)
def test_commands_without_synced_data_do_not_sync(repository, args):
    """Only links and time records are synced: commands that neither
    read nor change them have nothing to sync."""
    MoU.objects.create(name="nlnet-2026", selected=True)
    with patch.object(Sync, "pull") as pull:
        result = runner.invoke(app, args)

    assert result.exit_code == 0, result.output
    pull.assert_not_called()
    assert not cli._sync_pending


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
