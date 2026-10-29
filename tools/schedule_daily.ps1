<#
.SYNOPSIS
    Register (or remove) a daily Windows task that runs autodoriUI unattended.

.DESCRIPTION
    The task launches `autodoriUI.exe --auto`, which opens the UI and starts the
    全自动一轮 session by itself, so nothing has to be clicked.

    The task is registered with:
      WakeToRun           - wake the machine from sleep to run
      StartWhenAvailable  - run as soon as possible if the 07:00 start was missed
      ExecutionTimeLimit  - 3 hours

    A powered-off machine cannot be started by a scheduled task. Either keep the
    PC asleep rather than shut down, or enable an RTC wake-up alarm in BIOS.

.EXAMPLE
    .\tools\schedule_daily.ps1 -Time 07:00

.EXAMPLE
    .\tools\schedule_daily.ps1 -Time 07:00 -RunNow   # register and test right away

.EXAMPLE
    .\tools\schedule_daily.ps1 -Remove
#>
param(
    [string]$Time = "07:00",
    [string]$Exe,
    [string]$TaskName = "autodoriUI daily",
    [switch]$Remove,
    [switch]$RunNow
)

$ErrorActionPreference = "Stop"

if ($Remove) {
    Unregister-ScheduledTask -TaskName $TaskName -Confirm:$false -ErrorAction Stop
    Write-Host "Removed scheduled task '$TaskName'."
    return
}

if (-not $Exe) {
    $Exe = Join-Path (Split-Path $PSScriptRoot -Parent) "dist\autodoriUI.exe"
}
if (-not (Test-Path $Exe)) {
    throw "executable not found: $Exe"
}

$workDir = Split-Path $Exe -Parent

$action = New-ScheduledTaskAction -Execute $Exe -Argument "--auto" -WorkingDirectory $workDir
$trigger = New-ScheduledTaskTrigger -Daily -At $Time
$settings = New-ScheduledTaskSettingsSet `
    -WakeToRun `
    -StartWhenAvailable `
    -AllowStartIfOnBatteries `
    -DontStopIfGoingOnBatteries `
    -ExecutionTimeLimit (New-TimeSpan -Hours 3)
$principal = New-ScheduledTaskPrincipal `
    -UserId ("{0}\{1}" -f $env:USERDOMAIN, $env:USERNAME) `
    -LogonType Interactive `
    -RunLevel Limited

Register-ScheduledTask `
    -TaskName $TaskName `
    -Action $action `
    -Trigger $trigger `
    -Settings $settings `
    -Principal $principal `
    -Force | Out-Null

Write-Host "Registered '$TaskName'"
Write-Host "  program : $Exe --auto"
Write-Host "  schedule: every day at $Time"
Write-Host "  wakes the machine, runs after a missed start"

if ($RunNow) {
    Start-ScheduledTask -TaskName $TaskName
    Write-Host "Started it now for a test run."
}
