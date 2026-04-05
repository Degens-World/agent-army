from __future__ import annotations

import asyncio
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterable

from rich import box
from rich.console import Group
from rich.panel import Panel
from rich.table import Table
from rich.text import Text

from agent_army.db import Database
from agent_army.models import ArtifactDetail, RunDetail, RunSummary, TaskDetail, TaskStatus, TaskType


TERMINAL_TASK_STATES = {TaskStatus.completed, TaskStatus.failed, TaskStatus.rejected}
ROLE_CODENAMES = {
    "planner": [
        "General Tasker",
        "Major Mapstack",
        "Colonel Compass",
        "Brigadier Blueprint",
    ],
    "reviewer": [
        "Inspector Invasion",
        "Sergeant Sideeye",
        "Major Redpen",
        "Captain Checklist",
    ],
    "synthesizer": [
        "Marshal Merge",
        "Captain Cohesion",
        "General Gluegun",
        "Admiral Afteraction",
    ],
    "default": [
        "Private Prompt",
        "Corporal Callback",
        "Lieutenant Loop",
        "Sergeant Semaphore",
    ],
}


@dataclass(slots=True)
class MonitorSnapshot:
    run: RunDetail
    tasks: list[TaskDetail]
    artifacts: list[ArtifactDetail]


@dataclass(slots=True)
class MonitorEvent:
    timestamp: datetime
    run_id: str
    task_id: str
    agent: str
    title: str
    previous_status: str | None
    current_status: str
    error: str | None = None


def role_name(task: TaskDetail) -> str:
    if task.task_type == TaskType.plan:
        return "planner"
    if task.task_type == TaskType.review:
        return "reviewer"
    if task.task_type == TaskType.synthesize:
        return "synthesizer"
    return task.payload.get("role_hint", "worker")


def agent_name(task: TaskDetail) -> str:
    role = role_name(task)
    names = ROLE_CODENAMES.get(role, ROLE_CODENAMES["default"])
    index = _stable_index(task.id, len(names))
    name = names[index]
    if task.task_type == TaskType.execute:
        sequence = int(task.payload.get("sequence_index", 0)) + 1
        return f"{name} #{sequence}"
    if task.task_type == TaskType.review:
        target = task.payload.get("target_task_id", "")[-4:] or "task"
        return f"{name} [{target}]"
    return name


def status_style(status: TaskStatus) -> str:
    return {
        TaskStatus.pending: "yellow",
        TaskStatus.queued: "cyan",
        TaskStatus.running: "bold blue",
        TaskStatus.blocked: "magenta",
        TaskStatus.completed: "green",
        TaskStatus.failed: "bold red",
        TaskStatus.needs_retry: "bright_yellow",
        TaskStatus.rejected: "red",
    }[status]


def format_elapsed(dt: datetime) -> str:
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    delta = datetime.now(timezone.utc) - dt.astimezone(timezone.utc)
    seconds = max(0, int(delta.total_seconds()))
    minutes, seconds = divmod(seconds, 60)
    hours, minutes = divmod(minutes, 60)
    if hours:
        return f"{hours}h {minutes}m"
    if minutes:
        return f"{minutes}m {seconds}s"
    return f"{seconds}s"


def truncate(text: str | None, limit: int) -> str:
    if not text:
        return "-"
    clean = " ".join(text.split())
    if len(clean) <= limit:
        return clean
    return f"{clean[: limit - 3]}..."


def caper_text(task: TaskDetail) -> str:
    title = truncate(task.title.lower(), 36)
    if task.status == TaskStatus.pending:
        return f"is standing by in the barracks for '{title}'"
    if task.status == TaskStatus.queued:
        return f"is rolling onto the launchpad for '{title}'"
    if task.status == TaskStatus.running:
        if task.task_type == TaskType.plan:
            return "is drafting the battle plan"
        if task.task_type == TaskType.review:
            return f"is running inspection drills on '{title}'"
        if task.task_type == TaskType.synthesize:
            return "is assembling the final operation order"
        return f"is advancing on '{title}'"
    if task.status == TaskStatus.blocked:
        return "is pinned down waiting for supporting units"
    if task.status == TaskStatus.completed:
        return "secured the objective and radioed it in"
    if task.status == TaskStatus.needs_retry:
        return "is regrouping for another sortie"
    if task.status == TaskStatus.failed:
        return "took heavy fire and is requesting backup"
    return "was sent back to base for rework"


def event_blurb(event: MonitorEvent) -> str:
    if event.error:
        return event.error
    transition = (event.previous_status, event.current_status)
    if transition == (None, TaskStatus.pending.value):
        return "has joined the task force"
    if event.current_status == TaskStatus.running.value:
        return "is boots-on-ground and moving"
    if event.current_status == TaskStatus.completed.value:
        return "mission accomplished"
    if event.current_status == TaskStatus.needs_retry.value:
        return "is regrouping for another pass"
    if event.current_status == TaskStatus.failed.value:
        return "hit resistance and called for reinforcements"
    if event.current_status == TaskStatus.blocked.value:
        return "is holding position until support arrives"
    return "has shifted to a new phase of the operation"


def latest_artifact_by_task(artifacts: Iterable[ArtifactDetail]) -> dict[str, ArtifactDetail]:
    latest: dict[str, ArtifactDetail] = {}
    for artifact in artifacts:
        if not artifact.task_id:
            continue
        current = latest.get(artifact.task_id)
        if current is None or artifact.created_at > current.created_at:
            latest[artifact.task_id] = artifact
    return latest


def _stable_index(value: str, size: int) -> int:
    return sum(ord(char) for char in value) % size


def collect_events(previous: MonitorSnapshot | None, current: MonitorSnapshot) -> list[MonitorEvent]:
    if previous is None:
        return []
    previous_by_id = {task.id: task for task in previous.tasks}
    events: list[MonitorEvent] = []
    for task in current.tasks:
        prior = previous_by_id.get(task.id)
        previous_status = prior.status.value if prior else None
        current_status = task.status.value
        if previous_status == current_status:
            continue
        events.append(
            MonitorEvent(
                timestamp=task.updated_at,
                run_id=task.run_id,
                task_id=task.id,
                agent=agent_name(task),
                title=task.title,
                previous_status=previous_status,
                current_status=current_status,
                error=task.error,
            )
        )
    return sorted(events, key=lambda event: event.timestamp)


def render_dashboard(
    snapshot: MonitorSnapshot,
    events: list[MonitorEvent],
    *,
    show_completed: bool,
) -> Group:
    header = _render_run_header(snapshot.run)
    agents = _render_agents_table(snapshot.tasks, snapshot.artifacts, show_completed=show_completed)
    event_log = _render_event_log(events)
    return Group(header, agents, event_log)


async def load_snapshot(db_path: Path, run_id: str | None) -> MonitorSnapshot:
    db = Database(db_path)
    run = await resolve_run(db, run_id)
    if run is None:
        raise ValueError("No runs found in the database.")
    tasks = await db.list_tasks(run.id)
    artifacts = await db.list_artifacts(run.id)
    return MonitorSnapshot(run=run, tasks=tasks, artifacts=artifacts)


async def resolve_run(db: Database, run_id: str | None) -> RunDetail | None:
    if run_id:
        return await db.get_run(run_id)
    runs = await db.list_runs()
    if not runs:
        return None
    latest = runs[0]
    return await db.get_run(latest.id)


def _render_run_header(run: RunDetail) -> Panel:
    counts = ", ".join(f"{key}={value}" for key, value in sorted(run.task_counts.items())) or "no tasks"
    body = Text()
    body.append(f"Run {run.id}\n", style="bold")
    body.append(f"Status: {run.status.value}\n", style="cyan")
    body.append(f"Created: {run.created_at.isoformat()}\n")
    body.append(f"Updated: {run.updated_at.isoformat()}\n")
    body.append(f"Age: {format_elapsed(run.created_at)}\n")
    body.append(f"Task counts: {counts}\n")
    body.append(f"Goal: {truncate(run.goal, 160)}")
    return Panel(body, title="Run", border_style="blue")


def _render_agents_table(
    tasks: list[TaskDetail],
    artifacts: list[ArtifactDetail],
    *,
    show_completed: bool,
) -> Panel:
    artifact_by_task = latest_artifact_by_task(artifacts)
    rows = tasks if show_completed else [task for task in tasks if task.status not in TERMINAL_TASK_STATES]
    if not rows:
        rows = tasks[-5:]

    table = Table(box=box.SIMPLE_HEAVY, expand=True)
    table.add_column("Agent", style="bold")
    table.add_column("Type")
    table.add_column("Status")
    table.add_column("Retries", justify="right")
    table.add_column("Title", ratio=2)
    table.add_column("Caper", ratio=2)
    table.add_column("Last Output", ratio=3)

    for task in rows:
        artifact = artifact_by_task.get(task.id)
        preview = "-"
        if task.result and task.result.get("output_preview"):
            preview = task.result["output_preview"]
        elif artifact is not None:
            preview = artifact.content
        table.add_row(
            agent_name(task),
            task.task_type.value,
            Text(task.status.value, style=status_style(task.status)),
            str(task.retry_count),
            truncate(task.title, 48),
            truncate(caper_text(task), 56),
            truncate(preview, 88),
        )

    return Panel(table, title="Agents", border_style="green")


def _render_event_log(events: list[MonitorEvent]) -> Panel:
    table = Table(box=box.SIMPLE, expand=True)
    table.add_column("When", width=8)
    table.add_column("Agent", width=18)
    table.add_column("Transition", width=22)
    table.add_column("Task", ratio=2)
    table.add_column("Detail", ratio=3)

    if not events:
        table.add_row("-", "-", "-", "No changes yet", "-")
    else:
        for event in events[-10:]:
            transition = f"{event.previous_status or 'new'} -> {event.current_status}"
            detail = event_blurb(event)
            table.add_row(
                format_elapsed(event.timestamp),
                event.agent,
                transition,
                truncate(event.title, 48),
                truncate(detail, 88),
            )

    return Panel(table, title="Recent Events", border_style="yellow")


async def wait_for_terminal_state(db_path: Path, run_id: str, refresh_seconds: float) -> MonitorSnapshot:
    while True:
        snapshot = await load_snapshot(db_path, run_id)
        if snapshot.run.status.value in {"completed", "failed", "paused"}:
            return snapshot
        await asyncio.sleep(refresh_seconds)
