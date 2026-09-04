import warnings
from datetime import datetime, timedelta
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from django.core.exceptions import ValidationError
from django.utils import timezone

from nlnet_rfp_recorder.github import Issue, PullRequest
from nlnet_rfp_recorder.timesheet import TimesheetRow
from nlnet_rfp_recorder.timetracking.models import (
    GitHubToken,
    Link,
    MoU,
    Task,
    TaskWarning,
    TimeRecord,
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
    )


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
