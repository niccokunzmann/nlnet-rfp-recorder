from datetime import timedelta

import pytest

from nlnet_rfp_recorder.timesheet import (
    TimesheetRow,
    format_hhmmss,
    parse_hhmmss,
    read_csv,
    write_csv,
)


def test_format_hhmmss_pads_to_two_digits():
    assert format_hhmmss(timedelta(hours=1, minutes=2, seconds=3)) == "01:02:03"


def test_format_hhmmss_allows_hours_above_99():
    assert format_hhmmss(timedelta(hours=100)) == "100:00:00"


def test_parse_hhmmss_round_trips_with_format_hhmmss():
    duration = timedelta(hours=3, minutes=4, seconds=5)
    assert parse_hhmmss(format_hhmmss(duration)) == duration


def test_parse_hhmmss_rejects_wrong_shape():
    with pytest.raises(ValueError, match="HH:MM:SS"):
        parse_hhmmss("1:02")


def test_parse_hhmmss_rejects_non_numeric_parts():
    with pytest.raises(ValueError, match="HH:MM:SS"):
        parse_hhmmss("aa:bb:cc")


def test_write_csv_then_read_csv_round_trips_rows():
    rows = [
        TimesheetRow(
            pk=1,
            mou="my-mou",
            task="4a",
            start="2026-01-01T10:00:00+00:00",
            duration="01:00:00",
            link="https://github.com/org/repo/issues/1",
            tags="implementation",
        ),
        TimesheetRow(
            pk=None,
            mou="my-mou",
            task="4a",
            start="2026-01-02T10:00:00+00:00",
            duration="00:30:00",
            link="https://github.com/org/repo/issues/2",
            tags="",
        ),
    ]

    assert read_csv(write_csv(rows)) == rows


def test_read_csv_treats_blank_pk_as_none():
    text = "pk,mou,task,start,duration,link,tags\n,my-mou,4a,2026-01-01T10:00:00,00:10:00,https://x,\n"

    rows = read_csv(text)

    assert rows == [
        TimesheetRow(
            pk=None,
            mou="my-mou",
            task="4a",
            start="2026-01-01T10:00:00",
            duration="00:10:00",
            link="https://x",
            tags="",
        )
    ]


def test_read_csv_skips_blank_and_whitespace_only_lines():
    text = (
        "pk,mou,task,start,duration,link,tags\n"
        "\n"
        "   \n"
        ",my-mou,4a,2026-01-01T10:00:00,00:10:00,https://x,\n"
        "  \t  \n"
    )

    rows = read_csv(text)

    assert rows == [
        TimesheetRow(
            pk=None,
            mou="my-mou",
            task="4a",
            start="2026-01-01T10:00:00",
            duration="00:10:00",
            link="https://x",
            tags="",
        )
    ]
