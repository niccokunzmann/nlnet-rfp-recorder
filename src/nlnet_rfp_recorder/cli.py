import os
from importlib.metadata import version as get_version
from pathlib import Path
from typing import NoReturn

import django
import typer

os.environ.setdefault("DJANGO_SETTINGS_MODULE", "nlnet_rfp_recorder.settings")

app = typer.Typer(
    name="rfp",
    help="Create RfPs from work on issues and pull requests, record time.",
    no_args_is_help=True,
)


def _fail(message: str) -> NoReturn:
    typer.echo(message, err=True)
    raise typer.Exit(code=1)


@app.callback()
def callback(
    db: Path | None = typer.Option(
        None, "--db", help="Path to the sqlite database file."
    ),
) -> None:
    """Create RfPs from work on issues and pull requests, record time."""
    if db is not None:
        os.environ["NLNET_RFP_RECORDER_DB"] = str(db)
    django.setup()


@app.command()
def version() -> None:
    """Print the installed version."""
    typer.echo(get_version("nlnet-rfp-recorder"))


@app.command()
def task(name: str) -> None:
    """Select the current task, creating it if it doesn't exist yet."""
    from django.core.exceptions import ValidationError

    from nlnet_rfp_recorder.timetracking.models import Task

    try:
        Task.select(name)
    except ValidationError as error:
        _fail("; ".join(error.messages))

    typer.echo(f"Selected task: {name}")


@app.command()
def start(link: str) -> None:
    """Start a time entry for the currently selected task."""
    from nlnet_rfp_recorder.timetracking.models import TimeRecord

    try:
        record = TimeRecord.start(link)
    except ValueError as error:
        _fail(str(error))

    typer.echo(f"Started time entry for task {record.task.name}: {link}")


@app.command()
def stop() -> None:
    """Stop the currently running time entry, if any."""
    from nlnet_rfp_recorder.timetracking.models import TimeRecord

    record = TimeRecord.stop()
    if record is None:
        typer.echo("No time entry is running.")
        return

    typer.echo(f"Stopped time entry for task {record.task.name}: {record.duration}")


@app.command()
def migrate() -> None:
    """Create or update the database schema."""
    from django.conf import settings
    from django.core.management import call_command

    database_file = Path(settings.DATABASES["default"]["NAME"])
    database_file.parent.mkdir(parents=True, exist_ok=True)
    call_command("migrate", verbosity=0)
    typer.echo(f"Migrated {database_file}")


def main() -> None:
    app()
