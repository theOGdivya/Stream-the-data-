# Windows: start the agent automatically when this user logs in, and restart it if it stops.
# Run in PowerShell from the agent folder:  powershell -ExecutionPolicy Bypass -File scripts\install-windows-task.ps1
$AgentDir = (Resolve-Path "$PSScriptRoot\..").Path
$Python = Join-Path $AgentDir ".venv\Scripts\pythonw.exe"
if (-not (Test-Path $Python)) { Write-Error "Create the virtual environment first (see agent/README.md)"; exit 1 }

$Action = New-ScheduledTaskAction -Execute $Python -Argument "-m aqagent run -c config.yaml" -WorkingDirectory $AgentDir
$Trigger = New-ScheduledTaskTrigger -AtLogOn -User $env:USERNAME
$Settings = New-ScheduledTaskSettingsSet -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries `
  -RestartCount 999 -RestartInterval (New-TimeSpan -Minutes 1) -ExecutionTimeLimit ([TimeSpan]::Zero) -StartWhenAvailable
Register-ScheduledTask -TaskName "AQ capture agent" -Action $Action -Trigger $Trigger -Settings $Settings -Force | Out-Null
Start-ScheduledTask -TaskName "AQ capture agent"
Write-Host "Installed and started. Log file: $AgentDir\spool\agent.log"
