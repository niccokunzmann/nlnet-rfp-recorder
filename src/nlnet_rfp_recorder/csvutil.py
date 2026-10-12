import csv
import io


def read_dict_rows(text: str, fieldnames: list[str]) -> list[dict[str, str | None]]:
    """Parse CSV text into dicts, whether or not its title line is there.

    A first line made up only of known column names is the title line and
    decides the column order. Otherwise every line is data and the columns
    are taken to be `fieldnames` in order.
    """
    first = next(csv.reader(io.StringIO(text)), [])
    cells = {cell.strip() for cell in first}
    has_title = bool(cells) and cells <= set(fieldnames)
    return list(
        csv.DictReader(io.StringIO(text), fieldnames=None if has_title else fieldnames)
    )
