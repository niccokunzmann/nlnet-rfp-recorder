from datetime import datetime, timedelta

import pytest
from django.core.exceptions import ValidationError
from django.utils import timezone

from nlnet_rfp_recorder.timetracking.models import Link, Task, TimeRecord

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


def test_duration_of_a_finished_record():
    record = TimeRecord.objects.create(
        start_time=datetime(2026, 9, 4, 9, 0),
        end_time=datetime(2026, 9, 4, 10, 30),
    )

    assert record.duration == timedelta(hours=1, minutes=30)


def test_duration_of_a_running_record_counts_up_to_now():
    record = TimeRecord.objects.create(start_time=timezone.now() - timedelta(minutes=5))

    assert record.duration >= timedelta(minutes=5)


def test_link_belongs_to_a_time_record():
    record = TimeRecord.objects.create(start_time=datetime(2026, 9, 4, 9, 0))
    link = Link.objects.create(time_record=record, url="https://example.com/pr/2")

    assert link.time_record == record


def test_task_defaults_to_selected():
    task = Task.objects.create(name="10a")

    assert task.selected is True


def test_task_can_be_deselected():
    task = Task.objects.create(name="10a", selected=False)

    assert task.selected is False


def test_task_name_accepts_number_letters_format():
    Task(name="10a").full_clean()


def test_task_name_rejects_other_formats():
    with pytest.raises(ValidationError):
        Task(name="Write the RfP").full_clean()


def test_select_leaves_only_one_task_selected():
    Task.objects.create(name="10a", selected=True)
    Task.objects.create(name="11b", selected=True)

    Task.select("12c")

    selected = Task.objects.filter(selected=True)
    assert [task.name for task in selected] == ["12c"]


def test_get_selected_returns_none_when_nothing_is_selected():
    assert Task.get_selected() is None


def test_get_selected_returns_the_selected_task():
    Task.objects.create(name="10a", selected=False)
    selected = Task.objects.create(name="11b", selected=True)

    assert Task.get_selected() == selected


def test_start_without_a_task_and_none_selected_raises():
    with pytest.raises(ValueError):
        TimeRecord.start("https://example.com/issues/1")


def test_start_uses_the_selected_task_by_default():
    selected = Task.objects.create(name="10a")

    record = TimeRecord.start("https://example.com/issues/1")

    assert record.task == selected
    assert record.is_running is True
    assert [link.url for link in record.links.all()] == ["https://example.com/issues/1"]


def test_start_accepts_an_explicit_task():
    Task.objects.create(name="10a", selected=True)
    other = Task.objects.create(name="11b", selected=False)

    record = TimeRecord.start("https://example.com/issues/1", task=other)

    assert record.task == other


def test_start_stops_a_previously_running_record():
    Task.objects.create(name="10a")
    first = TimeRecord.start("https://example.com/issues/1")

    second = TimeRecord.start("https://example.com/issues/2")

    first.refresh_from_db()
    assert first.is_running is False
    assert second.is_running is True


def test_get_running_returns_none_when_nothing_is_running():
    assert TimeRecord.get_running() is None


def test_get_running_returns_the_most_recently_started_record():
    TimeRecord.objects.create(start_time=datetime(2026, 9, 4, 8, 0))
    later = TimeRecord.objects.create(start_time=datetime(2026, 9, 4, 9, 0))

    assert TimeRecord.get_running() == later


def test_stop_without_a_running_record_returns_none():
    assert TimeRecord.stop() is None


def test_stop_ends_the_running_record():
    Task.objects.create(name="10a")
    started = TimeRecord.start("https://example.com/issues/1")

    stopped = TimeRecord.stop()

    assert stopped.pk == started.pk
    assert stopped.is_running is False
    assert stopped.end_time is not None


def test_stop_twice_is_a_no_op_the_second_time():
    Task.objects.create(name="10a")
    TimeRecord.start("https://example.com/issues/1")

    TimeRecord.stop()

    assert TimeRecord.stop() is None


def test_stop_ends_the_most_recently_started_record():
    Task.objects.create(name="10a")
    TimeRecord.objects.create(start_time=datetime(2026, 9, 4, 8, 0))
    later = TimeRecord.objects.create(start_time=datetime(2026, 9, 4, 9, 0))

    stopped = TimeRecord.stop()

    assert stopped.pk == later.pk
