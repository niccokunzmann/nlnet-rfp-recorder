from __future__ import annotations

import csv
import io
from typing import TYPE_CHECKING

from django.conf import settings
from django.core.exceptions import ValidationError
from django.core.validators import RegexValidator
from django.db import models

from nlnet_rfp_recorder.budget import BudgetLine
from nlnet_rfp_recorder.mou_budget import parse_milestone_budgets

if TYPE_CHECKING:
    from .task import Task

mou_name_validator = RegexValidator(
    regex=r"^\S+$",
    message="MoU name must not contain spaces.",
)

# used_budget is exported for reference only - it is never applied on
# import, since its baseline is only ever meant to be set by `rfp mou
# import` or `rfp task set --used`, not by hand-editing a spreadsheet.
TASK_CSV_FIELDNAMES = [
    "id",
    "alias",
    "personal_budget",
    "max_budget",
    "used_budget",
    "description",
]


def _is_blank_row(row: dict[str, str | None]) -> bool:
    """True for a csv.DictReader row from a blank or whitespace-only line.

    csv.DictReader only drops a truly empty line on its own; one with
    stray whitespace comes back as a row with one None-filled field per
    extra column instead.
    """
    return not any(value and value.strip() for value in row.values())


class MoU(models.Model):
    name = models.CharField(
        max_length=64,
        unique=True,
        validators=[mou_name_validator],
        help_text="The MoU's identifier, e.g. 'nlnet-2026'. Must not contain spaces.",
    )
    selected = models.BooleanField(
        default=True,
        help_text=(
            "Whether this is the currently selected MoU (`rfp mou "
            "select`/`add`) - at most one MoU is selected at a time."
        ),
    )
    budget = models.TextField(
        blank=True,
        default="",
        help_text=(
            "The raw text of the last budget document imported via "
            "`rfp mou import` (a whole milestone table or takentaal "
            "document, not a short value - unbounded rather than a short "
            "CharField), used to regenerate each task's budget and shown "
            "by `rfp mou export`."
        ),
    )

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
        from .task import Task

        self.budget = text
        self.save(update_fields=["budget"])

        tasks: dict[str, Task] = {}
        for code, milestone in parse_milestone_budgets(text).items():
            used = milestone.amount if milestone.done else 0.0
            task, _ = Task.objects.update_or_create(
                mou=self,
                name=code,
                defaults={
                    "max_budget": milestone.amount,
                    "used_budget": used,
                    "description": milestone.description,
                },
            )
            tasks[code] = task
        return tasks

    def export_tasks(self) -> str:
        """Export this MoU's tasks as CSV, one row per task, sorted by id."""
        from .alias import Alias
        from .task import Task

        aliases: dict[str, str] = {}
        for alias in Alias.objects.filter(item_type="task", mou=self).order_by("pk"):
            aliases.setdefault(alias.target, alias.alias)

        tasks = sorted(Task.objects.filter(mou=self), key=lambda task: task.sort_key)
        buffer = io.StringIO()
        writer = csv.DictWriter(buffer, fieldnames=TASK_CSV_FIELDNAMES)
        writer.writeheader()
        for task in tasks:
            writer.writerow(
                {
                    "id": task.name,
                    "alias": aliases.get(task.name, ""),
                    "personal_budget": (
                        ""
                        if task.effective_personal_budget is None
                        else task.effective_personal_budget
                    ),
                    "max_budget": "" if task.max_budget is None else task.max_budget,
                    "used_budget": task.used_budget,
                    "description": task.description,
                }
            )
        return buffer.getvalue()

    def import_tasks(self, text: str) -> tuple[list[Task], list[str], list[str]]:
        """Replace/create this MoU's tasks from CSV produced by export_tasks().

        Returns (imported_tasks, missing_names, unused_aliases):
        imported_tasks is every task named in the CSV (created or
        updated); missing_names lists this MoU's task ids that exist in
        the database but weren't in the CSV; unused_aliases lists this
        MoU's task aliases that no row assigned to its task by this
        import (including a task present in the CSV with a blank alias
        column). The caller asks about removing both (see `rfp task
        import`).

        used_budget is read back but ignored - see TASK_CSV_FIELDNAMES.

        This import is the source of truth for which task has which
        alias: a row's alias always wins, stealing it from whatever task
        held it before (same as `rfp alias set`) rather than renaming
        either side.
        """
        from .alias import Alias
        from .task import Task

        rows = [
            row for row in csv.DictReader(io.StringIO(text)) if not _is_blank_row(row)
        ]

        def _parse_float(
            row: dict[str, str | None], field: str, name: str
        ) -> float | None:
            value = (row.get(field) or "").strip()
            if not value:
                return None
            try:
                return float(value)
            except ValueError:
                raise ValueError(f"Invalid {field} {value!r} for task {name}") from None

        imported: list[Task] = []
        seen_names: set[str] = set()
        aliased_names: set[str] = set()
        for row in rows:
            name = (row.get("id") or "").strip()
            if not name:
                raise ValueError("Task import row is missing an id.")
            if name in seen_names:
                raise ValueError(f"Duplicate task id in import: {name}")
            seen_names.add(name)

            personal_budget = _parse_float(row, "personal_budget", name)
            max_budget = _parse_float(row, "max_budget", name)
            description = row.get("description") or ""

            # export_tasks() writes the *effective* personal budget (see
            # Task.effective_personal_budget), so a value equal to
            # max_budget round-trips back to "unset" rather than pinning
            # it - only a value that actually differs from max_budget
            # means the person editing the CSV set one on purpose.
            if personal_budget is not None and personal_budget == max_budget:
                personal_budget = None

            # Validated before writing anything, same as Task.select() -
            # an invalid id should never end up persisted.
            try:
                Task(mou=self, name=name).full_clean(
                    exclude=["selected"],
                    validate_unique=False,
                    validate_constraints=False,
                )
            except ValidationError as error:
                raise ValueError("; ".join(error.messages)) from None

            task, _created = Task.objects.update_or_create(
                mou=self,
                name=name,
                defaults={
                    "personal_budget": personal_budget,
                    "max_budget": max_budget,
                    "description": description,
                },
            )
            imported.append(task)

            alias_text = (row.get("alias") or "").strip()
            if alias_text:
                try:
                    Alias.create("task", name, alias_text, mou=self)
                except ValueError as error:
                    raise ValueError(f"{error} (task {name})") from None
                aliased_names.add(name)

        missing_names = list(
            self.tasks.exclude(name__in=seen_names).values_list("name", flat=True)
        )
        unused_aliases = list(
            Alias.objects.filter(item_type="task", mou=self)
            .exclude(target__in=aliased_names)
            .order_by("alias")
            .values_list("alias", flat=True)
        )
        return imported, missing_names, unused_aliases

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
        return BudgetLine(
            used=self.total_used, total=total, rate=settings.RFP_EUROS_PER_HOUR
        )

    @property
    def display_name(self) -> str:
        """This MoU's name, with its alias appended in parens if it has one."""
        from .alias import Alias

        aliases = Alias.objects.filter(item_type="mou", target=self.name)
        alias = aliases.order_by("pk").first()
        return f"{self.name} ({alias.alias})" if alias else self.name

    def __str__(self) -> str:
        return self.name
