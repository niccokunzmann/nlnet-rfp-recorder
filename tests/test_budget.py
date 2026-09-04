from nlnet_rfp_recorder.budget import BudgetLine


def test_str_without_rate_omits_time_left():
    line = BudgetLine(used=100.0, total=500.0)

    assert str(line) == "Budget 100€/500€"


def test_str_with_rate_shows_time_left():
    line = BudgetLine(used=20.0, total=500.0, rate=20.0)

    assert str(line) == "Budget 20€/500€ - 24:00 left"


def test_remaining_is_total_minus_used():
    line = BudgetLine(used=100.0, total=500.0)

    assert line.remaining == 400.0


def test_str_shows_done_when_fully_used():
    line = BudgetLine(used=500.0, total=500.0, rate=20.0)

    assert str(line) == "Budget 500€/500€ - DONE"
