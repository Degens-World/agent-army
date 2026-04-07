from __future__ import annotations

from pydantic import ValidationError

from agent_army.models import ReviewDecision, TaskDetail
from agent_army.prompts import REVIEWER_SYSTEM_PROMPT, REVIEWER_USER_TEMPLATE
from agent_army.services.ollama import OllamaClient


class ReviewerService:
    def __init__(self, client: OllamaClient, model: str, temperature: float) -> None:
        self._client = client
        self._model = model
        self._temperature = temperature

    async def review(self, task: TaskDetail, worker_output: str) -> ReviewDecision:
        prompt = REVIEWER_USER_TEMPLATE.format(
            title=task.title,
            description=task.description,
            acceptance_criteria="\n".join(f"- {item}" for item in task.payload.get("acceptance_criteria", [])),
            worker_output=worker_output,
        )
        try:
            response = await self._client.generate(
                model=self._model,
                system=REVIEWER_SYSTEM_PROMPT,
                prompt=prompt,
                temperature=self._temperature,
            )
            raw = self._client.extract_json(response)
            return ReviewDecision.model_validate(raw)
        except Exception:
            return self.fallback_review(task, worker_output)

    @staticmethod
    def fallback_review(task: TaskDetail, worker_output: str) -> ReviewDecision:
        criteria = task.payload.get("acceptance_criteria", [])
        missing = [criterion for criterion in criteria if criterion.lower() not in worker_output.lower()]
        if missing:
            return ReviewDecision(
                approved=False,
                summary="Fallback reviewer rejected the output because acceptance criteria were not clearly covered.",
                issues=missing,
                suggested_fixes=[f"Address this explicitly: {criterion}" for criterion in missing],
            )
        return ReviewDecision(
            approved=bool(worker_output.strip()),
            summary="Fallback reviewer accepted the output.",
            issues=[],
            suggested_fixes=[],
        )
