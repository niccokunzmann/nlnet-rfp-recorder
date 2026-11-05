"""SQLite triggers that keep the sync bookkeeping right, whatever code
changes the data - save(), update(), bulk_update(), m2m changes or raw SQL.

- `modified`/`modified_by` of a time entry or link are set to now and
  this computer whenever a synced column changes, unless the change sets
  `modified` itself (that is `rfp sync` taking another computer's version).
- `modified_by` (and a time entry's `computer`) of a new row default to
  this computer.
- A deleted time entry or link is written to SyncDeletion.

Installed after every migrate (see apps.py): rebuilding a table in a
later migration drops its triggers, so they are put back every time.
"""

NOW = "strftime('%Y-%m-%dT%H:%M:%fZ', 'now')"
MACHINE = (
    "COALESCE((SELECT machine FROM timetracking_syncsettings ORDER BY id LIMIT 1), '')"
)


def _changed(columns: list[str]) -> str:
    return " OR ".join(f"NEW.{column} IS NOT OLD.{column}" for column in columns)


TABLES = {
    # table: (sync model name, columns whose change is a modification)
    "timetracking_timerecord": (
        "time_record",
        ["start_time", "end_time", "stop_reason", "link_id"],
    ),
    # Not title and closed_at: a cached GitHub answer is not an edit.
    "timetracking_link": ("link", ["url", "task_id", "excluded_from_reports"]),
}


def trigger_statements() -> list[str]:
    statements = []
    for table, (model, columns) in TABLES.items():
        name = f"rfp_sync_{model}"
        computer = (
            f", computer = COALESCE(NULLIF(NEW.computer, ''), {MACHINE})"
            if model == "time_record"
            else ""
        )
        statements += [
            f"""
            CREATE TRIGGER IF NOT EXISTS {name}_modified
            AFTER UPDATE ON {table} FOR EACH ROW
            WHEN NEW.modified IS OLD.modified AND ({_changed(columns)})
            BEGIN
                UPDATE {table} SET modified = {NOW}, modified_by = {MACHINE}
                WHERE id = NEW.id;
            END
            """,
            f"""
            CREATE TRIGGER IF NOT EXISTS {name}_created
            AFTER INSERT ON {table} FOR EACH ROW
            BEGIN
                UPDATE {table}
                SET modified_by = COALESCE(NULLIF(NEW.modified_by, ''), {MACHINE})
                    {computer}
                WHERE id = NEW.id;
            END
            """,
            f"""
            CREATE TRIGGER IF NOT EXISTS {name}_deleted
            AFTER DELETE ON {table} FOR EACH ROW
            BEGIN
                INSERT OR REPLACE INTO timetracking_syncdeletion
                    (model, uuid, deleted_at, deleted_by)
                VALUES ('{model}', OLD.uuid, {NOW}, {MACHINE});
            END
            """,
        ]
    for event, row in (("INSERT", "NEW"), ("DELETE", "OLD")):
        statements.append(
            f"""
            CREATE TRIGGER IF NOT EXISTS rfp_sync_link_tags_{event.lower()}
            AFTER {event} ON timetracking_link_tags FOR EACH ROW
            BEGIN
                UPDATE timetracking_link
                SET modified = {NOW}, modified_by = {MACHINE}
                WHERE id = {row}.link_id;
            END
            """
        )
    return statements


def install(connection) -> None:
    """Create the triggers that are missing and stamp rows from before
    sync existed with this computer."""
    if connection.vendor != "sqlite":
        return
    tables = set(connection.introspection.table_names())
    if not {"timetracking_syncdeletion", "timetracking_syncsettings"} <= tables:
        return  # not migrated that far yet
    from .models import SyncSettings

    machine = SyncSettings.get().machine
    with connection.cursor() as cursor:
        for statement in trigger_statements():
            cursor.execute(statement)
        cursor.execute(
            "UPDATE timetracking_timerecord SET computer = %s WHERE computer = ''",
            [machine],
        )
