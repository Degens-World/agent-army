from agent_army.models import TaskDetail, TaskStatus, TaskType
from agent_army.services.reviewer import ReviewerService


class ExplodingClient:
    async def generate(self, **_: object) -> str:
        raise TimeoutError()


def test_reviewer_service_falls_back_on_model_exception() -> None:
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
            "payload": {"acceptance_criteria": ["Mention the budget"]},
            "result": {},
            "created_at": "2026-01-01T00:00:00+00:00",
            "updated_at": "2026-01-01T00:00:00+00:00",
        }
    )

    service = ReviewerService(client=ExplodingClient(), model="unused", temperature=0.0)  # type: ignore[arg-type]

    import asyncio

    decision = asyncio.run(service.review(task, "Mention the budget."))

    assert decision.approved is True
