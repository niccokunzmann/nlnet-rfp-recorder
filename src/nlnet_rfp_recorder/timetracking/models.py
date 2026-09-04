import warnings
from collections.abc import Iterable
from datetime import datetime, timedelta
from functools import cached_property

from django.conf import settings
from django.core.validators import RegexValidator
from django.db import models
from django.utils import timezone

from nlnet_rfp_recorder.budget import BudgetLine
from nlnet_rfp_recorder.github import Issue, PullRequest, Status, fetch_statuses
from nlnet_rfp_recorder.mou_budget import parse_milestone_budgets
from nlnet_rfp_recorder.timesheet import TimesheetRow, format_hhmmss, parse_hhmmss
from nlnet_rfp_recorder.warnings import TaskWarning

task_name_validator = RegexValidator(
    regex=r"^\d+[a-z]+$",
    message="Task name must look like '10a': a number followed by letters.",
)

mou_name_validator = RegexValidator(
    regex=r"^\S+$",
    message="MoU name must not contain spaces.",
)

TAG_NAMES = ("implementation", "review")


class GitHubToken(models.Model):
    token = models.CharField(max_length=255)

    @classmethod
    def set(cls, token: str) -> GitHubToken:
        cls.objects.all().delete()
        return cls.objects.create(token=token)

    @classmethod
    def get(cls) -> str | None:
        obj = cls.objects.first()
        return obj.token if obj else None


class MoU(models.Model):
    name = models.CharField(max_length=64, unique=True, validators=[mou_name_validator])
    selected = models.BooleanField(default=True)
    budget = models.CharField(max_length=255, blank=True, default="")

    @classmethod
    def select(cls, name: str) -> MoU:
        cls(name=name).full_clean(
            exclude=["selected", "budget"],
            validate_unique=False,
            validate_constraints=False,
        )
        cls.objects.exclude(name=name).update(selected=False)
        mou, _ = cls.objects.update_or_create(name=name, defaults={"selected": True})
        return mou

    @classmethod
    def select_existing(cls, name: str) -> MoU:
        try:
            mou = cls.objects.get(name=name)
        except cls.DoesNotExist:
            raise ValueError(
                f"No such MoU: {name}. Run `rfp mou add {name}` to create it."
            ) from None
        cls.objects.exclude(pk=mou.pk).update(selected=False)
        mou.selected = True
        mou.save(update_fields=["selected"])
        return mou

    @classmethod
    def get_selected(cls) -> MoU | None:
        return cls.objects.filter(selected=True).first()

    def set_budget(self, text: str) -> dict[str, Task]:
        """Parse a budget table and cap each milestone's budget."""
        self.budget = text
        self.save(update_fields=["budget"])

        tasks: dict[str, Task] = {}
        for code, milestone in parse_milestone_budgets(text).items():
            used = milestone.amount if milestone.done else 0.0
            task, _ = Task.objects.update_or_create(
                mou=self,
                name=code,
                defaults={"max_budget": milestone.amount, "used_budget": used},
            )
            tasks[code] = task
        return tasks

    @property
    def total_budget(self) -> float | None:
        amounts = [task.max_budget for task in self.tasks.all()]
        if not amounts or all(amount is None for amount in amounts):
            return None
        return sum(amount or 0.0 for amount in amounts)

    @property
    def total_used(self) -> float:
        return sum(task.used_budget + (task.budget or 0.0) for task in self.tasks.all())

    @property
    def budget_line(self) -> BudgetLine | None:
        total = self.total_budget
        if total is None:
            return None
        return BudgetLine(used=self.total_used, total=total, rate=settings.RFP_EUROS)

    def __str__(self) -> str:
        return self.name


class Task(models.Model):
    mou = models.ForeignKey(
        MoU, null=True, blank=True, on_delete=models.SET_NULL, related_name="tasks"
    )
    name = models.CharField(max_length=16, validators=[task_name_validator])
    selected = models.BooleanField(default=True)
    max_budget = models.FloatField(null=True, blank=True)
    used_budget = models.FloatField(default=0.0)

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
                "the MoU's budget. Run `rfp mou budget` first if it isn't.",
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

    @cached_property
    def _pr_link_statuses(self) -> dict[int, Status]:
        links = list(self.links_with_tracked_time)
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
        # part of "how much has been used" until it's stopped.
        links = list(self.links_with_tracked_time)
        statuses = self._pr_link_statuses
        return [
            link
            for link in links
            if not link.is_pr or statuses.get(link.id) == Status.CLOSED
        ]

    @cached_property
    def excluded_pull_request_links(self) -> list[Link]:
        """Tracked PR links held back from billable_links because still open."""
        links = list(self.links_with_tracked_time)
        statuses = self._pr_link_statuses
        return [
            link
            for link in links
            if link.is_pr and statuses.get(link.id) != Status.CLOSED
        ]

    @property
    def tracked_time_records(self) -> models.QuerySet[TimeRecord]:
        link_ids = [link.id for link in self.billable_links]
        return TimeRecord.objects.filter(link_id__in=link_ids)

    @property
    def duration(self) -> timedelta:
        return sum(
            (record.duration for record in self.tracked_time_records), timedelta()
        )

    @property
    def budget(self) -> float | None:
        if settings.RFP_EUROS is None:
            return None
        return self.duration.total_seconds() / 3600 * settings.RFP_EUROS

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
    def other(self) -> list[str]:
        return [
            link.url
            for link in self.billable_links
            if not link.is_issue and not link.is_pr
        ]

    def __str__(self) -> str:
        return self.name


class Tag(models.Model):
    name = models.CharField(
        max_length=32, unique=True, choices=[(name, name) for name in TAG_NAMES]
    )

    def __str__(self) -> str:
        return self.name


class Link(models.Model):
    task = models.ForeignKey(
        Task, null=True, blank=True, on_delete=models.SET_NULL, related_name="links"
    )
    url = models.URLField(unique=True)
    tags = models.ManyToManyField(Tag, related_name="links", blank=True)

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

    def __str__(self) -> str:
        return self.url


class TimeRecordManager(models.Manager):
    def running(self) -> models.QuerySet[TimeRecord]:
        return self.filter(end_time__isnull=True)


class TimeRecord(models.Model):
    link = models.ForeignKey(
        Link,
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name="time_records",
    )
    start_time = models.DateTimeField()
    end_time = models.DateTimeField(null=True, blank=True)
    report = models.ForeignKey(
        "Report",
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name="time_records",
    )

    objects = TimeRecordManager()

    @classmethod
    def start(
        cls,
        url: str,
        task: Task | None = None,
        tags: Iterable[str] = ("implementation",),
    ) -> TimeRecord:
        task = task or Task.get_selected()
        if task is None:
            raise ValueError("No task selected. Run `rfp task <name>` first.")
        cls.stop()
        link = Link.get_or_create_for_task(url, task)
        for tag in tags:
            link.add_tag(tag)
        return cls.objects.create(link=link, start_time=timezone.now())

    @classmethod
    def get_running(cls) -> TimeRecord | None:
        return cls.objects.running().order_by("-start_time").first()

    @classmethod
    def get_last(cls) -> TimeRecord | None:
        return cls.objects.order_by("-start_time").first()

    @classmethod
    def stop(cls, url: str | None = None) -> TimeRecord | None:
        record = cls.get_running()
        if record is None:
            return None
        if url is not None and record.link is not None and record.link.task is not None:
            record.link = Link.get_or_create_for_task(url, record.link.task)
        record.end_time = timezone.now()
        record.save()
        return record

    @property
    def is_running(self) -> bool:
        return self.end_time is None

    @property
    def duration(self) -> timedelta:
        return (self.end_time or timezone.now()) - self.start_time

    @property
    def budget(self) -> float | None:
        if settings.RFP_EUROS is None:
            return None
        return self.duration.total_seconds() / 3600 * settings.RFP_EUROS

    @property
    def row(self) -> TimesheetRow:
        task = self.link.task if self.link else None
        mou = task.mou if task else None
        tags = (
            ",".join(sorted(tag.name for tag in self.link.tags.all()))
            if self.link
            else ""
        )
        return TimesheetRow(
            pk=self.pk,
            mou=mou.name if mou else "",
            task=task.name if task else "",
            start=self.start_time.isoformat(),
            duration=format_hhmmss(self.duration),
            link=self.link.url if self.link else "",
            tags=tags,
            report=self.report_id or "",
        )

    @classmethod
    def apply_row(
        cls, row: TimesheetRow, allow_create_with_pk: bool = False
    ) -> TimeRecord:
        try:
            mou = MoU.objects.get(name=row.mou)
        except MoU.DoesNotExist:
            raise ValueError(
                f"No such MoU: {row.mou}. Run `rfp mou add {row.mou}` first."
            ) from None
        try:
            task = Task.objects.get(mou=mou, name=row.task)
        except Task.DoesNotExist:
            raise ValueError(
                f"No such task: {row.task} for MoU {row.mou}. "
                f"Run `rfp task select {row.task}` first."
            ) from None

        link = Link.get_or_create_for_task(row.link, task) if row.link else None
        if link is not None:
            for tag in filter(None, (t.strip() for t in row.tags.split(","))):
                link.add_tag(tag)

        report = None
        if row.report:
            try:
                report = Report.objects.get(pk=row.report)
            except Report.DoesNotExist:
                raise ValueError(f"No such report: {row.report}.") from None

        try:
            start_time = datetime.fromisoformat(row.start)
        except ValueError:
            raise ValueError(
                f"Invalid start time: {row.start!r}; expected ISO format."
            ) from None
        duration = parse_hhmmss(row.duration)
        end_time = start_time + duration

        if row.pk is None:
            record = cls.objects.create(
                link=link, start_time=start_time, end_time=end_time
            )
        else:
            try:
                record = cls.objects.get(pk=row.pk)
            except cls.DoesNotExist:
                if not allow_create_with_pk:
                    raise ValueError(f"No such time record: {row.pk}.") from None
                record = cls.objects.create(
                    pk=row.pk, link=link, start_time=start_time, end_time=end_time
                )
            else:
                record.link = link
                record.start_time = start_time
                record.end_time = end_time
                record.save()

        if report is not None:
            report.add_time_record(record)
        elif record.report_id is not None:
            record.report.remove_time_record(record)

        return record

    def __str__(self) -> str:
        return f"TimeRecord({self.start_time} - {self.end_time or 'running'})"


def _round10(amount: float) -> int:
    return round(amount / 10) * 10


def _format_report_link(link: Link) -> str:
    if link.is_issue:
        url = link.issue.url
    elif link.is_pr:
        url = link.pr.url
    else:
        url = link.url
    extra_tags = [tag.name for tag in link.tags.all() if tag.name != "implementation"]
    if extra_tags:
        return f"{url} ({', '.join(extra_tags)})"
    return url


def _billable_and_excluded_links(mou: MoU) -> tuple[list[TimeRecord], list[Link]]:
    """Unreported billable records and excluded (open PR) links for `mou`.

    Checks every open PR across all of the MoU's tasks in a single batched
    GitHub status request, instead of one request per task - otherwise the
    time this takes scales with the number of tasks/PRs involved.
    """
    links_by_task = {
        task: list(task.links_with_tracked_time)
        for task in Task.objects.filter(mou=mou)
    }
    all_pr_links = [
        link for links in links_by_task.values() for link in links if link.is_pr
    ]
    references = [link.pr for link in all_pr_links]
    token = GitHubToken.get()
    statuses = fetch_statuses(references, token=token) if references else []
    status_by_link_id = dict(
        zip((link.id for link in all_pr_links), statuses, strict=True)
    )

    records: list[TimeRecord] = []
    excluded_links: list[Link] = []
    for links in links_by_task.values():
        billable_ids = {
            link.id
            for link in links
            if not link.is_pr or status_by_link_id.get(link.id) == Status.CLOSED
        }
        records += list(
            TimeRecord.objects.filter(link_id__in=billable_ids, report__isnull=True)
        )
        excluded_links += [
            link
            for link in links
            if link.is_pr and status_by_link_id.get(link.id) != Status.CLOSED
        ]
    return records, excluded_links


class Report(models.Model):
    id = models.CharField(max_length=80, primary_key=True, editable=False)
    mou = models.ForeignKey(
        MoU, null=True, blank=True, on_delete=models.SET_NULL, related_name="reports"
    )
    created = models.DateTimeField(auto_now_add=True)

    @classmethod
    def create(cls, mou: MoU) -> Report:
        # Report ids are "<MoU name>-<n>", numbered per MoU starting at 1.
        # Counted by id prefix (not the mou FK) so numbering survives the
        # MoU later being removed (which orphans, not deletes, old reports).
        prefix = f"{mou.name}-"
        existing_ids = cls.objects.filter(id__startswith=prefix).values_list(
            "id", flat=True
        )
        numbers = [
            int(suffix)
            for existing_id in existing_ids
            if (suffix := existing_id[len(prefix) :]).isdigit()
        ]
        next_number = max(numbers, default=0) + 1
        return cls.objects.create(id=f"{prefix}{next_number}", mou=mou)

    def add_time_record(self, record: TimeRecord) -> None:
        task = record.link.task if record.link else None
        record_mou = task.mou if task else None
        if record_mou is None or record_mou.id != self.mou_id:
            raise ValueError(
                f"TimeRecord {record.pk} does not belong to MoU "
                f"{self.mou.name if self.mou else 'none'}."
            )
        record.report = self
        record.save(update_fields=["report"])

    def add_unreported_time_records(self) -> list[Link]:
        """Attach unreported billable records; return the PR links excluded.

        Whether a PR was merged yet is decided once, right here, at create
        time - which PRs were excluded is not recorded anywhere, so printing
        this report later never needs to check GitHub again. The caller (the
        `create` command) is responsible for showing this list to the user
        now, since it won't be available again.
        """
        records, excluded_links = _billable_and_excluded_links(self.mou)
        for record in records:
            self.add_time_record(record)
        return excluded_links

    def remove_time_record(self, record: TimeRecord) -> None:
        if record.report_id != self.pk:
            raise ValueError(f"TimeRecord {record.pk} is not part of report {self.id}.")
        record.report = None
        record.save(update_fields=["report"])

    def __str__(self) -> str:
        return self.id

    @staticmethod
    def _budget_for(records: Iterable[TimeRecord]) -> float:
        if settings.RFP_EUROS is None:
            return 0.0
        duration = sum((record.duration for record in records), timedelta())
        return duration.total_seconds() / 3600 * settings.RFP_EUROS

    @property
    def total_budget(self) -> float:
        return self._budget_for(self.time_records.all())

    def generate_report(self) -> str:
        """Render this persisted report. Read-only: never hits the network.

        Every attached TimeRecord is treated as included - which PRs were
        excluded at create time is not recorded, so there is nothing to
        show here.
        """
        return self._render(
            self.time_records.select_related("link__task"),
            self.id or "(unreported)",
            excluded_links=[],
        )

    @classmethod
    def preview(cls, mou: MoU) -> str:
        """Render what `create` would produce for `mou`, without persisting it."""
        records, excluded_links = _billable_and_excluded_links(mou)
        return cls(mou=mou)._render(records, "(unreported)", excluded_links)

    def _render(
        self,
        records: Iterable[TimeRecord],
        title: str,
        excluded_links: list[Link],
    ) -> str:
        records = list(records)
        lines = [f"Report: {title}", f"MoU: {self.mou.name if self.mou else 'none'}"]

        records_by_task: dict[Task | None, list[TimeRecord]] = {}
        for record in records:
            task = record.link.task if record.link else None
            records_by_task.setdefault(task, []).append(record)

        for task, task_records in records_by_task.items():
            duration = sum((record.duration for record in task_records), timedelta())
            budget = (
                duration.total_seconds() / 3600 * settings.RFP_EUROS
                if settings.RFP_EUROS is not None
                else 0.0
            )
            lines.append(
                f"{task.name if task is not None else '?'}: {_round10(budget)}€"
            )

            links = list({record.link for record in task_records if record.link})
            issue_links = [link for link in links if link.is_issue]
            pr_links = [link for link in links if link.is_pr]
            other_links = [
                link for link in links if not link.is_issue and not link.is_pr
            ]

            if issue_links:
                lines.append("  Issues:")
                lines += [f"    - {_format_report_link(link)}" for link in issue_links]
            if pr_links:
                lines.append("  Pull Requests:")
                lines += [f"    - {_format_report_link(link)}" for link in pr_links]
            if other_links:
                lines.append("  Links:")
                lines += [f"    - {_format_report_link(link)}" for link in other_links]

        lines.append(f"Total: {_round10(self._budget_for(records))}€")

        excluded_section = Report.format_excluded_links(excluded_links)
        if excluded_section:
            lines.append(excluded_section)

        return "\n".join(lines)

    @staticmethod
    def format_excluded_links(excluded_links: list[Link]) -> str:
        if not excluded_links:
            return ""
        links_by_task: dict[Task | None, list[Link]] = {}
        for link in excluded_links:
            links_by_task.setdefault(link.task, []).append(link)

        lines = ["Excluded Pull Requests (not merged):"]
        for task, links in links_by_task.items():
            lines.append(f"  {task.name if task is not None else '?'}:")
            lines += [f"    - {_format_report_link(link)}" for link in links]
        return "\n".join(lines)
