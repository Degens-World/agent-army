from agent_army.models import TaskDetail, TaskStatus, TaskType
from agent_army.services.reviewer import ReviewerService


def test_fallback_reviewer_rejects_missing_criteria() -> None:
    task = TaskDetail.model_validate(
        {
            "id": "1",
            "run_id": "run",
            "task_type": TaskType.execute,
            "title": "Test task",
            "description": "Do the work",
            "status": TaskStatus.completed,
            "priority": 1,
            "depends_on": [],
            "payload": {"acceptance_criteria": ["Mention the budget", "Mention the risks"]},
            "result": {},
            "created_at": "2026-01-01T00:00:00+00:00",
            "updated_at": "2026-01-01T00:00:00+00:00",
        }
    )

    decision = ReviewerService.fallback_review(task, "This output mentions the budget only.")

    assert decision.approved is False
    assert "Mention the risks" in decision.issues
