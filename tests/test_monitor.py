from datetime import UTC, datetime

from agent_army.models import ArtifactDetail, RunDetail, RunStatus, TaskDetail, TaskStatus, TaskType
from agent_army.monitor import MonitorSnapshot, agent_name, caper_text, collect_events, event_blurb


def _task(*, status: TaskStatus, updated_at: str, task_type: TaskType = TaskType.execute) -> TaskDetail:
    return TaskDetail.model_validate(
        {
            "id": "task-1",
            "run_id": "run-1",
            "task_type": task_type,
            "title": "Example task",
            "description": "Example description",
            "status": status,
            "priority": 1,
            "depends_on": [],
            "payload": {"sequence_index": 0, "role_hint": "analyst"},
            "result": {},
            "created_at": "2026-01-01T00:00:00+00:00",
            "updated_at": updated_at,
        }
    )


def _snapshot(task: TaskDetail) -> MonitorSnapshot:
    run = RunDetail(
        id="run-1",
        goal="Goal text",
        status=RunStatus.running,
        created_at=datetime(2026, 1, 1, tzinfo=UTC),
        updated_at=datetime(2026, 1, 1, tzinfo=UTC),
        task_counts={task.status.value: 1},
    )
    return MonitorSnapshot(run=run, tasks=[task], artifacts=[])


def test_agent_name_uses_role_hint_and_sequence_for_execute_tasks() -> None:
    task = _task(status=TaskStatus.running, updated_at="2026-01-01T00:00:05+00:00")
    assert "#" in agent_name(task)
    assert agent_name(task).endswith("#1")


def test_collect_events_detects_task_status_changes() -> None:
    before = _snapshot(_task(status=TaskStatus.pending, updated_at="2026-01-01T00:00:01+00:00"))
    after = _snapshot(_task(status=TaskStatus.running, updated_at="2026-01-01T00:00:05+00:00"))

    events = collect_events(before, after)

    assert len(events) == 1
    assert events[0].previous_status == "pending"
    assert events[0].current_status == "running"


def test_caper_text_is_fun_and_status_specific() -> None:
    task = _task(status=TaskStatus.running, updated_at="2026-01-01T00:00:05+00:00")
    text = caper_text(task)

    assert "advancing" in text
    assert "example task" in text


def test_event_blurb_prefers_funny_status_copy_without_error() -> None:
    before = _snapshot(_task(status=TaskStatus.pending, updated_at="2026-01-01T00:00:01+00:00"))
    after = _snapshot(_task(status=TaskStatus.completed, updated_at="2026-01-01T00:00:05+00:00"))

    events = collect_events(before, after)

    assert "mission accomplished" in event_blurb(events[0])
