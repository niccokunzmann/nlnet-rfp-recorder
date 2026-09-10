from __future__ import annotations

import re

from django.core.exceptions import ValidationError
from django.core.validators import RegexValidator
from django.db import models

from nlnet_rfp_recorder.alias_types import AliasItemType
from nlnet_rfp_recorder.github import classify_issue_or_pr, parse_repo_url

from .mou import MoU
from .task import Task

ALIAS_ITEM_TYPES = tuple(AliasItemType)

alias_validator = RegexValidator(
    regex=r"^\S+$",
    message="Alias must not contain spaces.",
)


class Alias(models.Model):
    """A short nickname for a MoU name, a task code, or a repo base URL.

    Deliberately has no DB-level uniqueness constraint: `mou` is only set
    for item_type="task" (scoping the alias to the MoU it was created
    under), and SQL treats every NULL as distinct, so a constraint
    including `mou` wouldn't actually enforce uniqueness for the mou/url
    item types where it's always NULL. All uniqueness and format rules are
    validated in create(), the same way Task.select()/MoU.select() already
    validate in code rather than relying purely on DB constraints.
    """

    item_type = models.CharField(
        max_length=8, choices=[(t, t) for t in ALIAS_ITEM_TYPES]
    )
    mou = models.ForeignKey(
        MoU, null=True, blank=True, on_delete=models.CASCADE, related_name="aliases"
    )
    target = models.CharField(max_length=255)
    alias = models.CharField(max_length=64)

    class Meta:
        indexes = [models.Index(fields=["item_type", "alias"])]

    @classmethod
    def create(
        cls, item_type: str, id_or_target: str, alias: str, mou: MoU | None = None
    ) -> Alias:
        """Create an alias, reassigning it if it - or its target - is in use.

        An alias name already pointing elsewhere is moved to the new
        target; a target that already has a (possibly differently-named)
        alias loses it in favor of this one. Either way, the caller is
        responsible for telling the user what moved - see conflicts_for(),
        called with the same arguments *before* create(), since by the time
        create() returns the old rows are already gone.
        """
        if item_type not in ALIAS_ITEM_TYPES:
            raise ValueError(
                f"Unknown alias item type {item_type!r}. "
                f"Must be one of {ALIAS_ITEM_TYPES}."
            )

        try:
            alias_validator(alias)
        except ValidationError as error:
            raise ValueError("; ".join(error.messages)) from None
        if item_type == "task" and alias[:1].isdigit():
            raise ValueError("A task alias must not start with a number.")
        if item_type == "url" and "://" in alias:
            raise ValueError("A url alias must not contain '://'.")

        if item_type == "mou":
            if MoU.objects.filter(name=alias).exists():
                raise ValueError(f"Alias {alias!r} is already a MoU name.")
            if not MoU.objects.filter(name=id_or_target).exists():
                raise ValueError(f"No such MoU: {id_or_target}")
            target = id_or_target
        elif item_type == "task":
            if mou is None:
                raise ValueError("No MoU selected. Run `rfp mou add <name>` first.")
            if Task.objects.filter(mou=mou, name=alias).exists():
                raise ValueError(f"Alias {alias!r} is already a task name.")
            if not Task.objects.filter(mou=mou, name=id_or_target).exists():
                raise ValueError(f"No such task: {id_or_target}")
            target = id_or_target
        else:
            target = id_or_target

        cls.conflicts_for(item_type, target, alias, mou).delete()

        return cls.objects.create(
            item_type=item_type,
            mou=mou if item_type == "task" else None,
            target=target,
            alias=alias,
        )

    @classmethod
    def conflicts_for(
        cls, item_type: str, target: str, alias: str, mou: MoU | None = None
    ) -> models.QuerySet[Alias]:
        """Existing aliases create() would remove for these arguments.

        That's any alias already named `alias` (it's being reassigned to
        `target`) plus any alias `target` already has (it's being renamed
        to `alias`) - at most two rows, one of each, and possibly the same
        row if both conditions match it at once.
        """
        conflicts = cls.objects.filter(item_type=item_type).filter(
            models.Q(alias=alias) | models.Q(target=target)
        )
        if item_type == "task":
            conflicts = conflicts.filter(mou=mou)
        return conflicts

    def __str__(self) -> str:
        return f"{self.item_type}:{self.alias} -> {self.target}"


def resolve_mou_name(raw: str) -> str:
    """Return the MoU name `raw` refers to, following an alias if it is one."""
    alias = Alias.objects.filter(item_type="mou", alias=raw).first()
    return alias.target if alias is not None else raw


def resolve_task_name(raw: str, mou: MoU | None) -> str:
    """Return the task name `raw` refers to within `mou`, following an alias."""
    if mou is None:
        return raw
    alias = Alias.objects.filter(item_type="task", mou=mou, alias=raw).first()
    return alias.target if alias is not None else raw


LINK_ALIAS = re.compile(r"^(?P<alias>[^/]+)/(?P<number>\d+)$")


def resolve_link(raw: str, token: str | None = None) -> str:
    """Expand an "<alias>/<number>" shorthand link into a full GitHub URL,
    or add a missing "https://" to a link typed without one.

    Anything already containing "://" (a real URL) is returned unchanged.
    A remaining string shaped like "<alias>/<number>" is expanded via the
    registered url alias; one that instead looks like a bare domain (its
    first "/"-separated segment contains a ".", e.g. "github.com/...")
    gets "https://" added. Anything else is returned unchanged.
    """
    if "://" in raw:
        return raw
    match = LINK_ALIAS.match(raw)
    if match is None:
        first_segment = raw.split("/", 1)[0]
        return f"https://{raw}" if "." in first_segment else raw

    alias_name = match["alias"]
    number = match["number"]
    alias = Alias.objects.filter(item_type="url", alias=alias_name).first()
    if alias is None:
        raise ValueError(
            f"No alias {alias_name!r} for url. "
            f"Run `rfp alias set url <repo-url> {alias_name}` first."
        )

    parsed = parse_repo_url(alias.target)
    if parsed is None:
        raise ValueError(
            f"Alias {alias_name!r} does not point to a GitHub repo URL: {alias.target}"
        )
    owner, repo = parsed
    path = classify_issue_or_pr(owner, repo, int(number), token=token)
    return f"{alias.target}/{path}/{number}"
