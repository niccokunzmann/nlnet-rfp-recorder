from unittest.mock import AsyncMock, MagicMock, Mock, patch

import pytest

from nlnet_rfp_recorder.github import Issue, PullRequest, Status, fetch_statuses


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


def test_issue_current_status_queries_the_github_api():
    issue = Issue(owner="nlnet", repo="rfp-recorder", number=42)
    response = Mock(json=Mock(return_value={"state": "open"}))

    with patch("nlnet_rfp_recorder.github.niquests.get", return_value=response) as get:
        status = issue.current_status

    get.assert_called_once_with(
        "https://api.github.com/repos/nlnet/rfp-recorder/issues/42"
    )
    response.raise_for_status.assert_called_once()
    assert status == Status.OPEN


def test_pull_request_current_status_queries_the_github_api():
    pr = PullRequest(owner="nlnet", repo="rfp-recorder", number=7)
    response = Mock(json=Mock(return_value={"state": "closed"}))

    with patch("nlnet_rfp_recorder.github.niquests.get", return_value=response) as get:
        status = pr.current_status

    get.assert_called_once_with(
        "https://api.github.com/repos/nlnet/rfp-recorder/pulls/7"
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


@pytest.mark.asyncio
async def test_current_status_async_queries_the_github_api():
    issue = Issue(owner="nlnet", repo="rfp-recorder", number=42)
    session, response = _mock_session("open")

    status = await issue.current_status_async(session)

    session.get.assert_called_once_with(
        "https://api.github.com/repos/nlnet/rfp-recorder/issues/42", headers=None
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
    )


@pytest.mark.asyncio
async def test_current_status_async_is_nonexistent_when_not_found():
    issue = Issue(owner="nlnet", repo="rfp-recorder", number=999999)
    session, response = _mock_session(status_code=404)

    status = await issue.current_status_async(session)

    response.raise_for_status.assert_not_called()
    assert status == Status.NONEXISTENT


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
