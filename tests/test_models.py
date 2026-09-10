import csv
import io
import warnings
from datetime import datetime, timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from django.core.exceptions import ValidationError
from django.utils import timezone

from nlnet_rfp_recorder.github import TIMEOUT_SECONDS, Discussion, Issue, PullRequest
from nlnet_rfp_recorder.timesheet import TimesheetRow
from nlnet_rfp_recorder.timetracking.models import (
    Alias,
    GitHubToken,
    Link,
    MoU,
    Report,
    ReportLine,
    Task,
    TaskWarning,
    TimeRecord,
    resolve_link,
    resolve_mou_name,
    resolve_task_name,
)
from nlnet_rfp_recorder.timetracking.models.report import (
    _round_link_budget,
    _sum_link_budgets,
    _task_budget,
)

pytestmark = pytest.mark.django_db


def _mock_github_session(state: str = "closed"):
    """A mock niquests.AsyncSession() context manager returning `state`."""
    response = MagicMock(status_code=200)
    response.json = MagicMock(return_value={"state": state})
    session = MagicMock()
    session.get = AsyncMock(return_value=response)
    session.__aenter__ = AsyncMock(return_value=session)
    session.__aexit__ = AsyncMock(return_value=False)
    return patch(
        "nlnet_rfp_recorder.github.niquests.AsyncSession", return_value=session
    )


def test_save_and_reload_a_finished_record():
    task = Task.objects.create(name="10a")
    link = Link.objects.create(task=task, url="https://example.com/issues/1")
    record = TimeRecord.objects.create(
        link=link,
        start_time=datetime(2026, 9, 4, 9, 0),
        end_time=datetime(2026, 9, 4, 10, 0),
    )

    reloaded = TimeRecord.objects.get(pk=record.pk)
    assert reloaded.start_time == record.start_time
    assert reloaded.end_time == record.end_time
    assert reloaded.link.url == "https://example.com/issues/1"
    assert reloaded.is_running is False


def test_a_record_without_end_time_is_running():
    record = TimeRecord.objects.create(start_time=datetime(2026, 9, 4, 9, 0))

    assert record.is_running is True
    reloaded = TimeRecord.objects.get(pk=record.pk)
    assert reloaded.end_time is None


def test_finished_records_are_excluded_from_running():
    TimeRecord.objects.create(
        start_time=datetime(2026, 9, 4, 9, 0),
        end_time=datetime(2026, 9, 4, 10, 0),
    )

    assert list(TimeRecord.objects.running()) == []


def test_running_returns_only_unfinished_records():
    running = TimeRecord.objects.create(start_time=datetime(2026, 9, 4, 9, 0))
    TimeRecord.objects.create(
        start_time=datetime(2026, 9, 4, 9, 0),
        end_time=datetime(2026, 9, 4, 10, 0),
    )

    assert list(TimeRecord.objects.running()) == [running]


def test_duration_of_a_finished_record():
    record = TimeRecord.objects.create(
        start_time=datetime(2026, 9, 4, 9, 0),
        end_time=datetime(2026, 9, 4, 10, 30),
    )

    assert record.duration == timedelta(hours=1, minutes=30)


def test_duration_of_a_running_record_counts_up_to_now():
    record = TimeRecord.objects.create(start_time=timezone.now() - timedelta(minutes=5))

    assert record.duration >= timedelta(minutes=5)


def test_budget_is_none_without_rfp_euros(settings):
    settings.RFP_EUROS = None
    record = TimeRecord.objects.create(
        start_time=datetime(2026, 9, 4, 9, 0),
        end_time=datetime(2026, 9, 4, 10, 30),
    )

    assert record.budget is None


def test_budget_is_computed_from_rfp_euros(settings):
    settings.RFP_EUROS = 20.0
    record = TimeRecord.objects.create(
        start_time=datetime(2026, 9, 4, 9, 0),
        end_time=datetime(2026, 9, 4, 10, 30),
    )

    assert record.budget == 30.0


def test_link_belongs_to_a_task():
    task = Task.objects.create(name="10a")
    link = Link.objects.create(task=task, url="https://example.com/pr/2")

    assert link.task == task


def test_time_record_belongs_to_a_link():
    link = Link.objects.create(url="https://example.com/pr/2")
    record = TimeRecord.objects.create(link=link, start_time=datetime(2026, 9, 4, 9, 0))

    assert record.link == link


def test_link_to_an_issue_url():
    link = Link.objects.create(url="https://github.com/nlnet/rfp-recorder/issues/42")

    assert link.issue == Issue(owner="nlnet", repo="rfp-recorder", number=42)
    assert link.pr is None
    assert link.is_issue is True
    assert link.is_pr is False


def test_link_to_a_pull_request_url():
    link = Link.objects.create(url="https://github.com/nlnet/rfp-recorder/pull/7")

    assert link.pr == PullRequest(owner="nlnet", repo="rfp-recorder", number=7)
    assert link.issue is None
    assert link.is_pr is True
    assert link.is_issue is False


def test_link_to_an_unrelated_url():
    link = Link.objects.create(url="https://example.com/pr/2")

    assert link.issue is None
    assert link.pr is None
    assert link.is_issue is False
    assert link.is_pr is False


def test_link_to_a_discussion_url():
    link = Link.objects.create(
        url="https://github.com/nlnet/rfp-recorder/discussions/3"
    )

    assert link.discussion == Discussion(owner="nlnet", repo="rfp-recorder", number=3)
    assert link.issue is None
    assert link.pr is None
    assert link.is_discussion is True
    assert link.is_issue is False
    assert link.is_pr is False


def test_link_sort_key_orders_discussions_by_number():
    low = Link.objects.create(url="https://github.com/nlnet/rfp-recorder/discussions/2")
    high = Link.objects.create(
        url="https://github.com/nlnet/rfp-recorder/discussions/10"
    )

    assert low < high


def test_task_defaults_to_selected():
    task = Task.objects.create(name="10a")

    assert task.selected is True


def test_task_can_be_deselected():
    task = Task.objects.create(name="10a", selected=False)

    assert task.selected is False


def test_task_name_accepts_number_letters_format():
    Task(name="10a").full_clean()


def test_task_name_rejects_other_formats():
    with pytest.raises(ValidationError):
        Task(name="Write the RfP").full_clean()


def test_select_leaves_only_one_task_selected():
    MoU.select("nlnet-2026")
    Task.objects.create(name="10a", selected=True)
    Task.objects.create(name="11b", selected=True)

    Task.select("12c")

    selected = Task.objects.filter(selected=True)
    assert [task.name for task in selected] == ["12c"]


def test_get_selected_returns_none_when_nothing_is_selected():
    assert Task.get_selected() is None


def test_get_selected_returns_the_selected_task():
    Task.objects.create(name="10a", selected=False)
    selected = Task.objects.create(name="11b", selected=True)

    assert Task.get_selected() == selected


def test_task_duration_sums_its_links_time_records():
    task = Task.objects.create(name="10a")
    link_a = Link.objects.create(task=task, url="https://example.com/issues/1")
    link_b = Link.objects.create(task=task, url="https://example.com/issues/2")
    TimeRecord.objects.create(
        link=link_a,
        start_time=datetime(2026, 9, 4, 9, 0),
        end_time=datetime(2026, 9, 4, 10, 0),
    )
    TimeRecord.objects.create(
        link=link_b,
        start_time=datetime(2026, 9, 4, 11, 0),
        end_time=datetime(2026, 9, 4, 11, 30),
    )

    assert task.duration == timedelta(hours=1, minutes=30)


def test_task_duration_is_zero_without_time_records():
    task = Task.objects.create(name="10a")

    assert task.duration == timedelta()


def test_task_duration_counts_a_running_record_live():
    task = Task.objects.create(name="10a")
    link = Link.objects.create(task=task, url="https://example.com/issues/1")
    TimeRecord.objects.create(
        link=link, start_time=timezone.now() - timedelta(minutes=5)
    )

    assert task.duration >= timedelta(minutes=5)


def test_task_budget_is_none_without_rfp_euros(settings):
    settings.RFP_EUROS = None
    task = Task.objects.create(name="10a")
    link = Link.objects.create(task=task, url="https://example.com/issues/1")
    TimeRecord.objects.create(
        link=link,
        start_time=datetime(2026, 9, 4, 9, 0),
        end_time=datetime(2026, 9, 4, 10, 0),
    )

    assert task.budget is None


def test_task_budget_is_computed_from_rfp_euros(settings):
    settings.RFP_EUROS = 20.0
    task = Task.objects.create(name="10a")
    link = Link.objects.create(task=task, url="https://example.com/issues/1")
    TimeRecord.objects.create(
        link=link,
        start_time=datetime(2026, 9, 4, 9, 0),
        end_time=datetime(2026, 9, 4, 10, 0),
    )

    assert task.budget == 20.0


def test_task_budget_includes_reported_lines_plus_live_unreported_time(settings):
    # Once time is part of a report, its contribution comes from the
    # report line's locked-in budget - separately from whatever is still
    # unreported, which is still computed live from time records.
    settings.RFP_EUROS = 20.0
    mou = MoU.objects.create(name="nlnet-2026")
    task = Task.objects.create(mou=mou, name="10a")
    reported_link = Link.objects.create(task=task, url="https://example.com/issues/1")
    unreported_link = Link.objects.create(task=task, url="https://example.com/issues/2")
    now = timezone.now()
    reported_record = TimeRecord.objects.create(
        link=reported_link, start_time=now - timedelta(hours=1), end_time=now
    )
    TimeRecord.objects.create(
        link=unreported_link, start_time=now - timedelta(minutes=30), end_time=now
    )
    report = Report.create(mou)
    report.add_time_record(reported_record)

    assert task.budget == 30.0  # 20€ reported (1h) + 10€ live unreported (30min)


def test_task_budget_does_not_double_count_more_time_on_a_reported_link(settings):
    # More time tracked against an already-reported link must not silently
    # inflate the task's budget beyond what the report line locked in.
    settings.RFP_EUROS = 20.0
    mou = MoU.objects.create(name="nlnet-2026")
    task = Task.objects.create(mou=mou, name="10a")
    link = Link.objects.create(task=task, url="https://example.com/issues/1")
    now = timezone.now()
    first_record = TimeRecord.objects.create(
        link=link, start_time=now - timedelta(hours=1), end_time=now
    )
    report = Report.create(mou)
    report.add_time_record(first_record)
    # tracked after the report was created - not part of it
    TimeRecord.objects.create(
        link=link, start_time=now, end_time=now + timedelta(minutes=30)
    )

    assert task.budget == 30.0  # 20€ reported + 10€ live unreported


def test_task_reported_budget_reflects_a_manual_override(settings):
    settings.RFP_EUROS = 20.0
    mou = MoU.objects.create(name="nlnet-2026")
    task = Task.objects.create(mou=mou, name="10a")
    link = Link.objects.create(task=task, url="https://example.com/issues/1")
    now = timezone.now()
    record = TimeRecord.objects.create(
        link=link, start_time=now - timedelta(hours=1), end_time=now
    )
    report = Report.create(mou)
    report.add_time_record(record)
    line = ReportLine.objects.get(report=report, link=link)
    line.budget = 999.0
    line.save(update_fields=["budget"])

    assert task.reported_budget == 999.0
    assert task.budget == 999.0


def test_task_other_lists_links_that_are_not_issues_or_pull_requests():
    task = Task.objects.create(name="10a")
    other_link = Link.objects.create(task=task, url="https://example.com/docs/design")
    TimeRecord.objects.create(
        link=other_link,
        start_time=datetime(2026, 9, 4, 9, 0),
        end_time=datetime(2026, 9, 4, 10, 0),
    )
    issue_link = Link.objects.create(
        task=task, url="https://github.com/nlnet/rfp-recorder/issues/1"
    )
    TimeRecord.objects.create(
        link=issue_link,
        start_time=datetime(2026, 9, 4, 9, 0),
        end_time=datetime(2026, 9, 4, 10, 0),
    )

    # No mocking needed: issues never trigger a GitHub call now.
    assert task.other == ["https://example.com/docs/design"]


def test_task_discussions_lists_tracked_discussion_links():
    task = Task.objects.create(name="10a")
    discussion_link = Link.objects.create(
        task=task, url="https://github.com/nlnet/rfp-recorder/discussions/3"
    )
    TimeRecord.objects.create(
        link=discussion_link,
        start_time=datetime(2026, 9, 4, 9, 0),
        end_time=datetime(2026, 9, 4, 10, 0),
    )

    assert task.discussions == [
        Discussion(owner="nlnet", repo="rfp-recorder", number=3)
    ]
    assert task.other == []


def test_task_other_includes_links_with_a_running_record():
    # A running record's elapsed time counts live (see TimeRecord.duration),
    # so its link is included too - unlike a link with no tracked time at
    # all, which never shows up.
    task = Task.objects.create(name="10a")
    finished_link = Link.objects.create(
        task=task, url="https://example.com/docs/design"
    )
    TimeRecord.objects.create(
        link=finished_link,
        start_time=datetime(2026, 9, 4, 9, 0),
        end_time=datetime(2026, 9, 4, 10, 0),
    )
    running_link = Link.objects.create(task=task, url="https://example.com/docs/other")
    TimeRecord.objects.create(link=running_link, start_time=datetime(2026, 9, 4, 11, 0))
    Link.objects.create(task=task, url="https://example.com/docs/untouched")

    assert set(task.other) == {
        "https://example.com/docs/design",
        "https://example.com/docs/other",
    }


def test_link_get_or_create_for_task_creates_a_new_link():
    task = Task.objects.create(name="10a")

    link = Link.get_or_create_for_task("https://example.com/issues/1", task)

    assert link.url == "https://example.com/issues/1"
    assert link.task == task


def test_link_get_or_create_for_task_adopts_an_orphaned_link():
    task = Task.objects.create(name="10a")
    link = Link.objects.create(url="https://example.com/issues/1")

    adopted = Link.get_or_create_for_task("https://example.com/issues/1", task)

    assert adopted.pk == link.pk
    assert adopted.task == task


def test_link_get_or_create_for_task_warns_without_reassigning():
    task_a = Task.objects.create(name="10a")
    task_b = Task.objects.create(name="11b")
    link = Link.objects.create(task=task_a, url="https://example.com/issues/1")

    with pytest.warns(UserWarning):
        result = Link.get_or_create_for_task("https://example.com/issues/1", task_b)

    assert result.pk == link.pk
    assert result.task == task_a


def test_link_get_or_create_for_task_is_silent_for_the_same_task():
    task = Task.objects.create(name="10a")
    Link.objects.create(task=task, url="https://example.com/issues/1")

    with warnings.catch_warnings():
        warnings.simplefilter("error")
        result = Link.get_or_create_for_task("https://example.com/issues/1", task)

    assert result.task == task


def test_start_without_a_task_and_none_selected_raises():
    with pytest.raises(ValueError):
        TimeRecord.start("https://example.com/issues/1")


def test_start_uses_the_selected_task_by_default():
    selected = Task.objects.create(name="10a")

    record = TimeRecord.start("https://example.com/issues/1")

    assert record.link.task == selected
    assert record.link.url == "https://example.com/issues/1"
    assert record.is_running is True


def test_start_accepts_an_explicit_task():
    Task.objects.create(name="10a", selected=True)
    other = Task.objects.create(name="11b", selected=False)

    record = TimeRecord.start("https://example.com/issues/1", task=other)

    assert record.link.task == other


def test_start_reuses_the_same_link_across_sessions():
    Task.objects.create(name="10a")
    first = TimeRecord.start("https://example.com/issues/1")
    TimeRecord.stop()

    second = TimeRecord.start("https://example.com/issues/1")

    assert first.link_id == second.link_id


def test_start_reopens_the_last_stopped_entry_for_the_same_link():
    # Stop, then start the exact same link again: this resumes the same
    # row (one continuous session) instead of fragmenting into a second
    # one - the whole point of resuming, rather than starting fresh.
    Task.objects.create(name="10a")
    first = TimeRecord.start("https://example.com/issues/1")
    TimeRecord.stop()

    second = TimeRecord.start("https://example.com/issues/1")

    assert second.pk == first.pk
    assert second.is_running is True
    assert TimeRecord.objects.count() == 1


def test_start_does_not_reopen_the_last_stopped_entry_for_a_different_link():
    Task.objects.create(name="10a")
    TimeRecord.start("https://example.com/issues/1")
    TimeRecord.stop()

    second = TimeRecord.start("https://example.com/issues/2")

    assert TimeRecord.objects.count() == 2
    assert second.link.url == "https://example.com/issues/2"


def test_start_is_a_noop_when_already_running_the_same_link():
    Task.objects.create(name="10a")
    first = TimeRecord.start("https://example.com/issues/1")

    second = TimeRecord.start("https://example.com/issues/1")

    assert second.pk == first.pk
    assert TimeRecord.objects.count() == 1


def test_start_reopening_keeps_the_original_start_time():
    Task.objects.create(name="10a")
    first = TimeRecord.start(
        "https://example.com/issues/1", start_time=datetime(2026, 9, 4, 9, 0)
    )
    TimeRecord.stop(end_time=datetime(2026, 9, 4, 9, 30))

    second = TimeRecord.start(
        "https://example.com/issues/1", start_time=datetime(2026, 9, 4, 10, 0)
    )

    assert second.pk == first.pk
    assert second.start_time == datetime(2026, 9, 4, 9, 0)


def test_start_with_an_explicit_start_time_stops_the_previous_entry_at_it_too():
    Task.objects.create(name="10a")
    TimeRecord.start("https://example.com/issues/1")

    now = datetime(2026, 9, 4, 12, 0)
    TimeRecord.start("https://example.com/issues/2", start_time=now)

    first = TimeRecord.objects.get(link__url="https://example.com/issues/1")
    assert first.end_time == now


def test_start_warns_when_link_belongs_to_another_task():
    MoU.select("nlnet-2026")
    Task.objects.create(name="10a")
    TimeRecord.start("https://example.com/issues/1")
    TimeRecord.stop()

    Task.select("11b")
    with pytest.warns(UserWarning):
        record = TimeRecord.start("https://example.com/issues/1")

    assert record.link.task.name == "10a"


def test_start_stops_a_previously_running_record():
    Task.objects.create(name="10a")
    first = TimeRecord.start("https://example.com/issues/1")

    second = TimeRecord.start("https://example.com/issues/2")

    first.refresh_from_db()
    assert first.is_running is False
    assert second.is_running is True


def test_get_running_returns_none_when_nothing_is_running():
    assert TimeRecord.get_running() is None


def test_get_running_returns_the_most_recently_started_record():
    TimeRecord.objects.create(start_time=datetime(2026, 9, 4, 8, 0))
    later = TimeRecord.objects.create(start_time=datetime(2026, 9, 4, 9, 0))

    assert TimeRecord.get_running() == later


def test_get_last_returns_none_when_no_records_exist():
    assert TimeRecord.get_last() is None


def test_get_last_returns_the_most_recently_started_record_even_if_stopped():
    TimeRecord.objects.create(
        start_time=datetime(2026, 9, 4, 8, 0), end_time=datetime(2026, 9, 4, 9, 0)
    )
    later = TimeRecord.objects.create(start_time=datetime(2026, 9, 4, 10, 0))

    assert TimeRecord.get_last() == later


def test_stop_without_a_running_record_returns_none():
    assert TimeRecord.stop() is None


def test_stop_ends_the_running_record():
    Task.objects.create(name="10a")
    started = TimeRecord.start("https://example.com/issues/1")

    stopped = TimeRecord.stop()

    assert stopped.pk == started.pk
    assert stopped.is_running is False
    assert stopped.end_time is not None


def test_stop_with_a_url_replaces_the_running_records_link():
    task = Task.objects.create(name="10a")
    started = TimeRecord.start("https://example.com/issues/wrong")

    stopped = TimeRecord.stop("https://example.com/issues/right")

    assert stopped.pk == started.pk
    assert stopped.link.url == "https://example.com/issues/right"
    assert stopped.link.task == task


def test_stop_twice_is_a_no_op_the_second_time():
    Task.objects.create(name="10a")
    TimeRecord.start("https://example.com/issues/1")

    TimeRecord.stop()

    assert TimeRecord.stop() is None


def test_stop_ends_the_most_recently_started_record():
    Task.objects.create(name="10a")
    TimeRecord.objects.create(start_time=datetime(2026, 9, 4, 8, 0))
    later = TimeRecord.objects.create(start_time=datetime(2026, 9, 4, 9, 0))

    stopped = TimeRecord.stop()

    assert stopped.pk == later.pk


def test_mou_select_creates_and_selects():
    mou = MoU.select("nlnet-2026")

    assert mou.name == "nlnet-2026"
    assert mou.selected is True


def test_mou_select_rejects_a_name_with_spaces():
    with pytest.raises(ValidationError):
        MoU.select("nlnet 2026")


def test_mou_select_leaves_only_one_mou_selected():
    MoU.objects.create(name="nlnet-2025", selected=True)

    MoU.select("nlnet-2026")

    selected = MoU.objects.filter(selected=True)
    assert [mou.name for mou in selected] == ["nlnet-2026"]


def test_mou_select_reselecting_an_existing_mou_does_not_raise():
    MoU.select("nlnet-2026")

    mou = MoU.select("nlnet-2026")

    assert mou.name == "nlnet-2026"
    assert MoU.objects.count() == 1


def test_mou_get_selected_returns_none_when_nothing_is_selected():
    assert MoU.get_selected() is None


def test_mou_get_selected_returns_the_selected_mou():
    MoU.objects.create(name="nlnet-2025", selected=False)
    selected = MoU.objects.create(name="nlnet-2026", selected=True)

    assert MoU.get_selected() == selected


def test_mou_select_existing_raises_for_an_unknown_mou():
    with pytest.raises(ValueError):
        MoU.select_existing("does-not-exist")


def test_mou_select_existing_selects_without_creating():
    mou = MoU.objects.create(name="nlnet-2025", selected=False)
    MoU.objects.create(name="nlnet-2026", selected=True)

    selected = MoU.select_existing("nlnet-2025")

    assert selected == mou
    assert selected.selected is True
    assert MoU.objects.count() == 2
    assert MoU.get_selected() == mou


def test_mou_budget_defaults_to_empty():
    mou = MoU.objects.create(name="nlnet-2026")

    assert mou.budget == ""


def test_mou_set_budget_creates_tasks_with_max_budget():
    mou = MoU.objects.create(name="nlnet-2026")

    tasks = mou.set_budget("10a. Do the thing\t€ 500\n10b. Do another thing\t€ 250\n")

    assert set(tasks) == {"10a", "10b"}
    assert Task.objects.get(name="10a").max_budget == 500.0
    assert Task.objects.get(name="10b").max_budget == 250.0
    assert Task.objects.get(name="10a").mou == mou


def test_mou_set_budget_marks_done_milestones_as_fully_used():
    mou = MoU.objects.create(name="nlnet-2026")

    mou.set_budget("(DONE) 10a. Do the thing\t€ 500\n10b. Not done yet\t€ 250\n")

    assert Task.objects.get(name="10a").used_budget == 500.0
    assert Task.objects.get(name="10b").used_budget == 0.0


def test_mou_set_budget_stores_the_raw_text():
    mou = MoU.objects.create(name="nlnet-2026")
    text = "10a. Do the thing\t€ 500\n"

    mou.set_budget(text)

    mou.refresh_from_db()
    assert mou.budget == text


def test_mou_set_budget_stores_text_longer_than_a_short_charfield():
    # budget used to be a CharField(max_length=255) - too short for a
    # real milestone document; must not be truncated.
    mou = MoU.objects.create(name="nlnet-2026")
    lines = [f"{10 + i}a. Milestone {i}\t€ 100\n" for i in range(20)]
    text = "".join(lines)
    assert len(text) > 255

    mou.set_budget(text)

    mou.refresh_from_db()
    assert mou.budget == text


def test_mou_set_budget_saves_each_tasks_description():
    mou = MoU.objects.create(name="nlnet-2026")

    mou.set_budget("10a. Do the thing - see the details\t€ 500\n")

    assert Task.objects.get(name="10a").description == "Do the thing - see the details"


def test_mou_set_budget_saves_descriptions_from_a_takentaal_document():
    mou = MoU.objects.create(name="nlnet-2026")
    text = "takentaal v1.0\n\n## 1. A task\n\n* {500} Do the thing\n"

    mou.set_budget(text)

    assert Task.objects.get(name="1a").description == "Do the thing"


def test_mou_set_budget_updates_an_existing_tasks_max_budget():
    mou = MoU.objects.create(name="nlnet-2026")
    task = Task.objects.create(name="10a", mou=mou)

    mou.set_budget("10a. Do the thing\t€ 500\n")

    task.refresh_from_db()
    assert task.max_budget == 500.0


def test_mou_set_budget_creates_a_separate_task_for_a_different_mou():
    # Task identity is (mou, name): the same milestone code under a
    # different MoU is a distinct task, not a conflict.
    mou_a = MoU.objects.create(name="nlnet-2025")
    task_a = Task.objects.create(name="10a", mou=mou_a)
    mou_b = MoU.objects.create(name="nlnet-2026", selected=False)

    mou_b.set_budget("10a. Do the thing\t€ 500\n")

    task_a.refresh_from_db()
    assert task_a.max_budget is None
    task_b = Task.objects.get(mou=mou_b, name="10a")
    assert task_b.max_budget == 500.0
    assert Task.objects.filter(name="10a").count() == 2


def test_task_select_associates_with_the_selected_mou():
    mou = MoU.select("nlnet-2026")

    with pytest.warns(TaskWarning):
        task = Task.select("10a")

    assert task.mou == mou


def test_task_select_warns_when_creating_a_new_task():
    MoU.select("nlnet-2026")

    with pytest.warns(TaskWarning, match="10a is new"):
        Task.select("10a")


def test_task_select_does_not_warn_when_reselecting_an_existing_task():
    MoU.select("nlnet-2026")
    with pytest.warns(TaskWarning):
        Task.select("10a")

    with warnings.catch_warnings():
        warnings.simplefilter("error")
        Task.select("10a")


def test_task_select_raises_without_any_mou_selected():
    with pytest.raises(ValueError):
        Task.select("10a")


def test_task_select_does_not_adopt_an_orphaned_task_of_the_same_name():
    # An orphaned task (no mou) has a different identity than (mou, "10a"),
    # so selecting "10a" under a real MoU creates a new task rather than
    # adopting the orphan.
    orphan = Task.objects.create(name="10a")
    mou = MoU.select("nlnet-2026")

    selected = Task.select("10a")
    orphan.refresh_from_db()

    assert orphan.mou is None
    assert selected.pk != orphan.pk
    assert selected.mou == mou


def test_task_select_creates_a_separate_task_for_a_different_mou():
    mou_a = MoU.objects.create(name="nlnet-2025", selected=False)
    task_a = Task.select("10a", mou=mou_a)
    mou_b = MoU.select("nlnet-2026")

    task_b = Task.select("10a")

    assert task_b.pk != task_a.pk
    assert task_b.mou == mou_b
    task_a.refresh_from_db()
    assert task_a.mou == mou_a
    assert Task.objects.filter(name="10a").count() == 2


def test_task_unique_constraint_allows_same_name_under_different_mous():
    mou_a = MoU.objects.create(name="nlnet-2025")
    mou_b = MoU.objects.create(name="nlnet-2026", selected=False)

    Task.objects.create(mou=mou_a, name="10a")
    Task.objects.create(mou=mou_b, name="10a")

    assert Task.objects.filter(name="10a").count() == 2


def test_task_unique_constraint_rejects_the_same_name_under_the_same_mou():
    from django.db import IntegrityError

    mou = MoU.objects.create(name="nlnet-2026")
    Task.objects.create(mou=mou, name="10a")

    with pytest.raises(IntegrityError):
        Task.objects.create(mou=mou, name="10a")


def test_link_add_tag_rejects_unknown_tags():
    link = Link.objects.create(url="https://example.com/issues/1")

    with pytest.raises(ValueError):
        link.add_tag("not-a-real-tag")


def test_link_add_tag_adds_a_known_tag():
    link = Link.objects.create(url="https://example.com/issues/1")

    link.add_tag("review")

    assert [tag.name for tag in link.tags.all()] == ["review"]


def test_link_add_tag_is_idempotent():
    link = Link.objects.create(url="https://example.com/issues/1")

    link.add_tag("review")
    link.add_tag("review")

    assert [tag.name for tag in link.tags.all()] == ["review"]


def test_start_tags_the_link_with_implementation_by_default():
    Task.objects.create(name="10a")

    record = TimeRecord.start("https://example.com/issues/1")

    assert [tag.name for tag in record.link.tags.all()] == ["implementation"]


def test_start_accepts_explicit_tags():
    Task.objects.create(name="10a")

    record = TimeRecord.start("https://example.com/issues/1", tags=["review"])

    assert [tag.name for tag in record.link.tags.all()] == ["review"]


def test_github_token_get_returns_none_when_unset():
    assert GitHubToken.get() is None


def test_github_token_set_and_get():
    GitHubToken.set("secret")

    assert GitHubToken.get() == "secret"


def test_github_token_set_replaces_the_previous_token():
    GitHubToken.set("first")
    GitHubToken.set("second")

    assert GitHubToken.get() == "second"
    assert GitHubToken.objects.count() == 1


def test_billable_links_sends_the_saved_token():
    # Only PRs trigger a GitHub status check (issues are always billable
    # once finished), so use a PR link here to exercise the token header.
    GitHubToken.set("secret")
    task = Task.objects.create(name="10a")
    link = Link.objects.create(
        task=task, url="https://github.com/nlnet/rfp-recorder/pull/1"
    )
    TimeRecord.objects.create(
        link=link,
        start_time=datetime(2026, 9, 4, 9, 0),
        end_time=datetime(2026, 9, 4, 10, 0),
    )

    with _mock_github_session("closed") as mock_session_cls:
        billable = task.billable_links

    assert billable == [link]
    session = mock_session_cls.return_value
    session.get.assert_called_once_with(
        "https://api.github.com/repos/nlnet/rfp-recorder/pulls/1",
        headers={"Authorization": "Bearer secret"},
        timeout=TIMEOUT_SECONDS,
    )


def test_excluded_pull_request_links_holds_back_open_prs():
    task = Task.objects.create(name="10a")
    open_link = Link.objects.create(
        task=task, url="https://github.com/nlnet/rfp-recorder/pull/1"
    )
    TimeRecord.objects.create(
        link=open_link,
        start_time=datetime(2026, 9, 4, 9, 0),
        end_time=datetime(2026, 9, 4, 10, 0),
    )

    with _mock_github_session("open"):
        billable = task.billable_links
        excluded = task.excluded_pull_request_links

    assert billable == []
    assert excluded == [open_link]


def test_excluded_pull_request_links_is_empty_when_all_prs_are_closed():
    task = Task.objects.create(name="10a")
    closed_link = Link.objects.create(
        task=task, url="https://github.com/nlnet/rfp-recorder/pull/1"
    )
    TimeRecord.objects.create(
        link=closed_link,
        start_time=datetime(2026, 9, 4, 9, 0),
        end_time=datetime(2026, 9, 4, 10, 0),
    )

    with _mock_github_session("closed"):
        excluded = task.excluded_pull_request_links

    assert excluded == []


def test_billable_links_and_excluded_pull_request_links_share_one_status_check():
    # Both properties need each PR's open/closed status; they must share a
    # single GitHub round trip rather than fetching it twice.
    task = Task.objects.create(name="10a")
    link = Link.objects.create(
        task=task, url="https://github.com/nlnet/rfp-recorder/pull/1"
    )
    TimeRecord.objects.create(
        link=link,
        start_time=datetime(2026, 9, 4, 9, 0),
        end_time=datetime(2026, 9, 4, 10, 0),
    )

    with _mock_github_session("open") as mock_session_cls:
        task.billable_links
        task.excluded_pull_request_links

    session = mock_session_cls.return_value
    assert session.get.call_count == 1


def test_row_reflects_a_finished_record():
    mou = MoU.objects.create(name="nlnet-2026")
    task = Task.objects.create(mou=mou, name="10a")
    link = Link.objects.create(task=task, url="https://example.com/issues/1")
    link.add_tag("review")
    record = TimeRecord.objects.create(
        link=link,
        start_time=datetime(2026, 9, 4, 9, 0),
        end_time=datetime(2026, 9, 4, 10, 30),
    )

    row = record.row

    assert row == TimesheetRow(
        pk=record.pk,
        mou="nlnet-2026",
        task="10a",
        start=datetime(2026, 9, 4, 9, 0).isoformat(),
        duration="01:30:00",
        link="https://example.com/issues/1",
        tags="review",
    )


def test_row_without_a_link_leaves_fields_blank():
    record = TimeRecord.objects.create(
        start_time=datetime(2026, 9, 4, 9, 0), end_time=datetime(2026, 9, 4, 10, 0)
    )

    row = record.row

    assert row.mou == ""
    assert row.task == ""
    assert row.link == ""
    assert row.tags == ""


def test_apply_row_creates_a_new_record_without_a_pk():
    mou = MoU.objects.create(name="nlnet-2026")
    Task.objects.create(mou=mou, name="10a")
    row = TimesheetRow(
        pk=None,
        mou="nlnet-2026",
        task="10a",
        start=datetime(2026, 9, 4, 9, 0).isoformat(),
        duration="01:00:00",
        link="https://example.com/issues/1",
        tags="implementation",
    )

    record = TimeRecord.apply_row(row)

    assert record.pk is not None
    assert record.start_time == datetime(2026, 9, 4, 9, 0)
    assert record.end_time == datetime(2026, 9, 4, 10, 0)
    assert record.link.url == "https://example.com/issues/1"
    assert [tag.name for tag in record.link.tags.all()] == ["implementation"]


def test_apply_row_with_an_unknown_pk_raises_by_default():
    mou = MoU.objects.create(name="nlnet-2026")
    Task.objects.create(mou=mou, name="10a")
    row = TimesheetRow(
        pk=1000,
        mou="nlnet-2026",
        task="10a",
        start=datetime(2026, 9, 4, 9, 0).isoformat(),
        duration="01:00:00",
        link="https://example.com/issues/1",
        tags="",
    )

    with pytest.raises(ValueError, match="No such time record"):
        TimeRecord.apply_row(row)


def test_apply_row_with_allow_create_with_pk_creates_a_record_with_that_pk():
    mou = MoU.objects.create(name="nlnet-2026")
    Task.objects.create(mou=mou, name="10a")
    row = TimesheetRow(
        pk=1000,
        mou="nlnet-2026",
        task="10a",
        start=datetime(2026, 9, 4, 9, 0).isoformat(),
        duration="01:00:00",
        link="https://example.com/issues/1",
        tags="",
    )

    record = TimeRecord.apply_row(row, allow_create_with_pk=True)

    assert record.pk == 1000
    reloaded = TimeRecord.objects.get(pk=1000)
    assert reloaded.link.url == "https://example.com/issues/1"


def test_apply_row_with_allow_create_with_pk_still_updates_an_existing_pk():
    mou = MoU.objects.create(name="nlnet-2026")
    task = Task.objects.create(mou=mou, name="10a")
    link = Link.objects.create(task=task, url="https://example.com/issues/1")
    record = TimeRecord.objects.create(
        link=link,
        start_time=datetime(2026, 9, 4, 9, 0),
        end_time=datetime(2026, 9, 4, 10, 0),
    )
    row = TimesheetRow(
        pk=record.pk,
        mou="nlnet-2026",
        task="10a",
        start=datetime(2026, 9, 4, 9, 0).isoformat(),
        duration="02:00:00",
        link="https://example.com/issues/2",
        tags="",
    )

    updated = TimeRecord.apply_row(row, allow_create_with_pk=True)

    assert updated.pk == record.pk
    assert TimeRecord.objects.count() == 1
    assert updated.link.url == "https://example.com/issues/2"


def test_apply_row_updates_an_existing_record_by_pk():
    mou = MoU.objects.create(name="nlnet-2026")
    task = Task.objects.create(mou=mou, name="10a")
    link = Link.objects.create(task=task, url="https://example.com/issues/1")
    record = TimeRecord.objects.create(
        link=link,
        start_time=datetime(2026, 9, 4, 9, 0),
        end_time=datetime(2026, 9, 4, 10, 0),
    )
    row = TimesheetRow(
        pk=record.pk,
        mou="nlnet-2026",
        task="10a",
        start=datetime(2026, 9, 4, 9, 0).isoformat(),
        duration="02:00:00",
        link="https://example.com/issues/2",
        tags="",
    )

    updated = TimeRecord.apply_row(row)

    assert updated.pk == record.pk
    assert updated.end_time == datetime(2026, 9, 4, 11, 0)
    assert updated.link.url == "https://example.com/issues/2"


def test_apply_row_raises_for_an_unknown_pk():
    mou = MoU.objects.create(name="nlnet-2026")
    Task.objects.create(mou=mou, name="10a")
    row = TimesheetRow(
        pk=999,
        mou="nlnet-2026",
        task="10a",
        start=datetime(2026, 9, 4, 9, 0).isoformat(),
        duration="01:00:00",
        link="https://example.com/issues/1",
        tags="",
    )

    with pytest.raises(ValueError, match="No such time record"):
        TimeRecord.apply_row(row)


def test_apply_row_raises_for_an_unknown_mou():
    row = TimesheetRow(
        pk=None,
        mou="does-not-exist",
        task="10a",
        start=datetime(2026, 9, 4, 9, 0).isoformat(),
        duration="01:00:00",
        link="https://example.com/issues/1",
        tags="",
    )

    with pytest.raises(ValueError, match="No such MoU"):
        TimeRecord.apply_row(row)


def test_apply_row_changing_mou_looks_up_that_mous_task_without_mutating_tasks():
    # Changing `row.mou` on an existing record must not repoint either
    # task's own `mou` FK - it must look up the (new-mou, same-name-task)
    # pair and move the record's link there, leaving both tasks untouched.
    mou_a = MoU.objects.create(name="mou-a")
    mou_b = MoU.objects.create(name="mou-b")
    task_a = Task.objects.create(mou=mou_a, name="10a")
    task_b = Task.objects.create(mou=mou_b, name="10a")
    link = Link.objects.create(task=task_a, url="https://example.com/issues/1")
    record = TimeRecord.objects.create(
        link=link,
        start_time=datetime(2026, 9, 4, 9, 0),
        end_time=datetime(2026, 9, 4, 10, 0),
    )
    row = TimesheetRow(
        pk=record.pk,
        mou="mou-b",
        task="10a",
        start=datetime(2026, 9, 4, 9, 0).isoformat(),
        duration="01:00:00",
        link="https://example.com/issues/2",
        tags="",
    )

    updated = TimeRecord.apply_row(row)

    assert updated.link.task == task_b
    assert updated.link.url == "https://example.com/issues/2"
    task_a.refresh_from_db()
    task_b.refresh_from_db()
    assert task_a.mou == mou_a
    assert task_b.mou == mou_b
    # The original link (and task_a's association) is untouched.
    link.refresh_from_db()
    assert link.task == task_a


def test_apply_row_raises_for_an_unknown_task():
    MoU.objects.create(name="nlnet-2026")
    row = TimesheetRow(
        pk=None,
        mou="nlnet-2026",
        task="10a",
        start=datetime(2026, 9, 4, 9, 0).isoformat(),
        duration="01:00:00",
        link="https://example.com/issues/1",
        tags="",
    )

    with pytest.raises(ValueError, match="No such task"):
        TimeRecord.apply_row(row)


def test_report_create_starts_numbering_at_1():
    mou = MoU.objects.create(name="nlnet-2026")

    report = Report.create(mou)

    assert report.id == "nlnet-2026-1"
    assert report.mou == mou


def test_report_create_increments_per_mou():
    mou = MoU.objects.create(name="nlnet-2026")
    Report.create(mou)

    second = Report.create(mou)

    assert second.id == "nlnet-2026-2"


def test_report_create_numbers_are_independent_per_mou():
    mou_a = MoU.objects.create(name="mou-a")
    mou_b = MoU.objects.create(name="mou-b")
    Report.create(mou_a)

    first_for_b = Report.create(mou_b)

    assert first_for_b.id == "mou-b-1"


def test_report_create_continues_numbering_after_the_mou_is_removed():
    mou = MoU.objects.create(name="nlnet-2026")
    Report.create(mou)
    mou.delete()

    # A newly (re)created MoU with the same name must not reuse report ids
    # of the removed one's history.
    new_mou = MoU.objects.create(name="nlnet-2026")
    report = Report.create(new_mou)

    assert report.id == "nlnet-2026-2"


def test_report_add_time_record_attaches_a_record_of_the_same_mou():
    mou = MoU.objects.create(name="nlnet-2026")
    task = Task.objects.create(mou=mou, name="10a")
    link = Link.objects.create(task=task, url="https://example.com/issues/1")
    record = TimeRecord.objects.create(
        link=link,
        start_time=datetime(2026, 9, 4, 9, 0),
        end_time=datetime(2026, 9, 4, 10, 0),
    )
    report = Report.create(mou)

    report.add_time_record(record)

    record.refresh_from_db()
    assert record.report_line.report == report


def test_report_add_time_record_rejects_a_record_of_a_different_mou():
    mou_a = MoU.objects.create(name="mou-a")
    mou_b = MoU.objects.create(name="mou-b")
    task_b = Task.objects.create(mou=mou_b, name="10a")
    link = Link.objects.create(task=task_b, url="https://example.com/issues/1")
    record = TimeRecord.objects.create(
        link=link,
        start_time=datetime(2026, 9, 4, 9, 0),
        end_time=datetime(2026, 9, 4, 10, 0),
    )
    report = Report.create(mou_a)

    with pytest.raises(ValueError, match="does not belong to MoU"):
        report.add_time_record(record)

    record.refresh_from_db()
    assert record.report_line is None


def test_report_add_time_record_rejects_a_record_without_a_task():
    mou = MoU.objects.create(name="nlnet-2026")
    record = TimeRecord.objects.create(start_time=datetime(2026, 9, 4, 9, 0))
    report = Report.create(mou)

    with pytest.raises(ValueError, match="does not belong to MoU"):
        report.add_time_record(record)


def test_report_remove_time_record_detaches_it():
    mou = MoU.objects.create(name="nlnet-2026")
    task = Task.objects.create(mou=mou, name="10a")
    link = Link.objects.create(task=task, url="https://example.com/issues/1")
    record = TimeRecord.objects.create(
        link=link,
        start_time=datetime(2026, 9, 4, 9, 0),
        end_time=datetime(2026, 9, 4, 10, 0),
    )
    report = Report.create(mou)
    report.add_time_record(record)

    report.remove_time_record(record)

    record.refresh_from_db()
    assert record.report_line is None


def test_report_remove_time_record_rejects_a_record_from_another_report():
    mou = MoU.objects.create(name="nlnet-2026")
    task = Task.objects.create(mou=mou, name="10a")
    link = Link.objects.create(task=task, url="https://example.com/issues/1")
    record = TimeRecord.objects.create(
        link=link,
        start_time=datetime(2026, 9, 4, 9, 0),
        end_time=datetime(2026, 9, 4, 10, 0),
    )
    other_report = Report.create(mou)

    with pytest.raises(ValueError, match="is not part of report"):
        other_report.remove_time_record(record)


def test_report_add_time_record_groups_same_link_records_into_one_line(settings):
    settings.RFP_EUROS = 20.0
    mou = MoU.objects.create(name="nlnet-2026")
    task = Task.objects.create(mou=mou, name="10a")
    link = Link.objects.create(task=task, url="https://example.com/issues/1")
    now = timezone.now()
    record_a = TimeRecord.objects.create(
        link=link, start_time=now - timedelta(minutes=30), end_time=now
    )
    record_b = TimeRecord.objects.create(
        link=link, start_time=now - timedelta(minutes=30), end_time=now
    )
    report = Report.create(mou)

    report.add_time_record(record_a)
    report.add_time_record(record_b)

    assert ReportLine.objects.filter(report=report, link=link).count() == 1
    line = ReportLine.objects.get(report=report, link=link)
    assert line.budget == 20.0  # both 30min records summed
    assert set(line.time_records.all()) == {record_a, record_b}


def test_report_add_time_record_seeds_line_tags_from_the_link(settings):
    settings.RFP_EUROS = 20.0
    mou = MoU.objects.create(name="nlnet-2026")
    task = Task.objects.create(mou=mou, name="10a")
    link = Link.objects.create(task=task, url="https://example.com/issues/1")
    link.add_tag("review")
    record = TimeRecord.objects.create(
        link=link,
        start_time=datetime(2026, 9, 4, 9, 0),
        end_time=datetime(2026, 9, 4, 10, 0),
    )
    report = Report.create(mou)

    report.add_time_record(record)

    line = ReportLine.objects.get(report=report, link=link)
    assert line.tag_list == ["review"]


def test_report_line_tags_are_independent_of_the_links_tags(settings):
    settings.RFP_EUROS = 20.0
    mou = MoU.objects.create(name="nlnet-2026")
    task = Task.objects.create(mou=mou, name="10a")
    link = Link.objects.create(task=task, url="https://example.com/issues/1")
    link.add_tag("review")
    record = TimeRecord.objects.create(
        link=link,
        start_time=datetime(2026, 9, 4, 9, 0),
        end_time=datetime(2026, 9, 4, 10, 0),
    )
    report = Report.create(mou)
    report.add_time_record(record)
    line = ReportLine.objects.get(report=report, link=link)

    line.tags = "implementation"
    line.save(update_fields=["tags"])

    link.refresh_from_db()
    assert sorted(tag.name for tag in link.tags.all()) == ["review"]
    line.refresh_from_db()
    assert line.tag_list == ["implementation"]


def test_report_remove_time_record_deletes_an_empty_line(settings):
    settings.RFP_EUROS = 20.0
    mou = MoU.objects.create(name="nlnet-2026")
    task = Task.objects.create(mou=mou, name="10a")
    link = Link.objects.create(task=task, url="https://example.com/issues/1")
    record = TimeRecord.objects.create(
        link=link,
        start_time=datetime(2026, 9, 4, 9, 0),
        end_time=datetime(2026, 9, 4, 10, 0),
    )
    report = Report.create(mou)
    report.add_time_record(record)

    report.remove_time_record(record)

    assert ReportLine.objects.filter(report=report, link=link).exists() is False


def test_report_remove_orphans_time_records_when_the_report_is_deleted(settings):
    settings.RFP_EUROS = 20.0
    mou = MoU.objects.create(name="nlnet-2026")
    task = Task.objects.create(mou=mou, name="10a")
    link = Link.objects.create(task=task, url="https://example.com/issues/1")
    record = TimeRecord.objects.create(
        link=link,
        start_time=datetime(2026, 9, 4, 9, 0),
        end_time=datetime(2026, 9, 4, 10, 0),
    )
    report = Report.create(mou)
    report.add_time_record(record)

    report.delete()

    record.refresh_from_db()
    assert record.report_line is None


def test_report_review_tasks_matches_generate_report_per_task(settings):
    settings.RFP_EUROS = 20.0
    mou = MoU.objects.create(name="nlnet-2026")
    task_a = Task.objects.create(mou=mou, name="10a")
    task_b = Task.objects.create(mou=mou, name="10b")
    link_a = Link.objects.create(task=task_a, url="https://example.com/issues/1")
    link_b = Link.objects.create(task=task_b, url="https://example.com/issues/2")
    record_a = TimeRecord.objects.create(
        link=link_a,
        start_time=datetime(2026, 9, 4, 9, 0),
        end_time=datetime(2026, 9, 4, 10, 0),
    )
    record_b = TimeRecord.objects.create(
        link=link_b,
        start_time=datetime(2026, 9, 4, 9, 0),
        end_time=datetime(2026, 9, 4, 9, 6),
    )
    report = Report.create(mou)
    report.add_time_record(record_a)
    report.add_time_record(record_b)

    reviews = report.review_tasks()

    assert [task for task, _block, _total in reviews] == [task_a, task_b]
    a_block, a_total = next((b, t) for task, b, t in reviews if task == task_a)
    b_block, b_total = next((b, t) for task, b, t in reviews if task == task_b)
    assert a_total == 20
    assert "10a: 20€" in a_block
    assert "https://example.com/issues/1 - 20€" in a_block
    # 6 minutes @ 20€/h = 2€ - too small to show on the link itself, but
    # still pooled into the task total and rounded up to 10.
    assert b_total == 10
    assert "10b: 10€" in b_block
    # under 5, so no per-link figure - just the bare link.
    assert "https://example.com/issues/2\n" in b_block + "\n"
    generated = report.generate_report()
    assert a_block in generated
    assert b_block in generated


def test_report_review_tasks_empty_for_a_report_with_no_lines(settings):
    mou = MoU.objects.create(name="nlnet-2026")
    report = Report.create(mou)

    assert report.review_tasks() == []


def test_report_remove_task_lines_detaches_records_but_keeps_them(settings):
    settings.RFP_EUROS = 20.0
    mou = MoU.objects.create(name="nlnet-2026")
    task = Task.objects.create(mou=mou, name="10a")
    link = Link.objects.create(task=task, url="https://example.com/issues/1")
    record = TimeRecord.objects.create(
        link=link,
        start_time=datetime(2026, 9, 4, 9, 0),
        end_time=datetime(2026, 9, 4, 10, 0),
    )
    report = Report.create(mou)
    report.add_time_record(record)

    report.remove_task_lines(task)

    assert ReportLine.objects.filter(report=report, link=link).exists() is False
    assert TimeRecord.objects.filter(pk=record.pk).exists()
    record.refresh_from_db()
    assert record.report_line is None


def test_report_remove_task_lines_only_affects_the_given_task(settings):
    settings.RFP_EUROS = 20.0
    mou = MoU.objects.create(name="nlnet-2026")
    task_a = Task.objects.create(mou=mou, name="10a")
    task_b = Task.objects.create(mou=mou, name="10b")
    link_a = Link.objects.create(task=task_a, url="https://example.com/issues/1")
    link_b = Link.objects.create(task=task_b, url="https://example.com/issues/2")
    record_a = TimeRecord.objects.create(
        link=link_a,
        start_time=datetime(2026, 9, 4, 9, 0),
        end_time=datetime(2026, 9, 4, 10, 0),
    )
    record_b = TimeRecord.objects.create(
        link=link_b,
        start_time=datetime(2026, 9, 4, 9, 0),
        end_time=datetime(2026, 9, 4, 10, 0),
    )
    report = Report.create(mou)
    report.add_time_record(record_a)
    report.add_time_record(record_b)

    report.remove_task_lines(task_a)

    assert ReportLine.objects.filter(report=report, link=link_a).exists() is False
    assert ReportLine.objects.filter(report=report, link=link_b).exists()
    record_b.refresh_from_db()
    assert record_b.report_line is not None


def test_report_remove_task_lines_handles_links_with_no_task(settings):
    settings.RFP_EUROS = 20.0
    mou = MoU.objects.create(name="nlnet-2026")
    link = Link.objects.create(url="https://example.com/issues/1")
    record = TimeRecord.objects.create(
        link=link,
        start_time=datetime(2026, 9, 4, 9, 0),
        end_time=datetime(2026, 9, 4, 10, 0),
    )
    report = Report.create(mou)
    ReportLine.objects.create(report=report, link=link, budget=20.0)
    record.report_line = ReportLine.objects.get(report=report, link=link)
    record.save(update_fields=["report_line"])

    report.remove_task_lines(None)

    assert ReportLine.objects.filter(report=report, link=link).exists() is False
    record.refresh_from_db()
    assert record.report_line is None


def test_report_total_budget_reflects_a_manual_override(settings):
    settings.RFP_EUROS = 20.0
    mou = MoU.objects.create(name="nlnet-2026")
    task = Task.objects.create(mou=mou, name="10a")
    link = Link.objects.create(task=task, url="https://example.com/issues/1")
    record = TimeRecord.objects.create(
        link=link,
        start_time=datetime(2026, 9, 4, 9, 0),
        end_time=datetime(2026, 9, 4, 10, 0),
    )
    report = Report.create(mou)
    report.add_time_record(record)
    line = ReportLine.objects.get(report=report, link=link)
    line.budget = 999.0
    line.save(update_fields=["budget"])

    assert report.total_budget == 999.0


def test_report_export_then_import_round_trips_unchanged(settings):
    settings.RFP_EUROS = 20.0
    mou = MoU.objects.create(name="nlnet-2026")
    task = Task.objects.create(mou=mou, name="10a")
    link = Link.objects.create(task=task, url="https://example.com/issues/1")
    link.add_tag("review")
    record = TimeRecord.objects.create(
        link=link,
        start_time=datetime(2026, 9, 4, 9, 0),
        end_time=datetime(2026, 9, 4, 10, 0),
    )
    report = Report.create(mou)
    report.add_time_record(record)
    exported = report.export_lines()

    report.import_lines(exported)

    assert report.export_lines() == exported


def test_report_export_lines_rounds_budget_to_cents(settings):
    settings.RFP_EUROS = 20.0
    mou = MoU.objects.create(name="nlnet-2026")
    task = Task.objects.create(mou=mou, name="10a")
    link = Link.objects.create(task=task, url="https://example.com/issues/1")
    # 17 seconds at 20€/hour is not a round number of cents.
    record = TimeRecord.objects.create(
        link=link,
        start_time=datetime(2026, 9, 4, 9, 0, 0),
        end_time=datetime(2026, 9, 4, 9, 0, 17),
    )
    report = Report.create(mou)
    report.add_time_record(record)
    line = ReportLine.objects.get(report=report, link=link)
    assert round(line.budget, 2) != line.budget  # precondition: not already round

    rows = list(csv.DictReader(io.StringIO(report.export_lines())))

    assert rows[0]["budget"] == str(round(line.budget, 2))


def test_report_export_lines_includes_a_links_already_cached_title(settings):
    settings.RFP_EUROS = 20.0
    mou = MoU.objects.create(name="nlnet-2026")
    task = Task.objects.create(mou=mou, name="10a")
    link = Link.objects.create(
        task=task,
        url="https://github.com/nlnet/rfp-recorder/issues/1",
        title="Fix the thing",
    )
    record = TimeRecord.objects.create(
        link=link,
        start_time=datetime(2026, 9, 4, 9, 0),
        end_time=datetime(2026, 9, 4, 10, 0),
    )
    report = Report.create(mou)
    report.add_time_record(record)

    rows = list(csv.DictReader(io.StringIO(report.export_lines())))

    assert rows[0]["title"] == "Fix the thing"


def test_report_ensure_link_titles_caches_issue_and_pr_titles(settings):
    settings.RFP_EUROS = 20.0
    mou = MoU.objects.create(name="nlnet-2026")
    task = Task.objects.create(mou=mou, name="10a")
    issue_link = Link.objects.create(
        task=task, url="https://github.com/nlnet/rfp-recorder/issues/1"
    )
    pr_link = Link.objects.create(
        task=task, url="https://github.com/nlnet/rfp-recorder/pull/2"
    )
    for link in (issue_link, pr_link):
        TimeRecord.objects.create(
            link=link,
            start_time=datetime(2026, 9, 4, 9, 0),
            end_time=datetime(2026, 9, 4, 10, 0),
        )
    report = Report.create(mou)
    report.add_time_record(issue_link.time_records.get())
    report.add_time_record(pr_link.time_records.get())

    def fake_get(url, **kwargs):
        response = MagicMock(status_code=200)
        title = "Issue title" if url.endswith("/issues/1") else "PR title"
        response.json = MagicMock(return_value={"title": title})
        return response

    session = MagicMock()
    session.get = AsyncMock(side_effect=fake_get)
    session.__aenter__ = AsyncMock(return_value=session)
    session.__aexit__ = AsyncMock(return_value=False)

    with patch("nlnet_rfp_recorder.github.niquests.AsyncSession", return_value=session):
        report.ensure_link_titles()

    assert session.get.call_count == 2
    issue_link.refresh_from_db()
    pr_link.refresh_from_db()
    assert issue_link.title == "Issue title"
    assert pr_link.title == "PR title"


def test_report_ensure_link_titles_skips_already_cached_links(settings):
    settings.RFP_EUROS = 20.0
    mou = MoU.objects.create(name="nlnet-2026")
    task = Task.objects.create(mou=mou, name="10a")
    link = Link.objects.create(
        task=task,
        url="https://github.com/nlnet/rfp-recorder/issues/1",
        title="Already cached",
    )
    record = TimeRecord.objects.create(
        link=link,
        start_time=datetime(2026, 9, 4, 9, 0),
        end_time=datetime(2026, 9, 4, 10, 0),
    )
    report = Report.create(mou)
    report.add_time_record(record)

    with patch("nlnet_rfp_recorder.github.niquests.AsyncSession") as async_session:
        report.ensure_link_titles()

    async_session.assert_not_called()
    link.refresh_from_db()
    assert link.title == "Already cached"


def test_report_ensure_link_titles_skips_plain_links(settings):
    settings.RFP_EUROS = 20.0
    mou = MoU.objects.create(name="nlnet-2026")
    task = Task.objects.create(mou=mou, name="10a")
    link = Link.objects.create(task=task, url="https://example.com/not-github")
    record = TimeRecord.objects.create(
        link=link,
        start_time=datetime(2026, 9, 4, 9, 0),
        end_time=datetime(2026, 9, 4, 10, 0),
    )
    report = Report.create(mou)
    report.add_time_record(record)

    with patch("nlnet_rfp_recorder.github.niquests.AsyncSession") as async_session:
        report.ensure_link_titles()

    async_session.assert_not_called()
    link.refresh_from_db()
    assert link.title == ""


def test_report_import_skips_blank_and_whitespace_only_lines(settings):
    settings.RFP_EUROS = 20.0
    mou = MoU.objects.create(name="nlnet-2026")
    task = Task.objects.create(mou=mou, name="10a")
    link = Link.objects.create(task=task, url="https://example.com/issues/1")
    record = TimeRecord.objects.create(
        link=link,
        start_time=datetime(2026, 9, 4, 9, 0),
        end_time=datetime(2026, 9, 4, 10, 0),
    )
    report = Report.create(mou)

    report.import_lines(
        "task,link,budget,tags,records\n"
        "\n"
        "   \n"
        f"10a,{link.url},100,,{record.pk}\n"
        "  \t  \n"
    )

    line = ReportLine.objects.get(report=report, link=link)
    assert line.budget == 100.0
    record.refresh_from_db()
    assert record.report_line == line


def test_report_import_budget_column_wins_even_when_records_change(settings):
    settings.RFP_EUROS = 20.0
    mou = MoU.objects.create(name="nlnet-2026")
    task = Task.objects.create(mou=mou, name="10a")
    link = Link.objects.create(task=task, url="https://example.com/issues/1")
    record_a = TimeRecord.objects.create(
        link=link,
        start_time=datetime(2026, 9, 4, 9, 0),
        end_time=datetime(2026, 9, 4, 10, 0),
    )
    record_b = TimeRecord.objects.create(
        link=link,
        start_time=datetime(2026, 9, 4, 11, 0),
        end_time=datetime(2026, 9, 4, 12, 0),
    )
    report = Report.create(mou)
    report.add_time_record(record_a)

    report.import_lines(
        f"task,link,budget,tags,records\n10a,{link.url},777,review,{record_b.pk}\n"
    )

    line = ReportLine.objects.get(report=report, link=link)
    assert line.budget == 777.0
    assert set(line.time_records.all()) == {record_b}
    record_a.refresh_from_db()
    assert record_a.report_line is None


def test_report_import_moves_an_existing_link_to_a_different_task(settings):
    settings.RFP_EUROS = 20.0
    mou = MoU.objects.create(name="nlnet-2026")
    task_a = Task.objects.create(mou=mou, name="10a")
    task_b = Task.objects.create(mou=mou, name="10b")
    link = Link.objects.create(task=task_a, url="https://example.com/issues/1")
    record = TimeRecord.objects.create(
        link=link,
        start_time=datetime(2026, 9, 4, 9, 0),
        end_time=datetime(2026, 9, 4, 10, 0),
    )
    report = Report.create(mou)
    report.add_time_record(record)

    report.import_lines(
        f"task,link,budget,tags,records\n10b,{link.url},20,,{record.pk}\n"
    )

    link.refresh_from_db()
    assert link.task == task_b


def test_report_import_clears_an_existing_links_task_when_column_is_blank(settings):
    settings.RFP_EUROS = 20.0
    mou = MoU.objects.create(name="nlnet-2026")
    task = Task.objects.create(mou=mou, name="10a")
    link = Link.objects.create(task=task, url="https://example.com/issues/1")
    record = TimeRecord.objects.create(
        link=link,
        start_time=datetime(2026, 9, 4, 9, 0),
        end_time=datetime(2026, 9, 4, 10, 0),
    )
    report = Report.create(mou)
    report.add_time_record(record)

    report.import_lines(f"task,link,budget,tags,records\n,{link.url},20,,{record.pk}\n")

    link.refresh_from_db()
    assert link.task is None


def test_report_import_moves_a_record_to_a_different_links_task(settings):
    # Listing a record's pk under a row for a different, already-existing
    # link moves the record onto that link - not just onto its report
    # line - so its derived task (record.link.task) actually follows,
    # matching what the report now shows it under.
    settings.RFP_EUROS = 20.0
    mou = MoU.objects.create(name="nlnet-2026")
    task_a = Task.objects.create(mou=mou, name="10a")
    task_b = Task.objects.create(mou=mou, name="10b")
    link_a = Link.objects.create(task=task_a, url="https://example.com/issues/1")
    link_b = Link.objects.create(task=task_b, url="https://example.com/issues/2")
    record = TimeRecord.objects.create(
        link=link_a,
        start_time=datetime(2026, 9, 4, 9, 0),
        end_time=datetime(2026, 9, 4, 10, 0),
    )
    report = Report.create(mou)
    report.add_time_record(record)

    report.import_lines(
        f"task,link,budget,tags,records\n10b,{link_b.url},20,,{record.pk}\n"
    )

    record.refresh_from_db()
    assert record.link == link_b
    assert record.link.task == task_b
    assert record.report_line.link == link_b


def test_report_import_removes_lines_not_present_and_keeps_their_records(settings):
    settings.RFP_EUROS = 20.0
    mou = MoU.objects.create(name="nlnet-2026")
    task = Task.objects.create(mou=mou, name="10a")
    link = Link.objects.create(task=task, url="https://example.com/issues/1")
    record = TimeRecord.objects.create(
        link=link,
        start_time=datetime(2026, 9, 4, 9, 0),
        end_time=datetime(2026, 9, 4, 10, 0),
    )
    report = Report.create(mou)
    report.add_time_record(record)

    report.import_lines("task,link,budget,tags,records\n")

    assert ReportLine.objects.filter(report=report).exists() is False
    assert TimeRecord.objects.filter(pk=record.pk).exists()
    record.refresh_from_db()
    assert record.report_line is None


def test_report_import_on_remove_keep_leaves_the_line_untouched(settings):
    settings.RFP_EUROS = 20.0
    mou = MoU.objects.create(name="nlnet-2026")
    task = Task.objects.create(mou=mou, name="10a")
    link = Link.objects.create(task=task, url="https://example.com/issues/1")
    record = TimeRecord.objects.create(
        link=link,
        start_time=datetime(2026, 9, 4, 9, 0),
        end_time=datetime(2026, 9, 4, 10, 0),
    )
    report = Report.create(mou)
    report.add_time_record(record)

    report.import_lines(
        "task,link,budget,tags,records\n", on_remove=lambda line: "keep"
    )

    assert ReportLine.objects.filter(report=report, link=link).exists()
    record.refresh_from_db()
    assert record.report_line is not None
    assert link.excluded_from_reports is False


def test_report_import_on_remove_exclude_marks_the_link(settings):
    settings.RFP_EUROS = 20.0
    mou = MoU.objects.create(name="nlnet-2026")
    task = Task.objects.create(mou=mou, name="10a")
    link = Link.objects.create(task=task, url="https://example.com/issues/1")
    record = TimeRecord.objects.create(
        link=link,
        start_time=datetime(2026, 9, 4, 9, 0),
        end_time=datetime(2026, 9, 4, 10, 0),
    )
    report = Report.create(mou)
    report.add_time_record(record)

    report.import_lines(
        "task,link,budget,tags,records\n", on_remove=lambda line: "exclude"
    )

    assert ReportLine.objects.filter(report=report, link=link).exists() is False
    record.refresh_from_db()
    assert record.report_line is None
    link.refresh_from_db()
    assert link.excluded_from_reports is True


def test_report_import_on_remove_gets_the_actual_report_line(settings):
    settings.RFP_EUROS = 20.0
    mou = MoU.objects.create(name="nlnet-2026")
    task = Task.objects.create(mou=mou, name="10a")
    link = Link.objects.create(task=task, url="https://example.com/issues/1")
    record = TimeRecord.objects.create(
        link=link,
        start_time=datetime(2026, 9, 4, 9, 0),
        end_time=datetime(2026, 9, 4, 10, 0),
    )
    report = Report.create(mou)
    report.add_time_record(record)
    seen = []

    report.import_lines(
        "task,link,budget,tags,records\n",
        on_remove=lambda line: seen.append(line) or "remove",
    )

    assert len(seen) == 1
    assert seen[0].link == link
    assert seen[0].report == report


def test_report_import_on_remove_rejects_an_invalid_decision(settings):
    settings.RFP_EUROS = 20.0
    mou = MoU.objects.create(name="nlnet-2026")
    task = Task.objects.create(mou=mou, name="10a")
    link = Link.objects.create(task=task, url="https://example.com/issues/1")
    record = TimeRecord.objects.create(
        link=link,
        start_time=datetime(2026, 9, 4, 9, 0),
        end_time=datetime(2026, 9, 4, 10, 0),
    )
    report = Report.create(mou)
    report.add_time_record(record)

    with pytest.raises(ValueError, match="Invalid on_remove decision"):
        report.import_lines(
            "task,link,budget,tags,records\n", on_remove=lambda line: "bogus"
        )


def test_links_with_unreported_time_excludes_excluded_links(settings):
    settings.RFP_EUROS = 20.0
    mou = MoU.objects.create(name="nlnet-2026")
    task = Task.objects.create(mou=mou, name="10a")
    link = Link.objects.create(
        task=task, url="https://example.com/issues/1", excluded_from_reports=True
    )
    TimeRecord.objects.create(
        link=link,
        start_time=datetime(2026, 9, 4, 9, 0),
        end_time=datetime(2026, 9, 4, 10, 0),
    )

    assert link not in list(task.links_with_unreported_time)
    assert task.duration == timedelta()


def test_report_import_renames_the_link_when_records_unambiguously_identify_it(
    settings,
):
    settings.RFP_EUROS = 20.0
    mou = MoU.objects.create(name="nlnet-2026")
    task = Task.objects.create(mou=mou, name="10a")
    link = Link.objects.create(task=task, url="https://example.com/issues/1")
    record = TimeRecord.objects.create(
        link=link,
        start_time=datetime(2026, 9, 4, 9, 0),
        end_time=datetime(2026, 9, 4, 10, 0),
    )
    report = Report.create(mou)
    report.add_time_record(record)
    old_link_id = link.id

    report.import_lines(
        "task,link,budget,tags,records\n"
        f"10a,https://example.com/issues/2,100,,{record.pk}\n"
    )

    # same Link row, just renamed - not a new one
    assert Link.objects.filter(url="https://example.com/issues/1").exists() is False
    renamed = Link.objects.get(url="https://example.com/issues/2")
    assert renamed.id == old_link_id
    assert renamed.task == task
    record.refresh_from_db()
    assert record.link_id == old_link_id
    assert record.report_line.report == report
    assert record.report_line.budget == 100.0


def test_report_import_rename_still_reconciles_other_records_on_the_line(settings):
    settings.RFP_EUROS = 20.0
    mou = MoU.objects.create(name="nlnet-2026")
    task = Task.objects.create(mou=mou, name="10a")
    link = Link.objects.create(task=task, url="https://example.com/issues/1")
    record_a = TimeRecord.objects.create(
        link=link,
        start_time=datetime(2026, 9, 4, 9, 0),
        end_time=datetime(2026, 9, 4, 10, 0),
    )
    record_b = TimeRecord.objects.create(
        link=link,
        start_time=datetime(2026, 9, 4, 11, 0),
        end_time=datetime(2026, 9, 4, 12, 0),
    )
    report = Report.create(mou)
    report.add_time_record(record_a)
    report.add_time_record(record_b)

    # rename, but only keep record_a on the line
    report.import_lines(
        "task,link,budget,tags,records\n"
        f"10a,https://example.com/issues/2,50,,{record_a.pk}\n"
    )

    record_a.refresh_from_db()
    record_b.refresh_from_db()
    assert record_a.report_line is not None
    assert record_b.report_line is None


def test_report_import_creates_a_new_link_when_records_are_ambiguous(settings):
    settings.RFP_EUROS = 20.0
    mou = MoU.objects.create(name="nlnet-2026")
    task = Task.objects.create(mou=mou, name="10a")
    link_a = Link.objects.create(task=task, url="https://example.com/issues/1")
    link_b = Link.objects.create(task=task, url="https://example.com/issues/2")
    record_a = TimeRecord.objects.create(
        link=link_a,
        start_time=datetime(2026, 9, 4, 9, 0),
        end_time=datetime(2026, 9, 4, 10, 0),
    )
    record_b = TimeRecord.objects.create(
        link=link_b,
        start_time=datetime(2026, 9, 4, 11, 0),
        end_time=datetime(2026, 9, 4, 12, 0),
    )
    report = Report.create(mou)

    report.import_lines(
        "task,link,budget,tags,records\n"
        f'10a,https://example.com/issues/3,10,,"{record_a.pk},{record_b.pk}"\n'
    )

    # neither existing link was touched
    assert Link.objects.filter(pk=link_a.pk).exists()
    assert Link.objects.filter(pk=link_b.pk).exists()
    new_link = Link.objects.get(url="https://example.com/issues/3")
    assert new_link.task == task
    report_line = ReportLine.objects.get(report=report, link=new_link)
    assert report_line.time_records.count() == 0
    record_a.refresh_from_db()
    record_b.refresh_from_db()
    assert record_a.report_line is None
    assert record_b.report_line is None


def test_report_import_creates_a_new_link_when_records_column_is_empty(settings):
    settings.RFP_EUROS = 20.0
    mou = MoU.objects.create(name="nlnet-2026")
    task = Task.objects.create(mou=mou, name="10a")
    report = Report.create(mou)

    report.import_lines(
        "task,link,budget,tags,records\n10a,https://example.com/issues/9,25,,\n"
    )

    link = Link.objects.get(url="https://example.com/issues/9")
    assert link.task == task
    report_line = ReportLine.objects.get(report=report, link=link)
    assert report_line.budget == 25.0
    assert report_line.time_records.count() == 0


def test_report_import_creates_a_new_link_when_records_do_not_exist(settings):
    settings.RFP_EUROS = 20.0
    mou = MoU.objects.create(name="nlnet-2026")
    task = Task.objects.create(mou=mou, name="10a")
    report = Report.create(mou)

    report.import_lines(
        "task,link,budget,tags,records\n10a,https://example.com/issues/9,25,,999999\n"
    )

    link = Link.objects.get(url="https://example.com/issues/9")
    assert link.task == task
    assert ReportLine.objects.get(report=report, link=link).time_records.count() == 0


def test_report_import_new_link_has_no_task_when_the_row_task_does_not_resolve(
    settings,
):
    settings.RFP_EUROS = 20.0
    mou = MoU.objects.create(name="nlnet-2026")
    report = Report.create(mou)

    report.import_lines(
        "task,link,budget,tags,records\n99z,https://example.com/issues/9,25,,\n"
    )

    link = Link.objects.get(url="https://example.com/issues/9")
    assert link.task is None


def test_report_import_new_link_has_no_task_when_the_task_column_is_blank(settings):
    settings.RFP_EUROS = 20.0
    mou = MoU.objects.create(name="nlnet-2026")
    report = Report.create(mou)

    report.import_lines(
        "task,link,budget,tags,records\n,https://example.com/issues/9,25,,\n"
    )

    link = Link.objects.get(url="https://example.com/issues/9")
    assert link.task is None


def test_report_import_rename_still_validates_mou_ownership(settings):
    settings.RFP_EUROS = 20.0
    mou_a = MoU.objects.create(name="mou-a")
    mou_b = MoU.objects.create(name="mou-b")
    task_a = Task.objects.create(mou=mou_a, name="10a")
    link = Link.objects.create(task=task_a, url="https://example.com/issues/1")
    record = TimeRecord.objects.create(
        link=link,
        start_time=datetime(2026, 9, 4, 9, 0),
        end_time=datetime(2026, 9, 4, 10, 0),
    )
    report_a = Report.create(mou_a)
    report_a.add_time_record(record)
    report_b = Report.create(mou_b)

    with pytest.raises(ValueError, match="does not belong to MoU"):
        report_b.import_lines(
            "task,link,budget,tags,records\n"
            f"10a,https://example.com/issues/2,10,,{record.pk}\n"
        )


def test_report_import_rename_ignores_records_that_dont_currently_have_a_link(
    settings,
):
    settings.RFP_EUROS = 20.0
    mou = MoU.objects.create(name="nlnet-2026")
    task = Task.objects.create(mou=mou, name="10a")
    link = Link.objects.create(task=task, url="https://example.com/issues/1")
    linked_record = TimeRecord.objects.create(
        link=link,
        start_time=datetime(2026, 9, 4, 9, 0),
        end_time=datetime(2026, 9, 4, 10, 0),
    )
    linkless_record = TimeRecord.objects.create(
        start_time=datetime(2026, 9, 4, 11, 0), end_time=datetime(2026, 9, 4, 12, 0)
    )
    report = Report.create(mou)
    report.add_time_record(linked_record)

    # linkless_record has no link, so it can't make this ambiguous - the
    # rename is still unambiguous via linked_record alone.
    report.import_lines(
        "task,link,budget,tags,records\n"
        f"10a,https://example.com/issues/2,10,,"
        f'"{linked_record.pk},{linkless_record.pk}"\n'
    )

    assert Link.objects.filter(url="https://example.com/issues/2").exists()


def test_report_preview_shows_the_links_current_tags(settings):
    settings.RFP_EUROS = 20.0
    mou = MoU.objects.create(name="nlnet-2026")
    task = Task.objects.create(mou=mou, name="10a")
    link = Link.objects.create(task=task, url="https://example.com/issues/1")
    link.add_tag("review")
    TimeRecord.objects.create(
        link=link,
        start_time=datetime(2026, 9, 4, 9, 0),
        end_time=datetime(2026, 9, 4, 10, 0),
    )

    text = Report.preview(mou)

    assert "https://example.com/issues/1 - 20€ (review)" in text


@pytest.mark.parametrize(
    ("budget", "expected"),
    [
        # Below 5: no figure is shown at all - never "costs nothing", and
        # never inflated up to a misleading minimum either.
        (0.0, None),
        (-1.0, None),  # shouldn't occur in practice, but must not explode
        (0.01, None),
        (1.0, None),
        (2.5, None),
        (4.0, None),
        (4.99, None),
        (4.999999, None),
        # Exactly on a multiple of 5: stays exactly there, isn't bumped
        # to the next bucket.
        (5.0, 5),
        (10.0, 10),
        (15.0, 15),
        (100.0, 100),
        # Just above a multiple of 5: rounds UP to the next multiple, not
        # to the nearest (unlike round-to-nearest, 5.01 is not "close
        # enough" to 5 to stay there).
        (5.01, 10),
        (5.5, 10),
        (9.99, 10),
        (10.01, 15),
        (12.5, 15),
        (14.99, 15),
        (100.01, 105),
        # A tiny float epsilon must not silently vanish - 10.0000001
        # still counts as "just above 10" and rounds up to 15, matching
        # "rounding must be up for links".
        (10.0000001, 15),
    ],
)
def test_round_link_budget(budget, expected):
    assert _round_link_budget(budget) == expected


def test_sum_link_budgets_ignores_none_entries():
    assert _sum_link_budgets([5, None, 10, None, 15]) == 30


def test_sum_link_budgets_of_nothing_is_zero():
    assert _sum_link_budgets([]) == 0
    assert _sum_link_budgets([None, None]) == 0


def test_report_str_is_its_id():
    mou = MoU.objects.create(name="nlnet-2026")
    report = Report.create(mou)

    assert str(report) == "nlnet-2026-1"


def test_report_generate_report_groups_by_task_and_totals(settings):
    settings.RFP_EUROS = 20.0
    mou = MoU.objects.create(name="nlnet-2026")
    task = Task.objects.create(mou=mou, name="10a")
    issue_link = Link.objects.create(
        task=task, url="https://github.com/nlnet/rfp-recorder/issues/1"
    )
    other_link = Link.objects.create(task=task, url="https://example.com/docs/design")
    now = timezone.now()
    for link in (issue_link, other_link):
        TimeRecord.objects.create(
            link=link, start_time=now - timedelta(minutes=30), end_time=now
        )
    report = Report.create(mou)
    for record in TimeRecord.objects.all():
        report.add_time_record(record)

    text = report.generate_report()

    assert f"Report: {report.id}" in text
    assert "MoU: nlnet-2026" in text
    assert "10a: 20€" in text
    assert "Issues:" in text
    assert "- https://github.com/nlnet/rfp-recorder/issues/1" in text
    assert "Links:" in text
    assert "- https://example.com/docs/design" in text
    assert "Total: 20€" in text
    assert "Excluded Pull Requests" not in text


def test_report_generate_report_groups_discussions_separately(settings):
    settings.RFP_EUROS = 20.0
    mou = MoU.objects.create(name="nlnet-2026")
    task = Task.objects.create(mou=mou, name="10a")
    discussion_link = Link.objects.create(
        task=task, url="https://github.com/nlnet/rfp-recorder/discussions/3"
    )
    issue_link = Link.objects.create(
        task=task, url="https://github.com/nlnet/rfp-recorder/issues/1"
    )
    now = timezone.now()
    for link in (discussion_link, issue_link):
        TimeRecord.objects.create(
            link=link, start_time=now - timedelta(minutes=30), end_time=now
        )
    report = Report.create(mou)
    for record in TimeRecord.objects.all():
        report.add_time_record(record)

    text = report.generate_report()

    assert "Discussions:" in text
    assert "- https://github.com/nlnet/rfp-recorder/discussions/3" in text
    assert "Issues:" in text
    assert "- https://github.com/nlnet/rfp-recorder/issues/1" in text


def test_report_generate_report_orders_tasks_and_links_and_spaces_them_out(settings):
    settings.RFP_EUROS = 20.0
    mou = MoU.objects.create(name="nlnet-2026")
    # Created out of order, and "9a" would sort after "10a" as plain text -
    # both must be undone by number-then-letter task ordering.
    task_10a = Task.objects.create(mou=mou, name="10a")
    task_9a = Task.objects.create(mou=mou, name="9a")
    link_10a_5 = Link.objects.create(
        task=task_10a, url="https://github.com/nlnet/rfp-recorder/issues/5"
    )
    link_10a_2 = Link.objects.create(
        task=task_10a, url="https://github.com/nlnet/rfp-recorder/issues/2"
    )
    link_9a = Link.objects.create(
        task=task_9a, url="https://github.com/nlnet/rfp-recorder/issues/1"
    )
    now = timezone.now()
    for link in (link_10a_5, link_10a_2, link_9a):
        TimeRecord.objects.create(
            link=link, start_time=now - timedelta(minutes=30), end_time=now
        )
    report = Report.create(mou)
    for record in TimeRecord.objects.all():
        report.add_time_record(record)

    text = report.generate_report()

    assert text == (
        f"Report: {report.id}\n"
        "MoU: nlnet-2026\n"
        "9a: 10€\n"
        "  Issues:\n"
        "    - https://github.com/nlnet/rfp-recorder/issues/1 - 10€\n"
        "\n"
        "10a: 20€\n"
        "  Issues:\n"
        "    - https://github.com/nlnet/rfp-recorder/issues/2 - 10€\n"
        "    - https://github.com/nlnet/rfp-recorder/issues/5 - 10€\n"
        "\n"
        "Total: 30€"
    )


@pytest.mark.parametrize(
    ("budgets", "expected_total"),
    [
        ([], 0),
        ([7], 10),  # shown on its own, rounded up to 5's
        ([3], 10),  # too small to show, but its 3€ still pools -> 10
        ([1, 2], 10),  # 1+2=3 pooled -> 10
        ([3, 3, 3], 10),  # 9 pooled -> 10
        ([3, 3, 3, 3], 20),  # 12 pooled -> 20
        ([7, 3], 20),  # 10 shown + 3 pooled (-> 10) = 20
        ([7, 12], 25),  # 10 + 15 shown, nothing pooled
        ([0], 0),  # no time at all - nothing to pool either
        ([0, 0, 0], 0),
        ([4.99, 4.99], 10),  # 9.98 pooled -> 10
        ([5, 5, 5], 15),  # each shown individually (>=5)
        ([2, 2, 2, 2, 2], 10),  # 10 pooled exactly - stays 10, not bumped
        ([2, 2, 2, 2, 2, 2], 20),  # 12 pooled -> 20
        ([100, 3], 110),  # 100 shown + 3 pooled (-> 10)
    ],
)
def test_task_budget_totals_many_small_links_correctly(budgets, expected_total):
    lines = [SimpleNamespace(budget=b) for b in budgets]

    total, _ = _task_budget(lines)

    assert total == expected_total


def test_task_budget_line_budgets_match_round_link_budget_per_line():
    lines = [SimpleNamespace(budget=b) for b in (7, 3, 12)]

    _, line_budgets = _task_budget(lines)

    assert line_budgets[id(lines[0])] == 10
    assert line_budgets[id(lines[1])] is None
    assert line_budgets[id(lines[2])] == 15


def test_report_generate_report_pools_a_small_link_into_the_task_total(settings):
    # 7€ shows as its own 10€ line; 3€ is too small to show on its own but
    # still counts - pooled and rounded up to 10 - into the task total,
    # rather than vanishing. 12€ shows as its own 15€ line.
    settings.RFP_EUROS = 20.0
    mou = MoU.objects.create(name="nlnet-2026")
    task = Task.objects.create(mou=mou, name="10a")
    link_a = Link.objects.create(task=task, url="https://example.com/issues/1")
    link_b = Link.objects.create(task=task, url="https://example.com/issues/2")
    link_c = Link.objects.create(task=task, url="https://example.com/issues/3")
    TimeRecord.objects.create(
        link=link_a,
        start_time=datetime(2026, 9, 4, 9, 0),
        end_time=datetime(2026, 9, 4, 9, 21),  # 21min @ 20€/h = 7€
    )
    TimeRecord.objects.create(
        link=link_b,
        start_time=datetime(2026, 9, 4, 9, 0),
        end_time=datetime(2026, 9, 4, 9, 9),  # 9min @ 20€/h = 3€
    )
    TimeRecord.objects.create(
        link=link_c,
        start_time=datetime(2026, 9, 4, 9, 0),
        end_time=datetime(2026, 9, 4, 9, 36),  # 36min @ 20€/h = 12€
    )
    report = Report.create(mou)
    for record in TimeRecord.objects.all():
        report.add_time_record(record)

    text = report.generate_report()

    assert "https://example.com/issues/1 - 10€" in text
    assert "https://example.com/issues/2\n" in text  # listed, no figure
    assert "https://example.com/issues/2 - " not in text
    assert "https://example.com/issues/3 - 15€" in text
    assert "10a: 35€" in text  # 10 + 15 + (3 pooled -> 10)
    assert "Total: 35€" in text


def test_report_generate_report_pools_many_small_links_into_one_task_total(settings):
    # "I might have a lot of small links and they add up": five links each
    # too small (2€) to show individually still sum to a real 10€ task
    # total, not 0€.
    settings.RFP_EUROS = 20.0
    mou = MoU.objects.create(name="nlnet-2026")
    task = Task.objects.create(mou=mou, name="10a")
    for i in range(5):
        link = Link.objects.create(task=task, url=f"https://example.com/issues/{i}")
        TimeRecord.objects.create(
            link=link,
            start_time=datetime(2026, 9, 4, 9, 0),
            end_time=datetime(2026, 9, 4, 9, 6),  # 6min @ 20€/h = 2€
        )
    report = Report.create(mou)
    for record in TimeRecord.objects.all():
        report.add_time_record(record)

    text = report.generate_report()

    for i in range(5):
        assert f"https://example.com/issues/{i}\n" in text
        assert f"https://example.com/issues/{i} - " not in text
    assert "10a: 10€" in text
    assert "Total: 10€" in text


def test_report_generate_report_does_not_show_any_alias(settings):
    settings.RFP_EUROS = 20.0
    mou = MoU.objects.create(name="nlnet-2026")
    task = Task.objects.create(mou=mou, name="10a")
    Alias.create("mou", "nlnet-2026", "og")
    Alias.create("task", "10a", "lib", mou=mou)
    link = Link.objects.create(task=task, url="https://example.com/issues/1")
    now = timezone.now()
    TimeRecord.objects.create(
        link=link, start_time=now - timedelta(minutes=30), end_time=now
    )
    report = Report.create(mou)
    report.add_time_record(TimeRecord.objects.get())

    text = report.generate_report()

    assert "MoU: nlnet-2026\n" in text
    assert "10a: 10€" in text
    assert "og" not in text
    assert "lib" not in text


def test_report_preview_does_not_show_any_alias(settings):
    settings.RFP_EUROS = 20.0
    mou = MoU.objects.create(name="nlnet-2026")
    task = Task.objects.create(mou=mou, name="10a")
    Alias.create("mou", "nlnet-2026", "og")
    Alias.create("task", "10a", "lib", mou=mou)
    link = Link.objects.create(task=task, url="https://example.com/issues/1")
    now = timezone.now()
    TimeRecord.objects.create(
        link=link, start_time=now - timedelta(minutes=30), end_time=now
    )

    text = Report.preview(mou)

    assert "MoU: nlnet-2026\n" in text
    assert "10a: 10€" in text
    assert "og" not in text
    assert "lib" not in text


def test_format_excluded_links_does_not_show_the_task_alias():
    mou = MoU.objects.create(name="nlnet-2026")
    task = Task.objects.create(mou=mou, name="10a")
    Alias.create("task", "10a", "lib", mou=mou)
    link = Link.objects.create(
        task=task, url="https://github.com/nlnet/rfp-recorder/pull/1"
    )

    text = Report.format_excluded_links([link])

    assert "10a:" in text
    assert "lib" not in text


def test_format_excluded_links_groups_by_task():
    task_a = Task.objects.create(name="10a")
    task_b = Task.objects.create(name="10b")
    link_a1 = Link.objects.create(
        task=task_a, url="https://github.com/nlnet/rfp-recorder/pull/1"
    )
    link_a2 = Link.objects.create(
        task=task_a, url="https://github.com/nlnet/rfp-recorder/pull/2"
    )
    link_b1 = Link.objects.create(
        task=task_b, url="https://github.com/nlnet/rfp-recorder/pull/3"
    )

    text = Report.format_excluded_links([link_a1, link_a2, link_b1])

    assert text == (
        "Excluded Pull Requests (not merged):\n"
        "  10a:\n"
        "    - https://github.com/nlnet/rfp-recorder/pull/1\n"
        "    - https://github.com/nlnet/rfp-recorder/pull/2\n"
        "  10b:\n"
        "    - https://github.com/nlnet/rfp-recorder/pull/3"
    )


def test_format_excluded_links_orders_tasks_by_number_then_letter_and_prs_by_number():
    # Created out of order on purpose, and with a task name ("9a") that a
    # plain string sort would put after "10a"/"10b" - both must be undone
    # by sorting on (number, letters) and PR number, not creation order.
    task_10b = Task.objects.create(name="10b")
    task_10a = Task.objects.create(name="10a")
    task_9a = Task.objects.create(name="9a")
    link_10a_5 = Link.objects.create(
        task=task_10a, url="https://github.com/nlnet/rfp-recorder/pull/5"
    )
    link_10a_2 = Link.objects.create(
        task=task_10a, url="https://github.com/nlnet/rfp-recorder/pull/2"
    )
    link_10b_1 = Link.objects.create(
        task=task_10b, url="https://github.com/nlnet/rfp-recorder/pull/1"
    )
    link_9a_9 = Link.objects.create(
        task=task_9a, url="https://github.com/nlnet/rfp-recorder/pull/9"
    )

    text = Report.format_excluded_links([link_10a_5, link_10a_2, link_10b_1, link_9a_9])

    assert text == (
        "Excluded Pull Requests (not merged):\n"
        "  9a:\n"
        "    - https://github.com/nlnet/rfp-recorder/pull/9\n"
        "  10a:\n"
        "    - https://github.com/nlnet/rfp-recorder/pull/2\n"
        "    - https://github.com/nlnet/rfp-recorder/pull/5\n"
        "  10b:\n"
        "    - https://github.com/nlnet/rfp-recorder/pull/1"
    )


def test_report_add_unreported_time_records_excludes_open_pull_requests(settings):
    settings.RFP_EUROS = 20.0
    mou = MoU.objects.create(name="nlnet-2026")
    task = Task.objects.create(mou=mou, name="10a")
    closed_link = Link.objects.create(
        task=task, url="https://github.com/nlnet/rfp-recorder/pull/1"
    )
    open_link = Link.objects.create(
        task=task, url="https://github.com/nlnet/rfp-recorder/pull/2"
    )
    now = timezone.now()
    TimeRecord.objects.create(
        link=closed_link, start_time=now - timedelta(minutes=30), end_time=now
    )
    TimeRecord.objects.create(
        link=open_link, start_time=now - timedelta(minutes=30), end_time=now
    )

    def fake_status(url, **kwargs):
        response = MagicMock(status_code=200)
        state = "closed" if url.endswith("/pulls/1") else "open"
        response.json = MagicMock(return_value={"state": state})
        return response

    session = MagicMock()
    session.get = AsyncMock(side_effect=fake_status)
    session.__aenter__ = AsyncMock(return_value=session)
    session.__aexit__ = AsyncMock(return_value=False)

    with patch("nlnet_rfp_recorder.github.niquests.AsyncSession", return_value=session):
        report = Report.create(mou)
        excluded_links = report.add_unreported_time_records()
        text = report.generate_report()

    assert excluded_links == [open_link]
    # only the closed PR's time is billed and shown; the open one is excluded
    assert "10a: 10€" in text
    assert "Pull Requests:" in text
    assert "- https://github.com/nlnet/rfp-recorder/pull/1" in text
    assert "Excluded Pull Requests" not in text
    assert "- https://github.com/nlnet/rfp-recorder/pull/2" not in text


def test_report_generate_report_only_includes_its_own_records():
    mou = MoU.objects.create(name="nlnet-2026")
    task = Task.objects.create(mou=mou, name="10a")
    link_a = Link.objects.create(task=task, url="https://example.com/a")
    link_b = Link.objects.create(task=task, url="https://example.com/b")
    now = timezone.now()
    record_a = TimeRecord.objects.create(
        link=link_a, start_time=now - timedelta(minutes=10), end_time=now
    )
    TimeRecord.objects.create(
        link=link_b, start_time=now - timedelta(minutes=10), end_time=now
    )
    report = Report.create(mou)
    report.add_time_record(record_a)

    text = report.generate_report()

    assert "https://example.com/a" in text
    assert "https://example.com/b" not in text


def test_report_total_budget_is_zero_without_rfp_euros(settings):
    settings.RFP_EUROS = None
    mou = MoU.objects.create(name="nlnet-2026")
    report = Report.create(mou)

    assert report.total_budget == 0.0


def test_report_total_budget_sums_its_own_records(settings):
    settings.RFP_EUROS = 20.0
    mou = MoU.objects.create(name="nlnet-2026")
    task = Task.objects.create(mou=mou, name="10a")
    link = Link.objects.create(task=task, url="https://example.com/a")
    now = timezone.now()
    record = TimeRecord.objects.create(
        link=link, start_time=now - timedelta(hours=1), end_time=now
    )
    report = Report.create(mou)
    report.add_time_record(record)

    assert report.total_budget == 20.0


def test_alias_create_for_a_mou():
    MoU.objects.create(name="nlnet-2026")

    alias = Alias.create("mou", "nlnet-2026", "og")

    assert alias.item_type == "mou"
    assert alias.target == "nlnet-2026"
    assert alias.alias == "og"
    assert alias.mou is None


def test_alias_create_rejects_an_unknown_item_type():
    with pytest.raises(ValueError, match="Unknown alias item type"):
        Alias.create("repo", "x", "y")


def test_alias_create_for_a_mou_fails_for_an_unknown_mou():
    with pytest.raises(ValueError, match="No such MoU: nlnet-2026"):
        Alias.create("mou", "nlnet-2026", "og")


def test_alias_create_for_a_mou_rejects_whitespace():
    MoU.objects.create(name="nlnet-2026")

    with pytest.raises(ValueError, match="must not contain spaces"):
        Alias.create("mou", "nlnet-2026", "my alias")


def test_alias_create_for_a_mou_rejects_an_existing_mou_name_as_alias():
    MoU.objects.create(name="nlnet-2026")
    MoU.objects.create(name="nlnet-2027")

    with pytest.raises(ValueError, match="already a MoU name"):
        Alias.create("mou", "nlnet-2026", "nlnet-2027")


def test_alias_create_for_a_mou_steals_an_alias_already_used_elsewhere():
    MoU.objects.create(name="nlnet-2026")
    MoU.objects.create(name="nlnet-2027")
    Alias.create("mou", "nlnet-2026", "og")

    moved = Alias.create("mou", "nlnet-2027", "og")

    assert moved.target == "nlnet-2027"
    assert Alias.objects.filter(item_type="mou", alias="og").count() == 1
    assert Alias.objects.get(item_type="mou", alias="og").target == "nlnet-2027"


def test_alias_conflicts_for_reports_an_alias_already_used_elsewhere():
    MoU.objects.create(name="nlnet-2026")
    MoU.objects.create(name="nlnet-2027")
    existing = Alias.create("mou", "nlnet-2026", "og")

    conflicts = list(Alias.conflicts_for("mou", "nlnet-2027", "og"))

    assert conflicts == [existing]


def test_alias_create_for_a_task():
    mou = MoU.objects.create(name="nlnet-2026")
    Task.objects.create(mou=mou, name="10a")

    alias = Alias.create("task", "10a", "lib", mou=mou)

    assert alias.item_type == "task"
    assert alias.target == "10a"
    assert alias.mou == mou


def test_alias_create_for_a_task_requires_a_mou():
    with pytest.raises(ValueError, match="No MoU selected"):
        Alias.create("task", "10a", "lib")


def test_alias_create_for_a_task_fails_for_an_unknown_task():
    mou = MoU.objects.create(name="nlnet-2026")

    with pytest.raises(ValueError, match="No such task: 10a"):
        Alias.create("task", "10a", "lib", mou=mou)


def test_alias_create_for_a_task_rejects_an_alias_starting_with_a_digit():
    mou = MoU.objects.create(name="nlnet-2026")
    Task.objects.create(mou=mou, name="10a")

    with pytest.raises(ValueError, match="must not start with a number"):
        Alias.create("task", "10a", "1lib", mou=mou)


def test_alias_create_for_a_task_is_scoped_per_mou():
    # The same alias can mean a different task under a different MoU.
    mou_a = MoU.objects.create(name="nlnet-2025")
    mou_b = MoU.objects.create(name="nlnet-2026")
    Task.objects.create(mou=mou_a, name="10a")
    Task.objects.create(mou=mou_b, name="11b")

    Alias.create("task", "10a", "lib", mou=mou_a)
    alias_b = Alias.create("task", "11b", "lib", mou=mou_b)

    assert alias_b.target == "11b"
    assert Alias.objects.filter(item_type="task", alias="lib").count() == 2


def test_alias_create_for_a_url():
    alias = Alias.create("url", "https://github.com/collective/icalendar", "ical")

    assert alias.item_type == "url"
    assert alias.target == "https://github.com/collective/icalendar"
    assert alias.mou is None


def test_alias_create_for_a_url_rejects_an_alias_containing_a_scheme():
    with pytest.raises(ValueError, match="must not contain '://'"):
        Alias.create(
            "url",
            "https://github.com/collective/icalendar",
            "https://example.com",
        )


def test_alias_create_for_a_url_steals_an_alias_already_used_elsewhere():
    Alias.create("url", "https://github.com/collective/icalendar", "ical")

    moved = Alias.create("url", "https://github.com/pycalendar/other", "ical")

    assert moved.target == "https://github.com/pycalendar/other"
    assert Alias.objects.filter(item_type="url", alias="ical").count() == 1


def test_alias_create_replaces_a_targets_existing_alias():
    mou = MoU.objects.create(name="nlnet-2026")
    Task.objects.create(mou=mou, name="10a")
    Alias.create("task", "10a", "old", mou=mou)

    replacement = Alias.create("task", "10a", "new", mou=mou)

    assert replacement.alias == "new"
    assert Alias.objects.filter(item_type="task", mou=mou, target="10a").count() == 1
    assert not Alias.objects.filter(item_type="task", mou=mou, alias="old").exists()


def test_mou_display_name_without_an_alias_is_just_the_name():
    mou = MoU.objects.create(name="nlnet-2026")

    assert mou.display_name == "nlnet-2026"


def test_mou_display_name_appends_its_alias():
    mou = MoU.objects.create(name="nlnet-2026")
    Alias.create("mou", "nlnet-2026", "og")

    assert mou.display_name == "nlnet-2026 (og)"


def test_task_display_name_without_an_alias_is_just_the_name():
    mou = MoU.objects.create(name="nlnet-2026")
    task = Task.objects.create(mou=mou, name="10a")

    assert task.display_name == "10a"


def test_task_display_name_appends_its_alias():
    mou = MoU.objects.create(name="nlnet-2026")
    task = Task.objects.create(mou=mou, name="10a")
    Alias.create("task", "10a", "lib", mou=mou)

    assert task.display_name == "10a (lib)"


def test_task_display_name_ignores_an_alias_from_a_different_mou():
    mou_a = MoU.objects.create(name="nlnet-2025")
    mou_b = MoU.objects.create(name="nlnet-2026")
    Task.objects.create(mou=mou_a, name="10a")
    task_b = Task.objects.create(mou=mou_b, name="10a")
    Alias.create("task", "10a", "lib", mou=mou_a)

    assert task_b.display_name == "10a"


def test_resolve_mou_name_follows_an_alias():
    MoU.objects.create(name="nlnet-2026")
    Alias.create("mou", "nlnet-2026", "og")

    assert resolve_mou_name("og") == "nlnet-2026"


def test_resolve_mou_name_passes_through_an_unaliased_name():
    assert resolve_mou_name("nlnet-2026") == "nlnet-2026"


def test_resolve_task_name_follows_an_alias_scoped_to_its_mou():
    mou = MoU.objects.create(name="nlnet-2026")
    Task.objects.create(mou=mou, name="10a")
    Alias.create("task", "10a", "lib", mou=mou)

    assert resolve_task_name("lib", mou) == "10a"


def test_resolve_task_name_ignores_an_alias_from_a_different_mou():
    mou_a = MoU.objects.create(name="nlnet-2025")
    mou_b = MoU.objects.create(name="nlnet-2026")
    Task.objects.create(mou=mou_a, name="10a")
    Alias.create("task", "10a", "lib", mou=mou_a)

    assert resolve_task_name("lib", mou_b) == "lib"


def test_resolve_task_name_with_no_mou_passes_through():
    assert resolve_task_name("lib", None) == "lib"


def test_resolve_link_passes_through_a_real_url():
    assert (
        resolve_link("https://example.com/issues/1") == "https://example.com/issues/1"
    )


def test_resolve_link_passes_through_a_string_not_shaped_like_alias_slash_number():
    assert resolve_link("just-some-text") == "just-some-text"


def test_resolve_link_adds_https_to_a_bare_domain_link():
    assert (
        resolve_link("github.com/nlnet/rfp-recorder/issues/1")
        == "https://github.com/nlnet/rfp-recorder/issues/1"
    )


def test_resolve_link_adding_https_still_recognizes_a_real_alias():
    Alias.create("url", "https://github.com/collective/icalendar", "ical")

    with patch(
        "nlnet_rfp_recorder.timetracking.models.alias.classify_issue_or_pr",
        return_value="issues",
    ):
        # "ical/1782" has no dot in its first segment, so it's still an
        # alias lookup, not treated as a bare domain missing a scheme.
        result = resolve_link("ical/1782")

    assert result == "https://github.com/collective/icalendar/issues/1782"


def test_resolve_link_does_not_add_https_to_a_dotless_single_word():
    # No "/" at all, so there's no "first segment" that could look like a
    # domain - left alone, same as any other non-URL, non-alias text.
    assert resolve_link("notaurl") == "notaurl"


def test_resolve_link_fails_for_an_unregistered_alias():
    with pytest.raises(ValueError, match="No alias 'ical' for url"):
        resolve_link("ical/1782")


def test_resolve_link_expands_a_registered_alias():
    Alias.create("url", "https://github.com/collective/icalendar", "ical")

    with patch(
        "nlnet_rfp_recorder.timetracking.models.alias.classify_issue_or_pr",
        return_value="pull",
    ) as classify:
        result = resolve_link("ical/1782")

    classify.assert_called_once_with("collective", "icalendar", 1782, token=None)
    assert result == "https://github.com/collective/icalendar/pull/1782"


def test_resolve_link_sends_the_token_to_classify_issue_or_pr():
    Alias.create("url", "https://github.com/collective/icalendar", "ical")

    with patch(
        "nlnet_rfp_recorder.timetracking.models.alias.classify_issue_or_pr",
        return_value="issues",
    ) as classify:
        resolve_link("ical/1782", token="secret")

    classify.assert_called_once_with("collective", "icalendar", 1782, token="secret")
