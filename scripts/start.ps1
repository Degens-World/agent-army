$ErrorActionPreference = "Stop"

if (Test-Path ".\.venv\Scripts\Activate.ps1") {
    . .\.venv\Scripts\Activate.ps1
}

uvicorn agent_army.api:create_app --factory --host 0.0.0.0 --port 8000
