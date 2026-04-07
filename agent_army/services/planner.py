from __future__ import annotations

from pydantic import ValidationError

from agent_army.goal_profile import GoalProfile, infer_goal_profile
from agent_army.models import PlanResult, PlannedSubtask
from agent_army.prompts import (
    CODE_PLANNER_SYSTEM_PROMPT,
    CODE_PLANNER_USER_TEMPLATE,
    PLANNER_SYSTEM_PROMPT,
    PLANNER_USER_TEMPLATE,
)
from agent_army.services.ollama import OllamaClient


class PlannerService:
    def __init__(self, client: OllamaClient, model: str, temperature: float, max_plan_steps: int) -> None:
        self._client = client
        self._model = model
        self._temperature = temperature
        self._max_plan_steps = max_plan_steps

    async def plan(self, goal: str) -> PlanResult:
        profile = infer_goal_profile(goal)
        if profile.domain == "coding":
            return self.code_fallback_plan(goal, profile)
        else:
            prompt = PLANNER_USER_TEMPLATE.format(goal=goal, max_plan_steps=self._max_plan_steps)
            system_prompt = PLANNER_SYSTEM_PROMPT
        try:
            response = await self._client.generate(
                model=self._model,
                system=system_prompt,
                prompt=prompt,
                temperature=self._temperature,
            )
            raw = self._client.extract_json(response)
            result = PlanResult.model_validate(raw)
            if not result.subtasks:
                raise ValueError("Planner returned zero subtasks.")
            return self._normalize(result, profile=profile)
        except (ValueError, ValidationError):
            return self.fallback_plan(goal, profile=profile)

    def fallback_plan(self, goal: str, profile: GoalProfile | None = None) -> PlanResult:
        profile = profile or infer_goal_profile(goal)
        if profile.domain == "coding":
            return self.code_fallback_plan(goal, profile)

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

    def code_fallback_plan(self, goal: str, profile: GoalProfile) -> PlanResult:
        final_format = "code" if profile.artifact_format in {"single_html", "code_bundle"} else "markdown"
        subtasks = [
            PlannedSubtask(
                title="Define implementation contract",
                description="Extract the exact behavior, UI, rules, constraints, and deliverable contract for the requested software artifact.",
                acceptance_criteria=[
                    "Defines the required behavior and rules clearly",
                    "Specifies the deliverable shape and runtime assumptions",
                    "Identifies the minimum components needed for a complete implementation",
                ],
                output_format="markdown",
                role_hint="analyst",
                priority=1,
                depends_on_indexes=[],
            ),
            PlannedSubtask(
                title="Build complete runnable artifact",
                description=(
                    "Implement the full runnable artifact that satisfies the goal. "
                    f"Follow this final deliverable instruction: {profile.final_output_instruction}"
                ),
                acceptance_criteria=[
                    "Returns a complete runnable implementation rather than fragments",
                    "Implements the main requested behavior end to end",
                    "Matches the requested artifact format",
                ],
                output_format=final_format,
                role_hint="coder",
                priority=2,
                depends_on_indexes=[0],
            ),
            PlannedSubtask(
                title="Verify behavior and identify gaps",
                description="Review the implementation against the requested rules, interactions, and edge cases. List concrete defects or confirm coverage.",
                acceptance_criteria=[
                    "Checks the implementation against the requested behavior",
                    "Calls out missing rules, broken flows, or edge-case issues if present",
                    "Provides concrete fixes, not generic criticism",
                ],
                output_format="bullets",
                role_hint="reviewer",
                priority=3,
                depends_on_indexes=[1],
            ),
            PlannedSubtask(
                title="Produce corrected final artifact",
                description=(
                    "Incorporate the verified fixes and return the corrected final runnable artifact. "
                    f"Follow this final deliverable instruction: {profile.final_output_instruction}"
                ),
                acceptance_criteria=[
                    "Returns the final complete artifact",
                    "Addresses the issues found in the verification pass",
                    "Is internally consistent and ready to run",
                ],
                output_format=final_format,
                role_hint="coder",
                priority=4,
                depends_on_indexes=[1, 2],
            ),
        ]
        return PlanResult(summary=f"Integration-first coding plan for goal: {goal}", subtasks=subtasks[: min(self._max_plan_steps, 4)])

    @staticmethod
    def _normalize(plan: PlanResult, *, profile: GoalProfile) -> PlanResult:
        subtasks: list[PlannedSubtask] = []
        for index, subtask in enumerate(plan.subtasks):
            valid_deps = [dep for dep in subtask.depends_on_indexes if dep < index]
            subtasks.append(subtask.model_copy(update={"depends_on_indexes": valid_deps}))
        if profile.domain == "coding":
            subtasks = subtasks[:4]
        return plan.model_copy(update={"subtasks": subtasks})
