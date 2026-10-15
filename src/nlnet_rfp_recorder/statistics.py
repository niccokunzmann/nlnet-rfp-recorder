from __future__ import annotations

from dataclasses import dataclass
from datetime import date, timedelta
from typing import TYPE_CHECKING

from django.conf import settings
from django.utils import timezone

if TYPE_CHECKING:
    from nlnet_rfp_recorder.github import GitHubReference
    from nlnet_rfp_recorder.timetracking.models import MoU, Task


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
    def _for_window(
        cls, start: date | None, end: date | None, mou: MoU | None = None
    ) -> Statistics:
        from nlnet_rfp_recorder.timetracking.models import TimeRecord

        spans = TimeRecord.get_statistics(start=start, end=end)
        rate = settings.RFP_EUROS_PER_HOUR
        threshold = settings.REVIEW_DEFAULT_EXCLUDE_BELOW

        durations: dict[Task | None, timedelta] = {}
        new_durations: dict[Task | None, timedelta] = {}
        for span in spans:
            task = span.record.link.task if span.record.link else None
            if mou is not None and (task is None or task.mou_id != mou.pk):
                continue
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
    def today(cls, mou: MoU | None = None) -> Statistics:
        """Time tracked since midnight today, through now.

        With `mou`, only that MoU's tasks count (untracked time is left out).
        """
        return cls._for_window(timezone.now().date(), None, mou)

    @classmethod
    def days(cls, n: int, mou: MoU | None = None) -> Statistics:
        """Time tracked in the last `n` calendar days (including today),
        through now.
        """
        if n <= 0:
            raise ValueError("Number of days must be positive.")
        since = timezone.now().date() - timedelta(days=n - 1)
        return cls._for_window(since, None, mou)

    @classmethod
    def total(cls, mou: MoU | None = None) -> Statistics:
        """Time tracked across all recorded history, through now."""
        return cls._for_window(None, None, mou)


BIN_MINUTES = 10


def closed_link_durations(
    mou: MoU, tag: str, token: str | None = None
) -> tuple[list[timedelta], list[timedelta]]:
    """Total time per closed issue and per closed pull request.

    Returns (issue_durations, pull_request_durations), one entry per URL:
    the summed time of its finished records. Only links of `mou`'s tasks
    tagged `tag` ("implementation" or "review") whose GitHub state is
    closed are counted; other kinds of link are ignored.
    """
    from nlnet_rfp_recorder.timetracking.models import Link

    links = Link.objects.filter(task__mou=mou, tags__name=tag).distinct()
    totals: list[tuple[Link, GitHubReference, timedelta]] = []
    for link in links:
        reference = link.pr or link.issue
        if reference is None:
            continue
        total = sum(
            (
                record.end_time - record.start_time
                for record in link.time_records.exclude(end_time=None)
            ),
            timedelta(),
        )
        if total > timedelta():
            totals.append((link, reference, total))
    Link.update_from_github([link for link, _, _ in totals], token)
    issues, pull_requests = [], []
    for link, _, total in totals:
        if link.is_closed:
            (pull_requests if link.is_pr else issues).append(total)
    return issues, pull_requests


def histogram_rows(durations: list[timedelta]) -> list[tuple[str, int] | None]:
    """Count durations in 10-minute columns, from the first to the last
    non-empty one. Two or more empty columns in a row become one None
    (shown as "..."); a single empty column stays as a zero-count row.
    """
    counts: dict[int, int] = {}
    for duration in durations:
        index = int(duration.total_seconds() // 60) // BIN_MINUTES
        counts[index] = counts.get(index, 0) + 1
    if not counts:
        return []
    rows: list[tuple[str, int] | None] = []
    empty_run: list[int] = []

    def flush() -> None:
        if len(empty_run) == 1:
            rows.append(_bin_row(empty_run[0], 0))
        elif empty_run:
            rows.append(None)
        empty_run.clear()

    for index in range(min(counts), max(counts) + 1):
        if index in counts:
            flush()
            rows.append(_bin_row(index, counts[index]))
        else:
            empty_run.append(index)
    return rows


def _bin_row(index: int, count: int) -> tuple[str, int]:
    low = index * BIN_MINUTES
    return f"{low}-{low + BIN_MINUTES} min", count


def median_duration(durations: list[timedelta]) -> timedelta:
    ordered = sorted(durations)
    middle = len(ordered) // 2
    if len(ordered) % 2:
        return ordered[middle]
    return (ordered[middle - 1] + ordered[middle]) / 2
