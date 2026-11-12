"""Synchronize time records, links and tasks between computers through git.

Every computer writes only its own file, `<machine>.jsonl`, to the
repository - one JSON object per line and per synced object:

    {"model": "time_record", "uuid": "...", "deleted": false,
     "modified": "2026-11-05T17:22:31.811Z", "modified_by": "laptop-1a2b3c",
     "link": "<link uuid>", "start_time": "...", ...}

so git never has to merge anything. A computer's file holds everything
it knows: its own objects, the ones it got from other computers and the
deletions (`"deleted": true`, without data).

`modified` is when the object really changed - database triggers stamp
it, whichever code changes the row (see timetracking/triggers.py) - and
`modified_by` the computer it changed on. Across all files the version
with the newest (modified, modified_by) wins, so an edit made offline
wins over an older one made elsewhere.

Time records are the most valuable data, so sync never loses one to a
deletion it cannot be sure of: a link that still has time records here
is not deleted but restored, and so is a time record that is part of a
report here. A restored object gets a new `modified`, so the other
computers restore it too.

Tasks - known by MoU and name, see task_uuid - carry their budget and
description, merged value by value: every value has its own version
(`versions`: [when, computer], see Task.field_versions) and the newest
version wins. A value that was never set has no version, so a computer
that never read the budget does not wipe it on the others - it takes it
over. A value that was cleared on purpose has one, and stays cleared.
A task is only deleted while no link or report here needs it.

A task's alias is one more such value. An alias names one task of a MoU:
given to two tasks on two computers, the one given later keeps it.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import uuid as uuid_module
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

BRANCH = "main"
LINK = "link"
TIME_RECORD = "time_record"
TASK = "task"
MODELS = (LINK, TIME_RECORD, TASK)
# A change dated further ahead than this comes from a computer whose
# clock is wrong: its changes would win over everything until then.
CLOCK_TOLERANCE = timedelta(minutes=5)

Key = tuple[str, str]  # (model, uuid with dashes)
Entry = dict[str, Any]
Version = tuple[str, str]  # (modified, modified_by)


class SyncError(Exception):
    """git failed - the message is what git said, shortened."""


def _git_message(output: str, default: str) -> str:
    """The line of git's output that says what went wrong."""
    lines = [line.strip() for line in output.splitlines() if line.strip()]
    for line in lines:
        for prefix in ("fatal: ", "error: "):
            if line.startswith(prefix):
                return line.removeprefix(prefix)
    return lines[0] if lines else default


def _now() -> str:
    from nlnet_rfp_recorder.timetracking.models import sync_now

    return sync_now()


def _machine() -> str:
    from nlnet_rfp_recorder.timetracking.models import SyncSettings

    return SyncSettings.get().machine


def _datetime(value: datetime | None) -> str | None:
    """A database time - naive, in this computer's time zone - with its
    UTC offset, so a computer in another time zone reads the same moment."""
    return None if value is None else value.astimezone().isoformat()


def _parse_datetime(value: str | None) -> datetime | None:
    """The moment in this computer's time zone, naive like the database."""
    if value is None:
        return None
    moment = datetime.fromisoformat(value)
    if moment.tzinfo is None:
        return moment
    return moment.astimezone().replace(tzinfo=None)


def _key_uuid(value: Any) -> str:
    """A uuid as the files write it - the database keeps 32 hex digits."""
    return str(uuid_module.UUID(str(value)))


def version(entry: Entry) -> Version:
    return (entry["modified"], entry.get("modified_by") or "")


def repository_path() -> Path:
    """The clone of the sync repository, next to the database."""
    from django.conf import settings

    database_file = Path(settings.DATABASES["default"]["NAME"])
    return database_file.with_name(f"{database_file.stem}-sync")


# --- serialization ---------------------------------------------------------


def serialize_link(link) -> dict[str, Any]:
    task = link.task
    has_task = task is not None and task.mou is not None
    return {
        "url": link.url,
        "mou": task.mou.name if has_task else None,
        "task": task.name if has_task else None,
        "tags": sorted(tag.name for tag in link.tags.all()),
        "excluded_from_reports": link.excluded_from_reports,
        "title": link.title,
        "closed_at": _datetime(link.closed_at),
    }


def serialize_time_record(record) -> dict[str, Any]:
    return {
        "link": str(record.link.uuid) if record.link else None,
        "start_time": _datetime(record.start_time),
        "end_time": _datetime(record.end_time),
        "stop_reason": record.stop_reason,
        "computer": record.computer,
        "rfp_request": record.rfp_request,
    }


def serialize_task(task) -> dict[str, Any]:
    from nlnet_rfp_recorder.timetracking.models.task import (
        SYNCED_TASK_FIELDS,
        TASK_ALIAS_FIELD,
    )

    return {
        "mou": task.mou.name,
        "task": task.name,
        **{name: getattr(task, name) for name in SYNCED_TASK_FIELDS},
        TASK_ALIAS_FIELD: task.alias,
        "versions": {
            name: task.field_versions[name]
            for name in (*SYNCED_TASK_FIELDS, TASK_ALIAS_FIELD)
            if _field_version((task.field_versions or {}).get(name)) is not None
        },
    }


def _alias_value(value: Any) -> str | None:
    """A task alias from a file; ValueError if it cannot be one."""
    if value is None:
        return None
    if (
        not isinstance(value, str)
        or not value
        or len(value) > 64
        or any(character.isspace() for character in value)
        or value[0].isdigit()
    ):
        raise ValueError(f"{value!r} is no task alias")
    return value


def _field_version(value: Any) -> Version | None:
    """A value's version, None if it has none (or one that is not one)."""
    if (
        isinstance(value, list | tuple)
        and len(value) == 2
        and all(isinstance(part, str) for part in value)
    ):
        return (value[0], value[1])
    return None


def _entry(model: str, obj, serialize: Callable[[Any], dict]) -> Entry:
    return {
        "model": model,
        "uuid": str(obj.uuid),
        "modified": obj.modified,
        "modified_by": obj.modified_by,
        "deleted": False,
        **serialize(obj),
    }


def _deleted_entry(model: str, uuid: str, modified: str, modified_by: str) -> Entry:
    return {
        "model": model,
        "uuid": uuid,
        "modified": modified,
        "modified_by": modified_by,
        "deleted": True,
    }


def _key_of(entry: Entry) -> Key:
    return (entry["model"], entry["uuid"])


def local_objects() -> dict[Key, tuple[Any, Entry]]:
    """Every synced object in the database with its entry for the file."""
    from nlnet_rfp_recorder.timetracking.models import Link, Task, TimeRecord

    objects: dict[Key, tuple[Any, Entry]] = {}
    for task in Task.objects.select_related("mou").filter(mou__isnull=False):
        objects[(TASK, str(task.uuid))] = (task, _entry(TASK, task, serialize_task))
    for link in Link.objects.select_related("task__mou").prefetch_related("tags"):
        objects[(LINK, str(link.uuid))] = (link, _entry(LINK, link, serialize_link))
    for record in TimeRecord.objects.select_related("link"):
        objects[(TIME_RECORD, str(record.uuid))] = (
            record,
            _entry(TIME_RECORD, record, serialize_time_record),
        )
    return objects


# --- reading and writing files -------------------------------------------


def read_file(path: Path) -> list[Entry]:
    """The entries of a file - lines that are not one are left out."""
    entries = []
    for line in path.read_text(encoding="utf-8").splitlines():
        try:
            entry = json.loads(line)
            if not isinstance(entry, dict) or entry.get("model") not in MODELS:
                continue
            entry["uuid"] = _key_uuid(entry["uuid"])
            if not isinstance(entry["modified"], str):
                continue
            if not isinstance(entry.get("modified_by", ""), str):
                continue
        except ValueError, KeyError, TypeError, AttributeError:
            continue
        entries.append(entry)
    return entries


@dataclass
class Remote:
    """What the files say: the newest version of every object, and the
    newest version of it that is not a deletion (to restore a link)."""

    newest: dict[Key, Entry] = field(default_factory=dict)
    live: dict[Key, Entry] = field(default_factory=dict)

    @classmethod
    def read(cls, paths: list[Path]) -> Remote:
        remote = cls()
        for path in paths:
            for entry in read_file(path):
                remote.add(entry)
        return remote

    @classmethod
    def of(cls, *entries: Entry) -> Remote:
        remote = cls()
        for entry in entries:
            remote.add(entry)
        return remote

    def add(self, entry: Entry) -> None:
        key = _key_of(entry)
        known = self.newest.get(key)
        if known is None or version(entry) > version(known):
            self.newest[key] = entry
        if not entry.get("deleted"):
            known = self.live.get(key)
            if known is None or version(entry) > version(known):
                self.live[key] = entry


def export_entries(remote: Remote) -> list[Entry]:
    """This computer's file: its objects and every deletion it knows of.

    Deletions come from SyncDeletion (deleted here, not pushed yet) and
    from the files - unless the object lives on here, then it was changed
    or restored after the deletion.
    """
    from nlnet_rfp_recorder.timetracking.models import SyncDeletion

    objects = local_objects()
    entries: dict[Key, Entry] = {}

    def add_deleted(entry: Entry) -> None:
        key = _key_of(entry)
        if key in objects:
            return
        known = entries.get(key)
        if known is None or version(entry) > version(known):
            entries[key] = entry

    for entry in remote.newest.values():
        if entry.get("deleted"):
            add_deleted(_deleted_entry(*_key_of(entry), *version(entry)))
    for deletion in SyncDeletion.objects.all():
        add_deleted(
            _deleted_entry(
                deletion.model,
                _key_uuid(deletion.uuid),
                deletion.deleted_at,
                deletion.deleted_by,
            )
        )
    for key, (_, entry) in objects.items():
        entries[key] = entry
    return [entries[key] for key in sorted(entries)]


def write_file(path: Path, entries: list[Entry]) -> None:
    lines = [json.dumps(entry, sort_keys=True, ensure_ascii=False) for entry in entries]
    path.write_text("".join(f"{line}\n" for line in lines), encoding="utf-8")


# --- applying other computers' changes -----------------------------------


@dataclass
class ImportResult:
    changed: int = 0
    deleted: int = 0
    restored: int = 0
    warnings: list[str] = field(default_factory=list)

    @property
    def total(self) -> int:
        return self.changed + self.deleted + self.restored


def _stamp(model_class, pk: int, entry_version: Version) -> None:
    """Give a row the version it came with. Setting `modified` keeps the
    triggers from stamping it as changed here."""
    modified, modified_by = entry_version
    model_class.objects.filter(pk=pk).update(modified=modified, modified_by=modified_by)


def _restore(model_class, pk: int) -> None:
    """Mark a row as changed here and now, so its version wins over a
    deletion and the other computers restore it too."""
    model_class.objects.filter(pk=pk).update(modified=_now(), modified_by=_machine())


def _forget_deletion(model: str, uuid: str) -> None:
    """The trigger noted a deletion done by sync itself - the files have
    it already."""
    from nlnet_rfp_recorder.timetracking.models import SyncDeletion

    SyncDeletion.objects.filter(model=model, uuid=uuid_module.UUID(uuid).hex).delete()


def _is_newer_than(entry: Entry, obj) -> bool:
    return version(entry) > (obj.modified, obj.modified_by)


def _parse_sync_time(value: str) -> datetime:
    try:
        moment = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return datetime.min.replace(tzinfo=UTC)
    return moment if moment.tzinfo else moment.replace(tzinfo=UTC)


class _Importer:
    def __init__(self, remote: Remote):
        from nlnet_rfp_recorder.timetracking.models import (
            Link,
            SyncDeletion,
            Task,
            TimeRecord,
        )

        self.remote = remote
        self.result = ImportResult()
        self.links: dict[str, Any] = {}  # remote link uuid -> local link
        self.local: dict[Key, Version] = {}
        for deletion in SyncDeletion.objects.all():
            self.local[(deletion.model, _key_uuid(deletion.uuid))] = (
                deletion.deleted_at,
                deletion.deleted_by,
            )
        synced = (
            (LINK, Link.objects),
            (TIME_RECORD, TimeRecord.objects),
            (TASK, Task.objects.filter(mou__isnull=False)),
        )
        for model, objects in synced:
            for uuid, modified, modified_by in objects.values_list(
                "uuid", "modified", "modified_by"
            ):
                self.local[(model, str(uuid))] = (modified, modified_by)

    def is_newer(self, entry: Entry) -> bool:
        local = self.local.get(_key_of(entry))
        return local is None or version(entry) > local

    def run(self) -> ImportResult:
        newer = [
            entry
            for _, entry in sorted(self.remote.newest.items())
            if self.is_newer(entry)
        ]
        self._warn_about_clocks(newer)
        # Every task is merged, not only the newer ones: an older version
        # of the task can still have a newer version of one value.
        for key, entry in sorted(self.remote.live.items()):
            if key[0] == TASK:
                self._safely(self._merge_task, entry)
        # Tasks before the links on them, links before the time records
        # that point to them; deletions last, after a merged or restored
        # link has its records and a task its links.
        steps: list[tuple[str, bool, Callable[[Entry], None]]] = [
            (LINK, False, self._apply_link),
            (TIME_RECORD, False, self._apply_time_record),
            (TIME_RECORD, True, self._delete_time_record),
            (LINK, True, self._delete_link),
            (TASK, True, self._delete_task),
        ]
        for model, deleted, apply in steps:
            for entry in newer:
                if entry["model"] == model and bool(entry.get("deleted")) == deleted:
                    self._safely(apply, entry)
        self._warn_about_running_timers()
        return self.result

    def _safely(self, apply: Callable[[Entry], None], entry: Entry) -> None:
        from django.db import transaction

        try:
            with transaction.atomic():
                apply(entry)
        except Exception as error:
            self.result.warnings.append(
                f"Skipped a {entry['model'].replace('_', ' ')} "
                f"({entry['uuid']}) from {entry.get('modified_by') or 'a computer'} "
                f"that could not be read: {error}"
            )

    def _warn_about_clocks(self, entries: list[Entry]) -> None:
        limit = datetime.now(UTC) + CLOCK_TOLERANCE
        ahead = sorted(
            {
                entry.get("modified_by") or "?"
                for entry in entries
                if _parse_sync_time(entry["modified"]) > limit
            }
        )
        for machine in ahead:
            self.result.warnings.append(
                f"The clock of {machine} is ahead: its changes are dated in "
                "the future and win over newer ones. Set its clock right."
            )

    def _warn_about_running_timers(self) -> None:
        from nlnet_rfp_recorder.timetracking.models import TimeRecord

        computers = sorted(
            {record.computer or "?" for record in TimeRecord.objects.running()}
        )
        if len(computers) > 1:
            self.result.warnings.append(
                f"Timers are running on several computers: {', '.join(computers)}. "
                "Stop the one you are not using with `rfp stop` there."
            )

    # --- tasks

    def _merge_task(self, entry: Entry) -> None:
        from nlnet_rfp_recorder.timetracking.models import MoU, Task
        from nlnet_rfp_recorder.timetracking.models.task import (
            SYNCED_TASK_FIELDS,
            TASK_ALIAS_FIELD,
        )

        found = (
            Task.objects.filter(uuid=entry["uuid"], mou__isnull=False).first()
            or Task.objects.filter(mou__name=entry["mou"], name=entry["task"]).first()
        )
        created = found is None
        if found is not None:
            task = found
        else:
            newest = self.remote.newest.get(_key_of(entry))
            if newest is None or newest.get("deleted") or not self.is_newer(entry):
                return  # deleted there, or here after this version
            mou, _ = MoU.objects.get_or_create(name=entry["mou"])
            task = Task.objects.create(mou=mou, name=entry["task"], selected=False)

        versions = dict(task.field_versions or {})
        theirs = entry.get("versions")
        if not isinstance(theirs, dict):
            theirs = {}
        values: dict[str, Any] = {}
        for name in SYNCED_TASK_FIELDS:
            their_version = _field_version(theirs.get(name))
            if their_version is None:
                continue  # never set there: whatever is here stays
            our_version = _field_version(versions.get(name))
            if our_version is None or their_version > our_version:
                values[name] = entry.get(name)
                versions[name] = list(their_version)

        alias_changed = False
        their_version = _field_version(theirs.get(TASK_ALIAS_FIELD))
        our_version = _field_version(versions.get(TASK_ALIAS_FIELD))
        took_alias = their_version is not None and (
            our_version is None or their_version > our_version
        )
        if took_alias:
            try:
                alias = _alias_value(entry.get(TASK_ALIAS_FIELD))
            except ValueError as error:
                took_alias = False
                self.result.warnings.append(
                    f"Kept the alias of task {task.name}: {error}, "
                    f"from {entry.get('modified_by') or 'a computer'}."
                )
            else:
                alias_changed = self._set_alias(task, alias, their_version)
                versions[TASK_ALIAS_FIELD] = list(their_version)

        # After the alias: its triggers date it now, this dates it right.
        if values or took_alias:
            Task.objects.filter(pk=task.pk).update(**values, field_versions=versions)
            if alias_changed or any(
                getattr(task, name) != value for name, value in values.items()
            ):
                self.result.changed += 1
        if created:
            _stamp(Task, task.pk, version(entry))
        elif values or took_alias:
            # The values are of this version now - unless the task changed
            # here later, which a deletion from before must not undo.
            _stamp(
                Task, task.pk, max(version(entry), (task.modified, task.modified_by))
            )

    def _set_alias(self, task, alias: str | None, alias_version: Version) -> bool:
        """Give the task this alias - unless another task of its MoU got
        the same alias later, then the task has none. A task that got it
        earlier loses it, but keeps its version: the newer alias decides
        on every computer, whichever task is merged first.

        Returns whether the task's alias changed. The caller dates it.
        """
        from nlnet_rfp_recorder.timetracking.models import Alias, Task
        from nlnet_rfp_recorder.timetracking.models.task import TASK_ALIAS_FIELD

        current = task.alias
        if alias is not None:
            holders = Alias.objects.filter(
                item_type="task", mou=task.mou, alias=alias
            ).exclude(target=task.name)
            for holder in holders:
                other = Task.objects.filter(mou=task.mou, name=holder.target).first()
                other_version = (
                    None
                    if other is None
                    else _field_version(
                        (other.field_versions or {}).get(TASK_ALIAS_FIELD)
                    )
                )
                if other_version is not None and other_version > alias_version:
                    alias = None  # the other task got it later
                    break
                holder.delete()
                if other is not None:
                    # Undo what the trigger dated: this is not a change
                    # made here, and the other task's version stays.
                    Task.objects.filter(pk=other.pk).update(
                        field_versions=other.field_versions,
                        modified=other.modified,
                        modified_by=other.modified_by,
                    )
                    self.result.changed += 1
        if alias == current:
            return False
        Alias.objects.filter(item_type="task", mou=task.mou, target=task.name).delete()
        if alias is not None:
            Alias.objects.create(
                item_type="task", mou=task.mou, target=task.name, alias=alias
            )
        return True

    def _delete_task(self, entry: Entry) -> None:
        from nlnet_rfp_recorder.timetracking.models import ReportTask, Task

        task = Task.objects.filter(uuid=entry["uuid"], mou__isnull=False).first()
        if task is None or not _is_newer_than(entry, task):
            return
        if task.links.exists() or ReportTask.objects.filter(task=task).exists():
            # Links or reports here still count on it: keep it everywhere.
            _restore(Task, task.pk)
            self.result.restored += 1
            return
        task.delete()
        _forget_deletion(TASK, entry["uuid"])
        self.result.deleted += 1

    # --- links

    def _apply_link(self, entry: Entry) -> None:
        from nlnet_rfp_recorder.timetracking.models import Link

        link = self._upsert_link(entry)
        if link is not None:
            _stamp(Link, link.pk, version(entry))
            self.result.changed += 1

    def _upsert_link(self, entry: Entry):
        from nlnet_rfp_recorder.timetracking.models import Link, MoU, Tag, Task

        url = entry.get("url") or ""
        task = None
        if entry.get("task") is not None and entry.get("mou") is not None:
            # A MoU or task this computer does not know yet - e.g. syncing
            # into an empty database - is made by name; `rfp mou update`
            # fills it in.
            mou, _ = MoU.objects.get_or_create(name=entry["mou"])
            task, _ = Task.objects.get_or_create(
                mou=mou, name=entry["task"], defaults={"selected": False}
            )

        link = Link.objects.filter(uuid=entry["uuid"]).first()
        if link is None and url:
            # The same URL tracked on two computers before they synced:
            # both keep the link with the smaller uuid, so they agree.
            same_url = Link.objects.filter(url=url).first()
            if same_url is not None:
                if entry["uuid"] > str(same_url.uuid):
                    self.links[entry["uuid"]] = same_url
                    return None
                same_url.uuid = uuid_module.UUID(entry["uuid"])
                same_url.save(update_fields=["uuid"])
                link = same_url
        if link is not None and url and link.url != url:
            if Link.objects.filter(url=url).exclude(pk=link.pk).exists():
                self.result.warnings.append(
                    f"Kept {link.url}: another computer renamed it to {url}, "
                    "which is a link of its own here."
                )
                url = link.url
        if link is None:
            link = Link(uuid=uuid_module.UUID(entry["uuid"]))
        link.url = url
        link.task = task
        link.excluded_from_reports = bool(entry.get("excluded_from_reports"))
        link.title = entry.get("title") or ""
        link.closed_at = _parse_datetime(entry.get("closed_at"))
        link.save()
        link.tags.set(
            [Tag.objects.get_or_create(name=name)[0] for name in entry.get("tags", [])]
        )
        self.links[entry["uuid"]] = link
        return link

    def _link_for(self, entry: Entry):
        """The local link of a time record entry - restored from its last
        version in the files if it was deleted here or elsewhere."""
        from nlnet_rfp_recorder.timetracking.models import Link

        link_uuid = entry.get("link")
        if link_uuid is None:
            return None
        link_uuid = _key_uuid(link_uuid)
        link = self.links.get(link_uuid) or Link.objects.filter(uuid=link_uuid).first()
        if link is not None:
            return link
        last_version = self.remote.live.get((LINK, link_uuid))
        if last_version is None:
            raise ValueError(f"its link {link_uuid} is in no file")
        link = self._upsert_link(last_version)
        if link is None:
            return self.links[link_uuid]
        _restore(Link, link.pk)
        _forget_deletion(LINK, link_uuid)
        self.result.restored += 1
        return link

    def _delete_link(self, entry: Entry) -> None:
        from nlnet_rfp_recorder.timetracking.models import Link

        link = Link.objects.filter(uuid=entry["uuid"]).first()
        if link is None or not _is_newer_than(entry, link):
            return  # gone, or restored above in this sync
        if link.time_records.exists():
            # Deleting it would take the link from time records: restore
            # it everywhere instead.
            _restore(Link, link.pk)
            self.result.restored += 1
            return
        link.delete()
        _forget_deletion(LINK, entry["uuid"])
        self.result.deleted += 1

    # --- time records

    def _apply_time_record(self, entry: Entry) -> None:
        from nlnet_rfp_recorder.timetracking.models import TimeRecord

        link = self._link_for(entry)
        start_time = _parse_datetime(entry["start_time"])
        if start_time is None:
            raise ValueError("it has no start time")
        end_time = _parse_datetime(entry.get("end_time"))
        record = TimeRecord.objects.filter(uuid=entry["uuid"]).first()
        if record is None:
            # The same entry on two computers from before they synced -
            # e.g. a copied database, where each computer gave it its own
            # uuid: same link and the same start and end to the
            # microsecond. Both keep the one with the smaller uuid.
            same = TimeRecord.objects.filter(
                link=link, start_time=start_time, end_time=end_time
            ).first()
            if same is not None:
                if entry["uuid"] > str(same.uuid):
                    return
                same.uuid = uuid_module.UUID(entry["uuid"])
                same.save(update_fields=["uuid"])
                record = same
        if record is None:
            record = TimeRecord(uuid=uuid_module.UUID(entry["uuid"]))
        record.link = link
        record.start_time = start_time
        record.end_time = end_time
        record.stop_reason = entry.get("stop_reason") or "user"
        record.computer = entry.get("computer") or record.computer
        # Which request the time is in comes from the report here, if it
        # is in one; else from the computer that has the report.
        theirs = entry.get("rfp_request")
        if not isinstance(theirs, str):
            theirs = None  # from a computer that does not sync it yet
        if record.report_line_id is None and theirs is not None:
            record.rfp_request = theirs
        record.modified, record.modified_by = version(entry)
        record.save()
        _stamp(TimeRecord, record.pk, version(entry))
        self.result.changed += 1
        record.refresh_from_db(fields=["rfp_request"])
        if theirs is not None and record.rfp_request != theirs:
            # The other computer does not know the report here: tell it.
            _restore(TimeRecord, record.pk)

    def _delete_time_record(self, entry: Entry) -> None:
        from nlnet_rfp_recorder.timetracking.models import TimeRecord

        record = TimeRecord.objects.filter(uuid=entry["uuid"]).first()
        if record is None or not _is_newer_than(entry, record):
            return
        if record.report_line_id is not None:
            # Reports are not synced: the other computer cannot know this
            # time is reported here. Keep it, and restore it everywhere.
            _restore(TimeRecord, record.pk)
            self.result.restored += 1
            self.result.warnings.append(
                f"Kept the time entry from {record.start_time:%Y-%m-%d %H:%M}: "
                f"{entry.get('modified_by') or 'another computer'} deleted it, "
                f"but it is in report {record.report_line.report_id} here."
            )
            return
        record.delete()
        _forget_deletion(TIME_RECORD, entry["uuid"])
        self.result.deleted += 1


def apply_entries(remote: Remote) -> ImportResult:
    """Take every object whose newest version is not the one here."""
    return _Importer(remote).run()


# --- git -------------------------------------------------------------------


@dataclass
class Git:
    path: Path
    url: str
    name: str = ""
    email: str = ""
    interactive: bool = True

    def environment(self) -> dict[str, str]:
        env = dict(os.environ)
        # The overrides of `rfp sync --set-name/--set-email`, for these
        # commits only - no git config is changed.
        if self.name:
            env["GIT_AUTHOR_NAME"] = env["GIT_COMMITTER_NAME"] = self.name
        if self.email:
            env["GIT_AUTHOR_EMAIL"] = env["GIT_COMMITTER_EMAIL"] = self.email
        if not self.interactive:
            env["GIT_TERMINAL_PROMPT"] = "0"
        return env

    def run(
        self, *args: str, check: bool = True, cwd: Path | None = None
    ) -> subprocess.CompletedProcess:
        process = subprocess.run(
            ["git", *args],
            cwd=cwd or self.path,
            env=self.environment(),
            capture_output=True,
            text=True,
            stdin=None if self.interactive else subprocess.DEVNULL,
        )
        if check and process.returncode != 0:
            message = (process.stderr or process.stdout).strip()
            if "Author identity unknown" in message or "user.email" in message:
                message = (
                    "git does not know who you are. Run `rfp sync --set-name "
                    "<name>` and `rfp sync --set-email <email>`."
                )
            raise SyncError(_git_message(message, f"git {args[0]} failed"))
        return process

    def ensure(self) -> None:
        """Clone the repository next to the database. A clone of another
        repository (the URL changed) or a broken one is cloned anew - it
        only holds what is in a repository or comes from the database."""
        if (self.path / ".git").is_dir():
            remote = self.run("remote", "get-url", "origin", check=False)
            if remote.returncode == 0 and remote.stdout.strip() == self.url:
                return
        if self.path.exists():
            shutil.rmtree(self.path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        try:
            self.run(
                "clone", "--quiet", "--", self.url, str(self.path), cwd=self.path.parent
            )
        except BaseException:
            # Ctrl+C or a failure halfway: no half clone stays behind.
            shutil.rmtree(self.path, ignore_errors=True)
            raise

    def fetch(self) -> None:
        self.run("fetch", "--quiet", "--prune", "origin")

    def has_remote_branch(self) -> bool:
        ref = f"refs/remotes/origin/{BRANCH}"
        verify = self.run("rev-parse", "--verify", "--quiet", ref, check=False)
        return verify.returncode == 0

    def reset_to_remote(self) -> None:
        """Take what the others pushed. Nothing is lost: this computer's
        file is written anew from the database before every push."""
        if self.has_remote_branch():
            self.run("checkout", "--quiet", "--force", "-B", BRANCH, f"origin/{BRANCH}")

    def commit(self, file_name: str, message: str) -> bool:
        self.run("add", "--", file_name)
        if self.run("diff", "--cached", "--quiet", check=False).returncode == 0:
            return False
        self.run("commit", "--quiet", "--no-verify", "-m", message)
        return True

    def needs_push(self) -> bool:
        if self.run("rev-parse", "--verify", "--quiet", "HEAD", check=False).returncode:
            return False
        if not self.has_remote_branch():
            return True
        ahead = self.run("rev-list", "--count", f"origin/{BRANCH}..HEAD").stdout
        return int(ahead.strip() or 0) > 0

    def push(self) -> bool:
        """Push; False if the others pushed first."""
        process = self.run("push", "--quiet", "origin", f"HEAD:{BRANCH}", check=False)
        if process.returncode == 0:
            return True
        message = process.stderr or ""
        if any(
            reason in message
            for reason in ("rejected", "fetch first", "non-fast-forward")
        ):
            return False
        raise SyncError(_git_message(message, "git push failed"))


# --- the sync steps ----------------------------------------------------------


class Sync:
    """One sync of this database with the repository in its settings."""

    PUSH_ATTEMPTS = 3

    def __init__(self, interactive: bool = True):
        from nlnet_rfp_recorder.timetracking.models import SyncSettings

        self.settings = SyncSettings.get()
        self.git = Git(
            path=repository_path(),
            url=self.settings.url,
            name=self.settings.name,
            email=self.settings.email,
            interactive=interactive,
        )

    @property
    def file_name(self) -> str:
        return f"{self.settings.machine}.jsonl"

    def files(self) -> list[Path]:
        return sorted(self.git.path.glob("*.jsonl"))

    def machines(self) -> list[str]:
        """The computers that have a file in the repository."""
        return [path.stem for path in self.files()]

    def fetch(self) -> None:
        """The network part of a pull - it does not touch the database,
        so it can run in another thread."""
        self.git.ensure()
        self.git.fetch()

    def import_fetched(self) -> ImportResult:
        """Take the others' changes from what fetch() got."""
        from django.db import transaction

        self.git.reset_to_remote()
        with transaction.atomic():
            return apply_entries(Remote.read(self.files()))

    def pull(self) -> ImportResult:
        self.fetch()
        return self.import_fetched()

    def push(self) -> bool:
        """Write this computer's file, commit and push it. Returns
        whether anything was pushed."""
        from nlnet_rfp_recorder.timetracking.models import SyncDeletion

        self.git.ensure()
        for attempt in range(self.PUSH_ATTEMPTS):
            exported = list(SyncDeletion.objects.values_list("pk", flat=True))
            entries = export_entries(Remote.read(self.files()))
            write_file(self.git.path / self.file_name, entries)
            self.git.commit(self.file_name, f"Sync {self.settings.machine}")
            if not self.git.needs_push():
                return False
            if self.git.push():
                # The deletions are in the repository now: the files
                # remember them from here on.
                SyncDeletion.objects.filter(pk__in=exported).delete()
                return True
            if attempt < self.PUSH_ATTEMPTS - 1:
                # Someone pushed first: take their changes and try again.
                self.pull()
        raise SyncError("git push kept being rejected; try `rfp sync` again.")
