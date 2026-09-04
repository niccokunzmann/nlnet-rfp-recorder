from nlnet_rfp_recorder.mou_budget import Milestone, parse_milestone_budgets


def test_parses_a_plain_milestone_line():
    budgets = parse_milestone_budgets("4a. A: Do the thing\t€ 280\n")

    assert budgets == {"4a": Milestone(amount=280.0, done=False)}


def test_parses_a_done_milestone_line():
    budgets = parse_milestone_budgets("(DONE) 1a. A: Licensing\t€ 120\n")

    assert budgets == {"1a": Milestone(amount=120.0, done=True)}


def test_ignores_total_lines():
    budgets = parse_milestone_budgets("(DONE) 1a. A: Licensing\t€ 120\nTotal\t€ 120\n")

    assert budgets == {"1a": Milestone(amount=120.0, done=True)}


def test_ignores_the_task_header_line():
    budgets = parse_milestone_budgets("Task 1: 1. Work on icalendar (€ 1880)\n")

    assert budgets == {}


def test_ignores_the_table_header_line():
    budgets = parse_milestone_budgets("Milestone \tAmount\n")

    assert budgets == {}


def test_handles_urls_and_extra_punctuation_in_the_description():
    line = (
        "7a. Design spec, state store, local cache, and conflict detection "
        "https://github.com/pycalendar/calendaring-sync/milestone/1\t€ 5500\n"
    )

    assert parse_milestone_budgets(line) == {"7a": Milestone(amount=5500.0, done=False)}


def test_parses_a_full_table_with_multiple_tasks():
    text = """Task 1: 1. Work on a library (€ 1480)

Milestone 	Amount
(DONE) 1a. A: Licensing - check for issues.	€ 120
(DONE) 1b. B: Tests - parametrize tests.	€ 800
1c. C: Fixes - release the library.	€ 560
Total	€ 1480

Task 2: 2. Documentation (€ 1000)

Milestone 	Amount
2a. Create and publish documentation	€ 1000
Total	€ 1000
"""

    budgets = parse_milestone_budgets(text)

    assert budgets == {
        "1a": Milestone(amount=120.0, done=True),
        "1b": Milestone(amount=800.0, done=True),
        "1c": Milestone(amount=560.0, done=False),
        "2a": Milestone(amount=1000.0, done=False),
    }
