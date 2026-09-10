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
        MoU, null=True, blank=True, on_delete=models.SET_NULL, related_name="tasks"
    )
    name = models.CharField(max_length=16, validators=[task_name_validator])
    selected = models.BooleanField(default=True)
    max_budget = models.FloatField(null=True, blank=True)
    used_budget = models.FloatField(default=0.0)
    description = models.TextField(blank=True, default="")

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
        from .time_record import TimeRecord

        link_ids = [link.id for link in self.billable_links]
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

        if settings.RFP_EUROS is None:
            return 0.0
        return sum(line.budget for line in ReportLine.objects.filter(link__task=self))

    @property
    def budget(self) -> float | None:
        if settings.RFP_EUROS is None:
            return None
        live = self.duration.total_seconds() / 3600 * settings.RFP_EUROS
        return live + self.reported_budget

    @property
    def budget_line(self) -> BudgetLine | None:
        if self.max_budget is None:
            return None
        used = self.used_budget + (self.budget or 0.0)
        return BudgetLine(used=used, total=self.max_budget, rate=settings.RFP_EUROS)

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
    def display_name(self) -> str:
        """This task's name, with its alias appended in parens if it has one."""
        from .alias import Alias

        aliases = Alias.objects.filter(item_type="task", mou=self.mou, target=self.name)
        alias = aliases.order_by("pk").first()
        return f"{self.name} ({alias.alias})" if alias else self.name

    def __str__(self) -> str:
        return self.name
