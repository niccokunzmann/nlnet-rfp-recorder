"""Reading NLnet's "Request for Payment" pages.

A project's RfP pages (https://nlnet.nl/rfp/?project=ID&secret=SECRET) are
only ever fetched with GET requests - this module never sends a POST, so
nothing can be submitted by accident. The overview and the request pages
are read directly. The task list is only on the "New request" form, which
NLnet opens with a POST; save that page with your browser and pass the
file to `rfp mou update`.
"""

import datetime
import html
import re
from dataclasses import dataclass, field
from urllib.parse import parse_qs, urlparse

RFP_URL = "https://nlnet.nl/rfp/"

PROJECT_ID = re.compile(r"^\d{4}-\d{2}-[0-9a-z]+$")
SECRET = re.compile(r"^[0-9a-f]{8}(?:-[0-9a-f]{4}){3}-[0-9a-f]{12}$", re.IGNORECASE)

_TAG = re.compile(r"<[^>]+>")
_EURO = r"€\s*(?P<{}>\d[\d\s  ,.]*)"
_BUDGET_LEFT = re.compile(r"<strong>Budget left:</strong>\s*" + _EURO.format("amount"))
_REMAINING = re.compile(
    r"<strong>Remaining amount:</strong>.*?"
    r"<strong>" + _EURO.format("left") + r"</strong>\s*available\s*"
    r"\(out of\s*" + _EURO.format("total") + r"\)",
    re.DOTALL,
)
_TASK_TITLE = re.compile(r"<strong>(?P<number>\d+)\.\s[^<]*</strong>")
_MILESTONE = re.compile(
    r'<div class="col7">(?P<text>[^<]*)</div>\s*'
    r'<div class="col2 colamount txt_right">\s*<small>\s*'
    r"<label[^>]*>(?P<label>.*?)</label>",
    re.DOTALL,
)
_MILESTONE_TEXT = re.compile(r"^(?P<letter>[a-z]+)\.\s+(?P<description>.*)$", re.DOTALL)
_LABEL_ORIGINAL = re.compile(r"<em>\s*" + _EURO.format("amount") + r"\s*</em>")


@dataclass
class RfpMilestone:
    code: str
    description: str
    available: float
    amount: float  # the MoU amount; NLnet shows it only once it differs


@dataclass
class RfpPage:
    budget_left: float | None = None
    budget_total: float | None = None
    milestones: dict[str, RfpMilestone] = field(default_factory=dict)


def _amount(text: str) -> float:
    return float(re.sub(r"[\s  ,]", "", text))


def parse_rfp_link(text: str) -> tuple[str | None, str]:
    """(project, secret) from an RfP link, or (None, secret) for a bare secret."""
    text = text.strip()
    if SECRET.match(text):
        return None, text
    query = parse_qs(urlparse(text).query)
    secrets = query.get("secret", [])
    if not secrets or not SECRET.match(secrets[0]):
        raise ValueError(
            "Expected an RfP link like "
            f"{RFP_URL}?project=YYYY-MM-xxx&secret=... or the bare secret."
        )
    projects = query.get("project", [])
    project = projects[0] if projects else None
    if project is not None and not PROJECT_ID.match(project):
        raise ValueError(f"Invalid project id {project!r} in link.")
    return project, secrets[0]


def parse_rfp_page(text: str) -> RfpPage:
    """Read the budget left and every milestone from a new-request form."""
    page = RfpPage()
    remaining = _REMAINING.search(text)
    if remaining is not None:
        page.budget_left = _amount(remaining["left"])
        page.budget_total = _amount(remaining["total"])
    else:
        top = _BUDGET_LEFT.search(text)
        if top is not None:
            page.budget_left = _amount(top["amount"])

    # Milestones belong to the closest task title before them.
    events = sorted(
        [(m.start(), "task", m) for m in _TASK_TITLE.finditer(text)]
        + [(m.start(), "milestone", m) for m in _MILESTONE.finditer(text)],
        key=lambda event: event[0],
    )
    task_number: str | None = None
    for _, kind, match in events:
        if kind == "task":
            task_number = match["number"]
            continue
        text_match = _MILESTONE_TEXT.match(html.unescape(match["text"]).strip())
        if task_number is None or text_match is None:
            continue
        label = match["label"]
        original = _LABEL_ORIGINAL.search(label)
        available = _amount(re.search(_EURO.format("a"), label)["a"])
        code = f"{task_number}{text_match['letter']}"
        page.milestones[code] = RfpMilestone(
            code=code,
            description=text_match["description"].strip(),
            available=available,
            amount=_amount(original["amount"]) if original else available,
        )
    return page


@dataclass
class RfpRequest:
    """One row of the overview: a request for payment filed with NLnet."""

    id: str
    index: int  # 1-based position on the overview page
    date: datetime.date | None
    amount: float
    recipient: str
    confirmed: bool = False  # verified by email
    retracted: bool = False
    declined: bool = False
    approved_date: datetime.date | None = None
    paid_date: datetime.date | None = None

    @property
    def claims_money(self) -> bool:
        return not (self.declined or self.retracted)


@dataclass
class RfpRequestDetail:
    """A request's results text: what was requested per task and link."""

    id: str
    tasks: dict[str, float] = field(default_factory=dict)
    links: set[str] = field(default_factory=set)


_OVERVIEW_ROW = re.compile(
    r'<div class="row[^"]*">\s*<div class="col2">.*?'
    r'href="(?P<id>[0-9a-f-]{36})\?[^"]*">(?P<when>.*?)</a>.*?'
    r'<div class="col2 txt_right">(?P<amount_cell>.*?)</div>\s*'
    r'<div class="col2 txt_right">(?P<recipient>.*?)</div>\s*'
    r'<div class="col2 txt_center"[^>]*>(?P<confirmed>.*?)</div>\s*'
    r'<div class="col2 txt_center">(?P<approved>.*?)</div>\s*'
    r'<div class="col2 txt_center">(?P<paid>.*?)</div>',
    re.DOTALL,
)
_RESULTS = re.compile(r'<pre class="is-family-monospace">(.*?)</pre>', re.DOTALL)
_RESULT_TASK = re.compile(r"^(?P<task>\S+): (?P<amount>\d+)€\s*$", re.MULTILINE)
_RESULT_LINK = re.compile(r'<a href="(?P<url>[^"]+)">')


_DATE = re.compile(r"\d{4}-\d{2}-\d{2}")


def _date(text: str) -> datetime.date | None:
    found = _DATE.search(text)
    return datetime.date.fromisoformat(found[0]) if found else None


def parse_rfp_overview(text: str) -> list[RfpRequest]:
    """The requests listed on the overview page, in page order."""
    requests: dict[str, RfpRequest] = {}
    for row in _OVERVIEW_ROW.finditer(text):
        amount = re.search(_EURO.format("amount"), _TAG.sub("", row["amount_cell"]))
        if amount is None or row["id"] in requests:
            continue
        when = _TAG.sub("", row["when"])
        confirmed = html.unescape(_TAG.sub("", row["confirmed"])).strip()
        approved = html.unescape(_TAG.sub("", row["approved"])).strip()
        requests[row["id"]] = RfpRequest(
            id=row["id"],
            index=len(requests) + 1,
            date=_date(when),
            amount=_amount(amount["amount"]),
            recipient=_TAG.sub("", row["recipient"]).strip(),
            confirmed=confirmed == "\u2713",
            retracted="retracted" in confirmed,
            declined=approved.startswith("Declined"),
            approved_date=None if approved.startswith("Declined") else _date(approved),
            paid_date=_date(html.unescape(_TAG.sub("", row["paid"]))),
        )
    return list(requests.values())


def parse_rfp_request(request_id: str, text: str) -> RfpRequestDetail:
    """The per-task totals and links of the report text sent with a request."""
    detail = RfpRequestDetail(id=request_id)
    results = _RESULTS.search(text)
    if results is None:
        return detail
    body = results[1]
    for task in _RESULT_TASK.finditer(html.unescape(_TAG.sub("", body))):
        if task["task"] != "Total":
            detail.tasks[task["task"]] = float(task["amount"])
    detail.links = {html.unescape(m["url"]) for m in _RESULT_LINK.finditer(body)}
    return detail


def fetch_rfp_requests(
    project: str, secret: str
) -> list[tuple[RfpRequest, RfpRequestDetail]]:
    """Every request of the project with its details (GET requests only)."""
    import niquests

    query = {"project": project, "secret": secret}
    overview = niquests.get(RFP_URL + "overview", params=query, timeout=30)
    overview.raise_for_status()
    result = []
    for request in parse_rfp_overview(overview.text or ""):
        page = niquests.get(RFP_URL + request.id, params=query, timeout=30)
        page.raise_for_status()
        result.append((request, parse_rfp_request(request.id, page.text or "")))
    return result
