from __future__ import annotations

import asyncio
import logging
from contextlib import suppress

from agent_army.config import Settings
from agent_army.db import Database
from agent_army.models import RunStatus, TaskDetail, TaskStatus, TaskType
from agent_army.prompts import WORKER_SYSTEM_PROMPT, WORKER_USER_TEMPLATE
from agent_army.services.ollama import OllamaClient
from agent_army.services.planner import PlannerService
from agent_army.services.reviewer import ReviewerService
from agent_army.services.synthesizer import SynthesizerService

logger = logging.getLogger(__name__)


class Orchestrator:
    def __init__(self, db: Database, settings: Settings) -> None:
        self._db = db
        self._settings = settings
        self._client = OllamaClient(settings.ollama_host, settings.request_timeout_seconds)
        self._planner = PlannerService(
            self._client,
            settings.planner_model,
            settings.planner_temperature,
            settings.max_plan_steps,
        )
        self._reviewer = ReviewerService(
            self._client,
            settings.reviewer_model,
            settings.reviewer_temperature,
        )
        self._synthesizer = SynthesizerService(
            self._client,
            settings.synthesizer_model,
            settings.synthesizer_temperature,
        )
        self._queue: asyncio.Queue[str] = asyncio.Queue()
        self._dispatch_task: asyncio.Task[None] | None = None
        self._worker_tasks: list[asyncio.Task[None]] = []
        self._stop = asyncio.Event()
        self._in_flight: set[str] = set()

    async def start(self) -> None:
        await self._db.initialize()
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
            self._dispatch_task.cancel()
            with suppress(asyncio.CancelledError):
                await self._dispatch_task
        for task in self._worker_tasks:
            task.cancel()
        for task in self._worker_tasks:
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
            try:
                await self._execute_task(task_id)
            except Exception as exc:
                logger.exception("Worker %s failed task %s", worker_index, task_id)
                await self._db.update_task_status(task_id, TaskStatus.failed, error=str(exc))
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

        plan = await self._planner.plan(run.goal)
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
        prompt = WORKER_USER_TEMPLATE.format(
            goal=run.goal,
            title=task.title,
            description=task.description,
            acceptance_criteria="\n".join(f"- {item}" for item in task.payload.get("acceptance_criteria", [])),
            output_format=task.payload.get("output_format", "markdown"),
            dependency_context=dependency_context or "None",
            review_feedback=feedback if feedback else "None",
        )
        output = await self._client.generate(
            model=self._settings.worker_model,
            system=WORKER_SYSTEM_PROMPT,
            prompt=prompt,
            temperature=self._settings.worker_temperature,
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
            result={"artifact_id": artifact_id, "output_preview": output[:200]},
            error=None,
        )
        await self._db.create_task(
            run_id=task.run_id,
            parent_id=task.id,
            task_type=TaskType.review,
            title=f"Review: {task.title}",
            description=f"Review the worker output for '{task.title}'.",
            payload={"target_task_id": task.id},
            depends_on=[task.id],
            priority=max(1, task.priority),
        )

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

        if target_task.retry_count >= self._settings.max_review_retries:
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
        final_output = await self._synthesizer.synthesize(run.goal, artifacts)
        artifact_id = await self._db.create_artifact(
            run_id=run.id,
            task_id=task.id,
            kind="final",
            content=final_output,
            metadata={"goal": run.goal},
        )
        await self._db.set_final_artifact(run.id, artifact_id)
        await self._db.update_run_status(run.id, RunStatus.completed)
        await self._db.update_task_status(
            task.id,
            TaskStatus.completed,
            result={"artifact_id": artifact_id},
            error=None,
        )

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
