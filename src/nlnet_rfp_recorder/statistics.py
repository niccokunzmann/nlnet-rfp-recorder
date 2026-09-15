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
    """

    task: Task | None
    duration: timedelta
    budget: float | None


@dataclass
class Statistics:
    """Time (and budget) tracked in a date window, per task and in total.

    Not a Django model - built on top of TimeRecord.get_statistics(),
    which does the actual database work and window-clipping, so this
    class is plain grouping/summing logic that can be tested on its own.
    """

    per_task: list[TaskStatistics]
    total_duration: timedelta
    total_budget: float | None

    @classmethod
    def _for_window(cls, start: date | None, end: date | None) -> Statistics:
        from nlnet_rfp_recorder.timetracking.models import TimeRecord

        spans = TimeRecord.get_statistics(start=start, end=end)
        rate = settings.RFP_EUROS_PER_HOUR

        durations: dict[Task | None, timedelta] = {}
        for span in spans:
            task = span.record.link.task if span.record.link else None
            durations[task] = durations.get(task, timedelta()) + span.duration

        def _sort_key(task: Task | None) -> tuple:
            # Untracked time (no task) sorts last, after every real task
            # in number-then-letter order.
            return (1,) if task is None else (0, task.sort_key)

        def _budget(duration: timedelta) -> float | None:
            if rate is None:
                return None
            return duration.total_seconds() / 3600 * rate

        per_task = [
            TaskStatistics(
                task=task, duration=durations[task], budget=_budget(durations[task])
            )
            for task in sorted(durations, key=_sort_key)
        ]
        total_duration = sum(durations.values(), timedelta())
        return cls(
            per_task=per_task,
            total_duration=total_duration,
            total_budget=_budget(total_duration),
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
