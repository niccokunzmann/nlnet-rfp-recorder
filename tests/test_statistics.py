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


def test_statistics_total_includes_records_regardless_of_age():
    mou = MoU.objects.create(name="nlnet-2026")
    task = Task.objects.create(mou=mou, name="10a")
    link = Link.objects.create(task=task, url="https://example.com/issues/1")
    TimeRecord.objects.create(
        link=link,
        start_time=datetime(2010, 1, 1, 9, 0),
        end_time=datetime(2010, 1, 1, 10, 0),
    )

    with _now(datetime(2026, 9, 15, 12, 0)):
        stats = Statistics.total()

    assert stats.total_duration == timedelta(hours=1)


def test_statistics_total_stops_clipping_at_now():
    mou = MoU.objects.create(name="nlnet-2026")
    task = Task.objects.create(mou=mou, name="10a")
    link = Link.objects.create(task=task, url="https://example.com/issues/1")
    TimeRecord.objects.create(link=link, start_time=datetime(2026, 9, 15, 10, 0))

    with _now(datetime(2026, 9, 15, 12, 0)):
        stats = Statistics.total()

    assert stats.total_duration == timedelta(hours=2)


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


def test_statistics_new_above_threshold_is_set_when_new_budget_qualifies(settings):
    settings.RFP_EUROS_PER_HOUR = 20.0
    settings.REVIEW_DEFAULT_EXCLUDE_BELOW = 50
    mou = MoU.objects.create(name="nlnet-2026")
    task = Task.objects.create(mou=mou, name="10a")
    link = Link.objects.create(task=task, url="https://example.com/issues/1")
    TimeRecord.objects.create(
        link=link,
        start_time=datetime(2026, 9, 15, 6, 0),
        end_time=datetime(2026, 9, 15, 9, 0),
    )

    with _now(datetime(2026, 9, 15, 12, 0)):
        stats = Statistics.today()

    assert stats.threshold == 50
    assert stats.per_task[0].new_budget == 60.0
    assert stats.per_task[0].new_above_threshold == 60.0
    assert stats.total_new_above_threshold == 60.0


def test_statistics_new_above_threshold_is_none_below_the_threshold(settings):
    settings.RFP_EUROS_PER_HOUR = 20.0
    settings.REVIEW_DEFAULT_EXCLUDE_BELOW = 50
    mou = MoU.objects.create(name="nlnet-2026")
    task = Task.objects.create(mou=mou, name="10a")
    link = Link.objects.create(task=task, url="https://example.com/issues/1")
    TimeRecord.objects.create(
        link=link,
        start_time=datetime(2026, 9, 15, 11, 0),
        end_time=datetime(2026, 9, 15, 12, 0),
    )

    with _now(datetime(2026, 9, 15, 12, 0)):
        stats = Statistics.today()

    assert stats.per_task[0].new_budget == 20.0
    assert stats.per_task[0].new_above_threshold is None
    assert stats.total_new_above_threshold == 0.0


def test_statistics_total_new_above_threshold_sums_only_qualifying_tasks(settings):
    settings.RFP_EUROS_PER_HOUR = 20.0
    settings.REVIEW_DEFAULT_EXCLUDE_BELOW = 50
    mou = MoU.objects.create(name="nlnet-2026")
    task_a = Task.objects.create(mou=mou, name="10a")
    task_b = Task.objects.create(mou=mou, name="11b")
    link_a = Link.objects.create(task=task_a, url="https://example.com/issues/1")
    link_b = Link.objects.create(task=task_b, url="https://example.com/issues/2")
    TimeRecord.objects.create(
        link=link_a,
        start_time=datetime(2026, 9, 15, 6, 0),
        end_time=datetime(2026, 9, 15, 9, 0),
    )
    TimeRecord.objects.create(
        link=link_b,
        start_time=datetime(2026, 9, 15, 11, 0),
        end_time=datetime(2026, 9, 15, 12, 0),
    )

    with _now(datetime(2026, 9, 15, 12, 0)):
        stats = Statistics.today()

    by_task = {ts.task: ts for ts in stats.per_task}
    assert by_task[task_a].new_above_threshold == 60.0
    assert by_task[task_b].new_above_threshold is None
    assert stats.total_new_above_threshold == 60.0


def test_statistics_new_above_threshold_is_none_when_rate_is_unset(settings):
    settings.RFP_EUROS_PER_HOUR = None
    mou = MoU.objects.create(name="nlnet-2026")
    task = Task.objects.create(mou=mou, name="10a")
    link = Link.objects.create(task=task, url="https://example.com/issues/1")
    TimeRecord.objects.create(
        link=link,
        start_time=datetime(2026, 9, 15, 6, 0),
        end_time=datetime(2026, 9, 15, 9, 0),
    )

    with _now(datetime(2026, 9, 15, 12, 0)):
        stats = Statistics.today()

    assert stats.per_task[0].new_above_threshold is None
    assert stats.total_new_above_threshold is None


def test_statistics_total_budget_sums_the_rounded_per_task_values(settings):
    # Rate chosen so each task's exact budget is 0.6€ (rounds to 1€), but
    # the exact combined total is 1.2€ (rounds to 1€ on its own) - if the
    # total were recomputed from the aggregate duration instead of summing
    # what's actually shown per task, it would read 1€ instead of 2€.
    settings.RFP_EUROS_PER_HOUR = 60.0
    mou = MoU.objects.create(name="nlnet-2026")
    task_a = Task.objects.create(mou=mou, name="10a")
    task_b = Task.objects.create(mou=mou, name="11b")
    link_a = Link.objects.create(task=task_a, url="https://example.com/issues/1")
    link_b = Link.objects.create(task=task_b, url="https://example.com/issues/2")
    start = datetime(2026, 9, 15, 9, 0)
    TimeRecord.objects.create(
        link=link_a, start_time=start, end_time=start + timedelta(seconds=36)
    )
    TimeRecord.objects.create(
        link=link_b, start_time=start, end_time=start + timedelta(seconds=36)
    )

    with _now(datetime(2026, 9, 15, 12, 0)):
        stats = Statistics.today()

    assert [ts.budget for ts in stats.per_task] == [1, 1]
    assert stats.total_budget == 2
