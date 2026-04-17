from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any

import httpx

BOUNTY_LABELS = {
    "bug-bounty", "bounty", "hacktoberfest", "good-first-issue", "help wanted",
    "enhancement", "feature", "bug", "up-for-grabs", "beginner-friendly",
}
BOUNTY_KEYWORDS = re.compile(r"\$\s*\d+|\b(bounty|reward|prize|payout)\b", re.IGNORECASE)
ACTIVE_SIGNALS = re.compile(
    r"\b(working on|in progress|wip|i('ll| will) fix|taking this|assigned to me|pr #\d+)\b",
    re.IGNORECASE,
)


@dataclass
class Issue:
    number: int
    title: str
    body: str
    labels: list[str]
    assignees: list[str]
    html_url: str
    bounty_amount: str = ""
    repo: str = ""


@dataclass
class GitHubClient:
    token: str
    base_url: str = "https://api.github.com"
    _headers: dict[str, str] = field(init=False)

    def __post_init__(self) -> None:
        self._headers = {
            "Authorization": f"Bearer {self.token}",
            "Accept": "application/vnd.github+json",
            "X-GitHub-Api-Version": "2022-11-28",
        }

    async def _get(self, path: str, params: dict[str, Any] | None = None) -> Any:
        async with httpx.AsyncClient(headers=self._headers, timeout=30) as client:
            r = await client.get(f"{self.base_url}{path}", params=params)
            r.raise_for_status()
            return r.json()

    async def _post(self, path: str, json: dict[str, Any]) -> Any:
        async with httpx.AsyncClient(headers=self._headers, timeout=30) as client:
            r = await client.post(f"{self.base_url}{path}", json=json)
            r.raise_for_status()
            return r.json()

    async def get_authenticated_user(self) -> str:
        data = await self._get("/user")
        return data["login"]

    async def check_repo_legitimacy(self, owner: str, repo: str) -> tuple[bool, str]:
        """
        Returns (is_legitimate, reason). Flags repos that look like bounty farms:
        - Created less than 60 days ago with low stars
        - Fork ratio >> star ratio (bots forking to claim)
        - All issues posted by a single user
        """
        from datetime import datetime, timezone
        data = await self._get(f"/repos/{owner}/{repo}")
        stars = data.get("stargazers_count", 0)
        forks = data.get("forks_count", 0)
        created_at = data.get("created_at", "")
        age_days = 0
        if created_at:
            created = datetime.fromisoformat(created_at.replace("Z", "+00:00"))
            age_days = (datetime.now(timezone.utc) - created).days

        if age_days < 60 and stars < 5:
            return False, f"repo is only {age_days} days old with {stars} stars"

        if stars > 0 and forks / stars > 20:
            return False, f"suspicious fork/star ratio ({forks} forks, {stars} stars) — likely a bounty farm"

        issues = await self._get(f"/repos/{owner}/{repo}/issues", params={"state": "open", "per_page": 30})
        creators = {i["user"]["login"] for i in issues if not i.get("pull_request")}
        if len(creators) == 1 and len(issues) > 5:
            return False, f"all {len(issues)} issues created by a single user ({next(iter(creators))})"

        return True, "ok"

    async def search_issues(self, query: str, per_page: int = 50) -> list[Issue]:
        """Search GitHub issues using the search API."""
        data = await self._get(
            "/search/issues",
            params={"q": query, "per_page": per_page, "sort": "updated", "order": "desc"},
        )
        issues = []
        for item in data.get("items", []):
            if item.get("pull_request"):
                continue
            labels = [lb["name"].lower() for lb in item.get("labels", [])]
            body = item.get("body") or ""
            title = item.get("title") or ""
            amount_match = re.search(r"\$\s*(\d[\d,]*)", title + " " + body)
            bounty_amount = f"${amount_match.group(1)}" if amount_match else "unknown"
            repo_url = item.get("repository_url", "")
            repo_full = "/".join(repo_url.split("/")[-2:]) if repo_url else "unknown/unknown"
            issues.append(
                Issue(
                    number=item["number"],
                    title=title,
                    body=body,
                    labels=labels,
                    assignees=[a["login"] for a in item.get("assignees", [])],
                    html_url=item["html_url"],
                    bounty_amount=bounty_amount,
                    repo=repo_full,
                )
            )
        return issues

    async def list_bounty_issues(self, owner: str, repo: str) -> list[Issue]:
        data = await self._get(
            f"/repos/{owner}/{repo}/issues",
            params={"state": "open", "per_page": 100},
        )
        issues = []
        for item in data:
            if item.get("pull_request"):
                continue
            labels = [lb["name"].lower() for lb in item.get("labels", [])]
            body = item.get("body") or ""
            title = item.get("title") or ""
            has_label = bool(BOUNTY_LABELS.intersection(labels))
            has_keyword = bool(BOUNTY_KEYWORDS.search(title) or BOUNTY_KEYWORDS.search(body))
            if not (has_label or has_keyword):
                continue
            amount_match = re.search(r"\$\s*(\d[\d,]*)", title + " " + body)
            bounty_amount = f"${amount_match.group(1)}" if amount_match else "unknown"
            issues.append(
                Issue(
                    number=item["number"],
                    title=title,
                    body=body,
                    labels=labels,
                    assignees=[a["login"] for a in item.get("assignees", [])],
                    html_url=item["html_url"],
                    bounty_amount=bounty_amount,
                    repo=f"{owner}/{repo}",
                )
            )
        return issues

    async def is_actively_worked(self, owner: str, repo: str, issue: Issue) -> bool:
        if issue.assignees:
            return True
        comments = await self._get(f"/repos/{owner}/{repo}/issues/{issue.number}/comments")
        for c in comments:
            if ACTIVE_SIGNALS.search(c.get("body") or ""):
                return True
        linked = await self._get(
            f"/repos/{owner}/{repo}/issues/{issue.number}/timeline",
            params={"per_page": 30},
        )
        for event in linked:
            if event.get("event") in ("cross-referenced", "connected"):
                return True
        return False

    async def fork_repo(self, owner: str, repo: str) -> str:
        data = await self._post(f"/repos/{owner}/{repo}/forks", json={})
        return data["full_name"]

    async def create_pull_request(
        self,
        *,
        owner: str,
        repo: str,
        title: str,
        body: str,
        head: str,
        base: str = "main",
    ) -> str:
        data = await self._post(
            f"/repos/{owner}/{repo}/pulls",
            json={"title": title, "body": body, "head": head, "base": base},
        )
        return data["html_url"]

    async def get_repo_default_branch(self, owner: str, repo: str) -> str:
        data = await self._get(f"/repos/{owner}/{repo}")
        return data.get("default_branch", "main")

    async def get_file_tree(self, owner: str, repo: str, branch: str) -> list[str]:
        data = await self._get(
            f"/repos/{owner}/{repo}/git/trees/{branch}",
            params={"recursive": "1"},
        )
        return [item["path"] for item in data.get("tree", []) if item["type"] == "blob"]

    async def get_file_content(self, owner: str, repo: str, path: str, ref: str) -> str:
        import base64
        data = await self._get(
            f"/repos/{owner}/{repo}/contents/{path}",
            params={"ref": ref},
        )
        if data.get("encoding") == "base64":
            return base64.b64decode(data["content"]).decode("utf-8", errors="replace")
        return data.get("content", "")
