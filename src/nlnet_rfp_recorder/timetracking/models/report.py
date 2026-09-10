from __future__ import annotations

import csv
import io
import math
from collections.abc import Callable, Iterable
from dataclasses import dataclass
from datetime import timedelta

from django.conf import settings
from django.db import models

from nlnet_rfp_recorder.github import Status, fetch_statuses, fetch_titles

from .github_token import GitHubToken
from .link import Link
from .mou import MoU
from .tag import Tag
from .task import Task
from .time_record import TimeRecord


def _is_blank_row(row: dict[str, str | None]) -> bool:
    """True for a csv.DictReader row from a blank or whitespace-only line.

    csv.DictReader only drops a truly empty line on its own; one with
    stray whitespace comes back as a row with one None-filled field per
    extra column instead.
    """
    return not any(value and value.strip() for value in row.values())


def _round_link_budget(budget: float) -> int | None:
    """A link's displayed budget: rounded up to the nearest 5, or None if
    the exact amount doesn't even reach 5 - rounding up to a minimum
    would overstate it, and rounding down to 0 would print a link as
    costing nothing, so instead nothing is shown for it at all.

    A link with no figure of its own isn't dropped from its task's total,
    though - its raw amount is pooled with every other such link on the
    same task and added in as a lump sum; see _task_budget.
    """
    if budget < 5:
        return None
    return math.ceil(budget / 5) * 5


def _round_up_to_10(amount: float) -> int:
    if amount <= 0:
        return 0
    return math.ceil(amount / 10) * 10


def _sum_link_budgets(amounts: Iterable[int | None]) -> int:
    return sum(amount for amount in amounts if amount is not None)


def _task_budget(
    report_lines: Iterable[ReportLine | _PreviewLine],
) -> tuple[int, dict[int, int | None]]:
    """A task's displayed total, and each of its lines' own displayed budget.

    The total is the sum of every line's own rounded budget (see
    _round_link_budget) plus the raw amounts of the lines too small to
    show individually, pooled together and rounded up to the nearest 10 -
    real tracked time is never silently dropped just because no single
    line involved was big enough to show on its own.
    """
    report_lines = list(report_lines)
    line_budgets = {id(rl): _round_link_budget(rl.budget) for rl in report_lines}
    shown = _sum_link_budgets(line_budgets.values())
    unshown_raw = sum(rl.budget for rl in report_lines if line_budgets[id(rl)] is None)
    return shown + _round_up_to_10(unshown_raw), line_budgets


def _format_report_link(
    link: Link, tags: Iterable[str] | None = None, budget: int | None = None
) -> str:
    if link.is_issue:
        url = link.issue.url
    elif link.is_pr:
        url = link.pr.url
    elif link.is_discussion:
        url = link.discussion.url
    else:
        url = link.url
    if tags is None:
        tags = [tag.name for tag in link.tags.all()]
    extra_tags = [tag for tag in tags if tag != "implementation"]
    text = url
    if budget is not None:
        text += f" - {budget}€"
    if extra_tags:
        text += f" ({', '.join(extra_tags)})"
    return text


def _format_task_block(
    task: Task | None, task_lines: Iterable[ReportLine | _PreviewLine]
) -> tuple[list[str], int]:
    """The lines `_render` prints for one task: its total, then its links
    grouped into Issues/Pull Requests/Discussions/Links - reused as-is by
    `Report.review_tasks()` so a task is reviewed exactly as it will
    print in the report.
    """
    task_lines = list(task_lines)
    task_total, line_budgets = _task_budget(task_lines)
    task_display = task.name if task is not None else "?"
    lines = [f"{task_display}: {task_total}€"]

    def _bullet(rl: ReportLine | _PreviewLine) -> str:
        budget = line_budgets[id(rl)]
        return f"    - {_format_report_link(rl.link, rl.tag_list, budget)}"

    sorted_lines = sorted(task_lines, key=lambda report_line: report_line.link)
    issue_lines = [rl for rl in sorted_lines if rl.link.is_issue]
    pr_lines = [rl for rl in sorted_lines if rl.link.is_pr]
    discussion_lines = [rl for rl in sorted_lines if rl.link.is_discussion]
    other_lines = [
        rl
        for rl in sorted_lines
        if not rl.link.is_issue and not rl.link.is_pr and not rl.link.is_discussion
    ]

    if issue_lines:
        lines.append("  Issues:")
        lines += [_bullet(rl) for rl in issue_lines]
    if pr_lines:
        lines.append("  Pull Requests:")
        lines += [_bullet(rl) for rl in pr_lines]
    if discussion_lines:
        lines.append("  Discussions:")
        lines += [_bullet(rl) for rl in discussion_lines]
    if other_lines:
        lines.append("  Links:")
        lines += [_bullet(rl) for rl in other_lines]

    return lines, task_total


def _task_sort_key(task: Task | None) -> tuple:
    # Groups with no task (task was removed) sort last, after every real
    # task in number-then-letter order.
    return (1,) if task is None else (0, task.sort_key)


def _fetch_missing_titles(links: Iterable[Link], token: str | None) -> None:
    """Cache each issue/PR/discussion link's title, for those without one yet.

    Plain links, and links whose title was already fetched by an earlier
    call, cost no network request at all. Fetches whatever is pending in
    one batched, concurrent request (see fetch_titles).
    """
    pending = [
        link
        for link in links
        if not link.title and (link.is_issue or link.is_pr or link.is_discussion)
    ]
    if not pending:
        return
    references = [link.pr or link.issue or link.discussion for link in pending]
    titles = fetch_titles(references, token=token)
    fetched = []
    for link, title in zip(pending, titles, strict=True):
        if title:
            link.title = title
            fetched.append(link)
    if fetched:
        Link.objects.bulk_update(fetched, ["title"])


def _billable_and_excluded_links(mou: MoU) -> tuple[list[TimeRecord], list[Link]]:
    """Unreported billable records and excluded (open PR) links for `mou`.

    Checks every open PR across all of the MoU's tasks in a single batched
    GitHub status request, instead of one request per task - otherwise the
    time this takes scales with the number of tasks/PRs involved.
    """
    links_by_task = {
        task: list(task.links_with_unreported_time)
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
            TimeRecord.objects.filter(
                link_id__in=billable_ids, report_line__isnull=True
            )
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
                f"{self.mou.display_name if self.mou else 'none'}."
            )
        line, _ = ReportLine.objects.get_or_create(
            report=self,
            link=record.link,
            defaults={"tags": _joined_tags(record.link.tags.all())},
        )
        record.report_line = line
        record.save(update_fields=["report_line"])
        line.budget = self._budget_for(line.time_records.all())
        line.save(update_fields=["budget"])

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
        line = record.report_line
        if line is None or line.report_id != self.pk:
            raise ValueError(f"TimeRecord {record.pk} is not part of report {self.id}.")
        record.report_line = None
        record.save(update_fields=["report_line"])
        if line.time_records.exists():
            line.budget = self._budget_for(line.time_records.all())
            line.save(update_fields=["budget"])
        else:
            line.delete()

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
        return sum(line.budget for line in self.lines.all())

    @property
    def time_records(self) -> models.QuerySet[TimeRecord]:
        return TimeRecord.objects.filter(report_line__report=self)

    def review_tasks(self) -> list[tuple[Task | None, str, int]]:
        """This report's tasks, each rendered exactly as generate_report()
        would print it, for `report review` to walk through one by one.

        Returns one (task, block_text, task_total) tuple per task
        currently in the report, in the same order generate_report()
        prints them.
        """
        report_lines_by_task: dict[Task | None, list[ReportLine]] = {}
        for report_line in self.lines.select_related("link__task"):
            task = report_line.link.task if report_line.link else None
            report_lines_by_task.setdefault(task, []).append(report_line)

        result = []
        for task in sorted(report_lines_by_task, key=_task_sort_key):
            block, task_total = _format_task_block(task, report_lines_by_task[task])
            result.append((task, "\n".join(block), task_total))
        return result

    def remove_task_lines(self, task: Task | None) -> None:
        """Drop this report's lines for `task`, e.g. after `report review`
        decides it isn't worth reporting yet.

        Only detaches the underlying time records (report_line set back
        to None) - never deletes them - so a later report can still claim
        that time.
        """
        lines = (
            self.lines.filter(link__task__isnull=True)
            if task is None
            else self.lines.filter(link__task=task)
        )
        for line in lines:
            TimeRecord.objects.filter(report_line=line).update(report_line=None)
            line.delete()

    def generate_report(self) -> str:
        """Render this persisted report. Read-only: never hits the network.

        Renders from the persisted ReportLine budgets/tags, so a manual
        override (e.g. via `report import`) is reflected here, not
        recomputed from the underlying time records.
        """
        return self._render(
            self.lines.select_related("link__task"),
            self.id or "(unreported)",
            excluded_links=[],
        )

    @classmethod
    def preview(cls, mou: MoU) -> str:
        """Render what `create` would produce for `mou`, without persisting it."""
        records, excluded_links = _billable_and_excluded_links(mou)
        preview_lines = _group_into_preview_lines(records)
        return cls(mou=mou)._render(preview_lines, "(unreported)", excluded_links)

    def _render(
        self,
        report_lines: Iterable[ReportLine | _PreviewLine],
        title: str,
        excluded_links: list[Link],
    ) -> str:
        report_lines = list(report_lines)
        mou_display = self.mou.name if self.mou else "none"
        lines = [f"Report: {title}", f"MoU: {mou_display}"]

        report_lines_by_task: dict[Task | None, list[ReportLine | _PreviewLine]] = {}
        for report_line in report_lines:
            task = report_line.link.task if report_line.link else None
            report_lines_by_task.setdefault(task, []).append(report_line)

        report_total = 0
        for index, task in enumerate(sorted(report_lines_by_task, key=_task_sort_key)):
            if index > 0:
                lines.append("")
            block, task_total = _format_task_block(task, report_lines_by_task[task])
            report_total += task_total
            lines += block

        lines.append("")
        lines.append(f"Total: {report_total}€")

        excluded_section = Report.format_excluded_links(excluded_links)
        if excluded_section:
            lines.append(excluded_section)

        return "\n".join(lines)

    def ensure_link_titles(self) -> None:
        """Cache issue/PR titles for this report's links, for export_lines().

        Fetches only what isn't already cached (see _fetch_missing_titles),
        so calling this repeatedly (e.g. on every `report export`) costs
        network requests only for links seen for the first time.
        """
        links = [report_line.link for report_line in self.lines.select_related("link")]
        _fetch_missing_titles(links, GitHubToken.get())

    def export_lines(self) -> str:
        """Export this report's lines as CSV, sorted by task then link.

        The title column is read-only: it's a courtesy for whoever edits
        the file (so a bare issue/PR number is recognizable), populated
        from Link.title - call ensure_link_titles() first to fill it in.
        import_lines() ignores it entirely.
        """
        report_lines = list(self.lines.select_related("link__task"))
        report_lines.sort(
            key=lambda report_line: (
                _task_sort_key(report_line.link.task),
                report_line.link,
            )
        )
        buffer = io.StringIO()
        writer = csv.DictWriter(buffer, fieldnames=REPORT_LINE_FIELDNAMES)
        writer.writeheader()
        for report_line in report_lines:
            task = report_line.link.task
            pks = report_line.time_records.order_by("pk").values_list("pk", flat=True)
            writer.writerow(
                {
                    "task": task.name if task is not None else "",
                    "link": report_line.link.url,
                    "title": report_line.link.title,
                    "budget": round(report_line.budget, 2),
                    "tags": report_line.tags,
                    "records": ",".join(str(pk) for pk in pks),
                }
            )
        return buffer.getvalue()

    def import_lines(
        self, text: str, on_remove: Callable[[ReportLine], str] | None = None
    ) -> None:
        """Replace this report's lines from CSV produced by export_lines().

        Completely replaces the set of report lines (and their budget/tags),
        but never deletes a TimeRecord - only attaches it to or detaches it
        from a report line. Blank or whitespace-only lines are skipped -
        csv.DictReader only drops truly empty lines on its own, not ones
        with stray whitespace, which would otherwise fail as if they named
        a real (empty) link.

        `on_remove`, if given, is called once per report line the new text
        no longer includes, and must return one of:
          - "remove": detach it from this report; its time stays reportable
            later (the default when on_remove is None).
          - "exclude": detach it and mark its link as never reportable
            again (Link.excluded_from_reports).
          - "keep": leave this line in the report as if it hadn't been
            dropped from the text - e.g. because leaving it out was a
            mistake.

        A row's link column may name a URL no existing Link has. If the
        row's records column unambiguously identifies one existing link
        (every listed pk currently belongs to the same one), that link is
        renamed to the new URL - the records are what identifies the
        entry, not the URL text. Otherwise (no records, or they span more
        than one link) a new link is created with no time records
        attached, rather than guessing.

        A row's task column is applied to its link every time, even when
        the link already existed under a different task - editing that
        column is how a link gets moved to another task. A task name that
        doesn't resolve to a task of this report's MoU (including a blank
        column) clears the link's task rather than leaving it unchanged.
        """
        rows = list(csv.DictReader(io.StringIO(text)))
        wanted_links: set[Link] = set()

        for row in rows:
            if _is_blank_row(row):
                continue
            link_url = (row.get("link") or "").strip()
            task_name = (row.get("task") or "").strip()

            pk_text = (row.get("records") or "").strip()
            try:
                wanted_pks = {int(pk) for pk in pk_text.split(",") if pk.strip()}
            except ValueError:
                raise ValueError(
                    f"Invalid records {pk_text!r} for {link_url}"
                ) from None

            link = Link.objects.filter(url=link_url).first()
            if link is None:
                link, renamed = self._resolve_or_create_link(link_url, wanted_pks)
                if not renamed:
                    # Ambiguous (or no records) - a fresh link was created
                    # instead, so none of the requested records (which
                    # belong elsewhere) are attached to it.
                    wanted_pks = set()

            existing_task = link.task
            if existing_task is not None and existing_task.mou_id != self.mou_id:
                raise ValueError(
                    f"Link {link_url} does not belong to MoU "
                    f"{self.mou.display_name if self.mou else 'none'}."
                )

            target_task = (
                Task.objects.filter(mou=self.mou, name=task_name).first()
                if task_name
                else None
            )
            if link.task_id != (target_task.id if target_task is not None else None):
                link.task = target_task
                link.save(update_fields=["task"])
            wanted_links.add(link)

            try:
                budget = float(row["budget"]) if row.get("budget") else 0.0
            except ValueError:
                raise ValueError(
                    f"Invalid budget {row['budget']!r} for {link_url}"
                ) from None

            report_line, _ = ReportLine.objects.update_or_create(
                report=self,
                link=link,
                defaults={"budget": budget, "tags": (row.get("tags") or "").strip()},
            )

            current_pks = set(report_line.time_records.values_list("pk", flat=True))

            for pk in current_pks - wanted_pks:
                TimeRecord.objects.filter(pk=pk).update(report_line=None)
            for pk in wanted_pks - current_pks:
                try:
                    record = TimeRecord.objects.get(pk=pk)
                except TimeRecord.DoesNotExist:
                    raise ValueError(f"No such time record: {pk}") from None
                record.report_line = report_line
                # A record's task is derived from record.link.task, so
                # moving it onto this row's link (its task already
                # resolved above) is what actually moves its task - not
                # just attaching it to the report line.
                record.link = link
                record.save(update_fields=["report_line", "link"])

        for stale_line in self.lines.exclude(link__in=wanted_links):
            decision = on_remove(stale_line) if on_remove is not None else "remove"
            if decision not in ("remove", "exclude", "keep"):
                raise ValueError(f"Invalid on_remove decision: {decision!r}")
            if decision == "keep":
                continue

            link = stale_line.link
            TimeRecord.objects.filter(report_line=stale_line).update(report_line=None)
            stale_line.delete()
            if decision == "exclude":
                link.excluded_from_reports = True
                link.save(update_fields=["excluded_from_reports"])

    def _resolve_or_create_link(
        self, link_url: str, record_pks: set[int]
    ) -> tuple[Link, bool]:
        """A link URL from import_lines() with no existing Link of its own.

        If `record_pks` unambiguously identifies one existing link (every
        pk that actually exists currently belongs to the same link), that
        link is renamed to `link_url` and returned with True. Otherwise a
        new link is created with no task and returned with False, so the
        caller knows not to attach `record_pks` to it. Either way, the
        caller applies the row's task column itself, uniformly for every
        link (new, renamed, or already existing).
        """
        candidate_link_ids = set(
            TimeRecord.objects.filter(pk__in=record_pks, link__isnull=False)
            .values_list("link_id", flat=True)
            .distinct()
        )
        if len(candidate_link_ids) == 1:
            link = Link.objects.get(pk=candidate_link_ids.pop())
            link.url = link_url
            link.save(update_fields=["url"])
            return link, True

        return Link.objects.get_or_create(url=link_url)[0], False

    @staticmethod
    def format_excluded_links(excluded_links: list[Link]) -> str:
        if not excluded_links:
            return ""
        links_by_task: dict[Task | None, list[Link]] = {}
        for link in sorted(excluded_links):
            links_by_task.setdefault(link.task, []).append(link)

        lines = ["Excluded Pull Requests (not merged):"]
        for task in sorted(links_by_task, key=_task_sort_key):
            task_display = task.name if task is not None else "?"
            lines.append(f"  {task_display}:")
            lines += [
                f"    - {_format_report_link(link)}" for link in links_by_task[task]
            ]
        return "\n".join(lines)


REPORT_LINE_FIELDNAMES = ["task", "link", "title", "budget", "tags", "records"]


def _joined_tags(tags: Iterable[Tag]) -> str:
    return ",".join(sorted(tag.name for tag in tags))


class ReportLine(models.Model):
    """One row of a report: a link, its locked-in budget, and its tags.

    Uniquely identified by (report, link) - a report line can have several
    time records sharing that link. Tags are independent of Link.tags: a
    line's tags start as a snapshot of the link's tags when the line is
    created, but are then a report-scoped copy, editable (e.g. via
    `report import`) without affecting the link's tags anywhere else.
    """

    report = models.ForeignKey(Report, on_delete=models.CASCADE, related_name="lines")
    link = models.ForeignKey(
        Link, on_delete=models.CASCADE, related_name="report_lines"
    )
    budget = models.FloatField(default=0.0)
    tags = models.CharField(max_length=255, blank=True, default="")

    class Meta:
        constraints = [
            models.UniqueConstraint(
                fields=["report", "link"], name="unique_report_line"
            )
        ]

    @property
    def tag_list(self) -> list[str]:
        return [tag for tag in self.tags.split(",") if tag]

    def __str__(self) -> str:
        return f"{self.report_id}: {self.link}"


@dataclass
class _PreviewLine:
    """A not-yet-persisted stand-in for a ReportLine, used by Report.preview()."""

    link: Link
    budget: float
    tag_list: list[str]


def _group_into_preview_lines(records: Iterable[TimeRecord]) -> list[_PreviewLine]:
    records_by_link: dict[Link, list[TimeRecord]] = {}
    for record in records:
        if record.link is not None:
            records_by_link.setdefault(record.link, []).append(record)
    return [
        _PreviewLine(
            link=link,
            budget=Report._budget_for(link_records),
            tag_list=sorted(tag.name for tag in link.tags.all()),
        )
        for link, link_records in records_by_link.items()
    ]
