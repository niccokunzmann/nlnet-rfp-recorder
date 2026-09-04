from nlnet_rfp_recorder.mou_budget import Milestone, parse_milestone_budgets


def test_parses_a_plain_milestone_line():
    budgets = parse_milestone_budgets("4a. A: Do the thing\t€ 280\n")

    assert budgets == {
        "4a": Milestone(amount=280.0, done=False, description="A: Do the thing")
    }


def test_parses_a_done_milestone_line():
    budgets = parse_milestone_budgets("(DONE) 1a. A: Licensing\t€ 120\n")

    assert budgets == {
        "1a": Milestone(amount=120.0, done=True, description="A: Licensing")
    }


def test_ignores_total_lines():
    budgets = parse_milestone_budgets("(DONE) 1a. A: Licensing\t€ 120\nTotal\t€ 120\n")

    assert budgets == {
        "1a": Milestone(amount=120.0, done=True, description="A: Licensing")
    }


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

    assert parse_milestone_budgets(line) == {
        "7a": Milestone(
            amount=5500.0,
            done=False,
            description=(
                "Design spec, state store, local cache, and conflict detection "
                "https://github.com/pycalendar/calendaring-sync/milestone/1"
            ),
        )
    }


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
        "1a": Milestone(
            amount=120.0, done=True, description="A: Licensing - check for issues."
        ),
        "1b": Milestone(
            amount=800.0, done=True, description="B: Tests - parametrize tests."
        ),
        "1c": Milestone(
            amount=560.0, done=False, description="C: Fixes - release the library."
        ),
        "2a": Milestone(
            amount=1000.0, done=False, description="Create and publish documentation"
        ),
    }


def test_parses_a_takentaal_document():
    text = """takentaal v1.0

# A Project

Some introduction text.

## {920} 1. Work on a library

* {120} A: Licensing - check for issues.
* {800} B: Tests - parametrize tests.

## 2. Documentation

- {1000} Create and publish documentation
"""

    budgets = parse_milestone_budgets(text)

    assert budgets == {
        "1a": Milestone(
            amount=120.0, done=True, description="A: Licensing - check for issues."
        ),
        "1b": Milestone(
            amount=800.0, done=True, description="B: Tests - parametrize tests."
        ),
        "2a": Milestone(
            amount=1000.0, done=False, description="Create and publish documentation"
        ),
    }


def test_takentaal_partial_and_deprecated_subtasks_are_not_done():
    text = """takentaal v1.0

## 1. A task

- {100} Not started yet
/ {200} Partially done
! {50} Deprecated
* {400} Actually done
"""

    budgets = parse_milestone_budgets(text)

    assert budgets["1a"].done is False
    assert budgets["1b"].done is False
    assert budgets["1c"].done is False
    assert budgets["1d"].done is True


def test_takentaal_amendment_header_variant_is_recognized():
    text = "takentaal-amendment v1.0\n\n## 1. A task\n\n* {100} Done thing\n"

    assert parse_milestone_budgets(text) == {
        "1a": Milestone(amount=100.0, done=True, description="Done thing")
    }


def test_takentaal_subtask_letters_pass_26():
    lines = ["takentaal v1.0", "", "## 1. A big task", ""]
    lines += [f"- {{10}} Item {i}" for i in range(27)]
    text = "\n".join(lines)

    budgets = parse_milestone_budgets(text)

    assert "1z" in budgets
    assert "1aa" in budgets
    assert len(budgets) == 27
