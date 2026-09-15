from datetime import datetime, timedelta
from unittest.mock import patch

import pytest

from nlnet_rfp_recorder.statistics import Statistics
from nlnet_rfp_recorder.timetracking.models import Link, MoU, Task, TimeRecord

pytestmark = pytest.mark.django_db


def _now(dt: datetime):
    return patch("django.utils.timezone.now", return_value=dt)


def test_statistics_today_is_empty_when_nothing_tracked(settings):
    settings.RFP_EUROS_PER_HOUR = 20.0
    with _now(datetime(2026, 9, 15, 12, 0)):
        stats = Statistics.today()

    assert stats.per_task == []
    assert stats.total_duration == timedelta()
    assert stats.total_budget == 0.0


def test_statistics_today_groups_time_per_task_and_totals(settings):
    settings.RFP_EUROS_PER_HOUR = 20.0
    mou = MoU.objects.create(name="nlnet-2026")
    task_a = Task.objects.create(mou=mou, name="10a")
    task_b = Task.objects.create(mou=mou, name="11b")
    link_a = Link.objects.create(task=task_a, url="https://example.com/issues/1")
    link_b = Link.objects.create(task=task_b, url="https://example.com/issues/2")
    TimeRecord.objects.create(
        link=link_a,
        start_time=datetime(2026, 9, 15, 9, 0),
        end_time=datetime(2026, 9, 15, 10, 0),
    )
    TimeRecord.objects.create(
        link=link_b,
        start_time=datetime(2026, 9, 15, 10, 0),
        end_time=datetime(2026, 9, 15, 10, 30),
    )

    with _now(datetime(2026, 9, 15, 12, 0)):
        stats = Statistics.today()

    by_task = {ts.task: ts for ts in stats.per_task}
    assert by_task[task_a].duration == timedelta(hours=1)
    assert by_task[task_a].budget == 20.0
    assert by_task[task_b].duration == timedelta(minutes=30)
    assert by_task[task_b].budget == 10.0
    assert stats.total_duration == timedelta(hours=1, minutes=30)
    assert stats.total_budget == 30.0


def test_statistics_today_excludes_yesterdays_time():
    mou = MoU.objects.create(name="nlnet-2026")
    task = Task.objects.create(mou=mou, name="10a")
    link = Link.objects.create(task=task, url="https://example.com/issues/1")
    TimeRecord.objects.create(
        link=link,
        start_time=datetime(2026, 9, 14, 20, 0),
        end_time=datetime(2026, 9, 14, 21, 0),
    )

    with _now(datetime(2026, 9, 15, 12, 0)):
        stats = Statistics.today()

    assert stats.per_task == []


def test_statistics_today_only_counts_the_part_of_a_midnight_crossing_session():
    mou = MoU.objects.create(name="nlnet-2026")
    task = Task.objects.create(mou=mou, name="10a")
    link = Link.objects.create(task=task, url="https://example.com/issues/1")
    TimeRecord.objects.create(
        link=link,
        start_time=datetime(2026, 9, 14, 23, 0),
        end_time=datetime(2026, 9, 15, 1, 0),
    )

    with _now(datetime(2026, 9, 15, 12, 0)):
        stats = Statistics.today()

    assert stats.per_task[0].duration == timedelta(hours=1)


def test_statistics_today_groups_untracked_time_under_no_task():
    link = Link.objects.create(url="https://example.com/issues/1")
    TimeRecord.objects.create(
        link=link,
        start_time=datetime(2026, 9, 15, 9, 0),
        end_time=datetime(2026, 9, 15, 10, 0),
    )

    with _now(datetime(2026, 9, 15, 12, 0)):
        stats = Statistics.today()

    assert stats.per_task[0].task is None
    assert stats.per_task[0].duration == timedelta(hours=1)


def test_statistics_today_omits_budget_when_rate_is_unset(settings):
    settings.RFP_EUROS_PER_HOUR = None
    mou = MoU.objects.create(name="nlnet-2026")
    task = Task.objects.create(mou=mou, name="10a")
    link = Link.objects.create(task=task, url="https://example.com/issues/1")
    TimeRecord.objects.create(
        link=link,
        start_time=datetime(2026, 9, 15, 9, 0),
        end_time=datetime(2026, 9, 15, 10, 0),
    )

    with _now(datetime(2026, 9, 15, 12, 0)):
        stats = Statistics.today()

    assert stats.per_task[0].budget is None
    assert stats.total_budget is None


def test_statistics_days_includes_the_last_n_calendar_days():
    mou = MoU.objects.create(name="nlnet-2026")
    task = Task.objects.create(mou=mou, name="10a")
    link = Link.objects.create(task=task, url="https://example.com/issues/1")
    TimeRecord.objects.create(
        link=link,
        start_time=datetime(2026, 9, 13, 9, 0),
        end_time=datetime(2026, 9, 13, 10, 0),
    )

    with _now(datetime(2026, 9, 15, 12, 0)):
        stats = Statistics.days(3)

    assert stats.total_duration == timedelta(hours=1)


def test_statistics_days_excludes_time_before_the_window():
    mou = MoU.objects.create(name="nlnet-2026")
    task = Task.objects.create(mou=mou, name="10a")
    link = Link.objects.create(task=task, url="https://example.com/issues/1")
    TimeRecord.objects.create(
        link=link,
        start_time=datetime(2026, 9, 11, 9, 0),
        end_time=datetime(2026, 9, 11, 10, 0),
    )

    with _now(datetime(2026, 9, 15, 12, 0)):
        stats = Statistics.days(3)

    assert stats.total_duration == timedelta()


def test_statistics_days_clips_a_record_crossing_the_window_start():
    mou = MoU.objects.create(name="nlnet-2026")
    task = Task.objects.create(mou=mou, name="10a")
    link = Link.objects.create(task=task, url="https://example.com/issues/1")
    # days(3) as of "now" 2026-09-15 starts at midnight 2026-09-13.
    TimeRecord.objects.create(
        link=link,
        start_time=datetime(2026, 9, 12, 23, 0),
        end_time=datetime(2026, 9, 13, 1, 0),
    )

    with _now(datetime(2026, 9, 15, 12, 0)):
        stats = Statistics.days(3)

    assert stats.total_duration == timedelta(hours=1)


def test_statistics_days_rejects_a_non_positive_n():
    with pytest.raises(ValueError, match="positive"):
        Statistics.days(0)


def test_statistics_sorts_tasks_and_puts_untracked_time_last():
    mou = MoU.objects.create(name="nlnet-2026")
    task_10a = Task.objects.create(mou=mou, name="10a")
    task_9a = Task.objects.create(mou=mou, name="9a")
    link_10a = Link.objects.create(task=task_10a, url="https://example.com/issues/1")
    link_9a = Link.objects.create(task=task_9a, url="https://example.com/issues/2")
    link_none = Link.objects.create(url="https://example.com/issues/3")
    for link in (link_10a, link_9a, link_none):
        TimeRecord.objects.create(
            link=link,
            start_time=datetime(2026, 9, 15, 9, 0),
            end_time=datetime(2026, 9, 15, 10, 0),
        )

    with _now(datetime(2026, 9, 15, 12, 0)):
        stats = Statistics.today()

    assert [ts.task for ts in stats.per_task] == [task_9a, task_10a, None]
