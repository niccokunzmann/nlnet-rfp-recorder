import re
from dataclasses import dataclass

MILESTONE_START = re.compile(r"^\(?(?:DONE\)\s*)?(?P<code>\d+[a-z]+)\.\s")
AMOUNT_END = re.compile(r"€\s*(?P<amount>\d[\d,.\s]*)\s*$")

# NLnet's "takentaal" plaintext task format: a `takentaal[-...] vX.Y` header
# line, `## [{AMOUNT}] TITLE` task headers, and `PREFIX {AMOUNT} DESCRIPTION`
# subtask lines, one per line, where PREFIX is one of - (not started),
# / (partial), * (done), ! (deprecated). Milestone codes aren't written
# explicitly - a task's number is its 1-indexed position among `##`
# headers, and a subtask's letter is its 1-indexed position within that
# task's subtask lines, matching the numbering the table format spells out.
TAKENTAAL_HEADER = re.compile(r"(?im)^takentaal\b")
TAKENTAAL_TASK = re.compile(r"^##\s")
TAKENTAAL_SUBTASK = re.compile(
    r"^(?P<prefix>[-/*!])\s*\{(?P<amount>[\d,.\s]+)\}\s*(?P<description>.*)$"
)


@dataclass
class Milestone:
    amount: float
    done: bool
    description: str = ""


def parse_milestone_budgets(text: str) -> dict[str, Milestone]:
    """Extract {milestone_code: Milestone} from an MoU budget document.

    Accepts either the exported milestone table ("1a. A: Do the thing
    € 280") or NLnet's takentaal plaintext format - whichever the text
    is in is detected from its content.
    """
    if TAKENTAAL_HEADER.search(text):
        return _parse_takentaal(text)
    return _parse_table(text)


def _parse_table(text: str) -> dict[str, Milestone]:
    budgets: dict[str, Milestone] = {}
    for raw_line in text.splitlines():
        line = raw_line.strip()
        start_match = MILESTONE_START.match(line)
        if start_match is None:
            continue
        amount_match = AMOUNT_END.search(line)
        if amount_match is None:
            continue
        amount_str = amount_match["amount"].replace(",", "").replace(" ", "")
        done = line.startswith("(DONE)")
        description = line[start_match.end() : amount_match.start()].strip()
        budgets[start_match["code"]] = Milestone(
            amount=float(amount_str), done=done, description=description
        )
    return budgets


def _milestone_letters(index: int) -> str:
    """1 -> 'a', 26 -> 'z', 27 -> 'aa', ... (spreadsheet-style)."""
    letters = ""
    while index > 0:
        index, remainder = divmod(index - 1, 26)
        letters = chr(ord("a") + remainder) + letters
    return letters


def _parse_takentaal(text: str) -> dict[str, Milestone]:
    budgets: dict[str, Milestone] = {}
    task_number = 0
    subtask_index = 0
    for raw_line in text.splitlines():
        line = raw_line.strip()
        if TAKENTAAL_TASK.match(line):
            task_number += 1
            subtask_index = 0
            continue
        subtask_match = TAKENTAAL_SUBTASK.match(line)
        if subtask_match is None or task_number == 0:
            continue
        subtask_index += 1
        amount_str = subtask_match["amount"].replace(",", "").replace(" ", "")
        done = subtask_match["prefix"] == "*"
        code = f"{task_number}{_milestone_letters(subtask_index)}"
        budgets[code] = Milestone(
            amount=float(amount_str),
            done=done,
            description=subtask_match["description"].strip(),
        )
    return budgets
