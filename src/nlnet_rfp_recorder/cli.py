import os
import sys
import tempfile
import warnings
from datetime import timedelta
from importlib.metadata import version as get_version
from pathlib import Path
from typing import TYPE_CHECKING, NoReturn

import django
import typer
from typer.core import TyperGroup

if TYPE_CHECKING:
    from nlnet_rfp_recorder.timetracking.models import Link, MoU, Task, TimeRecord

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

DbOption = typer.Option(None, "--db", help="Path to the sqlite database file.")
TestOption = typer.Option(
    False,
    "--test",
    help="Run against a throwaway database in /tmp, pre-filled with sample data.",
)
TagsOption = typer.Option(
    "implementation", "--tags", help="Comma-separated tags (implementation, review)."
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
    hours, minutes = divmod(int(duration.total_seconds() // 60), 60)
    return f"{hours}:{minutes:02d}"


def _task_name(record: TimeRecord) -> str:
    if record.link is None or record.link.task is None:
        return "?"
    return record.link.task.name


def _echo_stopped(record: TimeRecord) -> None:
    duration = _format_duration(record.duration)
    url = record.link.url if record.link else ""
    typer.echo(f"Stopped {_task_name(record)} {duration} {url}")


def _round10(amount: float) -> int:
    return round(amount / 10) * 10


def _echo_task_status(task: Task) -> None:
    typer.echo(f"MoU: {task.mou.name}")
    typer.echo(f"Selected task: {task.name}")
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


def _complete_mou_name(incomplete: str) -> list[str]:
    try:
        django.setup()
        from nlnet_rfp_recorder.timetracking.models import MoU

        return list(
            MoU.objects.filter(name__startswith=incomplete).values_list(
                "name", flat=True
            )
        )
    except Exception:
        return []


def _complete_task_name(incomplete: str) -> list[str]:
    try:
        django.setup()
        from nlnet_rfp_recorder.timetracking.models import Task

        return list(
            Task.objects.filter(name__startswith=incomplete).values_list(
                "name", flat=True
            )
        )
    except Exception:
        return []


def _complete_link_url(incomplete: str) -> list[str]:
    try:
        django.setup()
        from nlnet_rfp_recorder.timetracking.models import Link as LinkModel

        return list(
            LinkModel.objects.filter(url__startswith=incomplete).values_list(
                "url", flat=True
            )
        )
    except Exception:
        return []


def _format_link(link: Link) -> str:
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

    from nlnet_rfp_recorder.timetracking.models import GitHubToken

    GitHubToken.set(value)
    typer.echo("Saved GitHub token.")


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
    typer.echo(f"MoU: {mou.name}")
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
        typer.echo(f"{marker} {existing.name}")


@mou_app.command("add")
def mou_add(name: str, db: Path | None = DbOption, test: bool = TestOption) -> None:
    """Add (and select) an MoU, creating it if it doesn't exist yet."""
    _setup(db, test)
    from nlnet_rfp_recorder.timetracking.models import MoU

    MoU.select(name)
    typer.echo(f"Selected MoU: {name}")


@mou_app.command("select")
def mou_select(
    name: str = typer.Argument(..., autocompletion=_complete_mou_name),
    db: Path | None = DbOption,
    test: bool = TestOption,
) -> None:
    """Select an existing MoU without creating it."""
    _setup(db, test)
    from nlnet_rfp_recorder.timetracking.models import MoU

    try:
        MoU.select_existing(name)
    except ValueError as error:
        _fail(str(error))

    typer.echo(f"Selected MoU: {name}")


@mou_app.command("remove")
def mou_remove(
    name: str = typer.Argument(..., autocompletion=_complete_mou_name),
    db: Path | None = DbOption,
    test: bool = TestOption,
) -> None:
    """Remove an MoU."""
    _setup(db, test)
    from nlnet_rfp_recorder.timetracking.models import MoU

    deleted, _ = MoU.objects.filter(name=name).delete()
    if deleted == 0:
        _fail(f"No such MoU: {name}")
    typer.echo(f"Removed MoU: {name}")


@mou_app.command("budget")
def mou_budget(
    path: Path | None = typer.Argument(
        None,
        exists=True,
        dir_okay=False,
        help="Path to a budget table. Omit to paste it via stdin.",
    ),
    mou: str | None = typer.Option(
        None,
        "--mou",
        autocompletion=_complete_mou_name,
        help="MoU to create (if needed) and select before setting its budget.",
    ),
    db: Path | None = DbOption,
    test: bool = TestOption,
) -> None:
    """Read milestone budgets from a file (or stdin) and cap matching tasks."""
    _setup(db, test)
    from nlnet_rfp_recorder.timetracking.models import MoU

    if mou is not None:
        selected = MoU.select(mou)
    else:
        selected = MoU.get_selected()
        if selected is None:
            _fail("No MoU selected. Run `rfp mou add <name>` first.")

    typer.echo(f"Current MoU: {selected.name}")

    text = path.read_text() if path is not None else _read_budget_from_stdin()
    tasks = selected.set_budget(text)
    source = str(path) if path is not None else "stdin"
    typer.echo(
        f"Set budget for MoU {selected.name} from {source} ({len(tasks)} tasks)."
    )


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
    from nlnet_rfp_recorder.timetracking.models import MoU, Task

    mou = MoU.get_selected()
    if mou is None:
        _fail("No MoU selected. Run `rfp mou add <name>` first.")

    tasks = Task.objects.filter(mou=mou)
    if not tasks:
        typer.echo("No tasks yet. Run `rfp task select <name>` first.")
        return

    for task in tasks:
        marker = "*" if task.selected else " "
        line = f"{marker} {task.name}"
        if task.budget_line is not None:
            line += f"  {task.budget_line}"
        typer.echo(line)


@task_app.command("select")
def task_select(
    name: str = typer.Argument(..., autocompletion=_complete_task_name),
    db: Path | None = DbOption,
    test: bool = TestOption,
) -> None:
    """Select a task, creating it if it doesn't exist yet."""
    _setup(db, test)
    from django.core.exceptions import ValidationError

    from nlnet_rfp_recorder.timetracking.models import Task

    try:
        with warnings.catch_warnings(record=True) as caught:
            warnings.simplefilter("always")
            task = Task.select(name)
    except ValidationError as error:
        _fail("; ".join(error.messages))
    except ValueError as error:
        _fail(str(error))

    for warning in caught:
        typer.echo(str(warning.message), err=True)

    _echo_task_status(task)


@task_app.command("set")
def task_set(
    name: str = typer.Argument(..., autocompletion=_complete_task_name),
    budget: float | None = typer.Option(
        None, "--budget", help="Set the task's total budget."
    ),
    used: float | None = typer.Option(
        None, "--used", help="Set the task's used budget baseline."
    ),
    db: Path | None = DbOption,
    test: bool = TestOption,
) -> None:
    """Manually set a task's total and/or used budget."""
    _setup(db, test)
    from nlnet_rfp_recorder.timetracking.models import MoU, Task

    mou = MoU.get_selected()
    try:
        task = Task.objects.get(mou=mou, name=name)
    except Task.DoesNotExist:
        _fail(f"No such task: {name}. Run `rfp task select {name}` first.")

    fields = []
    if budget is not None:
        task.max_budget = budget
        fields.append("max_budget")
    if used is not None:
        task.used_budget = used
        fields.append("used_budget")
    if fields:
        task.save(update_fields=fields)

    _echo_task_status(task)


@task_app.command("remove")
def task_remove(
    name: str = typer.Argument(..., autocompletion=_complete_task_name),
    db: Path | None = DbOption,
    test: bool = TestOption,
) -> None:
    """Remove a task from the current MoU."""
    _setup(db, test)
    from nlnet_rfp_recorder.timetracking.models import MoU, Task

    mou = MoU.get_selected()
    deleted, _ = Task.objects.filter(mou=mou, name=name).delete()
    if deleted == 0:
        _fail(f"No such task: {name}")
    typer.echo(f"Removed task: {name}")


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
        typer.echo(f"MoU: {mou.name if mou else 'none selected'}")
        typer.echo("Task: none selected")

    running = TimeRecord.get_running()
    if running is None:
        typer.echo("Not running.")
    else:
        url = running.link.url if running.link else ""
        typer.echo(f"Running: {url} ({_format_duration(running.duration)})")


def _start(link: str, tags: str) -> None:
    from nlnet_rfp_recorder.timetracking.models import TimeRecord

    stopped = TimeRecord.stop()
    if stopped is not None:
        _echo_stopped(stopped)

    tag_list = [tag.strip() for tag in tags.split(",") if tag.strip()]

    try:
        with warnings.catch_warnings(record=True) as caught:
            warnings.simplefilter("always")
            record = TimeRecord.start(link, tags=tag_list)
    except ValueError as error:
        _fail(str(error))

    for warning in caught:
        typer.echo(str(warning.message), err=True)

    typer.echo(f"Started time entry for task {_task_name(record)}: {link}")


@app.command()
def start(
    link: str = typer.Argument(..., autocompletion=_complete_link_url),
    tags: str = TagsOption,
    db: Path | None = DbOption,
    test: bool = TestOption,
) -> None:
    """Start a time entry for the currently selected task."""
    _setup(db, test)
    _start(link, tags)


@app.command()
def review(
    link: str = typer.Argument(..., autocompletion=_complete_link_url),
    db: Path | None = DbOption,
    test: bool = TestOption,
) -> None:
    """Start a time entry tagged 'review' for the currently selected task."""
    _setup(db, test)
    _start(link, "review")


@app.command()
def stop(db: Path | None = DbOption, test: bool = TestOption) -> None:
    """Stop the currently running time entry, if any."""
    _setup(db, test)
    from nlnet_rfp_recorder.timetracking.models import TimeRecord

    record = TimeRecord.stop()
    if record is None:
        typer.echo("No time entry is running.")
        return

    _echo_stopped(record)


@app.command()
def report(db: Path | None = DbOption, test: bool = TestOption) -> None:
    """Print a budget report grouped by task."""
    _setup(db, test)
    from django.conf import settings

    from nlnet_rfp_recorder.timetracking.models import Task, TimeRecord

    if settings.RFP_EUROS is None:
        _fail("RFP_EUROS is not set.")

    total = 0.0
    for task in Task.objects.all():
        budget = task.budget or 0.0
        total += budget
        typer.echo(f"{task.name}: {_round10(budget)}€")

        links = task.billable_links
        issue_links = [link for link in links if link.is_issue]
        pr_links = [link for link in links if link.is_pr]
        other_links = [link for link in links if not link.is_issue and not link.is_pr]

        if issue_links:
            typer.echo("  Issues:")
            for link in issue_links:
                typer.echo(f"    - {_format_link(link)}")

        if pr_links:
            typer.echo("  Pull Requests:")
            for link in pr_links:
                typer.echo(f"    - {_format_link(link)}")

        if other_links:
            typer.echo("  Links:")
            for link in other_links:
                typer.echo(f"    - {_format_link(link)}")

    typer.echo(f"Total: {_round10(total)}€")

    running = TimeRecord.objects.running()
    if running.exists():
        typer.echo("Not included in the report (still running):", err=True)
        for record in running:
            url = record.link.url if record.link else ""
            typer.echo(f"  - {_task_name(record)}: {url}", err=True)


@app.command()
def migrate(db: Path | None = DbOption, test: bool = TestOption) -> None:
    """Create or update the database schema."""
    _setup(db, test)
    from django.conf import settings

    typer.echo(f"Migrated {settings.DATABASES['default']['NAME']}")


def main() -> None:
    app()
