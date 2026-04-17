from __future__ import annotations

import json
import os
import re
import subprocess
import tempfile
from pathlib import Path
from typing import Any

from rich.console import Console
from rich.panel import Panel
from rich.prompt import Confirm
from rich.table import Table

from agent_army.bounty_hunter.db import BountyDB
from agent_army.bounty_hunter.github import GitHubClient, Issue
from agent_army.services.ollama import OllamaClient

_FILE_ID_PROMPT = """\
You are a senior software engineer. A GitHub issue is shown below.
Given the repository file tree, identify the 3-5 files most likely to need changes to fix this issue.
Respond with JSON only: {{"files": ["path/to/file.py", ...]}}

Issue #{number}: {title}
{body}

File tree (first 200 files):
{tree}
"""

_FIX_PROMPT = """\
You are a senior software engineer fixing a GitHub issue.
Produce a JSON response with the following structure:
{{
  "analysis": "brief explanation of root cause",
  "files": [
    {{"path": "relative/path/to/file", "content": "complete new file content as a string"}}
  ],
  "pr_title": "Fix: concise title",
  "pr_body": "markdown PR description referencing the issue"
}}

Issue #{number}: {title}
{body}

Current file contents:
{files}

Rules:
- Output ONLY valid JSON, no markdown fences.
- Each file entry must have the complete new content (not a diff).
- If a file doesn't need changes, omit it from the list.
- Keep pr_body under 400 chars.
"""


class BountyHunter:
    def __init__(
        self,
        *,
        github: GitHubClient,
        ollama: OllamaClient,
        db: BountyDB,
        model: str,
        console: Console,
    ) -> None:
        self._gh = github
        self._ollama = ollama
        self._db = db
        self._model = model
        self._console = console

    async def hunt(self, repo: str) -> None:
        owner, name = repo.split("/", 1)
        self._console.print(f"[bold cyan]Scanning {repo} for bounty issues...[/bold cyan]")
        issues = await self._gh.list_bounty_issues(owner, name)
        await self._filter_and_work(issues, default_repo=repo)

    async def hunt_org(self, org: str) -> None:
        self._console.print(f"[bold cyan]Searching org {org} for bounty issues...[/bold cyan]")
        queries = [
            f'org:{org} is:issue is:open label:"bug-bounty"',
            f'org:{org} is:issue is:open label:"bounty"',
            f'org:{org} is:issue is:open label:"help wanted"',
            f'org:{org} is:issue is:open label:"good-first-issue"',
            f'org:{org} is:issue is:open bounty in:title,body',
        ]
        seen: set[str] = set()
        issues: list[Issue] = []
        for q in queries:
            try:
                results = await self._gh.search_issues(q, per_page=30)
                for i in results:
                    key = i.html_url
                    if key not in seen:
                        seen.add(key)
                        issues.append(i)
            except Exception as e:
                self._console.print(f"[dim]Search query failed: {e}[/dim]")
        self._console.print(f"Found [bold]{len(issues)}[/bold] candidate(s).")
        await self._filter_and_work(issues)

    async def hunt_search(self, query: str) -> None:
        self._console.print(f"[bold cyan]Searching: {query}[/bold cyan]")
        issues = await self._gh.search_issues(query, per_page=50)
        self._console.print(f"Found [bold]{len(issues)}[/bold] candidate(s).")
        await self._filter_and_work(issues)

    async def _filter_and_work(self, issues: list[Issue], default_repo: str = "") -> None:
        if not issues:
            self._console.print("[yellow]No bounty issues found.[/yellow]")
            return

        self._console.print("Checking activity...")
        candidates: list[Issue] = []
        for issue in issues:
            repo = issue.repo or default_repo
            if not repo or "/" not in repo:
                candidates.append(issue)
                continue
            owner, name = repo.split("/", 1)
            active = await self._gh.is_actively_worked(owner, name, issue)
            if active:
                self._console.print(f"  [dim]#{issue.number} {repo} skipped — actively worked[/dim]")
            else:
                candidates.append(issue)

        if not candidates:
            self._console.print("[yellow]All issues are actively being worked on.[/yellow]")
            return

        for issue in candidates:
            repo = issue.repo or default_repo
            self._console.print()
            self._console.print(Panel(
                f"[bold]#{issue.number}[/bold] {issue.title}\n"
                f"[dim]{issue.html_url}[/dim]\n\n"
                f"{(issue.body or '')[:500]}"
                + ("..." if len(issue.body or '') > 500 else ""),
                title=f"[cyan]{repo}[/cyan]  Bounty: [green]{issue.bounty_amount}[/green]  Labels: {', '.join(issue.labels) or 'none'}",
                border_style="cyan",
            ))

            if not Confirm.ask("Work on this issue?", default=False):
                continue

            record_id = self._db.log(
                repo=repo,
                issue_number=issue.number,
                issue_title=issue.title,
                issue_url=issue.html_url,
                bounty_amount=issue.bounty_amount,
                status="in_progress",
            )

            if "/" not in repo:
                self._console.print("[red]Cannot determine repo for this issue — skipping fix.[/red]")
                self._db.update(record_id, status="failed")
                continue

            owner, name = repo.split("/", 1)
            pr_url = await self._fix_and_submit(owner, name, issue)
            if pr_url:
                self._db.update(record_id, status="pr_submitted", pr_url=pr_url)
                self._console.print(f"[bold green]PR submitted:[/bold green] {pr_url}")
            else:
                self._db.update(record_id, status="failed")
                self._console.print("[red]Failed to submit PR for this issue.[/red]")

    async def _fix_and_submit(self, owner: str, repo: str, issue: Issue) -> str:
        self._console.print("[cyan]Checking repo legitimacy...[/cyan]")
        legit, reason = await self._gh.check_repo_legitimacy(owner, repo)
        if not legit:
            self._console.print(f"[bold red]SKIPPED — repo failed legitimacy check: {reason}[/bold red]")
            self._console.print("[dim]This repo shows signs of being a bounty farm. No PR submitted.[/dim]")
            return ""

        branch = f"bounty-hunter/issue-{issue.number}"
        default_branch = await self._gh.get_repo_default_branch(owner, repo)

        self._console.print("[cyan]Fetching file tree...[/cyan]")
        tree = await self._gh.get_file_tree(owner, repo, default_branch)
        src_files = [f for f in tree if f.endswith((".py", ".js", ".ts", ".go", ".rs", ".java", ".rb", ".cpp", ".c", ".h"))][:200]

        self._console.print("[cyan]Asking Ollama to identify relevant files...[/cyan]")
        id_response = await self._ollama.generate(
            model=self._model,
            system="You are a code analysis assistant. Respond only with JSON.",
            prompt=_FILE_ID_PROMPT.format(
                number=issue.number,
                title=issue.title,
                body=(issue.body or "")[:2000],
                tree="\n".join(src_files),
            ),
            temperature=0.1,
        )
        try:
            identified = self._ollama.extract_json(id_response).get("files", [])[:5]
        except (ValueError, KeyError):
            identified = src_files[:3]

        self._console.print(f"[cyan]Reading {len(identified)} file(s)...[/cyan]")
        file_blocks: list[str] = []
        file_contents: dict[str, str] = {}
        for path in identified:
            try:
                content = await self._gh.get_file_content(owner, repo, path, default_branch)
                file_contents[path] = content
                file_blocks.append(f"=== {path} ===\n{content[:3000]}")
            except Exception:
                pass

        self._console.print("[cyan]Generating fix with Ollama...[/cyan]")
        fix_response = await self._ollama.generate(
            model=self._model,
            system="You are an expert software engineer. Output only valid JSON.",
            prompt=_FIX_PROMPT.format(
                number=issue.number,
                title=issue.title,
                body=(issue.body or "")[:2000],
                files="\n\n".join(file_blocks) if file_blocks else "No relevant files found.",
            ),
            temperature=0.15,
        )

        try:
            fix = self._ollama.extract_json(fix_response)
        except ValueError as exc:
            self._console.print(f"[red]Ollama did not return valid JSON: {exc}[/red]")
            return ""

        changed_files: list[dict[str, str]] = fix.get("files", [])
        if not changed_files:
            self._console.print("[red]Ollama returned no file changes.[/red]")
            return ""

        self._console.print(f"[dim]Analysis: {fix.get('analysis', '')}[/dim]")

        with tempfile.TemporaryDirectory() as tmpdir:
            clone_url = f"https://{self._gh.token}@github.com/{owner}/{repo}.git"
            result = subprocess.run(
                ["git", "clone", "--depth=1", clone_url, tmpdir],
                capture_output=True, text=True,
            )
            if result.returncode != 0:
                self._console.print(f"[red]Clone failed: {result.stderr}[/red]")
                return ""

            subprocess.run(["git", "-C", tmpdir, "checkout", "-b", branch], capture_output=True)

            for file_change in changed_files:
                fpath = Path(tmpdir) / file_change["path"]
                fpath.parent.mkdir(parents=True, exist_ok=True)
                fpath.write_text(file_change["content"], encoding="utf-8")
                self._console.print(f"  [green]Wrote[/green] {file_change['path']}")

            test_result = subprocess.run(
                ["python", "-m", "pytest", "--tb=short", "-q"],
                cwd=tmpdir, capture_output=True, text=True, timeout=120,
            )
            if test_result.returncode == 0:
                self._console.print("[green]Tests passed.[/green]")
            else:
                self._console.print(f"[yellow]Tests did not pass (proceeding anyway):[/yellow]\n{test_result.stdout[-500:]}")

            subprocess.run(["git", "-C", tmpdir, "add", "-A"], capture_output=True)
            commit_msg = fix.get("pr_title", f"Fix issue #{issue.number}")
            subprocess.run(
                ["git", "-C", tmpdir, "commit", "-m", commit_msg],
                capture_output=True,
                env={**os.environ, "GIT_AUTHOR_NAME": "Danny Degens", "GIT_AUTHOR_EMAIL": "danny@degens.world",
                     "GIT_COMMITTER_NAME": "Danny Degens", "GIT_COMMITTER_EMAIL": "danny@degens.world"},
            )

            me = await self._gh.get_authenticated_user()
            fork_name = f"{me}/{repo}"
            push_url = f"https://{self._gh.token}@github.com/{fork_name}.git"

            try:
                await self._gh.fork_repo(owner, repo)
                self._console.print(f"[cyan]Forked to {fork_name}[/cyan]")
            except Exception:
                pass

            push = subprocess.run(
                ["git", "-C", tmpdir, "push", push_url, f"HEAD:{branch}", "--force"],
                capture_output=True, text=True,
            )
            if push.returncode != 0:
                self._console.print(f"[red]Push failed: {push.stderr}[/red]")
                return ""

            pr_body = fix.get("pr_body", "") + f"\n\nCloses #{issue.number}"
            pr_url = await self._gh.create_pull_request(
                owner=owner,
                repo=repo,
                title=fix.get("pr_title", f"Fix issue #{issue.number}"),
                body=pr_body,
                head=f"{me}:{branch}",
                base=default_branch,
            )
            return pr_url
