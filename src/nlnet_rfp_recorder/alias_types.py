from enum import StrEnum


class AliasItemType(StrEnum):
    """What an alias can name: a MoU, a task, or a repository URL.

    Kept dependency-free (no Django import) so the CLI can use it as a
    typer.Argument type annotation without paying for the model layer's
    import cost on every invocation - see AliasItemType's use in cli.py.
    """

    MOU = "mou"
    TASK = "task"
    URL = "url"
