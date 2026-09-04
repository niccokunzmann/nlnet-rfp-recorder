import os
from datetime import timedelta
from importlib.metadata import version as get_version
from pathlib import Path
from typing import TYPE_CHECKING, NoReturn

import django
import typer

if TYPE_CHECKING:
    from nlnet_rfp_recorder.timetracking.models import TimeRecord

os.environ.setdefault("DJANGO_SETTINGS_MODULE", "nlnet_rfp_recorder.settings")

app = typer.Typer(
    name="rfp",
    help="Create RfPs from work on issues and pull requests, record time.",
    no_args_is_help=True,
)

DbOption = typer.Option(None, "--db", help="Path to the sqlite database file.")


def _setup(db: Path | None) -> None:
    if db is not None:
        os.environ["RFP_DB"] = str(db)
    django.setup()


def _fail(message: str) -> NoReturn:
    typer.echo(message, err=True)
    raise typer.Exit(code=1)


def _format_duration(duration: timedelta) -> str:
    hours, minutes = divmod(int(duration.total_seconds() // 60), 60)
    return f"{hours}:{minutes:02d}"


def _echo_stopped(record: TimeRecord) -> None:
    link = record.links.first()
    duration = _format_duration(record.duration)
    typer.echo(f"Stopped {record.task.name} {duration} {link.url if link else ''}")


@app.callback()
def callback(db: Path | None = DbOption) -> None:
    """Create RfPs from work on issues and pull requests, record time."""
    if db is not None:
        os.environ["RFP_DB"] = str(db)


@app.command()
def version(db: Path | None = DbOption) -> None:
    """Print the installed version."""
    _setup(db)
    typer.echo(get_version("nlnet-rfp-recorder"))


@app.command()
def task(name: str, db: Path | None = DbOption) -> None:
    """Select the current task, creating it if it doesn't exist yet."""
    _setup(db)
    from django.core.exceptions import ValidationError

    from nlnet_rfp_recorder.timetracking.models import Task

    try:
        Task.select(name)
    except ValidationError as error:
        _fail("; ".join(error.messages))

    typer.echo(f"Selected task: {name}")


@app.command()
def start(link: str, db: Path | None = DbOption) -> None:
    """Start a time entry for the currently selected task."""
    _setup(db)
    from nlnet_rfp_recorder.timetracking.models import TimeRecord

    stopped = TimeRecord.stop()
    if stopped is not None:
        _echo_stopped(stopped)

    try:
        record = TimeRecord.start(link)
    except ValueError as error:
        _fail(str(error))

    typer.echo(f"Started time entry for task {record.task.name}: {link}")


@app.command()
def stop(db: Path | None = DbOption) -> None:
    """Stop the currently running time entry, if any."""
    _setup(db)
    from nlnet_rfp_recorder.timetracking.models import TimeRecord

    record = TimeRecord.stop()
    if record is None:
        typer.echo("No time entry is running.")
        return

    _echo_stopped(record)


@app.command()
def migrate(db: Path | None = DbOption) -> None:
    """Create or update the database schema."""
    _setup(db)
    from django.conf import settings
    from django.core.management import call_command

    database_file = Path(settings.DATABASES["default"]["NAME"])
    database_file.parent.mkdir(parents=True, exist_ok=True)
    call_command("migrate", verbosity=0)
    typer.echo(f"Migrated {database_file}")


def main() -> None:
    app()
