from __future__ import annotations

import json
from collections import Counter
from contextlib import asynccontextmanager
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, AsyncIterator
from uuid import uuid4

import aiosqlite

from agent_army.models import ArtifactDetail, RunDetail, RunStatus, RunSummary, TaskDetail, TaskStatus, TaskType


def utcnow() -> datetime:
    return datetime.now(tz=UTC)


def dump_json(value: dict[str, Any] | list[Any] | None) -> str:
    if value is None:
        value = {}
    return json.dumps(value, ensure_ascii=True)


def load_json(value: str | None, default: Any) -> Any:
    if not value:
        return default
    return json.loads(value)


def parse_datetime(value: str) -> datetime:
    return datetime.fromisoformat(value)


@dataclass(slots=True)
class Database:
    path: Path

    @staticmethod
    async def _fetchone(conn: aiosqlite.Connection, query: str, params: tuple[Any, ...]) -> aiosqlite.Row | None:
        cursor = await conn.execute(query, params)
        try:
            return await cursor.fetchone()
        finally:
            await cursor.close()

    @asynccontextmanager
    async def connect(self) -> AsyncIterator[aiosqlite.Connection]:
        conn = await aiosqlite.connect(self.path)
        conn.row_factory = aiosqlite.Row
        await conn.execute("PRAGMA journal_mode=WAL;")
        await conn.execute("PRAGMA foreign_keys=ON;")
        try:
            yield conn
        finally:
            await conn.close()

    async def initialize(self) -> None:
        async with self.connect() as conn:
            await conn.executescript(
                """
                CREATE TABLE IF NOT EXISTS runs (
                    id TEXT PRIMARY KEY,
                    goal TEXT NOT NULL,
                    status TEXT NOT NULL,
                    metadata_json TEXT NOT NULL,
                    final_artifact_id TEXT,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );

                CREATE TABLE IF NOT EXISTS tasks (
                    id TEXT PRIMARY KEY,
                    run_id TEXT NOT NULL,
                    parent_id TEXT,
                    task_type TEXT NOT NULL,
                    title TEXT NOT NULL,
                    description TEXT NOT NULL,
                    status TEXT NOT NULL,
                    priority INTEGER NOT NULL,
                    depends_on_json TEXT NOT NULL,
                    payload_json TEXT NOT NULL,
                    result_json TEXT,
                    error TEXT,
                    retry_count INTEGER NOT NULL DEFAULT 0,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    FOREIGN KEY(run_id) REFERENCES runs(id) ON DELETE CASCADE
                );

                CREATE INDEX IF NOT EXISTS idx_tasks_run_id ON tasks(run_id);
                CREATE INDEX IF NOT EXISTS idx_tasks_status ON tasks(status);
                CREATE INDEX IF NOT EXISTS idx_tasks_type ON tasks(task_type);

                CREATE TABLE IF NOT EXISTS artifacts (
                    id TEXT PRIMARY KEY,
                    run_id TEXT NOT NULL,
                    task_id TEXT,
                    kind TEXT NOT NULL,
                    content TEXT NOT NULL,
                    metadata_json TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    FOREIGN KEY(run_id) REFERENCES runs(id) ON DELETE CASCADE
                );
                """
            )
            await conn.commit()

    async def create_run(
        self,
        *,
        goal: str,
        metadata: dict[str, Any],
        status: RunStatus = RunStatus.queued,
    ) -> str:
        run_id = str(uuid4())
        now = utcnow().isoformat()
        async with self.connect() as conn:
            await conn.execute(
                """
                INSERT INTO runs (id, goal, status, metadata_json, created_at, updated_at)
                VALUES (?, ?, ?, ?, ?, ?)
                """,
                (run_id, goal, status.value, dump_json(metadata), now, now),
            )
            await conn.commit()
        return run_id

    async def get_run(self, run_id: str) -> RunDetail | None:
        async with self.connect() as conn:
            row = await self._fetchone(conn, "SELECT * FROM runs WHERE id = ?", (run_id,))
            if row is None:
                return None
            task_rows = await conn.execute_fetchall("SELECT status FROM tasks WHERE run_id = ?", (run_id,))
            counts = Counter(task_row["status"] for task_row in task_rows)

            final_artifact = None
            if row["final_artifact_id"]:
                artifact_row = await self._fetchone(
                    conn,
                    "SELECT * FROM artifacts WHERE id = ?",
                    (row["final_artifact_id"],),
                )
                if artifact_row:
                    final_artifact = self._artifact_from_row(artifact_row).model_dump(mode="json")

        return RunDetail(
            id=row["id"],
            goal=row["goal"],
            status=RunStatus(row["status"]),
            created_at=parse_datetime(row["created_at"]),
            updated_at=parse_datetime(row["updated_at"]),
            final_artifact_id=row["final_artifact_id"],
            metadata=load_json(row["metadata_json"], {}),
            task_counts=dict(counts),
            final_artifact=final_artifact,
        )

    async def list_runs(self) -> list[RunSummary]:
        async with self.connect() as conn:
            rows = await conn.execute_fetchall("SELECT * FROM runs ORDER BY created_at DESC")
        return [
            RunSummary(
                id=row["id"],
                goal=row["goal"],
                status=RunStatus(row["status"]),
                created_at=parse_datetime(row["created_at"]),
                updated_at=parse_datetime(row["updated_at"]),
                final_artifact_id=row["final_artifact_id"],
                metadata=load_json(row["metadata_json"], {}),
            )
            for row in rows
        ]

    async def create_task(
        self,
        *,
        run_id: str,
        task_type: TaskType,
        title: str,
        description: str,
        payload: dict[str, Any],
        depends_on: list[str] | None = None,
        priority: int = 5,
        parent_id: str | None = None,
        status: TaskStatus = TaskStatus.pending,
    ) -> str:
        task_id = str(uuid4())
        now = utcnow().isoformat()
        async with self.connect() as conn:
            await conn.execute(
                """
                INSERT INTO tasks (
                    id, run_id, parent_id, task_type, title, description, status, priority,
                    depends_on_json, payload_json, result_json, error, retry_count, created_at, updated_at
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, NULL, NULL, 0, ?, ?)
                """,
                (
                    task_id,
                    run_id,
                    parent_id,
                    task_type.value,
                    title,
                    description,
                    status.value,
                    priority,
                    dump_json(depends_on or []),
                    dump_json(payload),
                    now,
                    now,
                ),
            )
            await conn.commit()
        return task_id

    async def get_task(self, task_id: str) -> TaskDetail | None:
        async with self.connect() as conn:
            row = await self._fetchone(conn, "SELECT * FROM tasks WHERE id = ?", (task_id,))
        return self._task_from_row(row) if row else None

    async def list_tasks(self, run_id: str) -> list[TaskDetail]:
        async with self.connect() as conn:
            rows = await conn.execute_fetchall(
                "SELECT * FROM tasks WHERE run_id = ? ORDER BY priority ASC, created_at ASC",
                (run_id,),
            )
        return [self._task_from_row(row) for row in rows]

    async def list_artifacts(self, run_id: str) -> list[ArtifactDetail]:
        async with self.connect() as conn:
            rows = await conn.execute_fetchall(
                "SELECT * FROM artifacts WHERE run_id = ? ORDER BY created_at ASC",
                (run_id,),
            )
        return [self._artifact_from_row(row) for row in rows]

    async def create_artifact(
        self,
        *,
        run_id: str,
        task_id: str | None,
        kind: str,
        content: str,
        metadata: dict[str, Any] | None = None,
    ) -> str:
        artifact_id = str(uuid4())
        now = utcnow().isoformat()
        async with self.connect() as conn:
            await conn.execute(
                """
                INSERT INTO artifacts (id, run_id, task_id, kind, content, metadata_json, created_at)
                VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (artifact_id, run_id, task_id, kind, content, dump_json(metadata), now),
            )
            await conn.commit()
        return artifact_id

    async def update_run_status(self, run_id: str, status: RunStatus) -> None:
        now = utcnow().isoformat()
        async with self.connect() as conn:
            await conn.execute(
                "UPDATE runs SET status = ?, updated_at = ? WHERE id = ?",
                (status.value, now, run_id),
            )
            await conn.commit()

    async def set_final_artifact(self, run_id: str, artifact_id: str) -> None:
        now = utcnow().isoformat()
        async with self.connect() as conn:
            await conn.execute(
                "UPDATE runs SET final_artifact_id = ?, updated_at = ? WHERE id = ?",
                (artifact_id, now, run_id),
            )
            await conn.commit()

    async def update_task_status(
        self,
        task_id: str,
        status: TaskStatus,
        *,
        error: str | None = None,
        result: dict[str, Any] | None = None,
    ) -> None:
        now = utcnow().isoformat()
        async with self.connect() as conn:
            await conn.execute(
                """
                UPDATE tasks
                SET status = ?, error = ?, result_json = ?, updated_at = ?
                WHERE id = ?
                """,
                (status.value, error, dump_json(result) if result is not None else None, now, task_id),
            )
            await conn.commit()

    async def increment_retry(self, task_id: str) -> None:
        now = utcnow().isoformat()
        async with self.connect() as conn:
            await conn.execute(
                "UPDATE tasks SET retry_count = retry_count + 1, updated_at = ? WHERE id = ?",
                (now, task_id),
            )
            await conn.commit()

    async def replace_task_payload(self, task_id: str, payload: dict[str, Any]) -> None:
        now = utcnow().isoformat()
        async with self.connect() as conn:
            await conn.execute(
                "UPDATE tasks SET payload_json = ?, updated_at = ? WHERE id = ?",
                (dump_json(payload), now, task_id),
            )
            await conn.commit()

    async def ensure_run_plan_task(self, run_id: str, goal: str, metadata: dict[str, Any]) -> str:
        async with self.connect() as conn:
            row = await self._fetchone(
                conn,
                "SELECT id FROM tasks WHERE run_id = ? AND task_type = ? LIMIT 1",
                (run_id, TaskType.plan.value),
            )
        if row:
            return row["id"]
        return await self.create_task(
            run_id=run_id,
            task_type=TaskType.plan,
            title="Plan run",
            description="Break the top-level goal into parallelizable subtasks.",
            payload={"goal": goal, "metadata": metadata},
            priority=0,
        )

    async def has_task_type(self, run_id: str, task_type: TaskType) -> bool:
        async with self.connect() as conn:
            row = await self._fetchone(
                conn,
                "SELECT 1 FROM tasks WHERE run_id = ? AND task_type = ? LIMIT 1",
                (run_id, task_type.value),
            )
        return row is not None

    async def find_open_runs(self) -> list[RunSummary]:
        async with self.connect() as conn:
            rows = await conn.execute_fetchall(
                "SELECT * FROM runs WHERE status IN (?, ?, ?, ?) ORDER BY created_at ASC",
                (
                    RunStatus.queued.value,
                    RunStatus.planning.value,
                    RunStatus.running.value,
                    RunStatus.synthesizing.value,
                ),
            )
        return [
            RunSummary(
                id=row["id"],
                goal=row["goal"],
                status=RunStatus(row["status"]),
                created_at=parse_datetime(row["created_at"]),
                updated_at=parse_datetime(row["updated_at"]),
                final_artifact_id=row["final_artifact_id"],
                metadata=load_json(row["metadata_json"], {}),
            )
            for row in rows
        ]

    async def fetch_ready_tasks(self, *, limit: int) -> list[TaskDetail]:
        async with self.connect() as conn:
            rows = await conn.execute_fetchall(
                """
                SELECT * FROM tasks
                WHERE status IN (?, ?, ?)
                ORDER BY priority ASC, created_at ASC
                LIMIT ?
                """,
                (
                    TaskStatus.pending.value,
                    TaskStatus.needs_retry.value,
                    TaskStatus.blocked.value,
                    limit,
                ),
            )
        ready: list[TaskDetail] = []
        for row in rows:
            task = self._task_from_row(row)
            deps_done = await self.dependencies_completed(task.depends_on)
            if deps_done:
                if task.status is TaskStatus.blocked:
                    await self.update_task_status(task.id, TaskStatus.pending, result=task.result, error=task.error)
                    task = await self.get_task(task.id) or task
                ready.append(task)
            elif task.status is not TaskStatus.blocked:
                await self.update_task_status(task.id, TaskStatus.blocked, result=task.result, error=task.error)
        return ready

    async def unblock_dependent_tasks(self, run_id: str) -> None:
        tasks = await self.list_tasks(run_id)
        for task in tasks:
            if task.status is not TaskStatus.blocked:
                continue
            if await self.dependencies_completed(task.depends_on):
                await self.update_task_status(task.id, TaskStatus.pending, result=task.result, error=task.error)

    async def dependencies_completed(self, depends_on: list[str]) -> bool:
        if not depends_on:
            return True
        placeholders = ",".join("?" for _ in depends_on)
        async with self.connect() as conn:
            rows = await conn.execute_fetchall(
                f"SELECT status FROM tasks WHERE id IN ({placeholders})",
                tuple(depends_on),
            )
        return len(rows) == len(depends_on) and all(row["status"] == TaskStatus.completed.value for row in rows)

    @staticmethod
    def _task_from_row(row: aiosqlite.Row) -> TaskDetail:
        return TaskDetail(
            id=row["id"],
            run_id=row["run_id"],
            parent_id=row["parent_id"],
            task_type=TaskType(row["task_type"]),
            title=row["title"],
            description=row["description"],
            status=TaskStatus(row["status"]),
            priority=row["priority"],
            depends_on=load_json(row["depends_on_json"], []),
            payload=load_json(row["payload_json"], {}),
            result=load_json(row["result_json"], None) if row["result_json"] else None,
            error=row["error"],
            retry_count=row["retry_count"],
            created_at=parse_datetime(row["created_at"]),
            updated_at=parse_datetime(row["updated_at"]),
        )

    @staticmethod
    def _artifact_from_row(row: aiosqlite.Row) -> ArtifactDetail:
        return ArtifactDetail(
            id=row["id"],
            run_id=row["run_id"],
            task_id=row["task_id"],
            kind=row["kind"],
            content=row["content"],
            metadata=load_json(row["metadata_json"], {}),
            created_at=parse_datetime(row["created_at"]),
        )
