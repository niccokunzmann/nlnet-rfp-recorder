from datetime import datetime

import pytest

from nlnet_rfp_recorder.timetracking.models import Link, TimeRecord

pytestmark = pytest.mark.django_db


def test_save_and_reload_a_finished_record():
    record = TimeRecord.objects.create(
        start_time=datetime(2026, 9, 4, 9, 0),
        end_time=datetime(2026, 9, 4, 10, 0),
    )
    record.links.create(url="https://example.com/issues/1")

    reloaded = TimeRecord.objects.get(pk=record.pk)
    assert reloaded.start_time == record.start_time
    assert reloaded.end_time == record.end_time
    assert [link.url for link in reloaded.links.all()] == [
        "https://example.com/issues/1"
    ]
    assert reloaded.is_running is False


def test_a_record_without_end_time_is_running():
    record = TimeRecord.objects.create(start_time=datetime(2026, 9, 4, 9, 0))

    assert record.is_running is True
    reloaded = TimeRecord.objects.get(pk=record.pk)
    assert reloaded.end_time is None


def test_finished_records_are_excluded_from_running():
    TimeRecord.objects.create(
        start_time=datetime(2026, 9, 4, 9, 0),
        end_time=datetime(2026, 9, 4, 10, 0),
    )

    assert list(TimeRecord.objects.running()) == []


def test_running_returns_only_unfinished_records():
    running = TimeRecord.objects.create(start_time=datetime(2026, 9, 4, 9, 0))
    TimeRecord.objects.create(
        start_time=datetime(2026, 9, 4, 9, 0),
        end_time=datetime(2026, 9, 4, 10, 0),
    )

    assert list(TimeRecord.objects.running()) == [running]


def test_link_belongs_to_a_time_record():
    record = TimeRecord.objects.create(start_time=datetime(2026, 9, 4, 9, 0))
    link = Link.objects.create(time_record=record, url="https://example.com/pr/2")

    assert link.time_record == record
