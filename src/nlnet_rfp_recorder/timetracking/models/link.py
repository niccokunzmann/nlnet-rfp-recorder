from __future__ import annotations

import warnings
from collections.abc import Iterable
from datetime import datetime

from django.db import models
from django.utils import timezone

from nlnet_rfp_recorder.github import (
    Discussion,
    Issue,
    PullRequest,
    Status,
    fetch_states,
)

from .tag import TAG_NAMES, Tag
from .task import Task


def _local_naive(moment: datetime | None) -> datetime | None:
    """GitHub's UTC time as local naive time - the database stores no zones."""
    if moment is None or moment.tzinfo is None:
        return moment
    return moment.astimezone().replace(tzinfo=None)


class Link(models.Model):
    task = models.ForeignKey(
        Task,
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name="links",
        help_text=(
            "The task this link's tracked time counts toward, or None if unassigned."
        ),
    )
    url = models.URLField(
        help_text=(
            "The issue/PR/discussion URL (or other link) time was tracked against."
        ),
    )
    tags = models.ManyToManyField(
        Tag,
        related_name="links",
        blank=True,
        help_text="How this link's time counts, e.g. 'implementation' or 'review'.",
    )
    excluded_from_reports = models.BooleanField(
        default=False,
        help_text=(
            "Set when the user decides, on removing this link from a "
            "report during `report import`, that its time should never be "
            "offered for a report again - not merely 'not yet reported'. "
            "See Task.links_with_unreported_time."
        ),
    )
    title = models.CharField(
        max_length=255,
        blank=True,
        default="",
        help_text=(
            "Cached issue/PR title, fetched from GitHub once (see "
            "Report.ensure_link_titles) and kept indefinitely - blank "
            "means not fetched yet (or not an issue/PR), not 'known to "
            "have no title'."
        ),
    )

    closed_at = models.DateTimeField(
        null=True,
        blank=True,
        help_text=(
            "When GitHub says this issue/PR was closed, or None if not "
            "known to be closed. A closed (or merged) issue/PR is assumed "
            "to stay closed, so its status is not fetched again (see "
            "statistics.closed_link_durations)."
        ),
    )

    class Meta:
        constraints = [
            # An empty url means "no link yet" - each such entry gets its
            # own Link, so its task and tags can differ from the others'.
            models.UniqueConstraint(
                fields=["url"],
                condition=~models.Q(url=""),
                name="unique_nonempty_link_url",
            )
        ]

    @classmethod
    def get_or_create_for_task(cls, url: str, task: Task | None) -> Link:
        """Get or create the link for `url`, assigning it to `task`.

        `task` may be None - e.g. a link started with no task selected -
        in which case a fresh link is simply created taskless, and an
        existing one's task is never touched.
        """
        if url == "":
            return cls.objects.create(url="", task=task)
        link, created = cls.objects.get_or_create(url=url, defaults={"task": task})
        if created or task is None:
            return link
        if link.task_id is None:
            link.task = task
            link.save(update_fields=["task"])
        elif link.task_id != task.id:
            warnings.warn(
                f"{url} is already part of task {link.task}; keeping it there.",
                stacklevel=2,
            )
        return link

    @property
    def is_closed(self) -> bool:
        """Whether GitHub is known to have closed (or merged) this link.

        Only true after update_from_github saw it closed - never fetched
        on its own.
        """
        return self.closed_at is not None

    @classmethod
    def update_from_github(
        cls, links: Iterable[Link], token: str | None = None
    ) -> None:
        """Refresh closed_at and title of issue/PR links from GitHub.

        The one place GitHub data is stored on links: one batched request
        per link that still needs it, none for anything else. A link
        needs a request unless it is already known closed (closed stays
        closed) and has its title. Discussions and plain links are ignored.
        """
        pending = [
            link
            for link in links
            if (link.is_issue or link.is_pr) and (not link.is_closed or not link.title)
        ]
        if not pending:
            return
        states = fetch_states(
            [link.pr or link.issue or link.discussion for link in pending], token=token
        )
        changed = []
        for link, state in zip(pending, states, strict=True):
            if (
                state.status is Status.CLOSED
                and not link.is_discussion
                and not link.is_closed
            ):
                link.closed_at = _local_naive(state.closed_at) or timezone.now()
                changed.append(link)
            if state.title and not link.title:
                link.title = state.title
                if link not in changed:
                    changed.append(link)
        if changed:
            cls.objects.bulk_update(changed, ["closed_at", "title"])

    def add_tag(self, name: str) -> Tag:
        if name not in TAG_NAMES:
            raise ValueError(f"Unknown tag {name!r}. Must be one of {TAG_NAMES}.")
        tag, _ = Tag.objects.get_or_create(name=name)
        self.tags.add(tag)
        return tag

    @property
    def issue(self) -> Issue | None:
        return Issue.from_url(self.url)

    @property
    def pr(self) -> PullRequest | None:
        return PullRequest.from_url(self.url)

    @property
    def is_issue(self) -> bool:
        return self.issue is not None

    @property
    def is_pr(self) -> bool:
        return self.pr is not None

    @property
    def discussion(self) -> Discussion | None:
        return Discussion.from_url(self.url)

    @property
    def is_discussion(self) -> bool:
        return self.discussion is not None

    @property
    def sort_key(self) -> tuple:
        # Issues, PRs, and discussions sort by number; anything else sorts
        # after them, by URL. Numbers are never compared against each
        # other across kinds here since callers always sort within one
        # already-filtered kind.
        reference = self.pr or self.issue or self.discussion
        if reference is not None:
            return (0, reference.number)
        return (1, self.url)

    def __lt__(self, other: object) -> bool:
        if not isinstance(other, Link):
            return NotImplemented
        return self.sort_key < other.sort_key

    def __str__(self) -> str:
        return self.url
