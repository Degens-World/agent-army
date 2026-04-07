param(
    [string]$DbPath = "output\chat.db",
    [string]$Mode = "dashboard"
)

$ErrorActionPreference = "Stop"

if (Test-Path ".\.venv\Scripts\Activate.ps1") {
    . .\.venv\Scripts\Activate.ps1
}

python -m agent_army.cli chat --db-path $DbPath --mode $Mode
