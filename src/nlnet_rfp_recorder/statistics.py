from __future__ import annotations

import math
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import date, timedelta
from typing import TYPE_CHECKING

from django.conf import settings
from django.utils import timezone

if TYPE_CHECKING:
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
    mou: MoU, tags: Sequence[str], token: str | None = None
) -> dict[str, tuple[list[timedelta], list[timedelta]]]:
    """Total time per closed issue and per closed pull request, per tag.

    Maps each tag ("implementation" or "review") to (issue_durations,
    pull_request_durations), one entry per URL: the summed time of its
    finished records. Only links of `mou`'s tasks carrying that tag whose
    GitHub state is closed are counted; other kinds of link are ignored.
    GitHub is asked once for all tags together (see Link.update_from_github).
    """
    from nlnet_rfp_recorder.timetracking.models import Link

    # A link carrying several tags is one object, so it is updated once.
    links: dict[int, Link] = {}
    totals: dict[str, list[tuple[Link, timedelta]]] = {}
    for tag in tags:
        totals[tag] = []
        for found in Link.objects.filter(task__mou=mou, tags__name=tag).distinct():
            link = links.setdefault(found.id, found)
            if not (link.is_pr or link.is_issue):
                continue
            total = sum(
                (
                    record.end_time - record.start_time
                    for record in link.time_records.exclude(end_time=None)
                ),
                timedelta(),
            )
            if total > timedelta():
                totals[tag].append((link, total))
    Link.update_from_github(
        [link for link in links.values() if link.is_pr or link.is_issue], token
    )
    result = {}
    for tag, entries in totals.items():
        issues = [total for link, total in entries if link.is_closed and not link.is_pr]
        pull_requests = [
            total for link, total in entries if link.is_closed and link.is_pr
        ]
        result[tag] = (issues, pull_requests)
    return result


def histogram_rows(
    series: Sequence[list[timedelta]],
) -> list[tuple[str, tuple[int, ...]] | None]:
    """Count each series' durations in shared 10-minute columns, from the
    first to the last column any series uses. Two or more columns in a row
    that are empty in every series become one None (shown as "..."); a
    single such column stays as a row of zero counts.
    """
    counts: dict[int, list[int]] = {}
    for position, durations in enumerate(series):
        for duration in durations:
            index = int(duration.total_seconds() // 60) // BIN_MINUTES
            counts.setdefault(index, [0] * len(series))[position] += 1
    if not counts:
        return []
    rows: list[tuple[str, tuple[int, ...]] | None] = []
    empty_run: list[int] = []

    def flush() -> None:
        if len(empty_run) == 1:
            rows.append(_bin_row(empty_run[0], [0] * len(series)))
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


def _bin_row(index: int, counts: list[int]) -> tuple[str, tuple[int, ...]]:
    low = index * BIN_MINUTES
    return f"{low}-{low + BIN_MINUTES} min", tuple(counts)


def median_duration(durations: list[timedelta]) -> timedelta:
    ordered = sorted(durations)
    middle = len(ordered) // 2
    if len(ordered) % 2:
        return ordered[middle]
    return (ordered[middle - 1] + ordered[middle]) / 2


def average_duration(durations: list[timedelta]) -> timedelta:
    """The mean - also the Poisson rate (lambda) estimate, so one function
    serves both the normal and the Poisson view."""
    return sum(durations, timedelta()) / len(durations)


def stdev_duration(durations: list[timedelta]) -> timedelta:
    """Sample standard deviation (normal distribution); zero for fewer
    than two durations."""
    if len(durations) < 2:
        return timedelta()
    seconds = [duration.total_seconds() for duration in durations]
    mean = sum(seconds) / len(seconds)
    variance = sum((value - mean) ** 2 for value in seconds) / (len(seconds) - 1)
    return timedelta(seconds=variance**0.5)


def poisson_stdev_duration(durations: list[timedelta]) -> timedelta:
    """Standard deviation if durations were Poisson counts of 10-minute
    units: the variance equals the mean, so sqrt(lambda) units.
    """
    rate = average_duration(durations) / timedelta(minutes=BIN_MINUTES)
    return timedelta(minutes=BIN_MINUTES) * rate**0.5


def poisson_median_duration(durations: list[timedelta]) -> timedelta:
    """Median of the Poisson distribution fitted to the durations (counted
    in 10-minute units): the smallest k whose cumulative probability
    reaches one half, so always a whole number of units.
    """
    rate = average_duration(durations) / timedelta(minutes=BIN_MINUTES)
    cumulative = 0.0
    k = 0
    while True:
        cumulative += math.exp(-rate + k * math.log(rate) - math.lgamma(k + 1))
        if cumulative >= 0.5:
            return timedelta(minutes=BIN_MINUTES) * k
        k += 1
