from __future__ import annotations

import argparse
import asyncio
from pathlib import Path

from rich.console import Console
from rich.live import Live
from rich.table import Table

from agent_army.monitor import collect_events, load_snapshot, render_dashboard


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
    return parser


def main() -> None:
    parser = build_parser()
    args = parser.parse_args()
    console = Console()

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
) -> None:
    previous = None
    all_events = []

    initial = await load_snapshot(db_path, run_id)
    if once:
        console.print(render_dashboard(initial, all_events, show_completed=show_completed))
        return

    if initial.run.status.value in {"completed", "failed", "paused"}:
        console.print(render_dashboard(initial, all_events, show_completed=show_completed))
        console.print(f"Run {initial.run.id} is already in terminal state: {initial.run.status.value}")
        return

    refresh_rate = max(1, int(1 / max(refresh_seconds, 0.1)))
    with Live(
        render_dashboard(initial, all_events, show_completed=show_completed),
        console=console,
        refresh_per_second=refresh_rate,
        screen=False,
        transient=False,
    ) as live:
        while True:
            snapshot = await load_snapshot(db_path, run_id)
            all_events.extend(collect_events(previous, snapshot))
            all_events = all_events[-50:]
            live.update(render_dashboard(snapshot, all_events, show_completed=show_completed))
            if snapshot.run.status.value in {"completed", "failed", "paused"}:
                break
            previous = snapshot
            await asyncio.sleep(refresh_seconds)

        console.print()
        console.print(f"Run {snapshot.run.id} finished with status: {snapshot.run.status.value}")


if __name__ == "__main__":
    main()
