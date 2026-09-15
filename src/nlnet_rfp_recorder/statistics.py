from __future__ import annotations

from dataclasses import dataclass
from datetime import date, timedelta
from typing import TYPE_CHECKING

from django.conf import settings
from django.utils import timezone

if TYPE_CHECKING:
    from nlnet_rfp_recorder.timetracking.models import Task


@dataclass
class TaskStatistics:
    """One task's tracked time (and budget, if the hourly rate is known)
    within a Statistics window.

    budget/new_budget/new_above_threshold are pre-rounded to the nearest
    whole euro - the same figure is used for display, for the threshold
    check, and for summing into Statistics.total_* (see there), so a
    printed total always matches adding up the rows printed above it.
    """

    task: Task | None
    duration: timedelta
    budget: int | None
    # The portion of `budget` for time not yet claimed by any report line -
    # i.e. still available to be reported. None alongside `budget` when
    # the hourly rate is unknown.
    new_budget: int | None
    # `new_budget`, but only when it's at least Statistics.threshold -
    # None below that (including when new_budget itself is None) - see
    # the REVIEW_DEFAULT_EXCLUDE_BELOW setting.
    new_above_threshold: int | None


@dataclass
class Statistics:
    """Time (and budget) tracked in a date window, per task and in total.

    Not a Django model - built on top of TimeRecord.get_statistics(),
    which does the actual database work and window-clipping, so this
    class is plain grouping/summing logic that can be tested on its own.
    """

    per_task: list[TaskStatistics]
    total_duration: timedelta
    # Each total_* is the sum of the matching (already-rounded) per_task
    # values, not independently recomputed from total_duration - so it
    # always equals adding up the rows shown above it.
    total_budget: int | None
    total_new_budget: int | None
    total_new_above_threshold: int | None
    threshold: float

    @classmethod
    def _for_window(cls, start: date | None, end: date | None) -> Statistics:
        from nlnet_rfp_recorder.timetracking.models import TimeRecord

        spans = TimeRecord.get_statistics(start=start, end=end)
        rate = settings.RFP_EUROS_PER_HOUR
        threshold = settings.REVIEW_DEFAULT_EXCLUDE_BELOW

        durations: dict[Task | None, timedelta] = {}
        new_durations: dict[Task | None, timedelta] = {}
        for span in spans:
            task = span.record.link.task if span.record.link else None
            durations[task] = durations.get(task, timedelta()) + span.duration
            if span.record.report_line_id is None:
                new_durations[task] = (
                    new_durations.get(task, timedelta()) + span.duration
                )

        def _sort_key(task: Task | None) -> tuple:
            # Untracked time (no task) sorts last, after every real task
            # in number-then-letter order.
            return (1,) if task is None else (0, task.sort_key)

        def _budget(duration: timedelta) -> int | None:
            if rate is None:
                return None
            return round(duration.total_seconds() / 3600 * rate)

        def _above_threshold(value: int | None) -> int | None:
            if value is None or value < threshold:
                return None
            return value

        per_task = []
        for task in sorted(durations, key=_sort_key):
            new_budget = _budget(new_durations.get(task, timedelta()))
            per_task.append(
                TaskStatistics(
                    task=task,
                    duration=durations[task],
                    budget=_budget(durations[task]),
                    new_budget=new_budget,
                    new_above_threshold=_above_threshold(new_budget),
                )
            )

        total_duration = sum(durations.values(), timedelta())

        def _total(values: list[int | None]) -> int | None:
            if rate is None:
                return None
            return sum(value for value in values if value is not None)

        return cls(
            per_task=per_task,
            total_duration=total_duration,
            total_budget=_total([ts.budget for ts in per_task]),
            total_new_budget=_total([ts.new_budget for ts in per_task]),
            total_new_above_threshold=_total(
                [ts.new_above_threshold for ts in per_task]
            ),
            threshold=threshold,
        )

    @classmethod
    def today(cls) -> Statistics:
        """Time tracked since midnight today, through now."""
        return cls._for_window(timezone.now().date(), None)

    @classmethod
    def days(cls, n: int) -> Statistics:
        """Time tracked in the last `n` calendar days (including today),
        through now.
        """
        if n <= 0:
            raise ValueError("Number of days must be positive.")
        since = timezone.now().date() - timedelta(days=n - 1)
        return cls._for_window(since, None)
