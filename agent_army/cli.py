from __future__ import annotations

import argparse
import asyncio
from pathlib import Path

from rich.console import Console
from rich.live import Live
from rich.table import Table

from agent_army.monitor import (
    TERMINAL_RUN_STATES,
    collect_events,
    load_snapshot,
    render_dashboard,
    render_scroll_events,
    render_scroll_snapshot,
)
from agent_army.chat import CHAT_HELP, chat_result_path, classify_chat_input
from agent_army.config import get_settings
from agent_army.runtime import AgentArmyRuntime
from agent_army.services.ollama import OllamaClient


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="agent-army", description="CLI tools for Agent Army.")
    subparsers = parser.add_subparsers(dest="command", required=True)

    runs_parser = subparsers.add_parser("runs", help="List runs in a database.")
    runs_parser.add_argument("--db-path", type=Path, default=Path("agent_army.db"))

    monitor_parser = subparsers.add_parser("monitor", help="Show a live run monitor.")
    monitor_parser.add_argument("--db-path", type=Path, default=Path("agent_army.db"))
    monitor_parser.add_argument("--run-id", type=str, default=None)
    monitor_parser.add_argument("--refresh", type=float, default=1.0)
    monitor_parser.add_argument("--once", action="store_true")
    monitor_parser.add_argument("--show-completed", action="store_true")
    monitor_parser.add_argument("--mode", choices=["scroll", "dashboard"], default="dashboard")

    chat_parser = subparsers.add_parser("chat", help="Interactive chat interface for starting and watching runs.")
    chat_parser.add_argument("--db-path", type=Path, default=Path("agent_army.db"))
    chat_parser.add_argument("--refresh", type=float, default=1.0)
    chat_parser.add_argument("--show-completed", action="store_true")
    chat_parser.add_argument("--mode", choices=["scroll", "dashboard"], default="dashboard")

    hunt_parser = subparsers.add_parser("bounty-hunt", help="Hunt for bug bounties in a GitHub repo.")
    hunt_parser.add_argument("repo", nargs="?", default=None, help="GitHub repo in owner/name format, e.g. torvalds/linux")
    hunt_parser.add_argument("--org", type=str, default=None, help="Search all repos in a GitHub org")
    hunt_parser.add_argument("--search", type=str, default=None, help="Raw GitHub issue search query")
    hunt_parser.add_argument("--db-path", type=Path, default=Path("agent_army.db"))
    hunt_parser.add_argument("--model", type=str, default=None, help="Ollama model override (default: worker_model from settings)")
    hunt_parser.add_argument("--github-token", type=str, default=None, help="GitHub token (default: GITHUB_TOKEN env var)")

    log_parser = subparsers.add_parser("bounty-log", help="Show the bounty hunt log.")
    log_parser.add_argument("--db-path", type=Path, default=Path("agent_army.db"))

    return parser


def main() -> None:
    parser = build_parser()
    args = parser.parse_args()
    console = Console()

    if args.command == "bounty-hunt":
        asyncio.run(_bounty_hunt(console, repo=args.repo, org=args.org, search=args.search, db_path=args.db_path, model=args.model, github_token=args.github_token))
        return
    if args.command == "bounty-log":
        _bounty_log(console, db_path=args.db_path)
        return
    if args.command == "runs":
        asyncio.run(_list_runs(console, db_path=args.db_path))
        return
    if args.command == "monitor":
        asyncio.run(
            _monitor(
                console,
                db_path=args.db_path,
                run_id=args.run_id,
                refresh_seconds=args.refresh,
                once=args.once,
                show_completed=args.show_completed,
                mode=args.mode,
            )
        )
        return
    if args.command == "chat":
        asyncio.run(
            _chat(
                console,
                db_path=args.db_path,
                refresh_seconds=args.refresh,
                show_completed=args.show_completed,
                mode=args.mode,
            )
        )
        return
    parser.error("Unknown command.")


async def _list_runs(console: Console, *, db_path: Path) -> None:
    from agent_army.db import Database

    db = Database(db_path)
    runs = await db.list_runs()

    table = Table(title=f"Runs in {db_path}")
    table.add_column("Run ID", style="bold")
    table.add_column("Status")
    table.add_column("Created")
    table.add_column("Goal")

    for run in runs:
        table.add_row(run.id, run.status.value, run.created_at.isoformat(), run.goal[:96])

    if not runs:
        table.add_row("-", "-", "-", "No runs found")

    console.print(table)


async def _monitor(
    console: Console,
    *,
    db_path: Path,
    run_id: str | None,
    refresh_seconds: float,
    once: bool,
    show_completed: bool,
    mode: str,
) -> None:
    previous = None
    all_events = []
    tick = 0

    initial = await load_snapshot(db_path, run_id)
    if once:
        if mode == "dashboard":
            console.print(render_dashboard(initial, all_events, show_completed=show_completed, frame_index=tick))
        else:
            console.print(render_scroll_snapshot(initial, frame_index=tick))
            for line in render_scroll_events(initial, all_events, show_completed=show_completed):
                console.print(line)
        return

    if initial.run.status.value in TERMINAL_RUN_STATES:
        if mode == "dashboard":
            console.print(render_dashboard(initial, all_events, show_completed=show_completed, frame_index=tick))
        else:
            console.print(render_scroll_snapshot(initial, frame_index=tick))
            for line in render_scroll_events(initial, all_events, show_completed=show_completed):
                console.print(line)
        console.print(f"Run {initial.run.id} is already in terminal state: {initial.run.status.value}")
        return

    if mode == "scroll":
        console.print(render_scroll_snapshot(initial, frame_index=tick))
        for line in render_scroll_events(initial, all_events, show_completed=show_completed):
            console.print(line)
        console.print("")

        while True:
            await asyncio.sleep(refresh_seconds)
            tick += 1
            snapshot = await load_snapshot(db_path, run_id)
            new_events = collect_events(previous or initial, snapshot)
            if new_events:
                console.print(render_scroll_snapshot(snapshot, frame_index=tick))
                for line in render_scroll_events(snapshot, new_events, show_completed=show_completed):
                    console.print(line)
                console.print("")
            previous = snapshot
            if snapshot.run.status.value in TERMINAL_RUN_STATES:
                console.print(f"Run {snapshot.run.id} finished with status: {snapshot.run.status.value}")
                break
        return

    refresh_rate = max(1, int(1 / max(refresh_seconds, 0.1)))
    with Live(
        render_dashboard(initial, all_events, show_completed=show_completed, frame_index=tick),
        console=console,
        refresh_per_second=refresh_rate,
        screen=True,
        transient=True,
    ) as live:
        while True:
            snapshot = await load_snapshot(db_path, run_id)
            all_events.extend(collect_events(previous, snapshot))
            all_events = all_events[-50:]
            tick += 1
            live.update(render_dashboard(snapshot, all_events, show_completed=show_completed, frame_index=tick))
            if snapshot.run.status.value in TERMINAL_RUN_STATES:
                break
            previous = snapshot
            await asyncio.sleep(refresh_seconds)

        console.print(render_dashboard(snapshot, all_events, show_completed=show_completed, frame_index=tick))
        console.print(f"Run {snapshot.run.id} finished with status: {snapshot.run.status.value}")


async def _chat(
    console: Console,
    *,
    db_path: Path,
    refresh_seconds: float,
    show_completed: bool,
    mode: str,
) -> None:
    settings = get_settings().model_copy(update={"db_path": db_path})
    runtime = AgentArmyRuntime.from_settings(settings)
    await runtime.start()
    console.print("[bold green]Agent Army chat is live.[/bold green]")
    console.print(CHAT_HELP)
    try:
        while True:
            raw = await asyncio.to_thread(console.input, "[bold cyan]task-hq> [/]")
            command = classify_chat_input(raw)

            if command.kind == "empty":
                continue
            if command.kind == "quit":
                console.print("Standing down.")
                return
            if command.kind == "help":
                console.print(CHAT_HELP)
                continue
            if command.kind == "runs":
                await _list_runs(console, db_path=db_path)
                continue
            if command.kind == "watch":
                console.print(f"Watching run {command.value}")
                await _monitor(
                    console,
                    db_path=db_path,
                    run_id=command.value,
                    refresh_seconds=refresh_seconds,
                    once=False,
                    show_completed=show_completed,
                    mode=mode,
                )
                await _announce_final_artifact(console, runtime, command.value)
                continue
            if command.kind == "revise_prompt":
                source_run_id = command.value.strip()
                if not source_run_id:
                    source_run_id = (await asyncio.to_thread(console.input, "[bold yellow]run-id> [/]")).strip()
                if not source_run_id:
                    console.print("Revision cancelled: no run id provided.")
                    continue
                instructions = (await asyncio.to_thread(console.input, "[bold yellow]revise> [/]What should be revised? ")).strip()
                if not instructions:
                    console.print("Revision cancelled: no revision instructions provided.")
                    continue
                run_id = await runtime.reopen_run(source_run_id=source_run_id, instructions=instructions)
                console.print(f"Dispatched revision run {run_id} from {source_run_id}")
                await _monitor(
                    console,
                    db_path=db_path,
                    run_id=run_id,
                    refresh_seconds=refresh_seconds,
                    once=False,
                    show_completed=show_completed,
                    mode=mode,
                )
                await _announce_final_artifact(console, runtime, run_id)
                continue
            if command.kind == "revise":
                source_run_id, instructions = command.value.split("|", 1)
                run_id = await runtime.reopen_run(source_run_id=source_run_id, instructions=instructions)
                console.print(f"Dispatched revision run {run_id} from {source_run_id}")
                await _monitor(
                    console,
                    db_path=db_path,
                    run_id=run_id,
                    refresh_seconds=refresh_seconds,
                    once=False,
                    show_completed=show_completed,
                    mode=mode,
                )
                await _announce_final_artifact(console, runtime, run_id)
                continue
            if command.kind == "task":
                run_id = await runtime.create_run(goal=command.value)
                console.print(f"Dispatched run {run_id}")
                await _monitor(
                    console,
                    db_path=db_path,
                    run_id=run_id,
                    refresh_seconds=refresh_seconds,
                    once=False,
                    show_completed=show_completed,
                    mode=mode,
                )
                await _announce_final_artifact(console, runtime, run_id)
    finally:
        await runtime.stop()


async def _announce_final_artifact(console: Console, runtime: AgentArmyRuntime, run_id: str) -> None:
    run = await runtime.db.get_run(run_id)
    if run is None or not run.final_artifact or "content" not in run.final_artifact:
        return
    workspace_path = run.final_artifact.get("metadata", {}).get("workspace_path")
    if workspace_path:
        console.print(f"Final workspace ready at {workspace_path}")
    output_path = chat_result_path(run_id)
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(run.final_artifact["content"], encoding="utf-8")
    console.print(f"Saved final artifact to {output_path}")


async def _bounty_hunt(
    console: Console,
    *,
    repo: str | None,
    org: str | None,
    search: str | None,
    db_path: Path,
    model: str | None,
    github_token: str | None,
) -> None:
    import os
    from agent_army.bounty_hunter.db import BountyDB
    from agent_army.bounty_hunter.github import GitHubClient
    from agent_army.bounty_hunter.hunter import BountyHunter

    token = github_token or os.environ.get("GITHUB_TOKEN", "")
    if not token:
        console.print("[red]GitHub token required. Set GITHUB_TOKEN or pass --github-token.[/red]")
        return
    if not repo and not org and not search:
        console.print("[red]Provide a repo (owner/name), --org <org>, or --search <query>.[/red]")
        return
    if repo and "/" not in repo:
        console.print("[red]Repo must be in owner/name format, e.g. torvalds/linux[/red]")
        return

    settings = get_settings()
    resolved_model = model or settings.worker_model
    ollama = OllamaClient(host=settings.ollama_host, timeout_seconds=settings.request_timeout_seconds)
    github = GitHubClient(token=token)
    db = BountyDB(db_path)

    hunter = BountyHunter(github=github, ollama=ollama, db=db, model=resolved_model, console=console)

    if search:
        await hunter.hunt_search(search)
    elif org:
        await hunter.hunt_org(org)
    else:
        await hunter.hunt(repo)  # type: ignore[arg-type]


def _bounty_log(console: Console, *, db_path: Path) -> None:
    from agent_army.bounty_hunter.db import BountyDB

    db = BountyDB(db_path)
    records = db.list_all()

    table = Table(title="Bounty Hunt Log")
    table.add_column("ID", style="dim")
    table.add_column("Repo")
    table.add_column("Issue")
    table.add_column("Title")
    table.add_column("Bounty", style="green")
    table.add_column("Status")
    table.add_column("PR")

    for r in records:
        table.add_row(
            str(r.id),
            r.repo,
            f"#{r.issue_number}",
            r.issue_title[:60],
            r.bounty_amount,
            r.status,
            r.pr_url or "—",
        )

    if not records:
        table.add_row("-", "-", "-", "No hunts logged yet", "-", "-", "-")

    console.print(table)


if __name__ == "__main__":
    main()
