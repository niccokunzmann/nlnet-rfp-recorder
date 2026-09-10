import csv
import io
from dataclasses import asdict, dataclass
from datetime import timedelta

FIELDNAMES = ["pk", "mou", "task", "start", "duration", "link", "tags", "report"]


@dataclass
class TimesheetRow:
    pk: int | None
    mou: str
    task: str
    start: str
    duration: str
    link: str
    tags: str = ""
    report: str = ""


def format_hhmmss(duration: timedelta) -> str:
    """Format a duration as 'HH:MM:SS', to second precision."""
    total_seconds = int(duration.total_seconds())
    hh, remainder = divmod(total_seconds, 3600)
    mm, ss = divmod(remainder, 60)
    return f"{hh:02d}:{mm:02d}:{ss:02d}"


def parse_hhmmss(text: str) -> timedelta:
    """Parse a 'HH:MM:SS' duration, as produced by format_hhmmss."""
    parts = text.split(":")
    if len(parts) != 3:
        raise ValueError(f"Invalid duration {text!r}; expected HH:MM:SS.")
    try:
        hh, mm, ss = (int(part) for part in parts)
    except ValueError:
        raise ValueError(f"Invalid duration {text!r}; expected HH:MM:SS.") from None
    return timedelta(hours=hh, minutes=mm, seconds=ss)


def write_csv(rows: list[TimesheetRow]) -> str:
    buffer = io.StringIO()
    writer = csv.DictWriter(buffer, fieldnames=FIELDNAMES)
    writer.writeheader()
    for row in rows:
        writer.writerow(asdict(row))
    return buffer.getvalue()


def _is_blank_row(row: dict[str, str | None]) -> bool:
    """True for a csv.DictReader row from a blank or whitespace-only line.

    csv.DictReader only drops a truly empty line on its own; one with
    stray whitespace comes back as a row with one None-filled field per
    extra column instead.
    """
    return not any(value and value.strip() for value in row.values())


def read_csv(text: str) -> list[TimesheetRow]:
    reader = csv.DictReader(io.StringIO(text))
    rows = []
    for line in reader:
        if _is_blank_row(line):
            continue
        pk = line["pk"].strip() if line.get("pk") else ""
        rows.append(
            TimesheetRow(
                pk=int(pk) if pk else None,
                mou=line["mou"],
                task=line["task"],
                start=line["start"],
                duration=line["duration"],
                link=line["link"],
                tags=line.get("tags") or "",
                report=line.get("report") or "",
            )
        )
    return rows
