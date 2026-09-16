from __future__ import annotations

import re
import warnings
from datetime import timedelta
from functools import cached_property
from typing import TYPE_CHECKING

from django.conf import settings
from django.core.validators import RegexValidator
from django.db import models

from nlnet_rfp_recorder.budget import BudgetLine
from nlnet_rfp_recorder.github import (
    Discussion,
    Issue,
    PullRequest,
    Status,
    fetch_statuses,
)
from nlnet_rfp_recorder.warnings import TaskWarning

from .github_token import GitHubToken
from .mou import MoU

if TYPE_CHECKING:
    from .link import Link
    from .time_record import TimeRecord

task_name_validator = RegexValidator(
    regex=r"^\d+[a-z]+$",
    message="Task name must look like '10a': a number followed by letters.",
)


class Task(models.Model):
    mou = models.ForeignKey(
        MoU,
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name="tasks",
        help_text="The MoU this task belongs to, or None if its MoU was removed.",
    )
    name = models.CharField(
        max_length=16,
        validators=[task_name_validator],
        help_text=(
            "The task's code as used in the MoU budget, e.g. '10a' (a "
            "number followed by letters)."
        ),
    )
    selected = models.BooleanField(
        default=True,
        help_text=(
            "Whether this is the currently selected task (`rfp task "
            "select`) - at most one task is selected at a time."
        ),
    )
    max_budget = models.FloatField(
        null=True,
        blank=True,
        help_text=(
            "The maximum amount (EUR) the MoU allows for this task, e.g. "
            "as imported from a milestone budget table."
        ),
    )
    used_budget = models.FloatField(
        default=0.0,
        help_text=(
            "Budget (EUR) already spent on this task before this tool "
            "started tracking it, e.g. from an earlier milestone report. "
            "Set via `rfp mou import` or `rfp task set used`."
        ),
    )
    personal_budget = models.FloatField(
        null=True,
        blank=True,
        help_text=(
            "The amount (EUR) you've personally decided to spend on this "
            "task, up to max_budget - used as the budget_line total for "
            "completion/time-left purposes. Set via `rfp task set "
            "--budget`. Falls back to max_budget when unset - see "
            "effective_personal_budget."
        ),
    )
    description = models.TextField(
        blank=True,
        default="",
        help_text=(
            "Free-text description of the task, shown by `rfp task "
            "status`/`select` and `rfp task list`."
        ),
    )

    class Meta:
        constraints = [
            models.UniqueConstraint(fields=["mou", "name"], name="unique_task_per_mou")
        ]

    @classmethod
    def select(cls, name: str, mou: MoU | None = None) -> Task:
        # A task's identity is (mou, name): the same milestone code can
        # exist under different MoUs without clashing.
        mou = mou or MoU.get_selected()
        if mou is None:
            raise ValueError("No MoU selected. Run `rfp mou add <name>` first.")
        # (mou, name) reusing an existing row is the expected, non-error
        # case here - get_or_create below handles it, so skip both
        # uniqueness checks (the legacy one and the Meta.constraints one).
        cls(name=name, mou=mou).full_clean(
            exclude=["selected"], validate_unique=False, validate_constraints=False
        )

        task, created = cls.objects.get_or_create(
            mou=mou, name=name, defaults={"selected": True}
        )
        if created:
            warnings.warn(
                f"Task {name} is new for MoU {mou}; it should already be in "
                "the MoU's budget. Run `rfp mou import` first if it isn't.",
                TaskWarning,
                stacklevel=2,
            )
        cls.objects.exclude(pk=task.pk).update(selected=False)
        if not created:
            task.selected = True
            task.save(update_fields=["selected"])
        return task

    @classmethod
    def get_selected(cls) -> Task | None:
        return cls.objects.filter(selected=True).first()

    def mark_selected(self) -> None:
        """Make this the currently selected task, deselecting any other.

        Unlike `select`, this takes an existing instance rather than a
        name to look up or create - for callers (like starting a time
        entry) that already have the task in hand and just need it to
        become the one "currently worked on".
        """
        if self.selected:
            return
        type(self).objects.exclude(pk=self.pk).update(selected=False)
        self.selected = True
        self.save(update_fields=["selected"])

    @classmethod
    def resolve_query(cls, query: str, mou: MoU | None = None) -> list[Task]:
        """Resolve a task-selector string to the matching tasks in `mou`.

        `query` is a comma-separated list of terms, each one of:

        - an exact task name or alias: "10a"
        - a dash-separated range of two task names/aliases, inclusive
          and ordered by (number, letters): "1a-1c" selects 1a, 1b, 1c
        - a bare prefix shared by several task names: "4" selects every
          task whose name starts with "4" (4a, 4b, 40a, ...) - handy
          for updating a whole numbered group at once

        Terms are tried in that order (exact, then range, then prefix),
        so an exact name/alias always wins over treating it as a
        prefix. Results are de-duplicated and returned sorted by
        sort_key, regardless of how the terms overlapped or their order
        in `query`.

        Raises ValueError - naming the offending term - if a term
        matches nothing, if a range's ends are out of order, or if no
        MoU is selected.
        """
        from .alias import resolve_task_name

        mou = mou or MoU.get_selected()
        if mou is None:
            raise ValueError("No MoU selected. Run `rfp mou add <name>` first.")

        terms = [term.strip() for term in query.split(",")]
        if not terms or any(not term for term in terms):
            raise ValueError(f"Empty task in query: {query!r}.")

        tasks = list(cls.objects.filter(mou=mou))
        by_name = {task.name: task for task in tasks}

        def _lookup(raw: str) -> Task | None:
            return by_name.get(resolve_task_name(raw, mou))

        matched: dict[int, Task] = {}
        for term in terms:
            if "-" in term:
                start_raw, _, end_raw = term.partition("-")
                if not start_raw or not end_raw or "-" in end_raw:
                    raise ValueError(f"Invalid task range: {term!r}.")
                start, end = _lookup(start_raw), _lookup(end_raw)
                if start is None:
                    raise ValueError(f"No such task: {start_raw}.")
                if end is None:
                    raise ValueError(f"No such task: {end_raw}.")
                if start.sort_key > end.sort_key:
                    raise ValueError(
                        f"Invalid task range: {term!r} (end before start)."
                    )
                for task in tasks:
                    if start.sort_key <= task.sort_key <= end.sort_key:
                        matched[task.pk] = task
                continue

            exact = _lookup(term)
            if exact is not None:
                matched[exact.pk] = exact
                continue

            prefix_matches = [task for task in tasks if task.name.startswith(term)]
            if not prefix_matches:
                raise ValueError(f"No such task: {term}.")
            for task in prefix_matches:
                matched[task.pk] = task

        return sorted(matched.values(), key=lambda task: task.sort_key)

    @property
    def links_with_tracked_time(self) -> models.QuerySet[Link]:
        return self.links.filter(time_records__isnull=False).distinct()

    @property
    def links_with_unreported_time(self) -> models.QuerySet[Link]:
        """Links with tracked time that isn't part of any report yet.

        A link can have both reported and unreported time records (more
        time tracked against it after it was already reported), so this is
        a distinct set from links_with_tracked_time, not a subset filter on
        top of it. Excludes links the user has permanently excluded from
        reports (see Link.excluded_from_reports).
        """
        return self.links.filter(
            time_records__isnull=False,
            time_records__report_line__isnull=True,
            excluded_from_reports=False,
        ).distinct()

    @cached_property
    def _pr_link_statuses(self) -> dict[int, Status]:
        links = list(self.links_with_unreported_time)
        pr_links = [link for link in links if link.is_pr]
        references = [link.pr for link in pr_links]
        token = GitHubToken.get()
        statuses = fetch_statuses(references, token=token) if references else []
        return dict(zip((link.id for link in pr_links), statuses, strict=True))

    @cached_property
    def billable_links(self) -> list[Link]:
        # Issues count once tracked regardless of GitHub status - opening
        # an issue is real work either way. PRs only count once merged or
        # closed, since work on an open PR isn't done yet. Anything that
        # isn't an issue or PR is always billable. A record still running
        # counts too (see TimeRecord.duration) - its live elapsed time is
        # part of "how much has been used" until it's stopped. Only
        # unreported links are considered - once time is part of a report,
        # its contribution comes from the report line's (possibly
        # overridden) budget instead; see reported_budget.
        links = list(self.links_with_unreported_time)
        statuses = self._pr_link_statuses
        return [
            link
            for link in links
            if not link.is_pr or statuses.get(link.id) == Status.CLOSED
        ]

    @cached_property
    def excluded_pull_request_links(self) -> list[Link]:
        """Tracked PR links held back from billable_links because still open."""
        links = list(self.links_with_unreported_time)
        statuses = self._pr_link_statuses
        return [
            link
            for link in links
            if link.is_pr and statuses.get(link.id) != Status.CLOSED
        ]

    @property
    def tracked_time_records(self) -> models.QuerySet[TimeRecord]:
        """Every unreported time record for this task, billable or not.

        Deliberately not filtered down to billable_links: this feeds
        duration/budget, which drive the personal status/task-list view
        of "how much have I worked and how much budget is left" - time
        on an open PR is real work already done even if it isn't
        reportable yet. Report generation applies its own, independent
        billable-vs-excluded check (see _billable_and_excluded_links in
        report.py) and never reads this property.
        """
        from .time_record import TimeRecord

        link_ids = [link.id for link in self.links_with_unreported_time]
        return TimeRecord.objects.filter(link_id__in=link_ids, report_line__isnull=True)

    @property
    def duration(self) -> timedelta:
        return sum(
            (record.duration for record in self.tracked_time_records), timedelta()
        )

    @property
    def reported_budget(self) -> float:
        """Budget already locked in by report lines for this task's links."""
        from .report import ReportLine

        if settings.RFP_EUROS_PER_HOUR is None:
            return 0.0
        return sum(line.budget for line in ReportLine.objects.filter(link__task=self))

    @property
    def budget(self) -> float | None:
        if settings.RFP_EUROS_PER_HOUR is None:
            return None
        live = self.duration.total_seconds() / 3600 * settings.RFP_EUROS_PER_HOUR
        return live + self.reported_budget

    @property
    def effective_personal_budget(self) -> float | None:
        """personal_budget, falling back to max_budget when unset."""
        if self.personal_budget is not None:
            return self.personal_budget
        return self.max_budget

    @property
    def budget_line(self) -> BudgetLine | None:
        """Completion (money used/total, and time left) against the
        effective personal budget - see effective_personal_budget.
        """
        total = self.effective_personal_budget
        if total is None:
            return None
        used = self.used_budget + (self.budget or 0.0)
        return BudgetLine(used=used, total=total, rate=settings.RFP_EUROS_PER_HOUR)

    def set_budget(self, amount: float) -> None:
        """Set personal_budget to an absolute `amount` (euros).

        Requires max_budget to already be set (via `rfp mou import` or
        `rfp task set max`) - this picks a target within the maximum,
        it doesn't establish one. Rejects a negative amount, or one
        exceeding max_budget.
        """
        if self.max_budget is None:
            raise ValueError(
                f"Task {self.name} has no maximum budget yet. Set one "
                f"first, e.g. `rfp task set max {self.name} <euros>`."
            )
        if amount < 0:
            raise ValueError("Task budget cannot be negative.")
        if amount > self.max_budget:
            raise ValueError(
                f"Task budget cannot exceed the maximum of {self.max_budget:.0f}€."
            )
        self.personal_budget = amount
        self.save(update_fields=["personal_budget"])

    def set_budget_fraction(self, fraction: float) -> None:
        """Set personal_budget to `fraction` of max_budget (0.5 = 50%).

        Same bounds as set_budget, just expressed as a share of the
        maximum instead of an absolute figure: 0.0 to 1.0 (0% to 100%).
        """
        if self.max_budget is None:
            raise ValueError(
                f"Task {self.name} has no maximum budget yet. Set one "
                f"first, e.g. `rfp task set max {self.name} <euros>`."
            )
        if not 0.0 <= fraction <= 1.0:
            raise ValueError("Budget percentage must be between 0% and 100%.")
        self.set_budget(fraction * self.max_budget)

    def set_used_budget(self, amount: float) -> None:
        """Set used_budget - money already spent before tracking began.

        Only rejects a negative amount. Unlike set_budget, there's no
        maximum check: a task can legitimately already be over budget
        before this tool starts tracking it.
        """
        if amount < 0:
            raise ValueError("Used budget cannot be negative.")
        self.used_budget = amount
        self.save(update_fields=["used_budget"])

    def set_budget_used_fraction(self, fraction: float) -> None:
        """Set used_budget to `fraction` of max_budget (0.5 = 50%).

        Same bounds as set_used_budget, just expressed as a share of
        the maximum: no upper limit, since already being over 100%
        used is a real, legitimate starting point.
        """
        if self.max_budget is None:
            raise ValueError(
                f"Task {self.name} has no maximum budget yet. Set one "
                f"first, e.g. `rfp task set max {self.name} <euros>`."
            )
        if fraction < 0.0:
            raise ValueError("Used-budget percentage cannot be negative.")
        self.set_used_budget(fraction * self.max_budget)

    def set_max_budget(self, amount: float) -> None:
        """Set max_budget directly - correcting an MoU import, not
        normal use (see `rfp task set max`'s help).

        Only rejects a negative amount. Doesn't touch personal_budget
        or used_budget even if they now exceed the new maximum -
        callers making a deliberate correction are trusted to fix
        those too if needed.
        """
        if amount < 0:
            raise ValueError("Maximum budget cannot be negative.")
        self.max_budget = amount
        self.save(update_fields=["max_budget"])

    def set_max_budget_fraction(self, fraction: float) -> None:
        """Scale max_budget by `fraction` of its current value (1.1 = +10%).

        Unlike the budget/used fractions, there's no larger ceiling for
        the maximum itself to be a share of, so this scales the
        existing value instead - 0.5 halves it, 1.1 raises it by 10%.
        Only rejects a negative fraction; anything above 1.0 is a
        deliberate increase, not an error.
        """
        if self.max_budget is None:
            raise ValueError(
                f"Task {self.name} has no maximum budget yet to scale. "
                f"Set one first, e.g. `rfp task set max {self.name} <euros>`."
            )
        if fraction < 0.0:
            raise ValueError("Maximum-budget percentage cannot be negative.")
        self.set_max_budget(fraction * self.max_budget)

    @property
    def issues(self) -> list[Issue]:
        return [link.issue for link in self.billable_links if link.is_issue]

    @property
    def pull_requests(self) -> list[PullRequest]:
        return [link.pr for link in self.billable_links if link.is_pr]

    @property
    def discussions(self) -> list[Discussion]:
        return [link.discussion for link in self.billable_links if link.is_discussion]

    @property
    def other(self) -> list[str]:
        return [
            link.url
            for link in self.billable_links
            if not link.is_issue and not link.is_pr and not link.is_discussion
        ]

    @property
    def sort_key(self) -> tuple[int, str]:
        # name is validated as \d+[a-z]+ ("10a"), so plain string sort
        # would put "10a" before "9a" - sort by the number, then letters.
        number, letters = re.match(r"^(\d+)([a-z]+)$", self.name).groups()
        return (int(number), letters)

    def __lt__(self, other: object) -> bool:
        if not isinstance(other, Task):
            return NotImplemented
        return self.sort_key < other.sort_key

    @property
    def alias(self) -> str | None:
        """This task's alias, or None if it has none."""
        from .alias import Alias

        aliases = Alias.objects.filter(item_type="task", mou=self.mou, target=self.name)
        alias = aliases.order_by("pk").first()
        return alias.alias if alias else None

    @property
    def display_name(self) -> str:
        """This task's name, with its alias appended in parens if it has one."""
        alias = self.alias
        return f"{self.name} ({alias})" if alias else self.name

    def __str__(self) -> str:
        return self.name
