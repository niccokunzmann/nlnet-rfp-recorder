"""SQLite triggers that keep the sync bookkeeping right, whatever code
changes the data - save(), update(), bulk_update(), m2m changes or raw SQL.

- `modified`/`modified_by` of a time entry, link or task are set to now and
  this computer whenever a synced column changes, unless the change sets
  `modified` itself (that is `rfp sync` taking another computer's version).
- `modified_by` (and a time entry's `computer`) of a new row default to
  this computer.
- A task's `field_versions` get [now, this computer] for every synced
  value that is set on a new task or changed on one, unless the change
  sets `field_versions` itself (that is `rfp sync` merging).
- A deleted time entry, link or task (of an MoU) is written to SyncDeletion,
  and so is a task whose MoU was removed: it is not synced any more.

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
    # Not selected: which task is selected is up to each computer.
    "timetracking_task": (
        "task",
        [
            "name",
            "max_budget",
            "remaining_budget",
            "used_budget",
            "personal_budget",
            "description",
        ],
    ),
}
# Only rows matching this are synced, so only their deletion is noted. A
# task without MoU is not synced: its uuid can be the one of a new task.
DELETED_WHEN = {"timetracking_task": "WHEN OLD.mou_id IS NOT NULL"}

TASK = "timetracking_task"
# A task's synced values, and when a new task's value counts as set -
# a default is no version, so it never wins over a value from elsewhere.
TASK_FIELDS = {
    "max_budget": "NEW.max_budget IS NOT NULL",
    "remaining_budget": "NEW.remaining_budget IS NOT NULL",
    "used_budget": "NEW.used_budget IS NOT NULL AND NEW.used_budget != 0",
    "personal_budget": "NEW.personal_budget IS NOT NULL",
    "description": "NEW.description != ''",
}
VERSIONS = "COALESCE(NULLIF(field_versions, ''), '{}')"


def _version_field(field: str, condition: str) -> str:
    return f"""
                UPDATE {TASK}
                SET field_versions = json_set(
                    {VERSIONS}, '$.{field}', json_array({NOW}, {MACHINE})
                )
                WHERE id = NEW.id AND {condition};"""


def task_statements() -> list[str]:
    created = "".join(
        _version_field(field, condition) for field, condition in TASK_FIELDS.items()
    )
    changed = "".join(
        _version_field(field, f"NEW.{field} IS NOT OLD.{field}")
        for field in TASK_FIELDS
    )
    return [
        f"""
            CREATE TRIGGER IF NOT EXISTS rfp_sync_task_fields_created
            AFTER INSERT ON {TASK} FOR EACH ROW
            BEGIN{created}
            END
            """,
        # json() compares the content, not how it was written.
        f"""
            CREATE TRIGGER IF NOT EXISTS rfp_sync_task_fields_modified
            AFTER UPDATE ON {TASK} FOR EACH ROW
            WHEN json({VERSIONS.replace("field_versions", "NEW.field_versions")})
                IS json({VERSIONS.replace("field_versions", "OLD.field_versions")})
            BEGIN{changed}
            END
            """,
        f"""
            CREATE TRIGGER IF NOT EXISTS rfp_sync_task_mou_removed
            AFTER UPDATE OF mou_id ON {TASK} FOR EACH ROW
            WHEN OLD.mou_id IS NOT NULL AND NEW.mou_id IS NULL
            BEGIN
                INSERT OR REPLACE INTO timetracking_syncdeletion
                    (model, uuid, deleted_at, deleted_by)
                VALUES ('task', OLD.uuid, {NOW}, {MACHINE});
            END
            """,
    ]


def trigger_statements(tables: set[str] | None = None) -> list[str]:
    """The triggers of `tables` (default: all synced tables)."""
    statements = []
    for table, (model, columns) in TABLES.items():
        if tables is not None and table not in tables:
            continue
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
            AFTER DELETE ON {table} FOR EACH ROW {DELETED_WHEN.get(table, "")}
            BEGIN
                INSERT OR REPLACE INTO timetracking_syncdeletion
                    (model, uuid, deleted_at, deleted_by)
                VALUES ('{model}', OLD.uuid, {NOW}, {MACHINE});
            END
            """,
        ]
    if tables is None or TASK in tables:
        statements += task_statements()
    for event, row in (("INSERT", "NEW"), ("DELETE", "OLD")):
        if tables is not None and "timetracking_link" not in tables:
            break
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
        # A table gets its triggers once a migration gave it the sync
        # columns - a trigger naming a missing column breaks every write.
        ready = {
            table
            for table in TABLES
            if table in tables
            and ({"modified", "field_versions"} if table == TASK else {"modified"})
            <= {
                column.name
                for column in connection.introspection.get_table_description(
                    cursor, table
                )
            }
        }
        for statement in trigger_statements(ready):
            cursor.execute(statement)
        cursor.execute(
            "UPDATE timetracking_timerecord SET computer = %s WHERE computer = ''",
            [machine],
        )
