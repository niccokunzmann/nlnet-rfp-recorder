import os
import shutil
import sys
import tempfile
import warnings
from datetime import datetime, timedelta
from importlib.metadata import version as get_version
from pathlib import Path
from typing import TYPE_CHECKING, NoReturn

import django
import typer
from typer.core import TyperGroup

from nlnet_rfp_recorder.budget import format_duration_hours
from nlnet_rfp_recorder.timesheet import TimesheetRow, parse_hhmmss

if TYPE_CHECKING:
    from nlnet_rfp_recorder.timetracking.models import MoU, Task, TimeRecord

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
    return format_duration_hours(duration.total_seconds() / 3600)


def _task_name(record: TimeRecord) -> str:
    if record.link is None or record.link.task is None:
        return "?"
    return record.link.task.name


def _echo_stopped(record: TimeRecord) -> None:
    duration = _format_duration(record.duration)
    url = record.link.url if record.link else ""
    typer.echo(f"Stopped {_task_name(record)} {duration} {url}")


def _echo_task_status(task: Task) -> None:
    # A task's MoU can be None: removing an MoU orphans (not deletes) its
    # tasks, so a still-selected task can outlive its MoU.
    mou_name = task.mou.name if task.mou is not None else "none (its MoU was removed)"
    typer.echo(f"MoU: {mou_name}")
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

    from nlnet_rfp_recorder.timetracking.models import MoU

    if mou is not None:
        try:
            selected = MoU.select(mou)
        except ValidationError as error:
            _fail("; ".join(error.messages))
    else:
        selected = MoU.get_selected()
        if selected is None:
            _fail("No MoU selected. Run `rfp mou add <name>` first.")

    typer.echo(f"Current MoU: {selected.name}")

    text = path.read_text() if path is not None else _read_budget_from_stdin()
    tasks = selected.set_budget(text)
    source = str(path) if path is not None else "stdin"
    typer.echo(
        f"Imported budget for MoU {selected.name} from {source} ({len(tasks)} tasks)."
    )


@mou_app.command("export")
def mou_export(db: Path | None = DbOption, test: bool = TestOption) -> None:
    """Print the raw budget text last imported for the selected MoU."""
    _setup(db, test)
    from nlnet_rfp_recorder.timetracking.models import MoU

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

    _, backup_file = _backup_database()
    typer.echo(f"Backed up database to {backup_file}")

    rows = read_csv(path.read_text())
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


@timesheet_app.command("edit")
def timesheet_edit(
    pk: int,
    mou: str,
    task: str,
    start: str,
    duration: str,
    link: str,
    tags: str = typer.Argument(""),
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
def edit(
    link: str | None = typer.Argument(None, autocompletion=_complete_link_url),
    tags: str | None = typer.Option(
        None, "--tags", help="Comma-separated tags to add (implementation, review)."
    ),
    db: Path | None = DbOption,
    test: bool = TestOption,
) -> None:
    """Edit the most recent time entry's link and/or tags."""
    _setup(db, test)
    from nlnet_rfp_recorder.timetracking.models import Link, TimeRecord

    record = TimeRecord.get_last()
    if record is None:
        _fail("No time entries yet.")

    if link is not None:
        if record.link is None or record.link.task is None:
            _fail("Cannot edit the link: this time entry has no task.")
        record.link = Link.get_or_create_for_task(link, record.link.task)
        record.save(update_fields=["link"])

    if tags is not None:
        if record.link is None:
            _fail("Cannot edit tags: this time entry has no link.")
        for tag in (t.strip() for t in tags.split(",") if t.strip()):
            record.link.add_tag(tag)

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

    record = TimeRecord.stop(link)
    if record is None:
        typer.echo("No time entry is running.")
        return

    _echo_stopped(record)


report_app = typer.Typer(
    help="Generate and manage reports.", no_args_is_help=True, cls=AlphabeticalGroup
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

    if settings.RFP_EUROS is None:
        _fail("RFP_EUROS is not set.")

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
        _fail(f"No unreported time records for MoU {mou.name}. Nothing to report.")

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
        help="Output file listing time record pks, one per line. Omit for stdout.",
    ),
    db: Path | None = DbOption,
    test: bool = TestOption,
) -> None:
    """Export a report's time record pks, to edit which records belong to it."""
    _setup(db, test)
    from nlnet_rfp_recorder.timetracking.models import Report

    try:
        report = Report.objects.get(pk=report_id)
    except Report.DoesNotExist:
        _fail(f"No such report: {report_id}")

    pks = list(report.time_records.order_by("pk").values_list("pk", flat=True))
    text = "".join(f"{pk}\n" for pk in pks)
    if path is None:
        typer.echo(text, nl=False)
    else:
        path.write_text(text)
        typer.echo(f"Exported {len(pks)} time record pks to {path}")


@report_app.command("import")
def report_import(
    report_id: str = typer.Argument(..., autocompletion=_complete_report_id),
    path: Path = typer.Argument(..., exists=True, dir_okay=False),
    db: Path | None = DbOption,
    test: bool = TestOption,
) -> None:
    """Replace a report's time records from a file of pks, one per line."""
    _setup(db, test)
    from nlnet_rfp_recorder.timetracking.models import Report, TimeRecord

    try:
        report = Report.objects.get(pk=report_id)
    except Report.DoesNotExist:
        _fail(f"No such report: {report_id}")

    lines = [line.strip() for line in path.read_text().splitlines() if line.strip()]
    try:
        wanted_pks = {int(line) for line in lines}
    except ValueError:
        _fail(f"Invalid pk in {path}; expected one integer per line.")

    current_pks = set(report.time_records.values_list("pk", flat=True))

    for pk in current_pks - wanted_pks:
        report.remove_time_record(TimeRecord.objects.get(pk=pk))

    for pk in wanted_pks - current_pks:
        try:
            record = TimeRecord.objects.get(pk=pk)
        except TimeRecord.DoesNotExist:
            _fail(f"No such time record: {pk}")
        try:
            report.add_time_record(record)
        except ValueError as error:
            _fail(str(error))

    typer.echo(f"Report {report_id} now has {len(wanted_pks)} time records.")


@app.command()
def migrate(db: Path | None = DbOption, test: bool = TestOption) -> None:
    """Create or update the database schema."""
    _setup(db, test)
    from django.conf import settings

    typer.echo(f"Migrated {settings.DATABASES['default']['NAME']}")


def _backup_database() -> tuple[Path, Path]:
    from django.conf import settings

    database_file = Path(settings.DATABASES["default"]["NAME"])
    # Microsecond precision: two backups within the same second (e.g.
    # `restore` backing up the current database right after a `backup` a
    # moment earlier) would otherwise collide on the same filename, and the
    # second copy would silently overwrite - and corrupt - the first.
    timestamp = datetime.now().strftime("%Y%m%dT%H%M%S%f")
    backup_file = database_file.with_name(
        f"{database_file.stem}-{timestamp}{database_file.suffix}"
    )
    shutil.copy2(database_file, backup_file)
    return database_file, backup_file


@app.command()
def backup(db: Path | None = DbOption, test: bool = TestOption) -> None:
    """Copy the database file, timestamped, next to itself."""
    _setup(db, test)
    database_file, backup_file = _backup_database()
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

    _, safety_backup = _backup_database()
    shutil.copy2(backup_file, database_file)
    typer.echo(f"Backed up current database to {safety_backup}")
    typer.echo(f"Restored {database_file} from {backup_file}")


def main() -> None:
    app()
