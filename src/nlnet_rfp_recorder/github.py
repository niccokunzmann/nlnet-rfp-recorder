import asyncio
import inspect
import re
from collections.abc import Iterable
from dataclasses import dataclass
from enum import StrEnum
from typing import ClassVar

import niquests

GITHUB_API = "https://api.github.com"
TIMEOUT_SECONDS = 10

ISSUE_URL = re.compile(
    r"^https://github\.com/(?P<owner>[^/]+)/(?P<repo>[^/]+)/issues/(?P<number>\d+)/?$"
)
PULL_REQUEST_URL = re.compile(
    r"^https://github\.com/(?P<owner>[^/]+)/(?P<repo>[^/]+)/pull/(?P<number>\d+)/?$"
)
DISCUSSION_URL = re.compile(
    r"^https://github\.com/(?P<owner>[^/]+)/(?P<repo>[^/]+)/discussions/(?P<number>\d+)/?$"
)
REPO_URL = re.compile(r"^https://github\.com/(?P<owner>[^/]+)/(?P<repo>[^/]+?)/?$")


def parse_repo_url(url: str) -> tuple[str, str] | None:
    """Split a bare repo URL ("https://github.com/OWNER/REPO") into its parts."""
    match = REPO_URL.match(url)
    if match is None:
        return None
    return match["owner"], match["repo"]


class Status(StrEnum):
    OPEN = "open"
    CLOSED = "closed"
    NONEXISTENT = "nonexistent"


def _detail_from_response(response: niquests.Response) -> str | None:
    """Extract GitHub's own explanation from an error response body, if any."""
    try:
        return response.json().get("message")
    except Exception:
        return None


async def _detail_from_response_async(response: niquests.Response) -> str | None:
    try:
        data = response.json()
        if inspect.isawaitable(data):
            data = await data
        return data.get("message")
    except Exception:
        return None


def _github_error(
    response: niquests.Response, url: str, detail: str | None
) -> Exception:
    suffix = f": {detail}" if detail else ""
    return niquests.exceptions.HTTPError(
        f"GitHub rejected the request for {url} ({response.status_code}){suffix}"
    )


def fetch_authenticated_login(token: str) -> str | None:
    """Return the GitHub login `token` authenticates as, or None if rejected."""
    response = niquests.get(
        f"{GITHUB_API}/user",
        headers={"Authorization": f"Bearer {token}"},
        timeout=TIMEOUT_SECONDS,
    )
    if response.status_code == 401:
        return None
    response.raise_for_status()
    return response.json()["login"]


def classify_issue_or_pr(
    owner: str, repo: str, number: int, token: str | None = None
) -> str:
    """Return 'pull' or 'issues' - the URL path segment identifying `number`.

    GitHub's /issues/{number} endpoint returns pull requests too (with a
    "pull_request" key), so one request classifies both. Falls back to
    "issues" if GitHub can't be reached at all - a network problem shouldn't
    block recording time against a link.
    """
    headers = {"Authorization": f"Bearer {token}"} if token else None
    try:
        response = niquests.get(
            f"{GITHUB_API}/repos/{owner}/{repo}/issues/{number}",
            headers=headers,
            timeout=TIMEOUT_SECONDS,
        )
    except niquests.exceptions.RequestException:
        return "issues"
    if response.status_code == 404:
        return "issues"
    response.raise_for_status()
    return "pull" if "pull_request" in response.json() else "issues"


def fetch_title(
    owner: str, repo: str, number: int, token: str | None = None
) -> str | None:
    """Fetch an issue or PR's title, or None if it can't be found or reached."""
    headers = {"Authorization": f"Bearer {token}"} if token else None
    try:
        response = niquests.get(
            f"{GITHUB_API}/repos/{owner}/{repo}/issues/{number}",
            headers=headers,
            timeout=TIMEOUT_SECONDS,
        )
    except niquests.exceptions.RequestException:
        return None
    if response.status_code != 200:
        return None
    return response.json().get("title")


def fetch_issue_author(
    owner: str, repo: str, number: int, token: str | None = None
) -> str | None:
    """Fetch an issue or PR's author login, or None if it can't be told.

    None on any failure (offline, not found, unexpected response shape) -
    the caller decides what to assume when authorship can't be confirmed.
    """
    headers = {"Authorization": f"Bearer {token}"} if token else None
    try:
        response = niquests.get(
            f"{GITHUB_API}/repos/{owner}/{repo}/issues/{number}",
            headers=headers,
            timeout=TIMEOUT_SECONDS,
        )
    except niquests.exceptions.RequestException:
        return None
    if response.status_code != 200:
        return None
    try:
        return response.json()["user"]["login"]
    except KeyError, TypeError:
        return None


@dataclass
class GitHubReference:
    owner: str
    repo: str
    number: int

    _api_resource: ClassVar[str]
    _web_path: ClassVar[str]

    @property
    def url(self) -> str:
        return f"https://github.com/{self.owner}/{self.repo}/{self._web_path}/{self.number}"

    @property
    def current_status(self) -> Status:
        path = f"repos/{self.owner}/{self.repo}/{self._api_resource}/{self.number}"
        response = niquests.get(f"{GITHUB_API}/{path}", timeout=TIMEOUT_SECONDS)
        if response.status_code == 404:
            return Status.NONEXISTENT
        try:
            response.raise_for_status()
        except niquests.exceptions.HTTPError as error:
            raise _github_error(
                response, self.url, _detail_from_response(response)
            ) from error
        return Status(response.json()["state"])

    async def current_status_async(
        self, session: niquests.AsyncSession, token: str | None = None
    ) -> Status:
        path = f"repos/{self.owner}/{self.repo}/{self._api_resource}/{self.number}"
        headers = {"Authorization": f"Bearer {token}"} if token else None
        response = await session.get(
            f"{GITHUB_API}/{path}", headers=headers, timeout=TIMEOUT_SECONDS
        )
        if response.status_code == 404:
            return Status.NONEXISTENT
        try:
            response.raise_for_status()
        except niquests.exceptions.HTTPError as error:
            raise _github_error(
                response, self.url, await _detail_from_response_async(response)
            ) from error
        data = response.json()
        if inspect.isawaitable(data):
            data = await data
        return Status(data["state"])

    async def current_title_async(
        self, session: niquests.AsyncSession, token: str | None = None
    ) -> str | None:
        path = f"repos/{self.owner}/{self.repo}/{self._api_resource}/{self.number}"
        headers = {"Authorization": f"Bearer {token}"} if token else None
        response = await session.get(
            f"{GITHUB_API}/{path}", headers=headers, timeout=TIMEOUT_SECONDS
        )
        if response.status_code != 200:
            return None
        data = response.json()
        if inspect.isawaitable(data):
            data = await data
        return data.get("title")


@dataclass
class Issue(GitHubReference):
    _api_resource: ClassVar[str] = "issues"
    _web_path: ClassVar[str] = "issues"

    @classmethod
    def from_url(cls, url: str) -> Issue | None:
        match = ISSUE_URL.match(url)
        if match is None:
            return None
        return cls(
            owner=match["owner"], repo=match["repo"], number=int(match["number"])
        )


@dataclass
class PullRequest(GitHubReference):
    _api_resource: ClassVar[str] = "pulls"
    _web_path: ClassVar[str] = "pull"

    @classmethod
    def from_url(cls, url: str) -> PullRequest | None:
        match = PULL_REQUEST_URL.match(url)
        if match is None:
            return None
        return cls(
            owner=match["owner"], repo=match["repo"], number=int(match["number"])
        )


@dataclass
class Discussion(GitHubReference):
    _api_resource: ClassVar[str] = "discussions"
    _web_path: ClassVar[str] = "discussions"

    @classmethod
    def from_url(cls, url: str) -> Discussion | None:
        match = DISCUSSION_URL.match(url)
        if match is None:
            return None
        return cls(
            owner=match["owner"], repo=match["repo"], number=int(match["number"])
        )


async def fetch_statuses_async(
    references: Iterable[GitHubReference], token: str | None = None
) -> list[Status]:
    """Fetch current_status for every reference concurrently, one session.

    Uses return_exceptions=True so a single failing request doesn't leave
    its siblings orphaned mid-flight when gather re-raises early - closing
    the session out from under still-running requests deadlocks instead of
    raising. The first exception (if any) is re-raised once every request
    has actually finished.
    """
    async with niquests.AsyncSession() as session:
        results = await asyncio.gather(
            *(
                reference.current_status_async(session, token)
                for reference in references
            ),
            return_exceptions=True,
        )
    for result in results:
        if isinstance(result, BaseException):
            raise result
    return results


def fetch_statuses(
    references: Iterable[GitHubReference], token: str | None = None
) -> list[Status]:
    """Sync entry point for fetch_statuses_async."""
    return asyncio.run(fetch_statuses_async(list(references), token))


async def fetch_titles_async(
    references: Iterable[GitHubReference], token: str | None = None
) -> list[str | None]:
    """Fetch current_title_async for every reference concurrently, one session.

    Same return_exceptions=True reasoning as fetch_statuses_async: closing
    the session out from under still-running requests (because gather
    re-raised early) deadlocks instead of raising.
    """
    async with niquests.AsyncSession() as session:
        results = await asyncio.gather(
            *(
                reference.current_title_async(session, token)
                for reference in references
            ),
            return_exceptions=True,
        )
    for result in results:
        if isinstance(result, BaseException):
            raise result
    return results


def fetch_titles(
    references: Iterable[GitHubReference], token: str | None = None
) -> list[str | None]:
    """Sync entry point for fetch_titles_async."""
    return asyncio.run(fetch_titles_async(list(references), token))
