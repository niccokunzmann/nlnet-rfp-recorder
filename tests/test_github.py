from unittest.mock import AsyncMock, MagicMock, Mock, patch

import niquests
import pytest

from nlnet_rfp_recorder.github import (
    TIMEOUT_SECONDS,
    Discussion,
    Issue,
    PullRequest,
    Status,
    classify_issue_or_pr,
    fetch_authenticated_login,
    fetch_issue_author,
    fetch_statuses,
    fetch_title,
    fetch_titles,
    parse_repo_url,
)


def _mock_session(state: str = "closed", status_code: int = 200):
    response = MagicMock(status_code=status_code)
    response.json = MagicMock(return_value={"state": state})
    session = MagicMock()
    session.get = AsyncMock(return_value=response)
    return session, response


def test_issue_from_url_parses_owner_repo_and_number():
    issue = Issue.from_url("https://github.com/nlnet/rfp-recorder/issues/42")

    assert issue == Issue(owner="nlnet", repo="rfp-recorder", number=42)


def test_issue_from_url_rejects_a_pull_request_url():
    assert Issue.from_url("https://github.com/nlnet/rfp-recorder/pull/42") is None


def test_issue_from_url_rejects_an_unrelated_url():
    assert Issue.from_url("https://example.com/issues/42") is None


def test_pull_request_from_url_parses_owner_repo_and_number():
    pr = PullRequest.from_url("https://github.com/nlnet/rfp-recorder/pull/7")

    assert pr == PullRequest(owner="nlnet", repo="rfp-recorder", number=7)


def test_pull_request_from_url_rejects_an_issue_url():
    assert (
        PullRequest.from_url("https://github.com/nlnet/rfp-recorder/issues/7") is None
    )


def test_discussion_from_url_parses_owner_repo_and_number():
    discussion = Discussion.from_url(
        "https://github.com/nlnet/rfp-recorder/discussions/3"
    )

    assert discussion == Discussion(owner="nlnet", repo="rfp-recorder", number=3)


def test_discussion_from_url_rejects_an_issue_url():
    assert Discussion.from_url("https://github.com/nlnet/rfp-recorder/issues/3") is None


def test_discussion_url_reconstructs_the_web_url():
    discussion = Discussion(owner="nlnet", repo="rfp-recorder", number=3)

    assert discussion.url == "https://github.com/nlnet/rfp-recorder/discussions/3"


def test_issue_current_status_queries_the_github_api():
    issue = Issue(owner="nlnet", repo="rfp-recorder", number=42)
    response = Mock(json=Mock(return_value={"state": "open"}))

    with patch("nlnet_rfp_recorder.github.niquests.get", return_value=response) as get:
        status = issue.current_status

    get.assert_called_once_with(
        "https://api.github.com/repos/nlnet/rfp-recorder/issues/42",
        timeout=TIMEOUT_SECONDS,
    )
    response.raise_for_status.assert_called_once()
    assert status == Status.OPEN


def test_pull_request_current_status_queries_the_github_api():
    pr = PullRequest(owner="nlnet", repo="rfp-recorder", number=7)
    response = Mock(json=Mock(return_value={"state": "closed"}))

    with patch("nlnet_rfp_recorder.github.niquests.get", return_value=response) as get:
        status = pr.current_status

    get.assert_called_once_with(
        "https://api.github.com/repos/nlnet/rfp-recorder/pulls/7",
        timeout=TIMEOUT_SECONDS,
    )
    response.raise_for_status.assert_called_once()
    assert status == Status.CLOSED


def test_issue_url_reconstructs_the_web_url():
    issue = Issue(owner="nlnet", repo="rfp-recorder", number=42)

    assert issue.url == "https://github.com/nlnet/rfp-recorder/issues/42"


def test_pull_request_url_reconstructs_the_web_url():
    pr = PullRequest(owner="nlnet", repo="rfp-recorder", number=7)

    assert pr.url == "https://github.com/nlnet/rfp-recorder/pull/7"


def test_current_status_is_nonexistent_when_not_found():
    issue = Issue(owner="nlnet", repo="rfp-recorder", number=999999)
    response = Mock(status_code=404)

    with patch("nlnet_rfp_recorder.github.niquests.get", return_value=response):
        status = issue.current_status

    response.raise_for_status.assert_not_called()
    assert status == Status.NONEXISTENT


def test_current_status_includes_githubs_own_message_on_error():
    issue = Issue(owner="collective", repo="icalendar", number=1559)
    response = MagicMock(status_code=403)
    response.raise_for_status = Mock(
        side_effect=niquests.exceptions.HTTPError("403 Client Error: Forbidden")
    )
    response.json = Mock(
        return_value={"message": "the organization forbids this token"}
    )

    with patch("nlnet_rfp_recorder.github.niquests.get", return_value=response):
        with pytest.raises(niquests.exceptions.HTTPError) as exc_info:
            issue.current_status

    message = str(exc_info.value)
    assert issue.url in message
    assert "403" in message
    assert "the organization forbids this token" in message


@pytest.mark.asyncio
async def test_current_status_async_queries_the_github_api():
    issue = Issue(owner="nlnet", repo="rfp-recorder", number=42)
    session, response = _mock_session("open")

    status = await issue.current_status_async(session)

    session.get.assert_called_once_with(
        "https://api.github.com/repos/nlnet/rfp-recorder/issues/42",
        headers=None,
        timeout=TIMEOUT_SECONDS,
    )
    response.raise_for_status.assert_called_once()
    assert status == Status.OPEN


@pytest.mark.asyncio
async def test_current_status_async_sends_the_token_as_a_bearer_header():
    issue = Issue(owner="nlnet", repo="rfp-recorder", number=42)
    session, _response = _mock_session("open")

    await issue.current_status_async(session, token="secret")

    session.get.assert_called_once_with(
        "https://api.github.com/repos/nlnet/rfp-recorder/issues/42",
        headers={"Authorization": "Bearer secret"},
        timeout=TIMEOUT_SECONDS,
    )


@pytest.mark.asyncio
async def test_current_status_async_is_nonexistent_when_not_found():
    issue = Issue(owner="nlnet", repo="rfp-recorder", number=999999)
    session, response = _mock_session(status_code=404)

    status = await issue.current_status_async(session)

    response.raise_for_status.assert_not_called()
    assert status == Status.NONEXISTENT


@pytest.mark.asyncio
async def test_current_status_async_includes_githubs_own_message_on_error():
    issue = Issue(owner="collective", repo="icalendar", number=1559)
    session, response = _mock_session(status_code=403)
    response.raise_for_status = Mock(
        side_effect=niquests.exceptions.HTTPError("403 Client Error: Forbidden")
    )
    response.json = Mock(
        return_value={"message": "the organization forbids this token"}
    )

    with pytest.raises(niquests.exceptions.HTTPError) as exc_info:
        await issue.current_status_async(session)

    message = str(exc_info.value)
    assert issue.url in message
    assert "403" in message
    assert "the organization forbids this token" in message


def test_fetch_statuses_runs_requests_concurrently_in_one_session():
    issue = Issue(owner="nlnet", repo="rfp-recorder", number=1)
    pr = PullRequest(owner="nlnet", repo="rfp-recorder", number=2)
    session, _response = _mock_session("closed")

    with patch(
        "nlnet_rfp_recorder.github.niquests.AsyncSession", return_value=session
    ) as async_session:
        async_session.return_value.__aenter__ = AsyncMock(return_value=session)
        async_session.return_value.__aexit__ = AsyncMock(return_value=False)
        statuses = fetch_statuses([issue, pr])

    assert statuses == [Status.CLOSED, Status.CLOSED]
    assert session.get.call_count == 2


@pytest.mark.asyncio
async def test_current_title_async_queries_the_github_api():
    issue = Issue(owner="nlnet", repo="rfp-recorder", number=42)
    response = MagicMock(status_code=200)
    response.json = MagicMock(return_value={"title": "Fix the thing"})
    session = MagicMock()
    session.get = AsyncMock(return_value=response)

    title = await issue.current_title_async(session)

    session.get.assert_called_once_with(
        "https://api.github.com/repos/nlnet/rfp-recorder/issues/42",
        headers=None,
        timeout=TIMEOUT_SECONDS,
    )
    assert title == "Fix the thing"


@pytest.mark.asyncio
async def test_current_title_async_returns_none_when_not_found():
    issue = Issue(owner="nlnet", repo="rfp-recorder", number=999999)
    response = MagicMock(status_code=404)
    session = MagicMock()
    session.get = AsyncMock(return_value=response)

    title = await issue.current_title_async(session)

    assert title is None


def test_fetch_titles_runs_requests_concurrently_in_one_session():
    issue = Issue(owner="nlnet", repo="rfp-recorder", number=1)
    pr = PullRequest(owner="nlnet", repo="rfp-recorder", number=2)
    response = MagicMock(status_code=200)
    response.json = MagicMock(return_value={"title": "Some title"})
    session = MagicMock()
    session.get = AsyncMock(return_value=response)

    with patch(
        "nlnet_rfp_recorder.github.niquests.AsyncSession", return_value=session
    ) as async_session:
        async_session.return_value.__aenter__ = AsyncMock(return_value=session)
        async_session.return_value.__aexit__ = AsyncMock(return_value=False)
        titles = fetch_titles([issue, pr])

    assert titles == ["Some title", "Some title"]
    assert session.get.call_count == 2


def test_fetch_titles_raises_after_every_request_finishes():
    # Same deadlock hazard as fetch_statuses: don't close the session
    # while a sibling request is still in flight because gather()
    # re-raised the first exception early.
    issue = Issue(owner="nlnet", repo="rfp-recorder", number=1)
    forbidden_pr = PullRequest(owner="nlnet", repo="rfp-recorder", number=2)
    ok_pr = PullRequest(owner="nlnet", repo="rfp-recorder", number=3)

    ok_response = MagicMock(status_code=200)
    ok_response.json = MagicMock(return_value={"title": "ok"})

    async def fake_get(url, **kwargs):
        if url.endswith("/pulls/2"):
            raise niquests.exceptions.HTTPError("boom")
        return ok_response

    session = MagicMock()
    session.get = AsyncMock(side_effect=fake_get)

    with patch(
        "nlnet_rfp_recorder.github.niquests.AsyncSession", return_value=session
    ) as async_session:
        async_session.return_value.__aenter__ = AsyncMock(return_value=session)
        async_session.return_value.__aexit__ = AsyncMock(return_value=False)
        with pytest.raises(niquests.exceptions.HTTPError):
            fetch_titles([issue, forbidden_pr, ok_pr])

    assert session.get.call_count == 3


def test_parse_repo_url_splits_owner_and_repo():
    assert parse_repo_url("https://github.com/collective/icalendar") == (
        "collective",
        "icalendar",
    )


def test_parse_repo_url_accepts_a_trailing_slash():
    assert parse_repo_url("https://github.com/collective/icalendar/") == (
        "collective",
        "icalendar",
    )


def test_parse_repo_url_rejects_a_non_repo_url():
    assert parse_repo_url("https://github.com/collective/icalendar/pull/1") is None
    assert parse_repo_url("https://example.com/collective/icalendar") is None


def test_classify_issue_or_pr_returns_issues_for_a_plain_issue():
    response = Mock(status_code=200, json=Mock(return_value={"number": 1782}))

    with patch("nlnet_rfp_recorder.github.niquests.get", return_value=response) as get:
        result = classify_issue_or_pr("collective", "icalendar", 1782)

    get.assert_called_once_with(
        "https://api.github.com/repos/collective/icalendar/issues/1782",
        headers=None,
        timeout=TIMEOUT_SECONDS,
    )
    assert result == "issues"


def test_classify_issue_or_pr_returns_pull_when_the_number_is_a_pr():
    response = Mock(
        status_code=200, json=Mock(return_value={"number": 1782, "pull_request": {}})
    )

    with patch("nlnet_rfp_recorder.github.niquests.get", return_value=response):
        result = classify_issue_or_pr("collective", "icalendar", 1782)

    assert result == "pull"


def test_classify_issue_or_pr_sends_the_token_as_a_bearer_header():
    response = Mock(status_code=200, json=Mock(return_value={"number": 1}))

    with patch("nlnet_rfp_recorder.github.niquests.get", return_value=response) as get:
        classify_issue_or_pr("collective", "icalendar", 1, token="secret")

    get.assert_called_once_with(
        "https://api.github.com/repos/collective/icalendar/issues/1",
        headers={"Authorization": "Bearer secret"},
        timeout=TIMEOUT_SECONDS,
    )


def test_classify_issue_or_pr_assumes_issue_when_not_found():
    response = Mock(status_code=404)

    with patch("nlnet_rfp_recorder.github.niquests.get", return_value=response):
        result = classify_issue_or_pr("collective", "icalendar", 999999)

    assert result == "issues"


def test_classify_issue_or_pr_assumes_issue_when_offline():
    with patch(
        "nlnet_rfp_recorder.github.niquests.get",
        side_effect=niquests.exceptions.ConnectionError("no network"),
    ):
        result = classify_issue_or_pr("collective", "icalendar", 1782)

    assert result == "issues"


def test_classify_issue_or_pr_raises_on_a_real_error():
    response = Mock(status_code=403)
    response.raise_for_status = Mock(
        side_effect=niquests.exceptions.HTTPError("403 Client Error: Forbidden")
    )

    with patch("nlnet_rfp_recorder.github.niquests.get", return_value=response):
        with pytest.raises(niquests.exceptions.HTTPError):
            classify_issue_or_pr("collective", "icalendar", 1782)


def test_fetch_title_returns_the_title():
    response = Mock(status_code=200, json=Mock(return_value={"title": "Fix the thing"}))

    with patch("nlnet_rfp_recorder.github.niquests.get", return_value=response) as get:
        result = fetch_title("collective", "icalendar", 1782)

    get.assert_called_once_with(
        "https://api.github.com/repos/collective/icalendar/issues/1782",
        headers=None,
        timeout=TIMEOUT_SECONDS,
    )
    assert result == "Fix the thing"


def test_fetch_title_sends_the_token_as_a_bearer_header():
    response = Mock(status_code=200, json=Mock(return_value={"title": "x"}))

    with patch("nlnet_rfp_recorder.github.niquests.get", return_value=response) as get:
        fetch_title("collective", "icalendar", 1782, token="secret")

    get.assert_called_once_with(
        "https://api.github.com/repos/collective/icalendar/issues/1782",
        headers={"Authorization": "Bearer secret"},
        timeout=TIMEOUT_SECONDS,
    )


def test_fetch_title_returns_none_when_not_found():
    response = Mock(status_code=404)

    with patch("nlnet_rfp_recorder.github.niquests.get", return_value=response):
        result = fetch_title("collective", "icalendar", 999999)

    assert result is None


def test_fetch_title_returns_none_on_a_server_error():
    response = Mock(status_code=500)

    with patch("nlnet_rfp_recorder.github.niquests.get", return_value=response):
        result = fetch_title("collective", "icalendar", 1782)

    assert result is None


def test_fetch_title_returns_none_when_offline():
    with patch(
        "nlnet_rfp_recorder.github.niquests.get",
        side_effect=niquests.exceptions.ConnectionError("no network"),
    ):
        result = fetch_title("collective", "icalendar", 1782)

    assert result is None


def test_fetch_issue_author_returns_the_login():
    response = Mock(
        status_code=200, json=Mock(return_value={"user": {"login": "octocat"}})
    )

    with patch("nlnet_rfp_recorder.github.niquests.get", return_value=response) as get:
        result = fetch_issue_author("collective", "icalendar", 1782)

    get.assert_called_once_with(
        "https://api.github.com/repos/collective/icalendar/issues/1782",
        headers=None,
        timeout=TIMEOUT_SECONDS,
    )
    assert result == "octocat"


def test_fetch_issue_author_sends_the_token_as_a_bearer_header():
    response = Mock(
        status_code=200, json=Mock(return_value={"user": {"login": "octocat"}})
    )

    with patch("nlnet_rfp_recorder.github.niquests.get", return_value=response) as get:
        fetch_issue_author("collective", "icalendar", 1782, token="secret")

    get.assert_called_once_with(
        "https://api.github.com/repos/collective/icalendar/issues/1782",
        headers={"Authorization": "Bearer secret"},
        timeout=TIMEOUT_SECONDS,
    )


def test_fetch_issue_author_returns_none_when_not_found():
    response = Mock(status_code=404)

    with patch("nlnet_rfp_recorder.github.niquests.get", return_value=response):
        result = fetch_issue_author("collective", "icalendar", 999999)

    assert result is None


def test_fetch_issue_author_returns_none_on_unexpected_response_shape():
    response = Mock(status_code=200, json=Mock(return_value={"no": "user field"}))

    with patch("nlnet_rfp_recorder.github.niquests.get", return_value=response):
        result = fetch_issue_author("collective", "icalendar", 1782)

    assert result is None


def test_fetch_issue_author_returns_none_when_offline():
    with patch(
        "nlnet_rfp_recorder.github.niquests.get",
        side_effect=niquests.exceptions.ConnectionError("no network"),
    ):
        result = fetch_issue_author("collective", "icalendar", 1782)

    assert result is None


def test_fetch_authenticated_login_returns_the_login():
    response = Mock(status_code=200, json=Mock(return_value={"login": "octocat"}))

    with patch("nlnet_rfp_recorder.github.niquests.get", return_value=response):
        result = fetch_authenticated_login("secret")

    assert result == "octocat"


def test_fetch_authenticated_login_returns_none_when_rejected():
    response = Mock(status_code=401)

    with patch("nlnet_rfp_recorder.github.niquests.get", return_value=response):
        result = fetch_authenticated_login("bad-token")

    assert result is None


def test_fetch_statuses_raises_after_every_request_finishes():
    # Regression test: gather() used to raise the first HTTPError while
    # sibling requests were still in flight, and closing the session out
    # from under them then deadlocked forever instead of raising. All
    # requests must complete before the exception surfaces.
    issue = Issue(owner="nlnet", repo="rfp-recorder", number=1)
    forbidden_pr = PullRequest(owner="nlnet", repo="rfp-recorder", number=2)
    closed_pr = PullRequest(owner="nlnet", repo="rfp-recorder", number=3)

    ok_response = MagicMock(status_code=200)
    ok_response.json = MagicMock(return_value={"state": "closed"})
    forbidden_response = MagicMock(status_code=200)
    forbidden_response.raise_for_status = Mock(
        side_effect=niquests.exceptions.HTTPError("403 Client Error: Forbidden")
    )

    async def fake_get(url, **kwargs):
        return forbidden_response if url.endswith("/pulls/2") else ok_response

    session = MagicMock()
    session.get = AsyncMock(side_effect=fake_get)

    with patch(
        "nlnet_rfp_recorder.github.niquests.AsyncSession", return_value=session
    ) as async_session:
        async_session.return_value.__aenter__ = AsyncMock(return_value=session)
        async_session.return_value.__aexit__ = AsyncMock(return_value=False)
        with pytest.raises(niquests.exceptions.HTTPError):
            fetch_statuses([issue, forbidden_pr, closed_pr])

    assert session.get.call_count == 3
