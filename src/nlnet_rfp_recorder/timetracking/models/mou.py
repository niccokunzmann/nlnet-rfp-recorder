from __future__ import annotations

from typing import TYPE_CHECKING

from django.conf import settings
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


class MoU(models.Model):
    name = models.CharField(max_length=64, unique=True, validators=[mou_name_validator])
    selected = models.BooleanField(default=True)
    # The raw text of the last budget document imported via set_budget() -
    # a whole milestone table or takentaal document, not a short value, so
    # this needs to be unbounded rather than a short CharField.
    budget = models.TextField(blank=True, default="")

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

    @property
    def display_name(self) -> str:
        """This MoU's name, with its alias appended in parens if it has one."""
        from .alias import Alias

        aliases = Alias.objects.filter(item_type="mou", target=self.name)
        alias = aliases.order_by("pk").first()
        return f"{self.name} ({alias.alias})" if alias else self.name

    def __str__(self) -> str:
        return self.name
