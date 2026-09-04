import re
from dataclasses import dataclass

MILESTONE_START = re.compile(r"^\(?(?:DONE\)\s*)?(?P<code>\d+[a-z]+)\.\s")
AMOUNT_END = re.compile(r"€\s*(?P<amount>\d[\d,.\s]*)\s*$")


@dataclass
class Milestone:
    amount: float
    done: bool


def parse_milestone_budgets(text: str) -> dict[str, Milestone]:
    """Extract {milestone_code: Milestone} from an MoU budget table."""
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
        budgets[start_match["code"]] = Milestone(amount=float(amount_str), done=done)
    return budgets
