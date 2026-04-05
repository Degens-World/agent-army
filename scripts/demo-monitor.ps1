param(
    [string]$DbPath = "output\guide_demo.db"
)

$ErrorActionPreference = "Stop"
Set-Location "d:\AgentArmy"

if (Test-Path ".\.venv\Scripts\Activate.ps1") {
    . .\.venv\Scripts\Activate.ps1
}

Remove-Item -Force "output\live_run_info.json","output\live_run_summary.json","output\guide-result.md" -ErrorAction SilentlyContinue

$runnerArgs = @(
    "scripts\live_demo.py",
    "--db-path", $DbPath,
    "--info-path", "output\live_run_info.json",
    "--summary-path", "output\live_run_summary.json",
    "--result-path", "output\guide-result.md"
)

$runner = Start-Process python -ArgumentList $runnerArgs -PassThru -WindowStyle Hidden -WorkingDirectory "d:\AgentArmy"

$deadline = (Get-Date).AddSeconds(45)
while (-not (Test-Path "output\live_run_info.json")) {
    if ($runner.HasExited) {
        throw "The demo runner exited before publishing run info."
    }
    if ((Get-Date) -gt $deadline) {
        throw "Timed out waiting for demo run info."
    }
    Start-Sleep -Milliseconds 500
}

$runInfo = Get-Content "output\live_run_info.json" | ConvertFrom-Json

$monitorCommand = "Set-Location 'd:\AgentArmy'; .\scripts\monitor.ps1 -DbPath `"$DbPath`" -RunId `"$($runInfo.run_id)`" -ShowCompleted"
Start-Process powershell -ArgumentList "-NoExit", "-Command", $monitorCommand

Write-Host "Monitor launched for run $($runInfo.run_id)"
Write-Host "Database: $DbPath"
Write-Host "Goal: default live demo guide generation"
