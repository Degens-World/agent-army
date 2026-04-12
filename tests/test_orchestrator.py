from pathlib import Path

from agent_army.config import Settings
from agent_army.db import Database
from agent_army.models import TaskDetail, TaskStatus, TaskType
from agent_army.orchestrator import Orchestrator


def _task(title: str) -> TaskDetail:
    return TaskDetail.model_validate(
        {
            "id": "task-1",
            "run_id": "run-1",
            "task_type": TaskType.execute,
            "title": title,
            "description": "desc",
            "status": TaskStatus.pending,
            "priority": 1,
            "depends_on": [],
            "payload": {"acceptance_criteria": []},
            "result": None,
            "created_at": "2026-01-01T00:00:00+00:00",
            "updated_at": "2026-01-01T00:00:00+00:00",
        }
    )


def test_worker_model_prefers_coder_model_when_available() -> None:
    chosen = Orchestrator._pick_model("missing", ["qwen3:4b", "qwen3-coder:30b"], role="worker")

    assert chosen == "qwen3-coder:30b"


def test_coding_phase_instructions_distinguish_verification_from_implementation() -> None:
    orchestrator = Orchestrator(Database(Path("test.db")), Settings())

    phase, guidance = orchestrator._coding_phase_instructions(_task("Verify behavior and identify gaps"))

    assert phase == "verification"
    assert "return a concrete verification report" in guidance.lower()


def test_source_workspace_file_context_uses_clean_excerpt_for_large_files(tmp_path: Path) -> None:
    workspace = tmp_path / "source"
    workspace.mkdir(parents=True)
    large_file = workspace / "index.html"
    large_file.write_text("A" * 8000 + "B" * 8000, encoding="utf-8")

    context = Orchestrator._source_workspace_file_context(
        {
            "source_workspace_path": str(workspace),
            "source_files": ["index.html"],
        },
        max_chars=4000,
    )

    assert "[... existing file truncated for prompt size ...]" in context
    assert context.endswith("```")
