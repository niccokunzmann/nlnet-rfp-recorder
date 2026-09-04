import asyncio
import inspect
import re
from collections.abc import Iterable
from dataclasses import dataclass
from enum import StrEnum
from typing import ClassVar

import niquests

GITHUB_API = "https://api.github.com"

ISSUE_URL = re.compile(
    r"^https://github\.com/(?P<owner>[^/]+)/(?P<repo>[^/]+)/issues/(?P<number>\d+)/?$"
)
PULL_REQUEST_URL = re.compile(
    r"^https://github\.com/(?P<owner>[^/]+)/(?P<repo>[^/]+)/pull/(?P<number>\d+)/?$"
)


class Status(StrEnum):
    OPEN = "open"
    CLOSED = "closed"
    NONEXISTENT = "nonexistent"


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
        response = niquests.get(f"{GITHUB_API}/{path}")
        if response.status_code == 404:
            return Status.NONEXISTENT
        response.raise_for_status()
        return Status(response.json()["state"])

    async def current_status_async(
        self, session: niquests.AsyncSession, token: str | None = None
    ) -> Status:
        path = f"repos/{self.owner}/{self.repo}/{self._api_resource}/{self.number}"
        headers = {"Authorization": f"Bearer {token}"} if token else None
        response = await session.get(f"{GITHUB_API}/{path}", headers=headers)
        if response.status_code == 404:
            return Status.NONEXISTENT
        response.raise_for_status()
        data = response.json()
        if inspect.isawaitable(data):
            data = await data
        return Status(data["state"])


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


async def fetch_statuses_async(
    references: Iterable[GitHubReference], token: str | None = None
) -> list[Status]:
    """Fetch current_status for every reference concurrently, one session."""
    async with niquests.AsyncSession() as session:
        return await asyncio.gather(
            *(
                reference.current_status_async(session, token)
                for reference in references
            )
        )


def fetch_statuses(
    references: Iterable[GitHubReference], token: str | None = None
) -> list[Status]:
    """Sync entry point for fetch_statuses_async."""
    return asyncio.run(fetch_statuses_async(list(references), token))
