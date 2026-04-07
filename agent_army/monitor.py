from __future__ import annotations

import asyncio
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterable

from rich import box
from rich.align import Align
from rich.console import Group
from rich.layout import Layout
from rich.panel import Panel
from rich.table import Table
from rich.text import Text

from agent_army.db import Database
from agent_army.models import ArtifactDetail, RunDetail, RunSummary, TaskDetail, TaskStatus, TaskType


TERMINAL_TASK_STATES = {TaskStatus.completed, TaskStatus.failed, TaskStatus.rejected}
TERMINAL_RUN_STATES = {"completed", "failed", "paused"}
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
WAR_HEADER_FRAMES = [
    [
        r"   o7        o7        o7        o7",
        r"  /|\       /|\       /|\       /|\      >>> TASK FRONT >>>",
        "  / \\       / \\       / \\       / \\",
    ],
    [
        r"    o7        o7        o7        o7",
        r"   /|\       /|\       /|\       /|\     >>> TASK FRONT >>>",
        "  _/ \\      _/ \\      _/ \\      _/ \\",
    ],
    [
        r"     o7        o7        o7        o7",
        r"    /|\       /|\       /|\       /|\    >>> TASK FRONT >>>",
        "    / \\_      / \\_      / \\_      / \\_",
    ],
]
UNIT_PORTRAIT_FRAMES = [
    [
        "      __",
        "     /__\\",
        r"    (o_o )",
        "    /|=|\\ ",
        "    / \\ \\ ",
        "  marching",
    ],
    [
        "      __",
        "     /__\\",
        r"    (o_o )",
        "   _/|=|\\_",
        "     / \\  ",
        " advancing",
    ],
    [
        "      __",
        "     /__\\",
        r"    (o_o )",
        "    /|=|\\ ",
        "   _/ \\   ",
        "  charging",
    ],
]


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
    frame_index: int = 0,
) -> Layout:
    layout = Layout(name="root")
    layout.split_column(
        Layout(_render_war_header(snapshot.run, frame_index), name="header", size=7),
        Layout(name="body", ratio=4),
        Layout(_render_action_ledger(snapshot.tasks, snapshot.artifacts, show_completed=show_completed), name="ledger", size=15),
    )
    layout["body"].split_row(
        Layout(_render_unit_panel(snapshot.run, snapshot.tasks, frame_index), name="unit", size=28),
        Layout(_render_pulse_feed(snapshot.tasks, events), name="pulses", ratio=3),
    )
    return layout


def render_scroll_snapshot(snapshot: MonitorSnapshot, *, frame_index: int = 0) -> str:
    lines = []
    lines.extend(_war_header_lines(snapshot.run, frame_index))
    counts = ", ".join(f"{key}={value}" for key, value in sorted(snapshot.run.task_counts.items())) or "no tasks"
    lines.append(
        f"[run] {snapshot.run.id} | status={snapshot.run.status.value} | counts={counts} | age={format_elapsed(snapshot.run.created_at)}"
    )
    lines.append(f"[goal] {truncate(snapshot.run.goal, 160)}")
    return "\n".join(lines)


def render_scroll_events(snapshot: MonitorSnapshot, events: list[MonitorEvent], *, show_completed: bool) -> list[str]:
    lines: list[str] = []
    if events:
        for event in events:
            lines.append(
                f"[{format_elapsed(event.timestamp):>7}] {event.agent}: {event.previous_status or 'new'} -> {event.current_status} | "
                f"{truncate(event.title, 52)} | {truncate(event_blurb(event), 88)}"
            )
    else:
        visible = _visible_tasks(snapshot.tasks, show_completed=show_completed)
        for task in visible[:6]:
            lines.append(
                f"[status ] {agent_name(task)} | {task.status.value:<11} | {truncate(task.title, 52)} | {truncate(caper_text(task), 88)}"
            )
    return lines


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


def _render_war_header(run: RunDetail, frame_index: int) -> Panel:
    text = Text()
    lines = _war_header_lines(run, frame_index)
    text.append(lines[0] + "\n", style="bold bright_green")
    text.append(lines[1] + "\n", style="bold bright_cyan")
    text.append(lines[2] + "\n", style="bold bright_green")
    text.append(lines[3], style="bold bright_magenta")
    return Panel(Align.center(text), title="[bold bright_magenta]Agent Army // Tactical Dashboard[/]", border_style="bright_blue")


def _war_header_lines(run: RunDetail, frame_index: int) -> list[str]:
    frame = WAR_HEADER_FRAMES[frame_index % len(WAR_HEADER_FRAMES)]
    objective = truncate(run.goal.upper(), 72)
    return [*frame, f" OPERATION: {objective}"]


def _render_agents_table(
    tasks: list[TaskDetail],
    artifacts: list[ArtifactDetail],
    *,
    show_completed: bool,
) -> Panel:
    artifact_by_task = latest_artifact_by_task(artifacts)
    rows = _visible_tasks(tasks, show_completed=show_completed)
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


def _visible_tasks(tasks: list[TaskDetail], *, show_completed: bool) -> list[TaskDetail]:
    if show_completed:
        return tasks
    return [task for task in tasks if task.status not in TERMINAL_TASK_STATES]


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


def _render_unit_panel(run: RunDetail, tasks: list[TaskDetail], frame_index: int) -> Panel:
    portrait = UNIT_PORTRAIT_FRAMES[frame_index % len(UNIT_PORTRAIT_FRAMES)]
    lead_task = _lead_task(tasks)
    counts = _status_counts_text(run)

    body = Text()
    body.append(f"{agent_name(lead_task) if lead_task else 'Command HQ'}\n", style="bold bright_yellow")
    for line in portrait:
        body.append(line + "\n", style="bright_green")
    body.append("\n")
    body.append(f"status: {run.status.value}\n", style="bright_cyan")
    body.append(f"counts: {counts}\n")
    if lead_task is not None:
        body.append(f"focus: {truncate(lead_task.title, 22)}\n", style="bright_white")
        body.append(f"order: {truncate(caper_text(lead_task), 26)}", style=status_style(lead_task.status))
    else:
        body.append("focus: awaiting orders")
    return Panel(body, title="[bold bright_green]Command Unit[/]", border_style="bright_green")


def _render_pulse_feed(tasks: list[TaskDetail], events: list[MonitorEvent]) -> Panel:
    lines = Table(box=box.SIMPLE, expand=True, show_header=False)
    lines.add_column("Pulse", ratio=1)

    if events:
        for event in events[-12:]:
            stamp = format_elapsed(event.timestamp)
            pulse = Text()
            pulse.append(f"[{stamp}] ", style="dim")
            pulse.append(f"{event.agent} ", style="bold bright_yellow")
            pulse.append(f"{event.previous_status or 'new'} -> {event.current_status} ", style="bright_cyan")
            pulse.append(f"{truncate(event.title, 38)} ", style="white")
            pulse.append(truncate(event_blurb(event), 72), style="bright_magenta" if event.error else "green")
            lines.add_row(pulse)
    else:
        for task in _active_tasks(tasks)[:10]:
            pulse = Text()
            pulse.append(f"{agent_name(task)} ", style="bold bright_yellow")
            pulse.append(f"{task.status.value} ", style=status_style(task.status))
            pulse.append(f"{truncate(task.title, 44)} ", style="white")
            pulse.append(truncate(caper_text(task), 72), style="bright_green")
            lines.add_row(pulse)

    return Panel(lines, title="[bold bright_cyan]Latest Pulses[/]", border_style="bright_cyan")


def _render_action_ledger(tasks: list[TaskDetail], artifacts: list[ArtifactDetail], *, show_completed: bool) -> Panel:
    artifact_by_task = latest_artifact_by_task(artifacts)
    rows = sorted(
        _visible_tasks(tasks, show_completed=show_completed) or tasks,
        key=lambda task: task.updated_at,
        reverse=True,
    )[:12]

    table = Table(box=box.SIMPLE_HEAVY, expand=True)
    table.add_column("Time", width=8, style="bright_cyan")
    table.add_column("Unit", width=24, style="bold bright_yellow")
    table.add_column("Stage", width=12)
    table.add_column("Task", ratio=2)
    table.add_column("Notes", ratio=3)

    for task in rows:
        artifact = artifact_by_task.get(task.id)
        notes = task.error or caper_text(task)
        if artifact is not None and artifact.kind in {"worker_output", "final"}:
            notes = truncate(artifact.content, 88)
        table.add_row(
            format_elapsed(task.updated_at),
            agent_name(task),
            task.status.value,
            truncate(task.title, 48),
            truncate(notes, 96),
        )

    return Panel(table, title="[bold bright_magenta]Ledger of Actions[/]", border_style="bright_magenta")


def _active_tasks(tasks: list[TaskDetail]) -> list[TaskDetail]:
    active_statuses = {TaskStatus.running, TaskStatus.queued, TaskStatus.pending, TaskStatus.needs_retry, TaskStatus.blocked}
    ordered = sorted(
        tasks,
        key=lambda task: (
            task.status in active_statuses,
            task.updated_at,
        ),
        reverse=True,
    )
    return ordered


def _lead_task(tasks: list[TaskDetail]) -> TaskDetail | None:
    active = _active_tasks(tasks)
    return active[0] if active else None


def _status_counts_text(run: RunDetail) -> str:
    return ", ".join(f"{key}:{value}" for key, value in sorted(run.task_counts.items())) or "none"


async def wait_for_terminal_state(db_path: Path, run_id: str, refresh_seconds: float) -> MonitorSnapshot:
    while True:
        snapshot = await load_snapshot(db_path, run_id)
        if snapshot.run.status.value in {"completed", "failed", "paused"}:
            return snapshot
        await asyncio.sleep(refresh_seconds)
