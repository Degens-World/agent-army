from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from agent_army.config import Settings
from agent_army.db import Database
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
