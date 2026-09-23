import os
import shutil
import sys
import tempfile
import warnings
from collections.abc import Callable
from datetime import datetime, timedelta
from importlib.metadata import version as get_version
from pathlib import Path
from typing import TYPE_CHECKING, NoReturn

import django
import typer
from typer.core import TyperGroup

from nlnet_rfp_recorder.alias_types import AliasItemType
from nlnet_rfp_recorder.budget import format_duration_hours
from nlnet_rfp_recorder.timesheet import TimesheetRow, parse_hhmmss

if TYPE_CHECKING:
    from nlnet_rfp_recorder.statistics import Statistics
    from nlnet_rfp_recorder.timetracking.models import (
        Link,
        MoU,
        ReportLine,
        Task,
        TimeRecord,
    )

os.environ.setdefault("DJANGO_SETTINGS_MODULE", "nlnet_rfp_recorder.settings")


class AlphabeticalGroup(TyperGroup):
    def list_commands(self, ctx: typer.Context) -> list[str]:
        return sorted(self.commands)


app = typer.Typer(
    name="rfp",
    help="Create RfPs from work on issues and pull requests, record time.",
    no_args_is_help=True,
    cls=AlphabeticalGroup,
)

DbOption = typer.Option(
    None,
    "--db",
    envvar="RFP_DB",
    help="Path to the sqlite database file. Falls back to the RFP_DB "
    "environment variable when not given.",
)
TestOption = typer.Option(
    False,
    "--test",
    help="Run against a throwaway database in /tmp, pre-filled with sample data.",
)
TagsOption = typer.Option(
    None,
    "--tags",
    help=(
        "Tag for this entry (implementation, review). Default: guessed "
        "from whether your saved GitHub token opened this issue/PR."
    ),
)

TEST_DB_FILE = Path(tempfile.gettempdir()) / "rfp-test.sqlite3"


def _setup(db: Path | None, test: bool = False) -> None:
    if test:
        db = TEST_DB_FILE
    if db is not None:
        os.environ["RFP_DB"] = str(db)
    django.setup()

    from django.conf import settings
    from django.core.management import call_command

    database_file = Path(settings.DATABASES["default"]["NAME"])
    database_file.parent.mkdir(parents=True, exist_ok=True)
    is_new = not database_file.exists()
    call_command("migrate", verbosity=0)

    if database_file == TEST_DB_FILE and is_new:
        call_command("loaddata", "sample_data", verbosity=0)


def _fail(message: str) -> NoReturn:
    typer.echo(message, err=True)
    raise typer.Exit(code=1)


def _format_duration(duration: timedelta) -> str:
    return format_duration_hours(duration.total_seconds() / 3600)


def _parse_duration_or_fail(text: str) -> tuple[timedelta, bool]:
    """Parse `edit --duration`'s value: plain minutes ('50') or 'H:MM'
    ('1:20') - both accepting any number of minutes, not just < 60 -
    set the duration outright. A leading '+' or '-' instead adjusts the
    current duration by that amount ('+15', '-1:20').

    Returns (amount, relative); `amount` already carries the sign for a
    '-' adjustment, so callers just pick between TimeRecord.add_duration
    (relative) and TimeRecord.set_duration (not relative).
    """
    relative = text[:1] in "+-"
    sign = -1 if text.startswith("-") else 1
    magnitude = text[1:] if relative else text

    if ":" in magnitude:
        hours_part, _, minutes_part = magnitude.partition(":")
        try:
            hours, minutes = int(hours_part), int(minutes_part)
        except ValueError:
            _fail(f"Invalid duration {text!r}; expected minutes or 'H:MM'.")
        return sign * timedelta(hours=hours, minutes=minutes), relative

    try:
        minutes = int(magnitude)
    except ValueError:
        _fail(f"Invalid duration {text!r}; expected minutes or 'H:MM'.")
    return sign * timedelta(minutes=minutes), relative


def _task_name(record: TimeRecord) -> str:
    if record.link is None or record.link.task is None:
        return "?"
    return record.link.task.display_name


def _echo_stopped(record: TimeRecord) -> None:
    duration = _format_duration(record.duration)
    url = record.link.url if record.link else ""
    typer.echo(f"Stopped {_task_name(record)} {duration} {url}")


def _resolve_link_or_fail(link: str) -> str:
    from nlnet_rfp_recorder.timetracking.models import GitHubToken, resolve_link

    try:
        return resolve_link(link, token=GitHubToken.get())
    except ValueError as error:
        _fail(str(error))


def _select_task_or_fail(name: str) -> Task:
    """Select a task by name/alias, creating it if needed - same as `task select`."""
    from django.core.exceptions import ValidationError

    from nlnet_rfp_recorder.timetracking.models import MoU, Task, resolve_task_name

    mou = MoU.get_selected()
    name = resolve_task_name(name, mou)
    try:
        with warnings.catch_warnings(record=True) as caught:
            warnings.simplefilter("always")
            task = Task.select(name, mou=mou)
    except ValidationError as error:
        _fail("; ".join(error.messages))
    except ValueError as error:
        _fail(str(error))

    for warning in caught:
        typer.echo(str(warning.message), err=True)
    return task


def _default_tag(link: str) -> str:
    """ "implementation" if the authenticated GitHub user opened this
    issue/PR, "review" otherwise - the guess used when --tags isn't given.

    Falls back to "implementation" (the previous, unconditional default)
    whenever this can't be confirmed: no GitHub token saved, the link
    isn't an issue/PR, or GitHub can't be reached - a network hiccup
    should never silently relabel someone's own work as a review.
    """
    import niquests

    from nlnet_rfp_recorder.github import (
        Issue,
        PullRequest,
        fetch_authenticated_login,
        fetch_issue_author,
    )
    from nlnet_rfp_recorder.timetracking.models import GitHubToken

    reference = PullRequest.from_url(link) or Issue.from_url(link)
    token = GitHubToken.get()
    if reference is None or token is None:
        return "implementation"

    try:
        my_login = fetch_authenticated_login(token)
        author_login = fetch_issue_author(
            reference.owner, reference.repo, reference.number, token=token
        )
    except niquests.exceptions.RequestException:
        return "implementation"

    if not my_login or not author_login:
        return "implementation"
    return "implementation" if my_login.lower() == author_login.lower() else "review"


def _apply_start_tag(link: Link, desired_tag: str) -> None:
    """Classify `link` as `desired_tag` ("implementation" or "review").

    A link with no such tag yet gets it right away - there's nothing to
    conflict with. One already classified differently is only changed
    after asking, since a link's tag governs every time entry recorded
    against it, not just the one just started - which is why this always
    runs after that entry already exists (see _start): the question can
    wait, starting the clock can't.
    """
    from nlnet_rfp_recorder.timetracking.models import TAG_NAMES, Tag

    current = {tag.name for tag in link.tags.all() if tag.name in TAG_NAMES}
    if desired_tag in current:
        return
    if not current:
        link.add_tag(desired_tag)
        return

    old = ", ".join(sorted(current))
    typer.echo(f"{link.url} is tagged '{old}', but this looks like '{desired_tag}'.")
    if typer.confirm(f"Change it to '{desired_tag}' for all its time entries?"):
        link.tags.remove(*Tag.objects.filter(name__in=current))
        link.add_tag(desired_tag)
        typer.echo(f"Tag changed to '{desired_tag}'.")


def _apply_start_task(link: Link, desired_task: Task, *, explicit: bool) -> None:
    """Move `link` to `desired_task` if it currently belongs elsewhere.

    A link with no task yet is assigned right away only when a task was
    typed explicitly - that's a deliberate choice, nothing to ask about.
    When no task was typed, `desired_task` is only the currently
    selected task filling in for a missing argument, not something the
    user actually asked for - so a taskless link is left as-is here
    (_start's call to _prompt_for_task asks instead, offering it as the
    default answer), and an *existing* assignment likewise wins without
    a question, with a hint for how to move it explicitly if that's
    wrong.
    """
    if link.task_id == desired_task.id:
        return
    if link.task_id is None:
        if explicit:
            link.task = desired_task
            link.save(update_fields=["task"])
        return

    if not explicit:
        typer.echo(f"{link.url} is under task {link.task.display_name}.")
        typer.echo(
            f"Run `rfp start {desired_task.name} {link.url}` to move it to "
            f"{desired_task.display_name} instead."
        )
        return

    typer.echo(
        f"{link.url} is under task {link.task.display_name}, but this "
        f"looks like {desired_task.display_name}."
    )
    if typer.confirm(f"Change it to {desired_task.display_name}?", default=True):
        link.task = desired_task
        link.save(update_fields=["task"])
        typer.echo(f"Task changed to {desired_task.display_name}.")


def _echo_task_status(task: Task) -> None:
    # A task's MoU can be None: removing an MoU orphans (not deletes) its
    # tasks, so a still-selected task can outlive its MoU.
    mou_name = (
        task.mou.display_name if task.mou is not None else "none (its MoU was removed)"
    )
    typer.echo(f"MoU: {mou_name}")
    typer.echo(f"Selected task: {task.display_name}")
    if task.description:
        typer.echo(task.description)
    if task.budget_line is not None:
        typer.echo(str(task.budget_line))


def _read_budget_from_stdin() -> str:
    typer.echo(
        "Paste the budget table below. Three blank lines in a row end input.",
        err=True,
    )
    lines: list[str] = []
    blank_streak = 0
    while blank_streak < 3:
        line = sys.stdin.readline()
        if line == "":
            break
        stripped = line.rstrip("\n")
        blank_streak = blank_streak + 1 if stripped == "" else 0
        lines.append(stripped)
    while lines and lines[-1] == "":
        lines.pop()
    typer.echo("Got it, parsing now - please stop typing.", err=True)
    return "\n".join(lines) + "\n"


def _ask_about_removed_report_line(line: ReportLine) -> str:
    from nlnet_rfp_recorder.github import fetch_title
    from nlnet_rfp_recorder.timetracking.models import GitHubToken

    link = line.link
    reference = link.pr or link.issue
    label = link.url
    if reference is not None:
        token = GitHubToken.get()
        title = fetch_title(
            reference.owner, reference.repo, reference.number, token=token
        )
        if title:
            label = f"{title} ({link.url})"

    typer.echo(f"\nNo longer in the imported file: {label}", err=True)
    typer.echo(
        "  1) remove from this report only - stays reportable later (default)",
        err=True,
    )
    typer.echo("  2) never report this link again", err=True)
    typer.echo("  3) keep it in this report - don't remove it", err=True)

    while True:
        choice = typer.prompt("Choice", default="1", err=True).strip()
        decision = {"1": "remove", "2": "exclude", "3": "keep"}.get(choice)
        if decision is not None:
            return decision
        typer.echo("Please enter 1, 2, or 3.", err=True)


def _complete_mou_name(incomplete: str) -> list[str]:
    try:
        django.setup()
        from nlnet_rfp_recorder.timetracking.models import Alias, MoU

        names = list(
            MoU.objects.filter(name__startswith=incomplete).values_list(
                "name", flat=True
            )
        )
        aliases = list(
            Alias.objects.filter(
                item_type="mou", alias__startswith=incomplete
            ).values_list("alias", flat=True)
        )
        return names + aliases
    except Exception:
        return []


def _complete_task_name(incomplete: str) -> list[str]:
    try:
        django.setup()
        from nlnet_rfp_recorder.timetracking.models import Alias, Task

        names = list(
            Task.objects.filter(name__startswith=incomplete).values_list(
                "name", flat=True
            )
        )
        aliases = list(
            Alias.objects.filter(
                item_type="task", alias__startswith=incomplete
            ).values_list("alias", flat=True)
        )
        return names + aliases
    except Exception:
        return []


def _complete_link_url(incomplete: str) -> list[str]:
    try:
        django.setup()
        from nlnet_rfp_recorder.timetracking.models import Alias
        from nlnet_rfp_recorder.timetracking.models import Link as LinkModel

        urls = list(
            LinkModel.objects.filter(url__startswith=incomplete).values_list(
                "url", flat=True
            )
        )
        shortcuts = [
            f"{alias}/"
            for alias in Alias.objects.filter(
                item_type="url", alias__startswith=incomplete
            ).values_list("alias", flat=True)
        ]
        return urls + shortcuts
    except Exception:
        return []


def _complete_task_or_link(incomplete: str) -> list[str]:
    # [TASK] LINK is variadic (see _parse_task_and_link), so there's no
    # single positional slot to hang task-only or link-only completion
    # off of - offer both kinds of candidate together.
    return _complete_task_name(incomplete) + _complete_link_url(incomplete)


def _parse_task_and_link(args: list[str], command: str) -> tuple[str | None, str]:
    """Split a review/start [TASK] LINK argument list.

    One argument is just the link (the currently selected task is used,
    as before); two are the task name/alias followed by the link. One
    argument with neither "/" nor ":" can't be a link at all - it's a
    bare task name or alias, started with an empty url instead.
    """
    if len(args) == 1:
        if "/" not in args[0] and ":" not in args[0]:
            typer.echo(f"{args[0]!r} isn't a link - starting it with an empty url.")
            return args[0], ""
        return None, args[0]
    if len(args) == 2:
        return args[0], args[1]
    _fail(f"Usage: rfp {command} [TASK] LINK")


def _complete_alias_name(ctx: typer.Context, incomplete: str) -> list[str]:
    try:
        django.setup()
        from nlnet_rfp_recorder.timetracking.models import Alias

        item_type = ctx.params.get("item")
        query = Alias.objects.filter(alias__startswith=incomplete)
        if item_type:
            query = query.filter(item_type=item_type)
        return list(query.values_list("alias", flat=True))
    except Exception:
        return []


def _complete_alias_id(ctx: typer.Context, incomplete: str) -> list[str]:
    """Complete `alias set`'s `id` argument - what it names depends on
    the already-typed `item` argument (mou/task/url).
    """
    item_type = ctx.params.get("item")
    if item_type == "mou":
        return _complete_mou_name(incomplete)
    if item_type == "task":
        return _complete_task_name(incomplete)
    if item_type == "url":
        return _complete_link_url(incomplete)
    return []


def _backup_glob_pattern(database_file: Path) -> str:
    return f"{database_file.stem}-*{database_file.suffix}"


def _complete_backup_name(incomplete: str) -> list[str]:
    try:
        django.setup()
        from django.conf import settings

        database_file = Path(settings.DATABASES["default"]["NAME"])
        pattern = _backup_glob_pattern(database_file)
        names = sorted(path.stem for path in database_file.parent.glob(pattern))
        return [name for name in names if name.startswith(incomplete)]
    except Exception:
        return []


def _complete_report_id(incomplete: str) -> list[str]:
    try:
        django.setup()
        from nlnet_rfp_recorder.timetracking.models import Report

        return list(
            Report.objects.filter(pk__startswith=incomplete).values_list(
                "pk", flat=True
            )
        )
    except Exception:
        return []


@app.callback()
def callback(db: Path | None = DbOption, test: bool = TestOption) -> None:
    """Create RfPs from work on issues and pull requests, record time."""
    if test:
        db = TEST_DB_FILE
    if db is not None:
        os.environ["RFP_DB"] = str(db)


@app.command()
def version(db: Path | None = DbOption, test: bool = TestOption) -> None:
    """Print the installed version."""
    _setup(db, test)
    typer.echo(get_version("nlnet-rfp-recorder"))


TOKEN_HELP = """\
Create a GitHub personal access token to raise the API rate limit used for
issue/PR status checks (unauthenticated requests are limited to 60/hour;
a token raises that to 5,000/hour). This tool only reads public issue and
pull request status, so the minimal token is enough:

  1. Go to https://github.com/settings/personal-access-tokens/new
  2. Under "Repository access", choose "Public Repositories (read-only)".
  3. Leave all permissions at their defaults - no extra scopes are needed.
  4. Click "Generate token" and copy it.

Then run:

  rfp token <token>\
"""


@app.command()
def token(
    value: str | None = typer.Argument(None),
    db: Path | None = DbOption,
    test: bool = TestOption,
) -> None:
    """Save a GitHub token, or print how to create one if none is given."""
    _setup(db, test)
    if value is None:
        typer.echo(TOKEN_HELP)
        return

    import niquests

    from nlnet_rfp_recorder.github import fetch_authenticated_login
    from nlnet_rfp_recorder.timetracking.models import GitHubToken

    try:
        login = fetch_authenticated_login(value)
    except niquests.exceptions.RequestException as error:
        _fail(f"Could not verify the GitHub token: {error}")

    if login is None:
        _fail(
            "GitHub rejected this token (401 Unauthorized). "
            "Check that you copied it correctly."
        )

    GitHubToken.set(value)
    typer.echo(f"Saved GitHub token for {login}.")


mou_app = typer.Typer(help="Manage MoUs.", no_args_is_help=True, cls=AlphabeticalGroup)
app.add_typer(mou_app, name="mou")


@mou_app.callback()
def mou_callback(db: Path | None = DbOption, test: bool = TestOption) -> None:
    """Manage MoUs."""
    if test:
        db = TEST_DB_FILE
    if db is not None:
        os.environ["RFP_DB"] = str(db)


def _echo_mou_status(mou: MoU) -> None:
    typer.echo(f"MoU: {mou.display_name}")
    if mou.budget_line is not None:
        typer.echo(str(mou.budget_line))


@mou_app.command("status")
def mou_status(db: Path | None = DbOption, test: bool = TestOption) -> None:
    """Print the current MoU's total budget."""
    _setup(db, test)
    from nlnet_rfp_recorder.timetracking.models import MoU

    selected = MoU.get_selected()
    if selected is None:
        _fail("No MoU selected. Run `rfp mou add <name>` first.")

    _echo_mou_status(selected)


@mou_app.command("list")
def mou_list(db: Path | None = DbOption, test: bool = TestOption) -> None:
    """List all MoUs."""
    _setup(db, test)
    from nlnet_rfp_recorder.timetracking.models import MoU

    mous = MoU.objects.all()
    if not mous:
        typer.echo("No MoUs yet. Run `rfp mou add <name>` first.")
        return

    for existing in mous:
        marker = "*" if existing.selected else " "
        typer.echo(f"{marker} {existing.display_name}")


@mou_app.command("add")
def mou_add(name: str, db: Path | None = DbOption, test: bool = TestOption) -> None:
    """Add (and select) an MoU, creating it if it doesn't exist yet."""
    _setup(db, test)
    from django.core.exceptions import ValidationError

    from nlnet_rfp_recorder.timetracking.models import MoU

    try:
        MoU.select(name)
    except ValidationError as error:
        _fail("; ".join(error.messages))
    typer.echo(f"Selected MoU: {name}")


@mou_app.command("select")
def mou_select(
    name: str = typer.Argument(..., autocompletion=_complete_mou_name),
    db: Path | None = DbOption,
    test: bool = TestOption,
) -> None:
    """Select an existing MoU without creating it."""
    _setup(db, test)
    from nlnet_rfp_recorder.timetracking.models import MoU, resolve_mou_name

    name = resolve_mou_name(name)
    try:
        selected = MoU.select_existing(name)
    except ValueError as error:
        _fail(str(error))

    typer.echo(f"Selected MoU: {selected.display_name}")


@mou_app.command("remove")
def mou_remove(
    name: str = typer.Argument(..., autocompletion=_complete_mou_name),
    db: Path | None = DbOption,
    test: bool = TestOption,
) -> None:
    """Remove an MoU."""
    _setup(db, test)
    from nlnet_rfp_recorder.timetracking.models import MoU, resolve_mou_name

    name = resolve_mou_name(name)
    try:
        display_name = MoU.objects.get(name=name).display_name
    except MoU.DoesNotExist:
        _fail(f"No such MoU: {name}")
    MoU.objects.filter(name=name).delete()
    typer.echo(f"Removed MoU: {display_name}")


@mou_app.command("import")
def mou_import(
    path: Path | None = typer.Argument(
        None,
        exists=True,
        dir_okay=False,
        help=(
            "Path to a budget document (milestone table or takentaal). "
            "Omit to paste it via stdin."
        ),
    ),
    mou: str | None = typer.Option(
        None,
        "--mou",
        autocompletion=_complete_mou_name,
        help="MoU to create (if needed) and select before importing its budget.",
    ),
    db: Path | None = DbOption,
    test: bool = TestOption,
) -> None:
    """Read milestone budgets from a file (or stdin) and cap matching tasks."""
    _setup(db, test)
    from django.core.exceptions import ValidationError

    from nlnet_rfp_recorder.timetracking.models import MoU, resolve_mou_name

    if mou is not None:
        try:
            selected = MoU.select(resolve_mou_name(mou))
        except ValidationError as error:
            _fail("; ".join(error.messages))
    else:
        selected = MoU.get_selected()
        if selected is None:
            _fail("No MoU selected. Run `rfp mou add <name>` first.")

    typer.echo(f"Current MoU: {selected.display_name}")

    _, backup_file = _backup_database("mou-import")
    typer.echo(f"Backed up database to {backup_file}")

    text = path.read_text() if path is not None else _read_budget_from_stdin()
    tasks = selected.set_budget(text)
    source = str(path) if path is not None else "stdin"
    typer.echo(
        f"Imported budget for MoU {selected.display_name} from {source} "
        f"({len(tasks)} tasks)."
    )


@mou_app.command("export")
def mou_export(
    name: str | None = typer.Argument(None, autocompletion=_complete_mou_name),
    db: Path | None = DbOption,
    test: bool = TestOption,
) -> None:
    """Print the raw budget text last imported for a MoU (default: selected)."""
    _setup(db, test)
    from nlnet_rfp_recorder.timetracking.models import MoU, resolve_mou_name

    if name is not None:
        try:
            selected = MoU.objects.get(name=resolve_mou_name(name))
        except MoU.DoesNotExist:
            _fail(f"No such MoU: {name}")
    else:
        selected = MoU.get_selected()
        if selected is None:
            _fail("No MoU selected. Run `rfp mou add <name>` first.")

    typer.echo(selected.budget, nl=False)


def _show_task_status() -> None:
    from nlnet_rfp_recorder.timetracking.models import Task

    current = Task.get_selected()
    if current is None:
        _fail("No task selected. Run `rfp task select <name>` first.")

    _echo_task_status(current)


task_app = typer.Typer(help="Manage the current task.", cls=AlphabeticalGroup)
app.add_typer(task_app, name="task")


@task_app.callback(invoke_without_command=True)
def task_callback(
    ctx: typer.Context, db: Path | None = DbOption, test: bool = TestOption
) -> None:
    """Show the current task's status, or manage tasks via a subcommand."""
    if test:
        db = TEST_DB_FILE
    if db is not None:
        os.environ["RFP_DB"] = str(db)

    if ctx.invoked_subcommand is not None:
        return

    _setup(db, test)
    _show_task_status()


@task_app.command("status")
def task_status(db: Path | None = DbOption, test: bool = TestOption) -> None:
    """Print the current task's statistics."""
    _setup(db, test)
    _show_task_status()


@task_app.command("list")
def task_list(db: Path | None = DbOption, test: bool = TestOption) -> None:
    """List tasks for the current MoU, marking the selected one."""
    _setup(db, test)
    from nlnet_rfp_recorder.timetracking.models import MoU

    mou = MoU.get_selected()
    if mou is None:
        _fail("No MoU selected. Run `rfp mou add <name>` first.")

    _print_task_table(mou)


def _print_task_table(mou: MoU) -> None:
    """Print `mou`'s tasks (name/alias, budget, description), marking
    the selected one - the body of `rfp task list`, also used by
    _prompt_for_task's '?' listing.
    """
    from nlnet_rfp_recorder.timetracking.models import Task

    tasks = sorted(Task.objects.filter(mou=mou), key=lambda task: task.sort_key)
    if not tasks:
        typer.echo("No tasks yet. Run `rfp task select <name>` first.")
        return

    # Columns line up by character position only within their own group
    # (tasks sharing the same leading number, e.g. 10a/10b/10c) - groups
    # can otherwise differ a lot in width ("9a" vs "10ab", "20€/500€" vs
    # "100€/500€"), so one column width shared across all of them would
    # either waste space or not line up anywhere. Within a group, the
    # hour figures of "time left" also right-align on their own digits
    # (" 1:57", "21:57", "121:57"), and the description always starts at
    # the same character position, whether or not this particular task
    # has a budget of its own to show before it.
    def _time_field(task: Task, width: int) -> str:
        time_left = task.budget_line.time_left if task.budget_line else None
        if time_left is None:
            return ""
        padded = f"{time_left:>{width}}"
        return padded if time_left == "DONE" else f"{padded} left"

    def _budget_chunk(task: Task, money_width: int, time_width: int) -> str:
        if task.budget_line is None:
            return ""
        money = f"{task.budget_line.money:<{money_width}}"
        time_field = _time_field(task, time_width)
        return money if not time_field else f"{money}  {time_field}"

    name_widths: dict[int, int] = {}
    money_widths: dict[int, int] = {}
    time_widths: dict[int, int] = {}
    for task in tasks:
        group = task.sort_key[0]
        name_widths[group] = max(name_widths.get(group, 0), len(task.display_name))
        if task.budget_line is not None:
            money_widths[group] = max(
                money_widths.get(group, 0), len(task.budget_line.money)
            )
            if task.budget_line.time_left is not None:
                time_widths[group] = max(
                    time_widths.get(group, 0), len(task.budget_line.time_left)
                )
    chunk_widths: dict[int, int] = {}
    for task in tasks:
        group = task.sort_key[0]
        chunk = _budget_chunk(
            task, money_widths.get(group, 0), time_widths.get(group, 0)
        )
        chunk_widths[group] = max(chunk_widths.get(group, 0), len(chunk))

    for task in tasks:
        marker = "*" if task.selected else " "
        group = task.sort_key[0]
        chunk = _budget_chunk(
            task, money_widths.get(group, 0), time_widths.get(group, 0)
        )

        columns = []
        if chunk_widths[group] > 0:
            columns.append(f"{chunk:<{chunk_widths[group]}}")
        if task.description:
            columns.append(task.description)

        if columns:
            name = f"{task.display_name:<{name_widths[group]}}"
            line = f"{marker} {name}  " + "  ".join(columns)
        else:
            line = f"{marker} {task.display_name}"
        typer.echo(line.rstrip())


@task_app.command("select")
def task_select(
    name: str | None = typer.Argument(None, autocompletion=_complete_task_name),
    db: Path | None = DbOption,
    test: bool = TestOption,
) -> None:
    """Select a task, creating it if it doesn't exist yet.

    Run without a name to deselect the current task instead.
    """
    _setup(db, test)
    if name is None:
        from nlnet_rfp_recorder.timetracking.models import Task

        current = Task.get_selected()
        Task.objects.filter(selected=True).update(selected=False)
        if current is None:
            typer.echo("No task was selected.")
        else:
            typer.echo(f"Deselected task: {current.display_name}")
        return

    task = _select_task_or_fail(name)
    _echo_task_status(task)


def _parse_budget_value(raw: str) -> tuple[float, bool]:
    """Parse a --budget/--used/--max VALUE: a plain euro amount, or a
    percentage of the task's maximum budget written as "N%".

    Returns (value, is_percentage): `value` is a euro figure for a
    plain amount, or a 0.0-based fraction (e.g. 0.5 for "50%") for a
    percentage. Only checks that the text is a well-formed number -
    the field-specific bounds (0-100% for budget, unbounded for used
    and max) are enforced by the Task setter this feeds, not here.
    """
    text = raw.strip()
    if text.endswith("%"):
        percent_text = text[:-1].strip()
        try:
            percent = float(percent_text)
        except ValueError:
            raise ValueError(f"Invalid percentage: {raw!r}.") from None
        return percent / 100, True
    try:
        return float(text), False
    except ValueError:
        raise ValueError(f"Invalid amount: {raw!r}.") from None


TasksArgument = typer.Argument(
    ...,
    metavar="TASKS",
    autocompletion=_complete_task_name,
    help=(
        "Which task(s) to update: an exact task name/alias ('10a'), a "
        "dash-separated range of tasks ('1a-1c'), a whole numbered group "
        "('4' selects 4a, 4b, ... but not 40a or 14a), a dash-separated "
        "range of numbered groups ('10-14' selects 10a, 10b, ..., 14a, "
        "...), or a comma-separated combination of these ('1a-1c,2f,2h')."
    ),
)
ValueArgument = typer.Argument(
    ...,
    metavar="EUROS|PERCENT",
    help=(
        "A plain euro amount ('150') or a percentage of the task's "
        "maximum budget ('50%')."
    ),
)


def _run_task_set(
    query: str,
    raw_value: str,
    set_amount: Callable[[Task, float], None],
    set_fraction: Callable[[Task, float], None],
) -> None:
    from django.db import transaction

    from nlnet_rfp_recorder.timetracking.models import MoU, Task

    mou = MoU.get_selected()
    try:
        tasks = Task.resolve_query(query, mou)
        value, is_percentage = _parse_budget_value(raw_value)
        setter = set_fraction if is_percentage else set_amount
        with transaction.atomic():
            for task in tasks:
                setter(task, value)
    except ValueError as error:
        _fail(str(error))

    if len(tasks) == 1:
        _echo_task_status(tasks[0])
        return
    typer.echo(f"Updated {len(tasks)} tasks:")
    for task in tasks:
        line = task.display_name
        if task.budget_line is not None:
            line += f": {task.budget_line}"
        typer.echo(line)


task_set_app = typer.Typer(
    help="Set a task's budget, used, or maximum - see subcommands.",
    no_args_is_help=True,
    cls=AlphabeticalGroup,
)
task_app.add_typer(task_set_app, name="set")


@task_set_app.command("budget")
def task_set_budget(
    tasks: str = TasksArgument,
    value: str = ValueArgument,
    db: Path | None = DbOption,
    test: bool = TestOption,
) -> None:
    """Set one or more tasks' personal budget - up to their maximum.

    TASKS must already have a maximum budget set (via `rfp mou import`
    or `rfp task set max`) before a personal budget can be set within
    it. A percentage is of that maximum, so it must be between 0% and
    100% - see `rfp task set max` to raise the ceiling itself instead.

    Examples:

        rfp task set budget 10a 150      Set 10a's budget to 150 EUR.

        rfp task set budget 10a 50%      Set it to 50% of 10a's maximum.

        rfp task set budget 1a-1c,2f 0   Zero out several tasks at once.

        rfp task set budget 4 0          Zero out every task starting with "4".
    """
    _setup(db, test)
    from nlnet_rfp_recorder.timetracking.models import Task

    _run_task_set(tasks, value, Task.set_budget, Task.set_budget_fraction)


@task_set_app.command("used")
def task_set_used(
    tasks: str = TasksArgument,
    value: str = ValueArgument,
    db: Path | None = DbOption,
    test: bool = TestOption,
) -> None:
    """Set one or more tasks' used-budget baseline (money already spent
    before this tool started tracking them).

    Unlike `rfp task set budget`, there's no upper bound - a task can
    already be over its maximum before tracking begins, so a
    percentage above 100% is accepted too. Still requires a maximum
    budget to be set first when given as a percentage (there's nothing
    to take a percentage of otherwise).

    Examples:

        rfp task set used 10a 100        Mark 100 EUR as already spent.

        rfp task set used 10a 100%       Mark 10a as fully used already.

        rfp task set used 1a-1c,2f,2h 0  Reset several tasks' baseline to 0.

        rfp task set used 4 0            Reset every task starting with "4".
    """
    _setup(db, test)
    from nlnet_rfp_recorder.timetracking.models import Task

    _run_task_set(tasks, value, Task.set_used_budget, Task.set_budget_used_fraction)


@task_set_app.command("max")
def task_set_max(
    tasks: str = TasksArgument,
    value: str = ValueArgument,
    db: Path | None = DbOption,
    test: bool = TestOption,
) -> None:
    """Set one or more tasks' maximum budget directly.

    This is a correction tool for fixing up what `rfp mou import`
    brought in, not something to reach for day to day - prefer
    re-importing the MoU's budget table if it changed. A percentage
    here scales the task's *current* maximum rather than being a share
    of some larger ceiling (there isn't one): "110%" raises it by 10%,
    "50%" halves it. Doesn't touch personal_budget or used_budget even
    if they now exceed the new maximum.

    Examples:

        rfp task set max 10a 500         Set 10a's maximum to 500 EUR.

        rfp task set max 10a 110%        Raise it by 10% over its current value.

        rfp task set max 1a-1c,2f,2h 500 Set several tasks' maximum at once.

        rfp task set max 4 500           Set every task starting with "4" to 500 EUR.
    """
    _setup(db, test)
    from nlnet_rfp_recorder.timetracking.models import Task

    _run_task_set(tasks, value, Task.set_max_budget, Task.set_max_budget_fraction)


@task_app.command("remove")
def task_remove(
    name: str = typer.Argument(..., autocompletion=_complete_task_name),
    db: Path | None = DbOption,
    test: bool = TestOption,
) -> None:
    """Remove a task from the current MoU."""
    _setup(db, test)
    from nlnet_rfp_recorder.timetracking.models import MoU, Task, resolve_task_name

    mou = MoU.get_selected()
    name = resolve_task_name(name, mou)
    try:
        display_name = Task.objects.get(mou=mou, name=name).display_name
    except Task.DoesNotExist:
        _fail(f"No such task: {name}")
    Task.objects.filter(mou=mou, name=name).delete()
    typer.echo(f"Removed task: {display_name}")


@task_app.command("export")
def task_export(
    path: Path | None = typer.Argument(
        None,
        help=(
            "Output CSV file (id, alias, personal_budget, max_budget, "
            "used_budget, description), one row per task. Omit for stdout."
        ),
    ),
    db: Path | None = DbOption,
    test: bool = TestOption,
) -> None:
    """Export the current MoU's tasks as CSV."""
    _setup(db, test)
    from nlnet_rfp_recorder.timetracking.models import MoU

    mou = MoU.get_selected()
    if mou is None:
        _fail("No MoU selected. Run `rfp mou add <name>` first.")

    text = mou.export_tasks()
    if path is None:
        typer.echo(text, nl=False)
    else:
        path.write_text(text)
        typer.echo(f"Exported {mou.tasks.count()} tasks to {path}")


@task_app.command("import")
def task_import(
    path: Path = typer.Argument(..., exists=True, dir_okay=False),
    yes: bool = typer.Option(
        False,
        "--yes",
        "-y",
        help="Assume yes: remove missing tasks and unused aliases without asking.",
    ),
    db: Path | None = DbOption,
    test: bool = TestOption,
) -> None:
    """Import tasks from a CSV file produced by `task export`, creating or
    updating by id.

    This import is the source of truth for task aliases: a row's alias
    is assigned even if another task held it before. A task id, or a
    task alias, no longer implied by the file is asked about
    individually - by default it's kept; confirming removes it.
    """
    _setup(db, test)
    from nlnet_rfp_recorder.timetracking.models import Alias, MoU, Task

    mou = MoU.get_selected()
    if mou is None:
        _fail("No MoU selected. Run `rfp mou add <name>` first.")

    _, backup_file = _backup_database("task-import")
    typer.echo(f"Backed up database to {backup_file}")

    try:
        imported, missing_names, unused_aliases = mou.import_tasks(path.read_text())
    except ValueError as error:
        _fail(str(error))

    if missing_names:
        names = ", ".join(missing_names)
        if yes or typer.confirm(
            f"{len(missing_names)} existing task(s) are missing from {path} "
            f"({names}). Remove them?"
        ):
            deleted, _ = Task.objects.filter(mou=mou, name__in=missing_names).delete()
            typer.echo(f"Deleted {deleted} tasks.")

    if unused_aliases:
        names = ", ".join(unused_aliases)
        if yes or typer.confirm(
            f"{len(unused_aliases)} task alias(es) are no longer used "
            f"({names}). Delete them?"
        ):
            deleted, _ = Alias.objects.filter(
                item_type="task", mou=mou, alias__in=unused_aliases
            ).delete()
            typer.echo(f"Deleted {deleted} aliases.")

    typer.echo(f"Imported {len(imported)} tasks from {path}")


timesheet_app = typer.Typer(
    help="Manage individual time entries.", no_args_is_help=True, cls=AlphabeticalGroup
)
app.add_typer(timesheet_app, name="timesheet")


@timesheet_app.callback()
def timesheet_callback(db: Path | None = DbOption, test: bool = TestOption) -> None:
    """Manage individual time entries."""
    if test:
        db = TEST_DB_FILE
    if db is not None:
        os.environ["RFP_DB"] = str(db)


def _timesheet_display_start(start: str) -> str:
    # Round down to the minute: no seconds, no microseconds.
    return datetime.fromisoformat(start).strftime("%Y-%m-%dT%H:%M")


def _timesheet_display_duration(duration: str) -> str:
    # Round up to the minute, so a few seconds of work never shows as 0:00.
    total_seconds = int(parse_hhmmss(duration).total_seconds())
    minutes = -(-total_seconds // 60)
    hh, mm = divmod(minutes, 60)
    return f"{hh}:{mm:02d}"


def _echo_timesheet_row(row: TimesheetRow) -> None:
    start = _timesheet_display_start(row.start)
    duration = _timesheet_display_duration(row.duration)
    link = row.link or "-"
    line = f"{row.pk} {row.mou} {row.task} {start} {duration} {link}"
    if row.tags:
        line += f" {row.tags}"
    typer.echo(line)


@timesheet_app.command("show")
def timesheet_show(db: Path | None = DbOption, test: bool = TestOption) -> None:
    """List all time entries, one per line."""
    _setup(db, test)
    from nlnet_rfp_recorder.timetracking.models import TimeRecord

    records = TimeRecord.objects.order_by("start_time")
    if not records:
        typer.echo("No time entries yet.")
        return

    for record in records:
        _echo_timesheet_row(record.row)


@timesheet_app.command("export")
def timesheet_export(
    path: Path | None = typer.Argument(
        None, help="Output CSV file. Omit to print to stdout."
    ),
    db: Path | None = DbOption,
    test: bool = TestOption,
) -> None:
    """Export all time entries as CSV."""
    _setup(db, test)
    from nlnet_rfp_recorder.timesheet import write_csv
    from nlnet_rfp_recorder.timetracking.models import TimeRecord

    rows = [record.row for record in TimeRecord.objects.order_by("start_time")]
    csv_text = write_csv(rows)
    if path is None:
        typer.echo(csv_text, nl=False)
    else:
        path.write_text(csv_text)
        typer.echo(f"Exported {len(rows)} time entries to {path}")


@timesheet_app.command("import")
def timesheet_import(
    path: Path = typer.Argument(..., exists=True, dir_okay=False),
    db: Path | None = DbOption,
    test: bool = TestOption,
) -> None:
    """Import time entries from a CSV file, creating or updating by pk."""
    _setup(db, test)
    from nlnet_rfp_recorder.timesheet import read_csv
    from nlnet_rfp_recorder.timetracking.models import TimeRecord

    rows = read_csv(path.read_text())

    pk_counts: dict[int, int] = {}
    for row in rows:
        if row.pk is not None:
            pk_counts[row.pk] = pk_counts.get(row.pk, 0) + 1
    duplicate_pks = sorted(pk for pk, count in pk_counts.items() if count > 1)
    if duplicate_pks:
        ids = ", ".join(str(pk) for pk in duplicate_pks)
        _fail(
            f"Duplicate pk(s) in {path}: {ids}. Check for a copy-paste error "
            "and fix the file before importing."
        )

    _, backup_file = _backup_database("timesheet-import")
    typer.echo(f"Backed up database to {backup_file}")

    imported_pks = {row.pk for row in rows if row.pk is not None}
    existing_pks = set(TimeRecord.objects.values_list("pk", flat=True))
    missing_pks = sorted(existing_pks - imported_pks)

    for row in rows:
        try:
            TimeRecord.apply_row(row, allow_create_with_pk=True)
        except ValueError as error:
            _fail(str(error))

    if missing_pks:
        ids = ", ".join(str(pk) for pk in missing_pks)
        if typer.confirm(
            f"{len(missing_pks)} existing time entries are missing from {path} "
            f"({ids}). Delete them?"
        ):
            deleted, _ = TimeRecord.objects.filter(pk__in=missing_pks).delete()
            typer.echo(f"Deleted {deleted} time entries.")

    typer.echo(f"Imported {len(rows)} time entries from {path}")


@timesheet_app.command("remove")
def timesheet_remove(
    pk: int, db: Path | None = DbOption, test: bool = TestOption
) -> None:
    """Remove a time entry by pk."""
    _setup(db, test)
    from nlnet_rfp_recorder.timetracking.models import TimeRecord

    deleted, _ = TimeRecord.objects.filter(pk=pk).delete()
    if deleted == 0:
        _fail(f"No such time entry: {pk}")
    typer.echo(f"Removed time entry: {pk}")


@timesheet_app.command(
    "edit",
    epilog=(
        "Example:\n\n"
        "  rfp timesheet edit 42 nlnet-2026 10a 2026-09-04T09:00:00 "
        "02:00:00 https://github.com/org/repo/issues/1 review"
    ),
)
def timesheet_edit(
    pk: int = typer.Argument(
        ..., help="The time entry's id, as shown by `rfp timesheet show`."
    ),
    mou: str = typer.Argument(
        ..., autocompletion=_complete_mou_name, help="The MoU this entry belongs to."
    ),
    task: str = typer.Argument(
        ...,
        autocompletion=_complete_task_name,
        help="The task this entry belongs to (name or alias).",
    ),
    start: str = typer.Argument(
        ..., help="When it started, ISO format (2026-09-04T09:00:00)."
    ),
    duration: str = typer.Argument(..., help="How long it ran, as HH:MM:SS."),
    link: str = typer.Argument(
        ..., autocompletion=_complete_link_url, help="The issue/PR/discussion URL."
    ),
    tags: str = typer.Argument(
        "", help="Comma-separated tags to add (implementation, review)."
    ),
    db: Path | None = DbOption,
    test: bool = TestOption,
) -> None:
    """Replace a time entry's fields by pk."""
    _setup(db, test)
    from nlnet_rfp_recorder.timesheet import TimesheetRow
    from nlnet_rfp_recorder.timetracking.models import TimeRecord

    row = TimesheetRow(
        pk=pk, mou=mou, task=task, start=start, duration=duration, link=link, tags=tags
    )
    try:
        record = TimeRecord.apply_row(row)
    except ValueError as error:
        _fail(str(error))

    _echo_timesheet_row(record.row)


@app.command()
def status(db: Path | None = DbOption, test: bool = TestOption) -> None:
    """Print MoU, task, and running-timer status."""
    _setup(db, test)
    from nlnet_rfp_recorder.timetracking.models import MoU, Task, TimeRecord

    task = Task.get_selected()
    if task is not None:
        _echo_task_status(task)
    else:
        mou = MoU.get_selected()
        typer.echo(f"MoU: {mou.display_name if mou else 'none selected'}")
        typer.echo("Task: none selected")

    running = TimeRecord.get_running()
    if running is None:
        typer.echo("Not running.")
    else:
        url = running.link.url if running.link else ""
        typer.echo(f"Running: {url} ({_format_duration(running.duration)})")


def _echo_stats(stats: Statistics) -> None:
    """Print a Statistics result as a table: id, alias, time, Euro, new,
    <threshold>+ - then a Total row (skipped when there's only one task,
    since that row already is the total).

    Covers every task across every MoU, not just the selected one - this
    is a personal "how much did I work" view, not a report. The alias
    column is dropped entirely when no task in the result has one; the
    money columns (Euro, new, <threshold>+) are dropped together when the
    hourly rate is unknown. A task's "new" cell is its budget not yet
    claimed by any report line; "<threshold>+" repeats that same figure
    only when it's at least REVIEW_DEFAULT_EXCLUDE_BELOW (the same
    threshold `rfp report review` uses) - both blank (not "0€") rather
    than showing zero/below-threshold. Every money value is already
    rounded to the whole euro in Statistics, Total included, so the
    printed Total always matches adding up the rows above it.
    """
    if not stats.per_task:
        typer.echo("No time tracked in this period.")
        return

    show_alias = any(ts.task is not None and ts.task.alias for ts in stats.per_task)
    show_money = stats.total_budget is not None
    threshold_header = f"{stats.threshold:g}+"

    def _euro(value: int | None) -> str:
        return "" if value is None else f"{value}€"

    def _new_euro(value: int | None) -> str:
        return "" if not value else f"{value}€"

    def _row(
        id_: str,
        task: Task | None,
        duration: timedelta,
        budget: int | None,
        new: int | None,
        new_above_threshold: int | None,
    ) -> list[str]:
        row = [id_]
        if show_alias:
            row.append((task.alias if task is not None else None) or "")
        row.append(_format_duration(duration))
        if show_money:
            row += [
                _euro(budget),
                _new_euro(new),
                _new_euro(new_above_threshold),
            ]
        return row

    headers = ["ID"]
    if show_alias:
        headers.append("alias")
    headers.append("time")
    if show_money:
        headers += ["Euro", "new", threshold_header]

    rows = [headers]
    for ts in stats.per_task:
        id_ = ts.task.name if ts.task is not None else "?"
        rows.append(
            _row(
                id_,
                ts.task,
                ts.duration,
                ts.budget,
                ts.new_budget,
                ts.new_above_threshold,
            )
        )
    if len(stats.per_task) > 1:
        # A single task's row already is the total - repeating it would
        # be redundant.
        rows.append(
            _row(
                "Total",
                None,
                stats.total_duration,
                stats.total_budget,
                stats.total_new_budget,
                stats.total_new_above_threshold,
            )
        )

    left_aligned = {"ID", "alias"}
    widths = [max(len(row[i]) for row in rows) for i in range(len(headers))]
    for row in rows:
        cells = [
            f"{cell:<{widths[i]}}"
            if headers[i] in left_aligned
            else f"{cell:>{widths[i]}}"
            for i, cell in enumerate(row)
        ]
        typer.echo("  ".join(cells).rstrip())


stats_app = typer.Typer(
    help="Show time/budget statistics.", no_args_is_help=True, cls=AlphabeticalGroup
)
app.add_typer(stats_app, name="stats")


@stats_app.callback()
def stats_callback(db: Path | None = DbOption, test: bool = TestOption) -> None:
    """Show time/budget statistics."""
    if test:
        db = TEST_DB_FILE
    if db is not None:
        os.environ["RFP_DB"] = str(db)


@stats_app.command("today")
def stats_today(db: Path | None = DbOption, test: bool = TestOption) -> None:
    """Show time and budget worked today, per task and in total."""
    _setup(db, test)
    from nlnet_rfp_recorder.statistics import Statistics

    _echo_stats(Statistics.today())


@stats_app.command("days")
def stats_days(
    n: int = typer.Argument(..., help="How many days back to look, including today."),
    db: Path | None = DbOption,
    test: bool = TestOption,
) -> None:
    """Show time and budget worked in the last N days, per task and in total."""
    _setup(db, test)
    from nlnet_rfp_recorder.statistics import Statistics

    try:
        stats = Statistics.days(n)
    except ValueError as error:
        _fail(str(error))
    _echo_stats(stats)


@stats_app.command("total")
def stats_total(db: Path | None = DbOption, test: bool = TestOption) -> None:
    """Show time and budget worked across all recorded time, per task and in total."""
    _setup(db, test)
    from nlnet_rfp_recorder.statistics import Statistics

    _echo_stats(Statistics.total())


def _enable_task_completion(mou: MoU) -> Callable[[], None]:
    """Best-effort tab-completion of `mou`'s task names/aliases for the
    next input() call (e.g. via typer.prompt) - readline isn't available
    everywhere (notably Windows), so this quietly does nothing there;
    the '?' listing in _prompt_for_task is the fallback that always
    works regardless.

    Returns a callback that restores the previous completer - always
    call it (in a finally) once done prompting, so an unrelated later
    input() isn't left completing task names.
    """
    try:
        import readline
    except ImportError:
        return lambda: None

    from nlnet_rfp_recorder.timetracking.models import Alias, Task

    names = Task.objects.filter(mou=mou).values_list("name", flat=True)
    aliases = Alias.objects.filter(item_type="task", mou=mou).values_list(
        "alias", flat=True
    )
    candidates = sorted(set(names) | set(aliases))

    def _complete(text: str, state: int) -> str | None:
        matches = [candidate for candidate in candidates if candidate.startswith(text)]
        return matches[state] if state < len(matches) else None

    previous_completer = readline.get_completer()
    previous_delims = readline.get_completer_delims()
    readline.set_completer_delims("")
    readline.set_completer(_complete)
    readline.parse_and_bind("tab: complete")

    def _restore() -> None:
        readline.set_completer(previous_completer)
        readline.set_completer_delims(previous_delims)

    return _restore


def _prompt_for_task(
    record: TimeRecord, default_task: Task | None = None
) -> Task | None:
    """Ask which task a just-started, still-taskless time entry should
    count toward.

    `default_task` - typically the currently selected task, i.e. "the
    last task worked on" - is offered as the answer plain Enter picks,
    when there is one. Absent that (nothing currently selected), the
    task of the most recent *other* time entry that had one is offered
    instead. '?' prints the task list (name/alias, budget, description
    - same as `rfp task list`) and asks again. Anything else must be an
    existing task's name or alias: unlike `rfp task select`, a typo
    here is never silently taken as a brand new task, since this is
    asked mid-flow rather than something deliberately typed.

    Returns None - leaving the entry taskless, to be assigned later via
    `rfp edit --task` - when there's no MoU to pick a task from yet.
    """
    from nlnet_rfp_recorder.timetracking.models import (
        MoU,
        Task,
        TimeRecord,
        resolve_task_name,
    )

    mou = MoU.get_selected()
    if mou is None:
        typer.echo(
            "No MoU selected, so this entry can't be assigned a task yet. "
            "Run `rfp mou add <name>` and `rfp task select <name>`, then "
            "`rfp edit --task <name>` to assign it."
        )
        return None

    if default_task is None:
        previous = (
            TimeRecord.objects.exclude(pk=record.pk)
            .exclude(link__task__isnull=True)
            .order_by("-start_time")
            .first()
        )
        default_task = previous.link.task if previous is not None else None
    # The submitted default has to be the plain name - an aliased task's
    # "10a (foo)" isn't itself a valid answer, and would fail to resolve
    # below - but the alias is still worth showing, so it's shown
    # separately (via display_name, show_default=False) rather than
    # through click's own default echo.
    default_name = default_task.name if default_task is not None else None
    prompt_text = "Which task should this be assigned to? (enter '?' to list tasks)"
    if default_task is not None:
        prompt_text += f" [{default_task.display_name}]"

    restore_completer = _enable_task_completion(mou)
    try:
        while True:
            answer = typer.prompt(
                prompt_text,
                default=default_name,
                show_default=False,
                prompt_suffix="\n> ",
            ).strip()

            if answer == "?":
                _print_task_table(mou)
                continue

            name = resolve_task_name(answer, mou)
            try:
                return Task.objects.get(mou=mou, name=name)
            except Task.DoesNotExist:
                typer.echo(f"No such task: {answer!r}. Enter '?' to list tasks.")
    finally:
        restore_completer()


def _start(link: str, tags: str | None, task_name: str | None = None) -> None:
    from django.utils import timezone

    from nlnet_rfp_recorder.timetracking.models import Task, TimeRecord

    # Captured before anything that might touch the network (link
    # resolution, and later the implementation/review guess) or ask the
    # user anything, so none of that latency ever shows up as recorded
    # time - the entry starts exactly when this command was run.
    now = timezone.now()
    resolved_link = _resolve_link_or_fail(link)
    task = _select_task_or_fail(task_name) if task_name is not None else None
    effective_task = task or Task.get_selected()
    previously_running = TimeRecord.get_running()

    # A link already under a different task only ever gets a warning
    # here (never reassigned) - suppressed, since _apply_start_task below
    # replaces it with a proper question once the entry exists. Starting
    # never fails for lack of a task any more - TimeRecord.start happily
    # creates a taskless entry, asked about below once the clock is
    # already running.
    with warnings.catch_warnings():
        warnings.simplefilter("ignore")
        record = TimeRecord.start(resolved_link, task=task, tags=(), start_time=now)

    if previously_running is not None and previously_running.pk != record.pk:
        previously_running.refresh_from_db()
        _echo_stopped(previously_running)

    # A record whose start_time isn't `now` was resumed rather than
    # freshly created - either it was already running, or the most
    # recently stopped entry was for this same link and got reopened
    # instead of fragmenting into a second row (see TimeRecord.start).
    verb = "Continuing" if record.start_time != now else "Started"
    typer.echo(f"{verb} time entry for task {_task_name(record)}: {resolved_link}")
    record_task = record.link.task if record.link else None
    if record_task is not None and record_task.description:
        typer.echo(record_task.description)

    # The clock is already running by this point - only now is it worth
    # spending network time (if tags weren't given) or asking a question
    # (if the link's task or tag would actually change).
    if record.link is not None:
        if effective_task is not None:
            _apply_start_task(
                record.link, effective_task, explicit=task_name is not None
            )
        desired_tag = tags if tags is not None else _default_tag(resolved_link)
        _apply_start_tag(record.link, desired_tag)
        if record.link.task is None:
            chosen_task = _prompt_for_task(record, default_task=effective_task)
            if chosen_task is not None:
                record.link.task = chosen_task
                record.link.save(update_fields=["task"])
        # Whatever task the entry ended up under - matched, freshly
        # assigned, kept, moved, or just now picked - is the one just
        # worked on, so it's the selected task from now on, even if an
        # explicit task name above picked a different one that a
        # declined confirm reverted.
        if record.link.task is not None:
            record.link.task.mark_selected()


@app.command()
def start(
    args: list[str] = typer.Argument(
        ...,
        metavar="[TASK] LINK",
        autocompletion=_complete_task_or_link,
        help=(
            "A link to start against the currently selected task, or a "
            "task (name or alias) followed by a link to select it first."
        ),
    ),
    tags: str | None = TagsOption,
    db: Path | None = DbOption,
    test: bool = TestOption,
) -> None:
    """Start a time entry, optionally selecting its task first."""
    _setup(db, test)
    task_name, link = _parse_task_and_link(args, "start")
    _start(link, tags, task_name)


@app.command()
def review(
    args: list[str] = typer.Argument(
        ...,
        metavar="[TASK] LINK",
        autocompletion=_complete_task_or_link,
        help=(
            "A link to review against the currently selected task, or a "
            "task (name or alias) followed by a link to select it first."
        ),
    ),
    db: Path | None = DbOption,
    test: bool = TestOption,
) -> None:
    """Start a time entry tagged 'review', optionally selecting its task first."""
    _setup(db, test)
    task_name, link = _parse_task_and_link(args, "review")
    _start(link, "review", task_name)


@app.command()
def implement(
    args: list[str] = typer.Argument(
        ...,
        metavar="[TASK] LINK",
        autocompletion=_complete_task_or_link,
        help=(
            "A link to implement against the currently selected task, or "
            "a task (name or alias) followed by a link to select it first."
        ),
    ),
    db: Path | None = DbOption,
    test: bool = TestOption,
) -> None:
    """Start a time entry tagged 'implementation', optionally selecting its task."""
    _setup(db, test)
    task_name, link = _parse_task_and_link(args, "implement")
    _start(link, "implementation", task_name)


def _resolve_edit_record(timesheet_pk: int | None) -> TimeRecord:
    """Resolve `rfp edit`'s optional TIMESHEET_PK to the entry to edit.

    Not given (None): the most recent entry, same as before this
    argument existed. A positive number is a literal time entry pk, as
    shown by `rfp timesheet show`. A negative number instead counts
    back from the most recent entry, Python-list-style: -1 is the most
    recent entry (same as not passing anything), -2 is the one before
    that, and so on.
    """
    from nlnet_rfp_recorder.timetracking.models import TimeRecord

    if timesheet_pk is None:
        record = TimeRecord.get_last()
        if record is None:
            _fail("No time entries yet.")
        return record

    if timesheet_pk == 0:
        _fail(
            "Invalid time entry: 0. Use a positive pk (see `rfp timesheet "
            "show`), or a negative index counting back from the most "
            "recent entry (-1 = most recent, -2 = the one before that, ...)."
        )

    if timesheet_pk > 0:
        try:
            return TimeRecord.objects.get(pk=timesheet_pk)
        except TimeRecord.DoesNotExist:
            _fail(f"No such time entry: {timesheet_pk}.")

    offset = -timesheet_pk - 1
    record = TimeRecord.objects.order_by("-start_time")[offset : offset + 1].first()
    if record is None:
        _fail(f"No time entry {timesheet_pk} entries back from the most recent.")
    return record


@app.command(
    # Click otherwise routes a leading "-1", "-2", ... to its normal
    # "-<letter>" short-option matching and rejects it as unknown before
    # TIMESHEET_PK ever sees it - this makes an unrecognized short
    # option fall through to the positional argument instead, which is
    # what actually lets a negative TIMESHEET_PK be typed at all.
    context_settings={"ignore_unknown_options": True},
    epilog=(
        "Examples:\n\n"
        "  Edit the most recent entry's task:\n\n"
        "    rfp edit --task 11b\n\n"
        "  Edit entry with pk 42:\n\n"
        "    rfp edit 42 --task 11b\n\n"
        "  Edit the entry before the most recent one:\n\n"
        "    rfp edit -2 --tags review\n\n"
        "  Replace the most recent entry's link:\n\n"
        "    rfp edit --url https://github.com/org/repo/issues/1\n\n"
        "  Edit several fields of entry 42 at once:\n\n"
        "    rfp edit 42 --url https://github.com/org/repo/issues/1 "
        "--task 11b --duration 1:20"
    ),
)
def edit(
    timesheet_pk: int | None = typer.Argument(
        None,
        metavar="[TIMESHEET_PK]",
        help=(
            "Which time entry to edit: its pk (see `rfp timesheet show`), "
            "or a negative index counting back from the most recent entry "
            "(-1 = most recent, -2 = the one before that, ...). Defaults "
            "to the most recent entry."
        ),
    ),
    link: str | None = typer.Option(
        None,
        "--url",
        autocompletion=_complete_link_url,
        help="Replace this time entry's link with a different URL.",
    ),
    tags: str | None = typer.Option(
        None, "--tags", help="Comma-separated tags to add (implementation, review)."
    ),
    task: str | None = typer.Option(
        None,
        "--task",
        autocompletion=_complete_task_name,
        help="Reassign this time entry's link to a different task (name or alias).",
    ),
    duration: str | None = typer.Option(
        None,
        "--duration",
        help=(
            "Set this entry's duration - minutes ('50') or 'H:MM' ('1:20') "
            "- or, prefixed with '+'/'-', adjust it ('+15', '-1:20') - by "
            "moving its start time; a still-running entry keeps running. "
            "For when you forgot to stop it."
        ),
    ),
    db: Path | None = DbOption,
    test: bool = TestOption,
) -> None:
    """Edit a time entry's link, tags, task, and/or duration.

    Defaults to the most recent entry - pass TIMESHEET_PK to edit a
    different one.
    """
    _setup(db, test)
    from nlnet_rfp_recorder.timetracking.models import Link

    record = _resolve_edit_record(timesheet_pk)

    if link is not None:
        if record.link is None or record.link.task is None:
            _fail("Cannot edit the link: this time entry has no task.")
        link = _resolve_link_or_fail(link)
        record.link = Link.get_or_create_for_task(link, record.link.task)
        record.save(update_fields=["link"])

    if tags is not None:
        if record.link is None:
            _fail("Cannot edit tags: this time entry has no link.")
        for tag in (t.strip() for t in tags.split(",") if t.strip()):
            record.link.add_tag(tag)

    if task is not None:
        if record.link is None:
            _fail("Cannot change the task: this time entry has no link.")
        from nlnet_rfp_recorder.timetracking.models import MoU, Task, resolve_task_name

        mou = MoU.get_selected()
        task_name = resolve_task_name(task, mou)
        try:
            resolved_task = Task.objects.get(mou=mou, name=task_name)
        except Task.DoesNotExist:
            _fail(
                f"No such task: {task_name}. Run `rfp task select {task_name}` first."
            )
        record.link.task = resolved_task
        record.link.save(update_fields=["task"])
        # Editing the task is a deliberate reassignment, just like an
        # explicit task name on `start`/`review`/`implement` - it should
        # become the selected task too (see _start's mark_selected call).
        resolved_task.mark_selected()

    if duration is not None:
        amount, relative = _parse_duration_or_fail(duration)
        if relative:
            record.add_duration(amount)
        else:
            record.set_duration(amount)
        typer.echo(f"New duration: {_format_duration(record.duration)}")

    url = record.link.url if record.link else ""
    typer.echo(f"Edited {_task_name(record)} {_format_duration(record.duration)} {url}")


@app.command()
def stop(
    link: str | None = typer.Argument(None, autocompletion=_complete_link_url),
    db: Path | None = DbOption,
    test: bool = TestOption,
) -> None:
    """Stop the currently running time entry, optionally replacing its link."""
    _setup(db, test)
    from nlnet_rfp_recorder.timetracking.models import TimeRecord

    if link is not None:
        link = _resolve_link_or_fail(link)

    record = TimeRecord.stop(link)
    if record is None:
        typer.echo("No time entry is running.")
        return

    _echo_stopped(record)


@app.command("continue")
def continue_(db: Path | None = DbOption, test: bool = TestOption) -> None:
    """Start a new time entry for the same link as the last stopped one.

    Unlike `rfp start` on that same link - which reopens the existing
    entry - this always creates a fresh time entry, so a new work
    session becomes its own report line instead of merging into the last.

    If a time entry is already running, nothing is created or stopped -
    that entry is already "continuing" - this just reports it instead.
    """
    _setup(db, test)
    from nlnet_rfp_recorder.timetracking.models import TimeRecord

    running = TimeRecord.get_running()
    if running is not None:
        url = running.link.url if running.link else ""
        typer.echo(
            f"Already running task {_task_name(running)}: {url} "
            f"({_format_duration(running.duration)})"
        )
        return

    try:
        record = TimeRecord.continue_last()
    except ValueError as error:
        _fail(str(error))

    typer.echo(
        f"Continuing task {_task_name(record)} as a new entry: {record.link.url}"
    )
    task = record.link.task
    if task is not None:
        # This link's task is the one just worked on again - same
        # invariant `_start` keeps (see its call to mark_selected).
        task.mark_selected()
        if task.description:
            typer.echo(task.description)


report_app = typer.Typer(
    help=(
        "Generate and manage reports.\n\n"
        "Typical workflow: \n1) create - generate a report from unreported "
        "time. \n2) export - to a CSV file; review/edit links there if "
        "needed. \n3) import - load the edited CSV back into the report. "
        "\n4) review - go task by task, drop what isn't worth reporting "
        "yet. \n5) print - print the final report and hand it in."
    ),
    no_args_is_help=True,
    cls=AlphabeticalGroup,
)
app.add_typer(report_app, name="report")


@report_app.callback()
def report_callback(db: Path | None = DbOption, test: bool = TestOption) -> None:
    """Generate and manage reports."""
    if test:
        db = TEST_DB_FILE
    if db is not None:
        os.environ["RFP_DB"] = str(db)


@report_app.command("create")
def report_create(db: Path | None = DbOption, test: bool = TestOption) -> None:
    """Generate and persist a budget report grouped by task."""
    _setup(db, test)
    import niquests
    from django.conf import settings

    from nlnet_rfp_recorder.timetracking.models import MoU, Report, TimeRecord

    if settings.RFP_EUROS_PER_HOUR is None:
        _fail("RFP_EUROS_PER_HOUR is not set.")

    mou = MoU.get_selected()
    if mou is None:
        _fail("No MoU selected. Run `rfp mou add <name>` first.")

    running = TimeRecord.get_running()
    if running is not None:
        url = running.link.url if running.link else ""
        _fail(
            f"{_task_name(running)}: {url} is still running. "
            "Stop it first (`rfp stop`) to get an accurate report."
        )

    generated = Report.create(mou)
    try:
        excluded_links = generated.add_unreported_time_records()
    except niquests.exceptions.RequestException as error:
        generated.delete()
        _fail(f"Could not check GitHub PR status: {error}")

    if not generated.time_records.exists():
        generated.delete()
        _fail(
            f"No unreported time records for MoU {mou.display_name}. Nothing to report."
        )

    message = (
        "The report was generated. Run this to view the report:\n\n"
        f"rfp report print {generated.id}"
    )
    excluded_section = Report.format_excluded_links(excluded_links)
    if excluded_section:
        message += f"\n\n{excluded_section}"
    typer.echo(message)


@report_app.command("print")
def report_print(
    report_id: str | None = typer.Argument(None, autocompletion=_complete_report_id),
    db: Path | None = DbOption,
    test: bool = TestOption,
) -> None:
    """Print a report by id, or a preview of currently unreported entries."""
    _setup(db, test)
    import niquests

    from nlnet_rfp_recorder.timetracking.models import MoU, Report

    if report_id is not None:
        try:
            report = Report.objects.get(pk=report_id)
        except Report.DoesNotExist:
            _fail(f"No such report: {report_id}")
        typer.echo(report.generate_report())
        return

    mou = MoU.get_selected()
    if mou is None:
        _fail("No MoU selected. Run `rfp mou add <name>` first.")

    try:
        typer.echo(Report.preview(mou))
    except niquests.exceptions.RequestException as error:
        _fail(f"Could not check GitHub PR status: {error}")


@report_app.command("list")
def report_list(db: Path | None = DbOption, test: bool = TestOption) -> None:
    """List reports for the selected MoU: id, creation date, budget used."""
    _setup(db, test)
    from nlnet_rfp_recorder.timetracking.models import MoU, Report

    mou = MoU.get_selected()
    if mou is None:
        _fail("No MoU selected. Run `rfp mou add <name>` first.")

    reports = Report.objects.filter(mou=mou).order_by("created")
    if not reports:
        typer.echo("No reports yet. Run `rfp report create` first.")
        return

    for report in reports:
        date = report.created.date().isoformat()
        budget = round(report.total_budget / 10) * 10
        typer.echo(f"{report.id} {date} {budget}€")


@report_app.command("remove")
def report_remove(
    report_id: str = typer.Argument(..., autocompletion=_complete_report_id),
    db: Path | None = DbOption,
    test: bool = TestOption,
) -> None:
    """Remove a report."""
    _setup(db, test)
    from nlnet_rfp_recorder.timetracking.models import Report

    deleted, _ = Report.objects.filter(pk=report_id).delete()
    if deleted == 0:
        _fail(f"No such report: {report_id}")
    typer.echo(f"Removed report: {report_id}")


@report_app.command("export")
def report_export(
    report_id: str = typer.Argument(..., autocompletion=_complete_report_id),
    path: Path | None = typer.Argument(
        None,
        help=(
            "Output CSV file (task, link, title, budget, tags, records), "
            "one row per report line, sorted by task then link. Omit for "
            "stdout."
        ),
    ),
    db: Path | None = DbOption,
    test: bool = TestOption,
) -> None:
    """Export a report's lines as CSV, to edit budgets, tags, or records."""
    _setup(db, test)
    import niquests

    from nlnet_rfp_recorder.timetracking.models import Report

    try:
        report = Report.objects.get(pk=report_id)
    except Report.DoesNotExist:
        _fail(f"No such report: {report_id}")

    try:
        report.ensure_link_titles()
    except niquests.exceptions.RequestException as error:
        _fail(f"Could not fetch issue/PR titles from GitHub: {error}")

    text = report.export_lines()
    if path is None:
        typer.echo(text, nl=False)
    else:
        path.write_text(text)
        line_count = report.lines.count()
        typer.echo(f"Exported {line_count} report lines to {path}")


@report_app.command("import")
def report_import(
    report_id: str = typer.Argument(..., autocompletion=_complete_report_id),
    path: Path = typer.Argument(..., exists=True, dir_okay=False),
    db: Path | None = DbOption,
    test: bool = TestOption,
) -> None:
    """Replace a report's lines from a CSV file produced by `report export`.

    A line no longer in the file is asked about individually - fetching
    its issue/PR title from GitHub - so you can choose to just remove it
    from this report, exclude it from reports permanently, or keep it
    after all.
    """
    _setup(db, test)
    from nlnet_rfp_recorder.timetracking.models import Report

    try:
        report = Report.objects.get(pk=report_id)
    except Report.DoesNotExist:
        _fail(f"No such report: {report_id}")

    _, backup_file = _backup_database("report-import")
    typer.echo(f"Backed up database to {backup_file}")

    try:
        report.import_lines(path.read_text(), on_remove=_ask_about_removed_report_line)
    except ValueError as error:
        _fail(str(error))

    typer.echo(f"Report {report_id} now has {report.lines.count()} report lines.")


@report_app.command("review")
def report_review(
    report_id: str = typer.Argument(..., autocompletion=_complete_report_id),
    db: Path | None = DbOption,
    test: bool = TestOption,
) -> None:
    """Walk through a report task by task, deciding what actually gets reported.

    Each task is printed exactly as it would appear in the report, then
    you're asked whether to use it as is (default) or exclude it for now
    - tasks under REVIEW_DEFAULT_EXCLUDE_BELOW default to excluded.
    Excluding a task only detaches its time records from this report;
    they stay reportable later. Nothing changes until you confirm the
    summary at the end.
    """
    _setup(db, test)
    from django.conf import settings

    from nlnet_rfp_recorder.timetracking.models import Report

    try:
        report = Report.objects.get(pk=report_id)
    except Report.DoesNotExist:
        _fail(f"No such report: {report_id}")

    task_reviews = report.review_tasks()
    if not task_reviews:
        typer.echo(f"Report {report_id} has no lines to review.")
        return

    included: list[Task | None] = []
    excluded: list[Task | None] = []
    for task, block, task_total in task_reviews:
        typer.echo(f"\n{block}")
        task_display = task.display_name if task is not None else "?"
        default = task_total >= settings.REVIEW_DEFAULT_EXCLUDE_BELOW
        use_as_is = typer.confirm(f"Use {task_display} as is?", default=default)
        (included if use_as_is else excluded).append(task)

    def _names(tasks: list[Task | None]) -> str:
        names = [task.display_name if task is not None else "?" for task in tasks]
        return ", ".join(names) if names else "none"

    typer.echo("\nSummary:")
    typer.echo(f"  Included: {_names(included)}")
    typer.echo(f"  Excluded: {_names(excluded)}")

    if not excluded:
        typer.echo("Nothing to change.")
        return

    if not typer.confirm("Write these changes?"):
        typer.echo("Cancelled - report left unchanged.")
        return

    for task in excluded:
        report.remove_task_lines(task)
    typer.echo(f"Report {report_id} updated: {len(excluded)} task(s) removed.")


alias_app = typer.Typer(
    help="Manage aliases for MoUs, tasks, and repository URLs.",
    no_args_is_help=True,
    cls=AlphabeticalGroup,
)
app.add_typer(alias_app, name="alias")


@alias_app.callback()
def alias_callback(db: Path | None = DbOption, test: bool = TestOption) -> None:
    """Manage aliases for MoUs, tasks, and repository URLs."""
    if test:
        db = TEST_DB_FILE
    if db is not None:
        os.environ["RFP_DB"] = str(db)


@alias_app.command("set")
def alias_set(
    item: AliasItemType = typer.Argument(...),
    id: str = typer.Argument(
        ...,
        autocompletion=_complete_alias_id,
        help="mou: MoU name. task: task code. url: repo base URL.",
    ),
    alias: str = typer.Argument(...),
    db: Path | None = DbOption,
    test: bool = TestOption,
) -> None:
    """Give a MoU, task, or repository URL a short alias."""
    _setup(db, test)
    from nlnet_rfp_recorder.timetracking.models import Alias, MoU

    mou = MoU.get_selected() if item == "task" else None
    replaced = list(Alias.conflicts_for(item, id, alias, mou))
    try:
        created = Alias.create(item, id, alias, mou=mou)
    except ValueError as error:
        _fail(str(error))

    typer.echo(f"Set alias {created.alias!r} for {item} {created.target!r}.")
    for old in replaced:
        typer.echo(f"Replaced alias {old.alias!r} (was for {item} {old.target!r}).")


@alias_app.command("remove")
def alias_remove(
    item: AliasItemType = typer.Argument(...),
    id_or_alias: str = typer.Argument(
        ..., autocompletion=_complete_alias_name, help="An alias, or the id it names."
    ),
    db: Path | None = DbOption,
    test: bool = TestOption,
) -> None:
    """Remove an alias, identified by its alias or the id it names."""
    _setup(db, test)
    from nlnet_rfp_recorder.timetracking.models import Alias, MoU

    mou = MoU.get_selected() if item == "task" else None
    query = Alias.objects.filter(item_type=item)
    if item == "task":
        query = query.filter(mou=mou)

    matches = list(query.filter(alias=id_or_alias))
    if not matches:
        matches = list(query.filter(target=id_or_alias))
    if not matches:
        _fail(f"No {item} alias found for {id_or_alias!r}.")
    if len(matches) > 1:
        names = ", ".join(repr(match.alias) for match in matches)
        _fail(f"{id_or_alias!r} matches more than one {item} alias: {names}.")

    matches[0].delete()
    typer.echo(f"Removed alias {matches[0].alias!r} for {item}.")


@alias_app.command("rename")
def alias_rename(
    item: AliasItemType = typer.Argument(...),
    old_alias: str = typer.Argument(..., autocompletion=_complete_alias_name),
    new_alias: str = typer.Argument(...),
    db: Path | None = DbOption,
    test: bool = TestOption,
) -> None:
    """Rename an existing alias."""
    _setup(db, test)
    from nlnet_rfp_recorder.timetracking.models import Alias, MoU

    mou = MoU.get_selected() if item == "task" else None
    query = Alias.objects.filter(item_type=item, alias=old_alias)
    if item == "task":
        query = query.filter(mou=mou)

    try:
        existing = query.get()
    except Alias.DoesNotExist:
        _fail(f"No {item} alias: {old_alias}")

    # create() below will also remove `existing` itself (same target) -
    # only report conflicts beyond that, i.e. new_alias stolen from
    # something else.
    replaced = [
        row
        for row in Alias.conflicts_for(item, existing.target, new_alias, mou)
        if row.pk != existing.pk
    ]
    try:
        replacement = Alias.create(item, existing.target, new_alias, mou=mou)
    except ValueError as error:
        _fail(str(error))

    typer.echo(f"Renamed alias {old_alias!r} to {replacement.alias!r} for {item}.")
    for old in replaced:
        typer.echo(f"Replaced alias {old.alias!r} (was for {item} {old.target!r}).")


@alias_app.command("list")
def alias_list(
    item: AliasItemType | None = typer.Argument(None),
    db: Path | None = DbOption,
    test: bool = TestOption,
) -> None:
    """List aliases, optionally filtered to one item type."""
    _setup(db, test)
    from nlnet_rfp_recorder.timetracking.models import Alias

    aliases = Alias.objects.all()
    if item is not None:
        aliases = aliases.filter(item_type=item)
    aliases = aliases.order_by("item_type", "alias")

    if not aliases:
        typer.echo("No aliases yet. Run `rfp alias set <item> <id> <alias>` first.")
        return

    for existing in aliases:
        typer.echo(f"{existing.item_type}  {existing.target} -> {existing.alias}")


@app.command()
def migrate(db: Path | None = DbOption, test: bool = TestOption) -> None:
    """Create or update the database schema."""
    _setup(db, test)
    from django.conf import settings

    typer.echo(f"Migrated {settings.DATABASES['default']['NAME']}")


def _backup_database(action: str) -> tuple[Path, Path]:
    """Copy the database file next to itself, named after `action` and now.

    `action` identifies what's about to happen, e.g. "timesheet-import" -
    the resulting name (rfp-2026-09-15_07-51_timesheet-import.db) says
    both when and why the backup was made, so a folder of backups reads
    as a history of bulk edits rather than an anonymous timestamp list.
    """
    from django.conf import settings

    database_file = Path(settings.DATABASES["default"]["NAME"])
    timestamp = datetime.now().strftime("%Y-%m-%d_%H-%M")
    stem = f"{database_file.stem}-{timestamp}_{action}"
    backup_file = database_file.with_name(f"{stem}{database_file.suffix}")
    # Minute precision means two backups for the same action within the
    # same minute (e.g. running the same import twice in a row) would
    # otherwise collide on the same filename - number them instead of
    # silently overwriting an earlier backup.
    suffix = 2
    while backup_file.exists():
        backup_file = database_file.with_name(f"{stem}-{suffix}{database_file.suffix}")
        suffix += 1
    shutil.copy2(database_file, backup_file)
    return database_file, backup_file


@app.command()
def backup(db: Path | None = DbOption, test: bool = TestOption) -> None:
    """Copy the database file, timestamped, next to itself."""
    _setup(db, test)
    database_file, backup_file = _backup_database("backup")
    typer.echo(f"Backed up {database_file} to {backup_file}")


@app.command()
def restore(
    name: str = typer.Argument(..., autocompletion=_complete_backup_name),
    db: Path | None = DbOption,
    test: bool = TestOption,
) -> None:
    """Restore the database from a backup created by `rfp backup`."""
    _setup(db, test)
    from django.conf import settings

    database_file = Path(settings.DATABASES["default"]["NAME"])
    backup_name = (
        name if name.endswith(database_file.suffix) else name + database_file.suffix
    )
    backup_file = database_file.parent / backup_name
    if not backup_file.is_file():
        _fail(f"No such backup: {backup_file}")

    _, safety_backup = _backup_database("restore")
    shutil.copy2(backup_file, database_file)
    typer.echo(f"Backed up current database to {safety_backup}")
    typer.echo(f"Restored {database_file} from {backup_file}")


def main() -> None:
    app()
