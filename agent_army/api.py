from __future__ import annotations

from contextlib import asynccontextmanager

from fastapi import FastAPI, HTTPException

from agent_army.config import get_settings
from agent_army.db import Database
from agent_army.models import ArtifactDetail, RunCreate, RunCreated, RunDetail, RunStatus, RunSummary, TaskDetail
from agent_army.orchestrator import Orchestrator


def create_app() -> FastAPI:
    settings = get_settings()
    db = Database(settings.db_path)
    orchestrator = Orchestrator(db, settings)

    @asynccontextmanager
    async def lifespan(_: FastAPI):
        await orchestrator.start()
        try:
            yield
        finally:
            await orchestrator.stop()

    app = FastAPI(title=settings.app_name, lifespan=lifespan)

    @app.get("/health")
    async def health() -> dict[str, str]:
        return {"status": "ok"}

    @app.post("/runs", response_model=RunCreated)
    async def create_run(payload: RunCreate) -> RunCreated:
        metadata = dict(payload.metadata)
        if payload.max_parallelism is not None:
            metadata["requested_max_parallelism"] = payload.max_parallelism
        run_id = await db.create_run(goal=payload.goal, metadata=metadata)
        await orchestrator.submit_run(run_id, payload.goal, metadata)
        return RunCreated(run_id=run_id, status=RunStatus.planning)

    @app.get("/runs", response_model=list[RunSummary])
    async def list_runs() -> list[RunSummary]:
        return await db.list_runs()

    @app.get("/runs/{run_id}", response_model=RunDetail)
    async def get_run(run_id: str) -> RunDetail:
        run = await db.get_run(run_id)
        if run is None:
            raise HTTPException(status_code=404, detail="Run not found.")
        return run

    @app.get("/runs/{run_id}/tasks", response_model=list[TaskDetail])
    async def list_tasks(run_id: str) -> list[TaskDetail]:
        run = await db.get_run(run_id)
        if run is None:
            raise HTTPException(status_code=404, detail="Run not found.")
        return await db.list_tasks(run_id)

    @app.get("/runs/{run_id}/artifacts", response_model=list[ArtifactDetail])
    async def list_artifacts(run_id: str) -> list[ArtifactDetail]:
        run = await db.get_run(run_id)
        if run is None:
            raise HTTPException(status_code=404, detail="Run not found.")
        return await db.list_artifacts(run_id)

    @app.post("/runs/{run_id}/resume", response_model=RunDetail)
    async def resume_run(run_id: str) -> RunDetail:
        run = await db.get_run(run_id)
        if run is None:
            raise HTTPException(status_code=404, detail="Run not found.")
        await orchestrator.submit_run(run_id, run.goal, run.metadata)
        refreshed = await db.get_run(run_id)
        assert refreshed is not None
        return refreshed

    return app
