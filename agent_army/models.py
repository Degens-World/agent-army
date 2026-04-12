from __future__ import annotations

from datetime import datetime
from enum import StrEnum
from typing import Any

from pydantic import BaseModel, Field


class RunStatus(StrEnum):
    queued = "queued"
    planning = "planning"
    running = "running"
    synthesizing = "synthesizing"
    completed = "completed"
    failed = "failed"
    paused = "paused"


class TaskStatus(StrEnum):
    pending = "pending"
    queued = "queued"
    running = "running"
    blocked = "blocked"
    completed = "completed"
    failed = "failed"
    needs_retry = "needs_retry"
    rejected = "rejected"


class TaskType(StrEnum):
    plan = "plan"
    execute = "execute"
    review = "review"
    synthesize = "synthesize"


class RunCreate(BaseModel):
    goal: str = Field(min_length=10)
    max_parallelism: int | None = Field(default=None, ge=1, le=64)
    metadata: dict[str, Any] = Field(default_factory=dict)


class RunReopen(BaseModel):
    instructions: str = Field(min_length=3)
    max_parallelism: int | None = Field(default=None, ge=1, le=64)
    metadata: dict[str, Any] = Field(default_factory=dict)


class RunSummary(BaseModel):
    id: str
    goal: str
    status: RunStatus
    created_at: datetime
    updated_at: datetime
    final_artifact_id: str | None = None
    metadata: dict[str, Any] = Field(default_factory=dict)


class RunDetail(RunSummary):
    task_counts: dict[str, int] = Field(default_factory=dict)
    final_artifact: dict[str, Any] | None = None


class TaskDetail(BaseModel):
    id: str
    run_id: str
    parent_id: str | None = None
    task_type: TaskType
    title: str
    description: str
    status: TaskStatus
    priority: int
    depends_on: list[str] = Field(default_factory=list)
    payload: dict[str, Any] = Field(default_factory=dict)
    result: dict[str, Any] | None = None
    error: str | None = None
    retry_count: int = 0
    created_at: datetime
    updated_at: datetime


class ArtifactDetail(BaseModel):
    id: str
    run_id: str
    task_id: str | None = None
    kind: str
    content: str
    metadata: dict[str, Any] = Field(default_factory=dict)
    created_at: datetime


class PlannedSubtask(BaseModel):
    title: str
    description: str
    acceptance_criteria: list[str] = Field(default_factory=list)
    output_format: str = "markdown"
    role_hint: str = "worker"
    priority: int = 5
    depends_on_indexes: list[int] = Field(default_factory=list)


class PlanResult(BaseModel):
    summary: str
    subtasks: list[PlannedSubtask]


class ReviewDecision(BaseModel):
    approved: bool
    summary: str
    issues: list[str] = Field(default_factory=list)
    suggested_fixes: list[str] = Field(default_factory=list)


class RunCreated(BaseModel):
    run_id: str
    status: RunStatus
