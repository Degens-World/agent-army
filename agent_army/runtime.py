from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from agent_army.config import Settings
from agent_army.db import Database
from agent_army.models import ArtifactDetail, RunDetail, TaskDetail, TaskType
from agent_army.orchestrator import Orchestrator


@dataclass(slots=True)
class AgentArmyRuntime:
    settings: Settings
    db: Database
    orchestrator: Orchestrator

    @classmethod
    def from_settings(cls, settings: Settings) -> "AgentArmyRuntime":
        db = Database(settings.db_path)
        orchestrator = Orchestrator(db, settings)
        return cls(settings=settings, db=db, orchestrator=orchestrator)

    async def start(self) -> None:
        await self.orchestrator.start()

    async def stop(self) -> None:
        await self.orchestrator.stop()

    async def create_run(
        self,
        *,
        goal: str,
        metadata: dict[str, Any] | None = None,
        max_parallelism: int | None = None,
    ) -> str:
        payload = dict(metadata or {})
        if max_parallelism is not None:
            payload["requested_max_parallelism"] = max_parallelism
        run_id = await self.db.create_run(goal=goal, metadata=payload)
        await self.orchestrator.submit_run(run_id, goal, payload)
        return run_id

    async def reopen_run(
        self,
        *,
        source_run_id: str,
        instructions: str,
        metadata: dict[str, Any] | None = None,
        max_parallelism: int | None = None,
    ) -> str:
        source_run = await self.db.get_run(source_run_id)
        if source_run is None:
            raise ValueError(f"Run not found: {source_run_id}")

        payload = dict(metadata or {})
        payload["reopen"] = await self._build_reopen_metadata(source_run, instructions)
        if max_parallelism is not None:
            payload["requested_max_parallelism"] = max_parallelism

        run_id = await self.db.create_run(goal=source_run.goal, metadata=payload)
        await self.orchestrator.submit_run(run_id, source_run.goal, payload)
        return run_id

    async def _build_reopen_metadata(self, source_run: RunDetail, instructions: str) -> dict[str, Any]:
        source_artifact, source_task = await self._resolve_reopen_source(source_run.id)
        metadata: dict[str, Any] = {
            "source_run_id": source_run.id,
            "source_goal": source_run.goal,
            "instructions": instructions,
        }
        if source_artifact is not None:
            metadata["source_artifact_id"] = source_artifact.id
            metadata["source_artifact_kind"] = source_artifact.kind
            if source_task is not None:
                metadata["source_task_title"] = source_task.title
            for key in ("workspace_path", "entrypoint", "files", "manifest_path"):
                value = source_artifact.metadata.get(key)
                if value is not None:
                    metadata[f"source_{key}"] = value
            workspace_path = source_artifact.metadata.get("workspace_path")
            if isinstance(workspace_path, str):
                from pathlib import Path

                metadata["source_project_root"] = str(Path(workspace_path).parent)
        return metadata

    async def _resolve_reopen_source(self, run_id: str) -> tuple[ArtifactDetail | None, TaskDetail | None]:
        run = await self.db.get_run(run_id)
        if run is None:
            return None, None
        artifacts = await self.db.list_artifacts(run_id)
        if run.final_artifact_id:
            final_artifact = next((artifact for artifact in artifacts if artifact.id == run.final_artifact_id), None)
            if final_artifact is not None:
                return final_artifact, None

        tasks = await self.db.list_tasks(run_id)
        execute_tasks = [task for task in tasks if task.task_type is TaskType.execute and task.result and task.result.get("artifact_id")]
        execute_tasks.sort(key=lambda task: task.updated_at, reverse=True)
        for task in execute_tasks:
            artifact = next((item for item in artifacts if item.id == task.result["artifact_id"]), None)
            if artifact is not None:
                return artifact, task
        return None, None
