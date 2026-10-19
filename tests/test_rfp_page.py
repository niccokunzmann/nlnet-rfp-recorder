from pathlib import Path

import pytest

from nlnet_rfp_recorder.rfp_page import parse_rfp_link, parse_rfp_page

SECRET = "05c87d16-06e5-4a78-8bae-7fc6e96ec0c1"
FIXTURE = Path(__file__).parent / "data" / "rfp_new_request.html"


def test_parses_budget_left():
    page = parse_rfp_page(FIXTURE.read_text())

    assert page.budget_left == 132250.0
    assert page.budget_total == 133750.0


def test_parses_milestones_by_task_number_and_letter():
    page = parse_rfp_page(FIXTURE.read_text())

    assert list(page.milestones) == ["1a", "1b", "2a"]
    assert page.milestones["1a"].description == "vCard support & testing"


def test_milestone_without_reduction_has_equal_amount_and_available():
    milestone = parse_rfp_page(FIXTURE.read_text()).milestones["1a"]

    assert (milestone.amount, milestone.available) == (3500.0, 3500.0)


def test_milestone_with_reduction_keeps_both_numbers():
    milestone = parse_rfp_page(FIXTURE.read_text()).milestones["1b"]

    assert (milestone.amount, milestone.available) == (4800.0, 4690.0)


def test_fully_paid_milestone_has_nothing_available():
    milestone = parse_rfp_page(FIXTURE.read_text()).milestones["2a"]

    assert (milestone.amount, milestone.available) == (600.0, 0.0)


def test_page_without_milestones_is_empty():
    assert parse_rfp_page("<body>nothing</body>").milestones == {}


def test_link_gives_project_and_secret():
    link = f"https://nlnet.nl/rfp/?project=2026-04-3e6&secret={SECRET}"

    assert parse_rfp_link(link) == ("2026-04-3e6", SECRET)


def test_bare_secret_has_no_project():
    assert parse_rfp_link(f" {SECRET}\n") == (None, SECRET)


@pytest.mark.parametrize(
    "text",
    ["", "hello", "https://nlnet.nl/rfp/?project=2026-04-3e6", "?secret=nope"],
)
def test_invalid_links_are_rejected(text):
    with pytest.raises(ValueError):
        parse_rfp_link(text)


def test_overview_gives_each_requests_status():
    from nlnet_rfp_recorder.rfp_page import parse_rfp_overview

    path = Path(__file__).parent / "data" / "rfp_overview.html"

    requests = parse_rfp_overview(path.read_text())

    assert [
        (r.index, str(r.date), r.recipient, r.confirmed, r.retracted, r.declined)
        for r in requests
    ] == [
        (1, "2026-09-10", "someone", True, False, True),
        (2, "2026-09-16", "other", False, True, False),
        (3, "2026-09-23", "me", True, False, False),
    ]
    assert not requests[0].claims_money
    assert not requests[1].claims_money
    assert requests[2].claims_money


def test_request_gives_task_totals_and_links():
    from nlnet_rfp_recorder.rfp_page import parse_rfp_request

    detail = parse_rfp_request(
        "x", (Path(__file__).parent / "data" / "rfp_request.html").read_text()
    )

    assert detail.tasks == {"1l": 85.0, "1n": 370.0}
    assert detail.links == {
        "https://example.com/issues/1",
        "https://example.com/issues/2",
        "https://example.com/pull/3",
    }
