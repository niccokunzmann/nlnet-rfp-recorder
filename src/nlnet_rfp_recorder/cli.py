import os
from importlib.metadata import version as get_version
from pathlib import Path

import typer

os.environ.setdefault("DJANGO_SETTINGS_MODULE", "nlnet_rfp_recorder.settings")

app = typer.Typer(
    name="rfp",
    help="Create RfPs from work on issues and pull requests, record time.",
    no_args_is_help=True,
)


@app.callback()
def callback(
    db: Path | None = typer.Option(
        None, "--db", help="Path to the sqlite database file."
    ),
) -> None:
    """Create RfPs from work on issues and pull requests, record time."""
    if db is not None:
        os.environ["NLNET_RFP_RECORDER_DB"] = str(db)
    import django

    django.setup()


@app.command()
def version() -> None:
    """Print the installed version."""
    typer.echo(get_version("nlnet-rfp-recorder"))


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
