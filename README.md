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

For coding goals, the system also:

- materializes task and final outputs into real workspace folders
- keeps a manifest for each workspace
- supports reopening an existing run to modify or fix the original deliverable

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
$env:AGENT_ARMY_WORKSPACE_ROOT = "output\runs"
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

### 7. Reopen an existing run

You can ask the army to revise an existing run instead of starting over:

```powershell
Invoke-RestMethod -Method Post -Uri "http://127.0.0.1:8000/runs/<run-id>/reopen" -ContentType "application/json" -Body '{"instructions":"Fix the restart flow and keep the rest of the game unchanged."}'
```

## API overview

- `POST /runs`
- `GET /runs`
- `GET /runs/{run_id}`
- `GET /runs/{run_id}/tasks`
- `GET /runs/{run_id}/artifacts`
- `POST /runs/{run_id}/resume`
- `POST /runs/{run_id}/reopen`

## CLI monitor

The CLI monitor polls the SQLite database and renders:

- run metadata and aggregate task counts
- a live table of logical agents and their task status
- recent task state transitions inferred from polling diffs
- an animated ASCII operation header in dashboard mode

Useful options:

- `--run-id <id>` to watch a specific run
- `--refresh 0.5` to poll faster
- `--mode dashboard` for the fixed tactical dashboard
- `--mode scroll` for log-style output
- `--once` to render a snapshot and exit
- `--show-completed` to keep completed tasks visible in the table

## Chat interface

You can start runs directly from an interactive terminal chat:

```powershell
python -m agent_army.cli chat --db-path output\chat.db
```

The chat UI uses dashboard mode by default. If you want a plain log feed instead:

```powershell
python -m agent_army.cli chat --db-path output\chat.db --mode scroll
```

Inside chat:

- type plain text to start a new run
- use `/runs` to list recent runs
- use `/watch <run-id>` to follow a run
- use `/revise <run-id>` to reopen a run and then enter revision instructions
- use `/revise <run-id> <instructions>` to reopen a run inline
- use `/help` for commands
- use `/quit` to exit

Each completed chat run writes its final artifact to `output/chat-<run-id>.md`.

## Coding workspaces

Coding runs now materialize task outputs into human-readable project folders under `output/runs/`.

- project roots use a readable slug such as `output/runs/make-an-html5-checkers-game/`
- if the same goal already has a folder for a different run, the next folder becomes `...-2`, `...-3`, and so on
- task-level work is written to `output/runs/<project-slug>/tasks/<step-name>/`
- the final deliverable is written to `output/runs/<project-slug>/final/`
- each workspace includes an `artifact_manifest.json`

For single-file browser tasks like an HTML game, the final folder will typically contain:

- `index.html`
- `artifact_manifest.json`

The final artifact metadata returned by the API includes the workspace path, entrypoint, and file list.

## Revision behavior

When you reopen a coding run:

- the new run gets its own run id in the database
- the agents reuse the original project workspace on disk
- revised task outputs are written back into the original `tasks/` and `final/` folders
- the reopen request carries the original workspace path, file list, and revision instructions into the next planning and execution cycle

## Bounty Hunter

The bounty hunter scans GitHub repos and orgs for open issues tagged as bug bounties or marked with help-wanted labels, checks whether they are actively being worked on, prompts you to approve each one, then uses a local Ollama model to analyze the codebase, generate a fix, run tests, fork the repo, and submit a PR. All activity is logged to a local SQLite table.

### Setup

```bash
export GITHUB_TOKEN=your_personal_access_token
```

### Commands

Hunt a single repo:

```bash
agent-army bounty-hunt owner/repo
```

Hunt an entire GitHub org across all its repos:

```bash
agent-army bounty-hunt --org ergoplatform
```

Custom GitHub issue search query:

```bash
agent-army bounty-hunt --search "org:ergoplatform is:issue is:open bounty"
```

Override the Ollama model used for fixing:

```bash
agent-army bounty-hunt owner/repo --model qwen2.5-coder:7b
```

View your hunt history and bounty log:

```bash
agent-army bounty-log
```

### How it works

1. **Scan** — fetches open issues matching bounty labels (`bug-bounty`, `bounty`, `help wanted`, `good-first-issue`, `enhancement`) and keywords (`$`, `bounty`, `reward`)
2. **Activity check** — skips issues that are assigned, have "in progress" comments, or have linked PRs
3. **Legitimacy check** — flags repos that look like bounty farms: too new with low stars, abnormal fork/star ratio, or all issues from one user. Refuses to submit a PR to flagged repos
4. **Approval** — presents each candidate interactively; you hit `y` or `n`
5. **Fix** — asks Ollama to identify relevant files, read them, generate a patch
6. **Test** — runs `pytest` if present (best effort, doesn't block the PR)
7. **Submit** — forks the repo, commits the fix to a branch, opens a PR with a structured description
8. **Log** — records repo, issue number, bounty amount, PR URL, and status to SQLite

### Legitimacy checks

Before submitting any PR the hunter verifies the target repo:

- Repo must be older than 60 days **or** have 5+ stars
- Fork/star ratio must be below 20× (high ratio signals bot farming)
- Issues must not all come from a single user

If any check fails the issue is skipped with an explanation and nothing is submitted.

## Notes on scale

This MVP supports hundreds of logical subtasks but intentionally limits simultaneous model executions. Real throughput depends on prompt size, model size, GPU or CPU capacity, and review overhead.

If you want to distribute workers across machines later, the main seams to replace are:

- SQLite with Postgres
- in-process queueing with Redis
- local worker loops with separate worker processes
