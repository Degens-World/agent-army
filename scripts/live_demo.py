from __future__ import annotations

import argparse
import json
import os
import time
from pathlib import Path

from fastapi.testclient import TestClient

from agent_army.api import create_app


DEFAULT_GOAL = (
    "Write a 10-step practical guide for working with AI agents. "
    "Include short explanations for each step and concrete code examples in Python "
    "for planning tasks, tool use, retries, review, and orchestration. "
    "Keep it structured and developer-friendly."
)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Run a live Agent Army demo job.")
    parser.add_argument("--goal", default=DEFAULT_GOAL)
    parser.add_argument("--db-path", type=Path, default=Path("output") / "guide_demo.db")
    parser.add_argument("--info-path", type=Path, default=Path("output") / "live_run_info.json")
    parser.add_argument("--summary-path", type=Path, default=Path("output") / "live_run_summary.json")
    parser.add_argument("--result-path", type=Path, default=Path("output") / "guide-result.md")
    parser.add_argument("--planner-model", default="qwen3:4b")
    parser.add_argument("--worker-model", default="qwen3:4b")
    parser.add_argument("--reviewer-model", default="qwen3:4b")
    parser.add_argument("--synthesizer-model", default="qwen3:4b")
    parser.add_argument("--max-active-executions", type=int, default=2)
    parser.add_argument("--max-plan-steps", type=int, default=6)
    parser.add_argument("--request-timeout-seconds", type=float, default=180.0)
    parser.add_argument("--task-poll-interval-seconds", type=float, default=0.5)
    parser.add_argument("--timeout-seconds", type=float, default=600.0)
    return parser


def main() -> None:
    args = build_parser().parse_args()
    args.db_path.parent.mkdir(parents=True, exist_ok=True)

    os.environ["AGENT_ARMY_OLLAMA_HOST"] = "http://127.0.0.1:11434"
    os.environ["AGENT_ARMY_PLANNER_MODEL"] = args.planner_model
    os.environ["AGENT_ARMY_WORKER_MODEL"] = args.worker_model
    os.environ["AGENT_ARMY_REVIEWER_MODEL"] = args.reviewer_model
    os.environ["AGENT_ARMY_SYNTHESIZER_MODEL"] = args.synthesizer_model
    os.environ["AGENT_ARMY_MAX_ACTIVE_EXECUTIONS"] = str(args.max_active_executions)
    os.environ["AGENT_ARMY_MAX_PLAN_STEPS"] = str(args.max_plan_steps)
    os.environ["AGENT_ARMY_REQUEST_TIMEOUT_SECONDS"] = str(args.request_timeout_seconds)
    os.environ["AGENT_ARMY_TASK_POLL_INTERVAL_SECONDS"] = str(args.task_poll_interval_seconds)
    os.environ["AGENT_ARMY_DB_PATH"] = str(args.db_path)

    with TestClient(create_app()) as client:
        create_resp = client.post("/runs", json={"goal": args.goal})
        create_resp.raise_for_status()
        run_id = create_resp.json()["run_id"]

        info = {
            "run_id": run_id,
            "db_path": str(args.db_path.resolve()),
            "goal": args.goal,
        }
        args.info_path.write_text(json.dumps(info, indent=2), encoding="utf-8")
        print(json.dumps(info, indent=2), flush=True)

        deadline = time.time() + args.timeout_seconds
        while time.time() < deadline:
            run_resp = client.get(f"/runs/{run_id}")
            run_resp.raise_for_status()
            run = run_resp.json()
            if run["status"] in {"completed", "failed"}:
                break
            time.sleep(1)
        else:
            raise TimeoutError("Live demo timed out.")

        tasks = client.get(f"/runs/{run_id}/tasks").json()
        artifacts = client.get(f"/runs/{run_id}/artifacts").json()
        final_artifact = run.get("final_artifact")
        if final_artifact:
            args.result_path.write_text(final_artifact["content"], encoding="utf-8")

        summary = {
            "run_id": run_id,
            "status": run["status"],
            "task_counts": run.get("task_counts", {}),
            "final_artifact_id": run.get("final_artifact_id"),
            "result_path": str(args.result_path.resolve()) if final_artifact else None,
            "tasks": [
                {
                    "task_type": task["task_type"],
                    "title": task["title"],
                    "status": task["status"],
                    "error": task.get("error"),
                }
                for task in tasks
            ],
            "artifacts": [
                {
                    "kind": artifact["kind"],
                    "task_id": artifact.get("task_id"),
                    "preview": artifact["content"][:160],
                }
                for artifact in artifacts
            ],
        }
        args.summary_path.write_text(json.dumps(summary, indent=2), encoding="utf-8")
        print(json.dumps(summary, indent=2), flush=True)


if __name__ == "__main__":
    main()
