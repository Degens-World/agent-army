from functools import lru_cache
from pathlib import Path

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_prefix="AGENT_ARMY_",
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    app_name: str = "Agent Army"
    db_path: Path = Field(default=Path("agent_army.db"))
    workspace_root: Path = Field(default=Path("output") / "runs")
    ollama_host: str = "http://127.0.0.1:11434"
    planner_model: str = "qwen2.5:14b"
    worker_model: str = "qwen2.5-coder:14b"
    reviewer_model: str = "qwen2.5:14b"
    synthesizer_model: str = "qwen2.5:14b"
    request_timeout_seconds: float = 180.0
    max_active_executions: int = 4
    max_plan_steps: int = 8
    task_poll_interval_seconds: float = 1.0
    max_review_retries: int = 2
    planner_temperature: float = 0.2
    worker_temperature: float = 0.2
    reviewer_temperature: float = 0.1
    synthesizer_temperature: float = 0.2
    auto_resume_pending_runs: bool = True


@lru_cache
def get_settings() -> Settings:
    return Settings()
