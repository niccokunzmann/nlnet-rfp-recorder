from __future__ import annotations

import warnings

from django.db import models

from nlnet_rfp_recorder.github import Discussion, Issue, PullRequest

from .tag import TAG_NAMES, Tag
from .task import Task


class Link(models.Model):
    task = models.ForeignKey(
        Task, null=True, blank=True, on_delete=models.SET_NULL, related_name="links"
    )
    url = models.URLField(unique=True)
    tags = models.ManyToManyField(Tag, related_name="links", blank=True)
    # Set when the user decides, on removing this link from a report during
    # `report import`, that its time should never be offered for a report
    # again - not merely "not yet reported". See Task.links_with_unreported_time.
    excluded_from_reports = models.BooleanField(default=False)
    # Cached issue/PR title, fetched from GitHub once (see
    # Report.ensure_link_titles) and kept indefinitely - blank means not
    # fetched yet (or not an issue/PR), not "known to have no title".
    title = models.CharField(max_length=255, blank=True, default="")

    @classmethod
    def get_or_create_for_task(cls, url: str, task: Task) -> Link:
        link, created = cls.objects.get_or_create(url=url, defaults={"task": task})
        if created:
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
