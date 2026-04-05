param(
    [string]$DbPath = "agent_army.db",
    [string]$RunId = "",
    [switch]$Once,
    [switch]$ShowCompleted
)

$ErrorActionPreference = "Stop"

if (Test-Path ".\.venv\Scripts\Activate.ps1") {
    . .\.venv\Scripts\Activate.ps1
}

$args = @("monitor", "--db-path", $DbPath)

if ($RunId) {
    $args += @("--run-id", $RunId)
}

if ($Once) {
    $args += "--once"
}

if ($ShowCompleted) {
    $args += "--show-completed"
}

python -m agent_army.cli @args
