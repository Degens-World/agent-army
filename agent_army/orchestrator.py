from __future__ import annotations

import asyncio
import logging
from contextlib import suppress
from pathlib import Path

from agent_army.config import Settings
from agent_army.db import Database
from agent_army.goal_profile import infer_goal_profile
from agent_army.models import ReviewDecision, RunStatus, TaskDetail, TaskStatus, TaskType
from agent_army.prompts import (
    CODE_REVIEWER_SYSTEM_PROMPT,
    CODE_REVIEWER_USER_TEMPLATE,
    CODE_SYNTHESIZER_SYSTEM_PROMPT,
    CODE_SYNTHESIZER_USER_TEMPLATE,
    CODE_WORKER_SYSTEM_PROMPT,
    CODE_WORKER_USER_TEMPLATE,
    WORKER_SYSTEM_PROMPT,
    WORKER_USER_TEMPLATE,
)
from agent_army.services.ollama import OllamaClient, OllamaError
from agent_army.services.planner import PlannerService
from agent_army.services.reviewer import ReviewerService
from agent_army.services.synthesizer import SynthesizerService
from agent_army.validator import CodingValidator, ValidationResult
from agent_army.workspace import WorkspaceManager

logger = logging.getLogger(__name__)


class Orchestrator:
    def __init__(self, db: Database, settings: Settings) -> None:
        self._db = db
        self._settings = settings
        self._client = OllamaClient(settings.ollama_host, settings.request_timeout_seconds)
        self._planner_model = settings.planner_model
        self._worker_model = settings.worker_model
        self._reviewer_model = settings.reviewer_model
        self._synthesizer_model = settings.synthesizer_model
        self._planner = PlannerService(
            self._client,
            self._planner_model,
            settings.planner_temperature,
            settings.max_plan_steps,
        )
        self._reviewer = ReviewerService(
            self._client,
            self._reviewer_model,
            settings.reviewer_temperature,
        )
        self._synthesizer = SynthesizerService(
            self._client,
            self._synthesizer_model,
            settings.synthesizer_temperature,
        )
        self._workspace = WorkspaceManager(settings.workspace_root)
        self._validator = CodingValidator()
        self._queue: asyncio.Queue[str | None] = asyncio.Queue()
        self._dispatch_task: asyncio.Task[None] | None = None
        self._worker_tasks: list[asyncio.Task[None]] = []
        self._stop = asyncio.Event()
        self._in_flight: set[str] = set()

    async def start(self) -> None:
        await self._db.initialize()
        await self._resolve_models()
        if self._settings.auto_resume_pending_runs:
            await self.resume_open_runs()
        self._dispatch_task = asyncio.create_task(self._dispatch_loop())
        self._worker_tasks = [
            asyncio.create_task(self._worker_loop(index))
            for index in range(max(1, self._settings.max_active_executions))
        ]
        logger.info("Orchestrator started with %s workers", len(self._worker_tasks))

    async def stop(self) -> None:
        self._stop.set()
        if self._dispatch_task:
            try:
                await asyncio.wait_for(self._dispatch_task, timeout=self._settings.task_poll_interval_seconds + 2)
            except (asyncio.TimeoutError, asyncio.CancelledError):
                self._dispatch_task.cancel()
                with suppress(asyncio.CancelledError):
                    await self._dispatch_task
        for _ in self._worker_tasks:
            await self._queue.put(None)
        for task in self._worker_tasks:
            try:
                await asyncio.wait_for(task, timeout=2)
            except (asyncio.TimeoutError, asyncio.CancelledError):
                task.cancel()
                with suppress(asyncio.CancelledError):
                    await task
        self._worker_tasks.clear()
        self._in_flight.clear()

    async def submit_run(self, run_id: str, goal: str, metadata: dict) -> None:
        await self._db.ensure_run_plan_task(run_id, goal, metadata)
        await self._db.update_run_status(run_id, RunStatus.planning)

    async def resume_open_runs(self) -> None:
        runs = await self._db.find_open_runs()
        for run in runs:
            await self._db.ensure_run_plan_task(run.id, run.goal, run.metadata)
            await self._db.unblock_dependent_tasks(run.id)

    async def _dispatch_loop(self) -> None:
        while not self._stop.is_set():
            try:
                ready_tasks = await self._db.fetch_ready_tasks(limit=self._settings.max_active_executions * 4)
                for task in ready_tasks:
                    if task.id in self._in_flight:
                        continue
                    await self._db.update_task_status(task.id, TaskStatus.queued, result=task.result, error=task.error)
                    self._in_flight.add(task.id)
                    await self._queue.put(task.id)
            except Exception:
                logger.exception("Dispatch loop failed")
            await asyncio.sleep(self._settings.task_poll_interval_seconds)

    async def _worker_loop(self, worker_index: int) -> None:
        while not self._stop.is_set():
            task_id = await self._queue.get()
            if task_id is None:
                self._queue.task_done()
                break
            try:
                await self._execute_task(task_id)
            except Exception as exc:
                logger.exception("Worker %s failed task %s", worker_index, task_id)
                await self._db.update_task_status(task_id, TaskStatus.failed, error=str(exc))
                task = await self._db.get_task(task_id)
                if task is not None:
                    await self._db.update_run_status(task.run_id, RunStatus.failed)
            finally:
                self._in_flight.discard(task_id)
                self._queue.task_done()

    async def _execute_task(self, task_id: str) -> None:
        task = await self._db.get_task(task_id)
        if task is None:
            return
        await self._db.update_task_status(task.id, TaskStatus.running, result=task.result, error=task.error)

        if task.task_type == TaskType.plan:
            await self._handle_plan_task(task)
        elif task.task_type == TaskType.execute:
            await self._handle_execute_task(task)
        elif task.task_type == TaskType.review:
            await self._handle_review_task(task)
        elif task.task_type == TaskType.synthesize:
            await self._handle_synthesize_task(task)

        await self._db.unblock_dependent_tasks(task.run_id)

    async def _handle_plan_task(self, task: TaskDetail) -> None:
        run = await self._db.get_run(task.run_id)
        if run is None:
            await self._db.update_task_status(task.id, TaskStatus.failed, error="Run not found.")
            return

        plan_goal = self._planning_goal(run.goal, run.metadata)
        plan = await self._planner.plan(plan_goal)
        execute_ids: list[str] = []
        for index, subtask in enumerate(plan.subtasks):
            depends_on = [execute_ids[dep] for dep in subtask.depends_on_indexes if dep < len(execute_ids)]
            execute_id = await self._db.create_task(
                run_id=run.id,
                parent_id=task.id,
                task_type=TaskType.execute,
                title=subtask.title,
                description=subtask.description,
                payload={
                    "goal": run.goal,
                    "acceptance_criteria": subtask.acceptance_criteria,
                    "output_format": subtask.output_format,
                    "role_hint": subtask.role_hint,
                    "plan_summary": plan.summary,
                    "sequence_index": index,
                },
                depends_on=depends_on,
                priority=max(1, subtask.priority),
            )
            execute_ids.append(execute_id)

        await self._db.create_artifact(
            run_id=run.id,
            task_id=task.id,
            kind="plan",
            content=plan.model_dump_json(indent=2),
            metadata={"summary": plan.summary},
        )
        await self._db.update_run_status(run.id, RunStatus.running)
        await self._db.update_task_status(
            task.id,
            TaskStatus.completed,
            result={"summary": plan.summary, "subtask_count": len(plan.subtasks)},
        )

    async def _handle_execute_task(self, task: TaskDetail) -> None:
        run = await self._db.get_run(task.run_id)
        if run is None:
            await self._db.update_task_status(task.id, TaskStatus.failed, error="Run not found.")
            return

        dependency_context = await self._collect_dependency_context(task.depends_on)
        feedback = task.payload.get("review_feedback")
        profile = infer_goal_profile(run.goal)
        model = self._worker_model
        task_phase = "general"
        reopen_context = self._reopen_context(run.metadata)
        project_root_override = self._reopen_project_root(run.metadata)
        if profile.domain == "coding":
            task_phase, phase_guidance = self._coding_phase_instructions(task)
            if task_phase in {"contract", "verification"}:
                prompt = WORKER_USER_TEMPLATE.format(
                    goal=run.goal,
                    title=task.title,
                    description=f"{task.description}\n\nPhase guidance:\n{phase_guidance}",
                    acceptance_criteria="\n".join(f"- {item}" for item in task.payload.get("acceptance_criteria", [])),
                    output_format=task.payload.get("output_format", "markdown"),
                    dependency_context=dependency_context or "None",
                    reopen_context=reopen_context or "None",
                    review_feedback=feedback if feedback else "None",
                )
                system_prompt = WORKER_SYSTEM_PROMPT
                model = self._reviewer_model
            else:
                prompt = CODE_WORKER_USER_TEMPLATE.format(
                    goal=run.goal,
                    title=task.title,
                    task_phase=task_phase,
                    description=task.description,
                    acceptance_criteria="\n".join(f"- {item}" for item in task.payload.get("acceptance_criteria", [])),
                    artifact_format=profile.artifact_format,
                    language_hint=profile.language_hint,
                    final_output_instruction=profile.final_output_instruction,
                    dependency_context=dependency_context or "None",
                    reopen_context=reopen_context or "None",
                    review_feedback=feedback if feedback else "None",
                    phase_guidance=phase_guidance,
                )
                system_prompt = CODE_WORKER_SYSTEM_PROMPT
        else:
            prompt = WORKER_USER_TEMPLATE.format(
                goal=run.goal,
                title=task.title,
                description=task.description,
                acceptance_criteria="\n".join(f"- {item}" for item in task.payload.get("acceptance_criteria", [])),
                output_format=task.payload.get("output_format", "markdown"),
                dependency_context=dependency_context or "None",
                reopen_context=reopen_context or "None",
                review_feedback=feedback if feedback else "None",
            )
            system_prompt = WORKER_SYSTEM_PROMPT
        output = await self._client.generate(
            model=model,
            system=system_prompt,
            prompt=prompt,
            temperature=self._settings.worker_temperature,
        )
        workspace_metadata: dict[str, object] = {}
        if profile.domain == "coding":
            workspace = self._workspace.materialize_task_output(
                run_id=task.run_id,
                task_id=task.id,
                goal=run.goal,
                title=task.title,
                sequence_index=task.payload.get("sequence_index"),
                profile=profile,
                phase=task_phase,
                raw_output=output,
                existing_root=project_root_override,
            )
            workspace_metadata = workspace.metadata()
            await self._db.create_artifact(
                run_id=task.run_id,
                task_id=task.id,
                kind="workspace",
                content=f"Workspace materialized at {workspace.root}",
                metadata=workspace_metadata,
            )

        artifact_id = await self._db.create_artifact(
            run_id=task.run_id,
            task_id=task.id,
            kind="worker_output",
            content=output,
            metadata={"task_title": task.title, "retry_count": task.retry_count},
        )
        await self._db.update_task_status(
            task.id,
            TaskStatus.completed,
            result={
                "artifact_id": artifact_id,
                "output_preview": output[:200],
                **workspace_metadata,
            },
            error=None,
        )
        review_task_id = await self._db.create_task(
            run_id=task.run_id,
            parent_id=task.id,
            task_type=TaskType.review,
            title=f"Review: {task.title}",
            description=f"Review the worker output for '{task.title}'.",
            payload={"target_task_id": task.id},
            depends_on=[task.id],
            priority=max(1, task.priority),
        )
        await self._gate_dependents_on_review(task.run_id, task.id, review_task_id)

    async def _handle_review_task(self, task: TaskDetail) -> None:
        target_task_id = task.payload["target_task_id"]
        target_task = await self._db.get_task(target_task_id)
        if target_task is None or not target_task.result or "artifact_id" not in target_task.result:
            await self._db.update_task_status(task.id, TaskStatus.failed, error="Review target missing artifact.")
            return

        artifacts = await self._db.list_artifacts(task.run_id)
        worker_artifact = next((item for item in artifacts if item.id == target_task.result["artifact_id"]), None)
        if worker_artifact is None:
            await self._db.update_task_status(task.id, TaskStatus.failed, error="Worker artifact not found.")
            return

        run = await self._db.get_run(task.run_id)
        if run is None:
            await self._db.update_task_status(task.id, TaskStatus.failed, error="Run not found.")
            return

        profile = infer_goal_profile(run.goal)
        if profile.domain == "coding":
            task_phase, phase_guidance = self._coding_review_instructions(target_task)
            workspace_path = None
            if target_task.result and target_task.result.get("workspace_path"):
                workspace_path = Path(str(target_task.result["workspace_path"]))
            validator_result = self._validator.validate(
                goal=run.goal,
                profile=profile,
                phase=task_phase,
                workspace_path=workspace_path,
            )
            await self._db.create_artifact(
                run_id=task.run_id,
                task_id=task.id,
                kind="validation",
                content=validator_result.summary,
                metadata={
                    "approved": validator_result.approved,
                    "issues": validator_result.issues,
                    "suggested_fixes": validator_result.suggested_fixes,
                },
            )
            prompt = CODE_REVIEWER_USER_TEMPLATE.format(
                title=target_task.title,
                task_phase=task_phase,
                description=target_task.description,
                acceptance_criteria="\n".join(f"- {item}" for item in target_task.payload.get("acceptance_criteria", [])),
                artifact_format=profile.artifact_format,
                language_hint=profile.language_hint,
                worker_output=worker_artifact.content,
                reopen_context=self._reopen_context(run.metadata) or "None",
                workspace_summary=self._format_workspace_summary(target_task.result),
                validator_summary=self._format_validator_summary(validator_result),
                phase_guidance=phase_guidance,
            )
            decision = self._coding_fallback_review(target_task, worker_artifact.content, validator_result)
            try:
                response = await self._client.generate(
                    model=self._reviewer_model,
                    system=CODE_REVIEWER_SYSTEM_PROMPT,
                    prompt=prompt,
                    temperature=self._settings.reviewer_temperature,
                )
                decision = ReviewDecision.model_validate(self._client.extract_json(response))
            except Exception:
                logger.warning("Coding review model failed for task %s; using fallback decision.", task.id, exc_info=True)
            decision = self._merge_validator_into_decision(decision, validator_result)
        else:
            decision = await self._reviewer.review(target_task, worker_artifact.content)
        await self._db.create_artifact(
            run_id=task.run_id,
            task_id=task.id,
            kind="review",
            content=decision.model_dump_json(indent=2),
            metadata={"approved": decision.approved, "target_task_id": target_task.id},
        )

        if decision.approved:
            await self._db.update_task_status(
                task.id,
                TaskStatus.completed,
                result=decision.model_dump(mode="json"),
                error=None,
            )
            await self._maybe_schedule_synthesis(task.run_id)
            return

        profile = infer_goal_profile(run.goal)
        max_retries = self._settings.max_review_retries + 2 if profile.domain == "coding" else self._settings.max_review_retries
        if target_task.retry_count >= max_retries:
            await self._db.update_task_status(
                target_task.id,
                TaskStatus.failed,
                error="Maximum review retries exceeded.",
                result=target_task.result,
            )
            await self._db.update_task_status(
                task.id,
                TaskStatus.rejected,
                result=decision.model_dump(mode="json"),
                error=decision.summary,
            )
            await self._db.update_run_status(task.run_id, RunStatus.failed)
            return

        await self._db.increment_retry(target_task.id)
        await self._db.replace_task_payload(
            target_task.id,
            {
                **target_task.payload,
                "review_feedback": {
                    "issues": decision.issues,
                    "suggested_fixes": decision.suggested_fixes,
                },
            },
        )
        await self._db.update_task_status(
            target_task.id,
            TaskStatus.needs_retry,
            error=decision.summary,
            result={"previous_artifact_id": target_task.result["artifact_id"]},
        )
        await self._db.update_task_status(
            task.id,
            TaskStatus.completed,
            result=decision.model_dump(mode="json"),
            error=None,
        )

    async def _handle_synthesize_task(self, task: TaskDetail) -> None:
        run = await self._db.get_run(task.run_id)
        if run is None:
            await self._db.update_task_status(task.id, TaskStatus.failed, error="Run not found.")
            return

        artifacts = await self._approved_worker_outputs(run.id)
        profile = infer_goal_profile(run.goal)
        if profile.domain == "coding":
            final_output = await self._select_coding_final_output(run.id, artifacts, run.goal)
            final_workspace = self._workspace.materialize_final_output(
                run_id=run.id,
                goal=run.goal,
                profile=profile,
                raw_output=final_output,
                existing_root=self._reopen_project_root(run.metadata),
            )
            final_metadata = {"goal": run.goal, **final_workspace.metadata()}
        else:
            final_output = await self._synthesizer.synthesize(run.goal, artifacts)
            final_metadata = {"goal": run.goal}
        artifact_id = await self._db.create_artifact(
            run_id=run.id,
            task_id=task.id,
            kind="final",
            content=final_output,
            metadata=final_metadata,
        )
        await self._db.set_final_artifact(run.id, artifact_id)
        await self._db.update_run_status(run.id, RunStatus.completed)
        await self._db.update_task_status(
            task.id,
            TaskStatus.completed,
            result={"artifact_id": artifact_id},
            error=None,
        )

    async def _gate_dependents_on_review(self, run_id: str, execute_task_id: str, review_task_id: str) -> None:
        tasks = await self._db.list_tasks(run_id)
        for candidate in tasks:
            if candidate.id == review_task_id or candidate.task_type == TaskType.review:
                continue
            if execute_task_id not in candidate.depends_on:
                continue
            if review_task_id in candidate.depends_on:
                continue
            new_dependencies = list(candidate.depends_on)
            new_dependencies.append(review_task_id)
            await self._db.replace_task_dependencies(candidate.id, new_dependencies)

    async def _resolve_models(self) -> None:
        available = await self._client.list_models()
        if not available:
            raise OllamaError("No Ollama models are installed. Run `ollama pull <model>` first.")

        self._planner_model = self._pick_model(self._planner_model, available, role="planner")
        self._worker_model = self._pick_model(self._worker_model, available, role="worker")
        self._reviewer_model = self._pick_model(self._reviewer_model, available, role="reviewer")
        self._synthesizer_model = self._pick_model(self._synthesizer_model, available, role="synthesizer")

        self._planner = PlannerService(
            self._client,
            self._planner_model,
            self._settings.planner_temperature,
            self._settings.max_plan_steps,
        )
        self._reviewer = ReviewerService(
            self._client,
            self._reviewer_model,
            self._settings.reviewer_temperature,
        )
        self._synthesizer = SynthesizerService(
            self._client,
            self._synthesizer_model,
            self._settings.synthesizer_temperature,
        )

    @staticmethod
    def _pick_model(configured: str, available: list[str], *, role: str) -> str:
        if configured in available:
            return configured

        if role == "worker":
            preferred = [
                "qwen3-coder:30b",
                "gpt-oss:20b",
                "deepseek-r1:8b",
                "qwen3:4b",
                "llama3.1:8b",
                "gemma4:latest",
                "mistral-nemo:latest",
                "ministral-3:8b",
            ]
        elif role == "reviewer":
            preferred = [
                "gpt-oss:20b",
                "qwen3-coder:30b",
                "deepseek-r1:8b",
                "qwen3:4b",
                "llama3.1:8b",
                "gemma4:latest",
                "mistral-nemo:latest",
                "ministral-3:8b",
            ]
        else:
            preferred = [
                "qwen3:4b",
                "gpt-oss:20b",
                "llama3.1:8b",
                "gemma4:latest",
                "mistral-nemo:latest",
                "ministral-3:8b",
                "deepseek-r1:8b",
                "qwen3-coder:30b",
            ]
        for candidate in preferred:
            if candidate in available:
                logger.warning(
                    "Configured %s model '%s' not installed. Falling back to '%s'.",
                    role,
                    configured,
                    candidate,
                )
                return candidate

        fallback = available[0]
        logger.warning(
            "Configured %s model '%s' not installed. Falling back to first available model '%s'.",
            role,
            configured,
            fallback,
        )
        return fallback

    async def _collect_dependency_context(self, dependency_ids: list[str]) -> str:
        if not dependency_ids:
            return ""
        tasks = [await self._db.get_task(task_id) for task_id in dependency_ids]
        run_id = next((task.run_id for task in tasks if task is not None), None)
        if run_id is None:
            return ""
        artifacts = await self._db.list_artifacts(run_id)
        lines: list[str] = []
        for task in tasks:
            if task is None or not task.result:
                continue
            artifact_id = task.result.get("artifact_id")
            if not artifact_id:
                continue
            artifact = next((item for item in artifacts if item.id == artifact_id), None)
            if artifact:
                lines.append(f"{task.title}:\n{artifact.content}")
        return "\n\n".join(lines)

    async def _approved_worker_outputs(self, run_id: str) -> list[str]:
        tasks = await self._db.list_tasks(run_id)
        latest_reviews = self._latest_reviews_by_target(tasks)
        artifacts = await self._db.list_artifacts(run_id)
        outputs: list[str] = []
        for task in tasks:
            if task.task_type != TaskType.execute or not task.result:
                continue
            review = latest_reviews.get(task.id)
            if review is None or not review.result or not review.result.get("approved"):
                continue
            artifact_id = task.result.get("artifact_id")
            artifact = next((item for item in artifacts if item.id == artifact_id), None)
            if artifact:
                outputs.append(artifact.content)
        return outputs

    async def _select_coding_final_output(self, run_id: str, artifacts: list[str], goal: str) -> str:
        tasks = await self._db.list_tasks(run_id)
        latest_reviews = self._latest_reviews_by_target(tasks)
        run_artifacts = await self._db.list_artifacts(run_id)

        preferred_titles = (
            "Produce corrected final artifact",
            "Build complete runnable artifact",
        )
        for preferred_title in preferred_titles:
            for task in reversed(tasks):
                if task.task_type != TaskType.execute or task.title != preferred_title or not task.result:
                    continue
                review = latest_reviews.get(task.id)
                if review is None or not review.result or not review.result.get("approved"):
                    continue
                artifact_id = task.result.get("artifact_id")
                artifact = next((item for item in run_artifacts if item.id == artifact_id), None)
                if artifact and artifact.content.strip():
                    return artifact.content

        if artifacts:
            return artifacts[-1]

        profile = infer_goal_profile(goal)
        prompt = CODE_SYNTHESIZER_USER_TEMPLATE.format(
            goal=goal,
            artifact_format=profile.artifact_format,
            language_hint=profile.language_hint,
            final_output_instruction=profile.final_output_instruction,
            artifacts="\n\n".join(f"Subtask {index + 1}:\n{artifact}" for index, artifact in enumerate(artifacts)),
        )
        return await self._client.generate(
            model=self._synthesizer_model,
            system=CODE_SYNTHESIZER_SYSTEM_PROMPT,
            prompt=prompt,
            temperature=self._settings.synthesizer_temperature,
        )

    @staticmethod
    def _coding_phase(task: TaskDetail) -> str:
        title = task.title.lower()
        if "contract" in title:
            return "contract"
        if "verify" in title:
            return "verification"
        if "corrected final" in title or "final artifact" in title:
            return "finalization"
        return "implementation"

    def _coding_phase_instructions(self, task: TaskDetail) -> tuple[str, str]:
        phase = self._coding_phase(task)
        if phase == "contract":
            return (
                phase,
                (
                    "Return a concise implementation contract only. Do not return source code. "
                    "Describe deliverable shape, game rules, state model, UI interactions, win conditions, and edge cases. "
                    "If an existing artifact is provided, describe the required modifications while preserving unaffected behavior."
                ),
            )
        if phase == "verification":
            return (
                phase,
                (
                    "Inspect the implementation from dependency context and return a concrete verification report. "
                    "Use bullets. Identify exact defects, missing rules, and edge cases, or state that coverage looks complete. "
                    "When revising an existing artifact, focus on whether the requested changes were applied correctly without regressions."
                ),
            )
        if phase == "finalization":
            return (
                phase,
                (
                    "Return the corrected final runnable artifact only. Incorporate the verification findings from dependency context "
                    "and ensure previously reported defects are fixed in this full artifact. "
                    "If an existing artifact is provided, modify it rather than redesigning unrelated parts."
                ),
            )
        return (
            phase,
            (
                "Return a full runnable implementation that follows the implementation contract from dependency context. "
                "Do not return fragments, TODOs, or explanatory prose outside the artifact. "
                "If an existing artifact is provided, treat it as the base implementation and preserve unrelated working behavior."
            ),
        )

    def _coding_review_instructions(self, task: TaskDetail) -> tuple[str, str]:
        phase = self._coding_phase(task)
        if phase == "contract":
            return (
                phase,
                (
                    "Approve when the output is a clear specification rather than code and it defines required behavior, components, "
                    "state, and edge cases. Reject if it mostly returns implementation code instead of a contract."
                ),
            )
        if phase == "verification":
            return (
                phase,
                (
                    "Approve when the output is a concrete audit of the implementation with specific findings or an explicit coverage verdict. "
                    "Do not reject just because the output is not code."
                ),
            )
        if phase == "finalization":
            return (
                phase,
                (
                    "Approve only if the artifact appears complete, runnable, and addresses the defects found during verification. "
                    "Reject for material remaining logic gaps."
                ),
            )
        return (
            phase,
            (
                "Approve only if the artifact is a full runnable implementation that matches the contract and requested behavior. "
                "Reject partial implementations, pseudocode, or artifacts missing core rules."
            ),
        )

    @staticmethod
    def _planning_goal(goal: str, metadata: dict[str, object]) -> str:
        reopen = metadata.get("reopen")
        if not isinstance(reopen, dict):
            return goal
        instructions = str(reopen.get("instructions", "")).strip()
        if not instructions:
            return goal
        return (
            f"{goal}\n\n"
            "Revision request:\n"
            f"{instructions}\n\n"
            "Modify the existing deliverable instead of starting over. Preserve unaffected behavior."
        )

    def _reopen_context(self, metadata: dict[str, object]) -> str:
        reopen = metadata.get("reopen")
        if not isinstance(reopen, dict):
            return ""

        lines = [
            f"Source run: {reopen.get('source_run_id', 'unknown')}",
            f"Original goal: {reopen.get('source_goal', 'unknown')}",
            f"Requested change: {reopen.get('instructions', 'none')}",
        ]
        workspace_path = reopen.get("source_workspace_path")
        entrypoint = reopen.get("source_entrypoint")
        files = reopen.get("source_files")
        if workspace_path:
            lines.append(f"Source workspace: {workspace_path}")
        if entrypoint:
            lines.append(f"Source entrypoint: {entrypoint}")
        if isinstance(files, list) and files:
            lines.append("Source files:")
            lines.extend(f"- {item}" for item in files)
        file_context = self._source_workspace_file_context(reopen)
        if file_context:
            lines.append("Source file contents:")
            lines.append(file_context)
        return "\n".join(lines)

    @staticmethod
    def _reopen_project_root(metadata: dict[str, object]) -> Path | None:
        reopen = metadata.get("reopen")
        if not isinstance(reopen, dict):
            return None
        project_root = reopen.get("source_project_root")
        if isinstance(project_root, str) and project_root.strip():
            return Path(project_root)
        workspace_path = reopen.get("source_workspace_path")
        if isinstance(workspace_path, str) and workspace_path.strip():
            return Path(workspace_path).parent
        return None

    @staticmethod
    def _source_workspace_file_context(reopen: dict[str, object], *, max_chars: int = 60000) -> str:
        workspace_path = reopen.get("source_workspace_path")
        if not isinstance(workspace_path, str):
            return ""
        root = Path(workspace_path)
        files = reopen.get("source_files")
        if not root.exists() or not isinstance(files, list):
            return ""

        chunks: list[str] = []
        remaining = max_chars
        for item in files:
            if not isinstance(item, str) or item == "artifact_manifest.json":
                continue
            file_path = root / item
            if not file_path.exists() or not file_path.is_file():
                continue
            suffix = file_path.suffix.lstrip(".") or "text"
            content = file_path.read_text(encoding="utf-8")
            chunk = f"FILE: {item}\n```{suffix}\n{content}\n```"
            if len(chunk) > remaining and chunks:
                break
            if len(chunk) > remaining:
                body_budget = max(2000, remaining - len(f"FILE: {item}\n```{suffix}\n\n```") - 32)
                head_budget = body_budget // 2
                tail_budget = body_budget - head_budget
                excerpt = (
                    content[:head_budget]
                    + "\n\n[... existing file truncated for prompt size ...]\n\n"
                    + content[-tail_budget:]
                )
                chunk = f"FILE: {item}\n```{suffix}\n{excerpt}\n```"
            chunks.append(chunk)
            remaining -= len(chunk)
            if remaining <= 0:
                break
        return "\n\n".join(chunks)

    @staticmethod
    def _format_workspace_summary(result: dict[str, object] | None) -> str:
        if not result:
            return "No workspace materialized."
        workspace_path = result.get("workspace_path", "unknown")
        entrypoint = result.get("entrypoint") or "none"
        files = result.get("files", [])
        file_lines = "\n".join(f"- {item}" for item in files) if isinstance(files, list) and files else "- none"
        return f"Workspace: {workspace_path}\nEntrypoint: {entrypoint}\nFiles:\n{file_lines}"

    @staticmethod
    def _format_validator_summary(result: ValidationResult) -> str:
        lines = [f"Summary: {result.summary}", f"Approved: {result.approved}"]
        if result.issues:
            lines.append("Issues:")
            lines.extend(f"- {issue}" for issue in result.issues)
        if result.suggested_fixes:
            lines.append("Suggested fixes:")
            lines.extend(f"- {fix}" for fix in result.suggested_fixes)
        return "\n".join(lines)

    @staticmethod
    def _coding_fallback_review(
        task: TaskDetail,
        worker_output: str,
        validator_result: ValidationResult,
    ) -> ReviewDecision:
        if not validator_result.approved:
            return ReviewDecision(
                approved=False,
                summary=validator_result.summary,
                issues=list(validator_result.issues),
                suggested_fixes=list(validator_result.suggested_fixes),
            )
        phase = Orchestrator._coding_phase(task)
        summary = (
            "Coding fallback reviewer accepted the analytical output."
            if phase in {"contract", "verification"}
            else "Coding fallback reviewer accepted the runnable artifact."
        )
        return ReviewDecision(
            approved=bool(worker_output.strip()),
            summary=summary,
            issues=[],
            suggested_fixes=[],
        )

    @staticmethod
    def _merge_validator_into_decision(decision: ReviewDecision, validator_result: ValidationResult) -> ReviewDecision:
        if validator_result.approved:
            return decision
        return ReviewDecision(
            approved=False,
            summary=validator_result.summary,
            issues=list(dict.fromkeys([*validator_result.issues, *decision.issues])),
            suggested_fixes=list(dict.fromkeys([*validator_result.suggested_fixes, *decision.suggested_fixes])),
        )

    async def _maybe_schedule_synthesis(self, run_id: str) -> None:
        tasks = await self._db.list_tasks(run_id)
        execute_tasks = [task for task in tasks if task.task_type == TaskType.execute]
        if not execute_tasks:
            return
        latest_reviews = self._latest_reviews_by_target(tasks)
        if len(latest_reviews) < len(execute_tasks):
            return
        for execute_task in execute_tasks:
            review = latest_reviews.get(execute_task.id)
            if review is None:
                return
            if review.status not in {TaskStatus.completed, TaskStatus.rejected}:
                return
            if not review.result or not review.result.get("approved"):
                return
        if await self._db.has_task_type(run_id, TaskType.synthesize):
            return

        await self._db.update_run_status(run_id, RunStatus.synthesizing)
        await self._db.create_task(
            run_id=run_id,
            task_type=TaskType.synthesize,
            title="Synthesize final result",
            description="Merge all approved outputs into a final deliverable.",
            payload={},
            depends_on=[review.id for review in latest_reviews.values()],
            priority=999,
        )

    @staticmethod
    def _latest_reviews_by_target(tasks: list[TaskDetail]) -> dict[str, TaskDetail]:
        latest: dict[str, TaskDetail] = {}
        for task in tasks:
            if task.task_type != TaskType.review:
                continue
            target = task.payload.get("target_task_id")
            if not target:
                continue
            current = latest.get(target)
            if current is None or task.updated_at > current.updated_at:
                latest[target] = task
        return latest
