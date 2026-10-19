# Registers a Task Scheduler job that starts the Thought Router worker at logon.
# Run once:  powershell -ExecutionPolicy Bypass -File worker\install-task.ps1
$ErrorActionPreference = 'Stop'
$python = "$env:USERPROFILE\anaconda3\pythonw.exe"
$script = Join-Path $PSScriptRoot 'router.py'

$action = New-ScheduledTaskAction -Execute $python -Argument "`"$script`"" -WorkingDirectory $PSScriptRoot
$trigger = New-ScheduledTaskTrigger -AtLogOn -User $env:USERNAME
$settings = New-ScheduledTaskSettingsSet -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries `
    -ExecutionTimeLimit ([TimeSpan]::Zero) -RestartCount 3 -RestartInterval (New-TimeSpan -Minutes 5)

Register-ScheduledTask -TaskName 'ThoughtRouter' -Action $action -Trigger $trigger -Settings $settings `
    -Description 'Routes phone notes to journal / todo / Claude agent' -Force | Out-Null
Start-ScheduledTask -TaskName 'ThoughtRouter'
Write-Host "ThoughtRouter task registered and started. Logs: $PSScriptRoot\logs\router.log"
