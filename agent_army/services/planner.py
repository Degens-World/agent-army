from __future__ import annotations

from pydantic import ValidationError

from agent_army.models import PlanResult, PlannedSubtask
from agent_army.prompts import PLANNER_SYSTEM_PROMPT, PLANNER_USER_TEMPLATE
from agent_army.services.ollama import OllamaClient


class PlannerService:
    def __init__(self, client: OllamaClient, model: str, temperature: float, max_plan_steps: int) -> None:
        self._client = client
        self._model = model
        self._temperature = temperature
        self._max_plan_steps = max_plan_steps

    async def plan(self, goal: str) -> PlanResult:
        prompt = PLANNER_USER_TEMPLATE.format(goal=goal, max_plan_steps=self._max_plan_steps)
        try:
            response = await self._client.generate(
                model=self._model,
                system=PLANNER_SYSTEM_PROMPT,
                prompt=prompt,
                temperature=self._temperature,
            )
            raw = self._client.extract_json(response)
            result = PlanResult.model_validate(raw)
            if not result.subtasks:
                raise ValueError("Planner returned zero subtasks.")
            return self._normalize(result)
        except (ValueError, ValidationError):
            return self.fallback_plan(goal)

    def fallback_plan(self, goal: str) -> PlanResult:
        base_tasks = [
            PlannedSubtask(
                title="Clarify scope and constraints",
                description="Extract the core objective, assumptions, constraints, and success metrics from the goal.",
                acceptance_criteria=[
                    "States the objective in concrete terms",
                    "Identifies the likely constraints or unknowns",
                    "Provides a short definition of success",
                ],
                output_format="bullets",
                role_hint="analyst",
                priority=1,
                depends_on_indexes=[],
            ),
            PlannedSubtask(
                title="Produce main deliverable draft",
                description="Create the primary deliverable that addresses the goal directly and concretely.",
                acceptance_criteria=[
                    "Addresses the full goal directly",
                    "Uses clear structure",
                    "Includes concrete recommendations or outputs",
                ],
                output_format="markdown",
                role_hint="writer",
                priority=2,
                depends_on_indexes=[0],
            ),
            PlannedSubtask(
                title="Review for gaps and risks",
                description="Identify omissions, contradictions, risks, and improvements for the draft deliverable.",
                acceptance_criteria=[
                    "Finds material risks or gaps if present",
                    "Provides practical fixes",
                    "Keeps focus on the stated goal",
                ],
                output_format="bullets",
                role_hint="reviewer",
                priority=3,
                depends_on_indexes=[1],
            ),
        ]
        return PlanResult(summary=f"Fallback plan for goal: {goal}", subtasks=base_tasks[: self._max_plan_steps])

    @staticmethod
    def _normalize(plan: PlanResult) -> PlanResult:
        subtasks: list[PlannedSubtask] = []
        for index, subtask in enumerate(plan.subtasks):
            valid_deps = [dep for dep in subtask.depends_on_indexes if dep < index]
            subtasks.append(subtask.model_copy(update={"depends_on_indexes": valid_deps}))
        return plan.model_copy(update={"subtasks": subtasks})
