# Agent Army

Agent Army is a local multi-agent orchestration MVP that uses Ollama as the model backend and coordinates planning, execution, review, and synthesis through a persistent task graph.

The system is designed around a few principles:

- many logical agents, few active model calls
- narrow subtasks with explicit output contracts
- persistent run and task state
- automatic review and retry
- local-first operation with SQLite and Ollama

## What it does

For each run, the system:

1. creates a planning task from a top-level goal
2. asks a planner model to break the work into independent subtasks
3. executes ready subtasks in parallel with bounded concurrency
4. reviews each subtask result against acceptance criteria
5. synthesizes approved outputs into a final result

## Architecture

- `FastAPI` exposes the API
- `SQLite` persists runs, tasks, dependencies, and artifacts
- `asyncio` drives the dispatcher and worker pool
- `Ollama` provides local model inference over HTTP

Main roles:

- `planner`
- `worker`
- `reviewer`
- `synthesizer`
- `orchestrator`

## Quick start

### 1. Install dependencies

Using a dedicated virtual environment is recommended. This machine already has unrelated packages installed globally, and that can create dependency conflicts for `starlette` or related ASGI packages.

```powershell
python -m venv .venv
. .\.venv\Scripts\Activate.ps1
python -m pip install -e .[dev]
```

### 2. Pull the models you want to use

```powershell
ollama pull qwen2.5:14b
ollama pull qwen2.5-coder:14b
```

### 3. Configure environment variables

```powershell
$env:AGENT_ARMY_DB_PATH = "agent_army.db"
$env:AGENT_ARMY_OLLAMA_HOST = "http://127.0.0.1:11434"
$env:AGENT_ARMY_PLANNER_MODEL = "qwen2.5:14b"
$env:AGENT_ARMY_WORKER_MODEL = "qwen2.5-coder:14b"
$env:AGENT_ARMY_REVIEWER_MODEL = "qwen2.5:14b"
$env:AGENT_ARMY_SYNTHESIZER_MODEL = "qwen2.5:14b"
$env:AGENT_ARMY_MAX_ACTIVE_EXECUTIONS = "4"
```

### 4. Run the API

```powershell
uvicorn agent_army.api:create_app --factory --host 0.0.0.0 --port 8000
```

Or use the helper script:

```powershell
.\scripts\start.ps1
```

### 4a. Monitor a run in the terminal

List available runs:

```powershell
agent-army runs --db-path output\e2e_agent_army.db
```

Watch the latest run in that database:

```powershell
agent-army monitor --db-path output\e2e_agent_army.db --show-completed
```

Or use the helper script:

```powershell
.\scripts\monitor.ps1 -DbPath output\e2e_agent_army.db
```

### 5. Create a run

```powershell
Invoke-RestMethod -Method Post -Uri "http://127.0.0.1:8000/runs" -ContentType "application/json" -Body '{"goal":"Design a launch plan for a local coding assistant product, including pricing, customer segments, risks, and a rollout sequence."}'
```

### 6. Inspect the run

```powershell
Invoke-RestMethod -Uri "http://127.0.0.1:8000/runs/<run-id>"
Invoke-RestMethod -Uri "http://127.0.0.1:8000/runs/<run-id>/tasks"
Invoke-RestMethod -Uri "http://127.0.0.1:8000/runs/<run-id>/artifacts"
```

## API overview

- `POST /runs`
- `GET /runs`
- `GET /runs/{run_id}`
- `GET /runs/{run_id}/tasks`
- `GET /runs/{run_id}/artifacts`
- `POST /runs/{run_id}/resume`

## CLI monitor

The CLI monitor polls the SQLite database and renders:

- run metadata and aggregate task counts
- a live table of logical agents and their task status
- recent task state transitions inferred from polling diffs

Useful options:

- `--run-id <id>` to watch a specific run
- `--refresh 0.5` to poll faster
- `--once` to render a snapshot and exit
- `--show-completed` to keep completed tasks visible in the table

## Notes on scale

This MVP supports hundreds of logical subtasks but intentionally limits simultaneous model executions. Real throughput depends on prompt size, model size, GPU or CPU capacity, and review overhead.

If you want to distribute workers across machines later, the main seams to replace are:

- SQLite with Postgres
- in-process queueing with Redis
- local worker loops with separate worker processes
