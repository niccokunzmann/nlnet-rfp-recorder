from datetime import timedelta

from nlnet_rfp_recorder.budget import BudgetLine, format_duration_hours


def test_str_without_rate_omits_time_left():
    line = BudgetLine(used=100.0, total=500.0)

    assert str(line) == "100€/500€"


def test_str_with_rate_shows_time_left():
    line = BudgetLine(used=20.0, total=500.0, rate=20.0)

    assert str(line) == "20€/500€ 24:00 left"


def test_remaining_is_total_minus_used():
    line = BudgetLine(used=100.0, total=500.0)

    assert line.remaining == 400.0


def test_str_shows_done_when_fully_used():
    line = BudgetLine(used=500.0, total=500.0, rate=20.0)

    assert str(line) == "500€/500€ DONE"


def test_time_left_is_none_without_a_rate():
    line = BudgetLine(used=100.0, total=500.0)

    assert line.time_left is None


def test_time_left_is_the_raw_hours_remaining():
    line = BudgetLine(used=20.0, total=500.0, rate=20.0)

    assert line.time_left == "24:00"


def test_time_left_is_done_when_fully_used():
    line = BudgetLine(used=500.0, total=500.0, rate=20.0)

    assert line.time_left == "DONE"


def test_format_duration_hours_floors_to_the_whole_minute():
    # 1h 29m 59s -> floors down, does not round up to 1:30
    assert format_duration_hours(89.99 / 60) == "1:29"


def test_format_duration_hours_matches_cli_format_duration():
    # BudgetLine's "left" figure and the CLI's TimeRecord duration display
    # must use the exact same rounding rule, or the same underlying time
    # can show two different numbers next to each other.
    from nlnet_rfp_recorder.cli import _format_duration

    duration = timedelta(hours=1, minutes=23, seconds=45)
    assert format_duration_hours(duration.total_seconds() / 3600) == _format_duration(
        duration
    )
