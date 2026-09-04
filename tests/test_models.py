import warnings
from datetime import datetime, timedelta
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from django.core.exceptions import ValidationError
from django.utils import timezone

from nlnet_rfp_recorder.github import TIMEOUT_SECONDS, Issue, PullRequest
from nlnet_rfp_recorder.timesheet import TimesheetRow
from nlnet_rfp_recorder.timetracking.models import (
    GitHubToken,
    Link,
    MoU,
    Report,
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
    assert record.report == report


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
    assert record.report is None


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
    assert record.report is None


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
        "    - https://github.com/nlnet/rfp-recorder/issues/1\n"
        "\n"
        "10a: 20€\n"
        "  Issues:\n"
        "    - https://github.com/nlnet/rfp-recorder/issues/2\n"
        "    - https://github.com/nlnet/rfp-recorder/issues/5\n"
        "\n"
        "Total: 30€"
    )


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
