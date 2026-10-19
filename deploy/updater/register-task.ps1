# Registers (or updates) the Windows scheduled task "TSI monthly data update" (see deploy/updater/README.md,
# "Scheduled update on this PC"). The task runs deploy\updater\update.ps1 -Scheduled -Sync -Publish once a month,
# only while you are logged on (no password is stored), and starts at the next opportunity if the PC was off.
#
#   .\deploy\updater\register-task.ps1                    # day 1 of each month at 03:00
#   .\deploy\updater\register-task.ps1 -Day 2 -Time 06:30 -Index
#   .\deploy\updater\register-task.ps1 -NoPublish -NoSync
#   .\deploy\updater\register-task.ps1 -WhatIf            # prints the task that would be registered, changes nothing
#
# Idempotent: run it again to change the options. Remove the task with unregister-task.ps1.
# Compatible with Windows PowerShell 5.1.

param(
    [string]$Time = "03:00",
    [int]$Day = 1,
    [switch]$NoPublish,
    [switch]$NoSync,
    [switch]$Index,
    [switch]$WhatIf,
    [string]$TaskName = "TSI monthly data update"
)

$ErrorActionPreference = "Stop"
$root = Split-Path -Parent (Split-Path -Parent (Split-Path -Parent $MyInvocation.MyCommand.Path))
$updateScript = Join-Path $root "deploy\updater\update.ps1"

if ($Time -notmatch "^([01][0-9]|2[0-3]):[0-5][0-9]$") { throw "-Time must be HH:mm (24 h), for example 03:00" }
if ($Day -lt 1 -or $Day -gt 28) { throw "-Day must be between 1 and 28 (so that the task runs every month)" }
if (-not (Test-Path $updateScript)) { throw "update script not found: $updateScript" }

$taskArgs = "-NoProfile -ExecutionPolicy Bypass -File `"" + $updateScript + "`" -Scheduled"
if (-not $NoSync) { $taskArgs += " -Sync" }
if (-not $NoPublish) { $taskArgs += " -Publish" }
if ($Index) { $taskArgs += " -Index" }

$user = [System.Security.Principal.WindowsIdentity]::GetCurrent().Name
$startBoundary = (Get-Date -Format "yyyy-MM-dd") + "T" + $Time + ":00"

function ConvertTo-XmlText([string]$value) { return [System.Security.SecurityElement]::Escape($value) }

# Task XML: monthly calendar trigger, start when available (missed run), interactive token (only when the user is
# logged on, no stored password), no stop on battery, one instance at a time, 12 h limit.
$xml = @"
<?xml version="1.0" encoding="UTF-16"?>
<Task version="1.2" xmlns="http://schemas.microsoft.com/windows/2004/02/mit/task">
  <RegistrationInfo>
    <Description>Monthly update of the TSI data (deploy\updater\update.ps1). Log: logs\update-*.log in the repository.</Description>
  </RegistrationInfo>
  <Triggers>
    <CalendarTrigger>
      <StartBoundary>$startBoundary</StartBoundary>
      <Enabled>true</Enabled>
      <ScheduleByMonth>
        <DaysOfMonth>
          <Day>$Day</Day>
        </DaysOfMonth>
        <Months>
          <January /><February /><March /><April /><May /><June /><July /><August /><September /><October /><November /><December />
        </Months>
      </ScheduleByMonth>
    </CalendarTrigger>
  </Triggers>
  <Principals>
    <Principal id="Author">
      <UserId>$(ConvertTo-XmlText $user)</UserId>
      <LogonType>InteractiveToken</LogonType>
      <RunLevel>LeastPrivilege</RunLevel>
    </Principal>
  </Principals>
  <Settings>
    <MultipleInstancesPolicy>IgnoreNew</MultipleInstancesPolicy>
    <DisallowStartIfOnBatteries>false</DisallowStartIfOnBatteries>
    <StopIfGoingOnBatteries>false</StopIfGoingOnBatteries>
    <StartWhenAvailable>true</StartWhenAvailable>
    <RunOnlyIfNetworkAvailable>true</RunOnlyIfNetworkAvailable>
    <ExecutionTimeLimit>PT12H</ExecutionTimeLimit>
    <Enabled>true</Enabled>
  </Settings>
  <Actions Context="Author">
    <Exec>
      <Command>powershell.exe</Command>
      <Arguments>$(ConvertTo-XmlText $taskArgs)</Arguments>
      <WorkingDirectory>$(ConvertTo-XmlText $root)</WorkingDirectory>
    </Exec>
  </Actions>
</Task>
"@
[xml]$xml | Out-Null   # well-formed before registering

Write-Host "Task      : $TaskName"
Write-Host "Schedule  : day $Day of each month at $Time (local time); if the PC is off or you are not logged on, at the next opportunity"
Write-Host "Runs as   : $user, only while logged on (no password stored)"
Write-Host "Command   : powershell.exe $taskArgs"
Write-Host "Folder    : $root"

if ($WhatIf) {
    Write-Host ""
    Write-Host "WhatIf: nothing registered. Task definition:"
    Write-Host $xml
    exit 0
}

$existing = Get-ScheduledTask -TaskName $TaskName -ErrorAction SilentlyContinue
Register-ScheduledTask -TaskName $TaskName -Xml $xml -Force | Out-Null
if ($existing) { Write-Host "Task updated." -ForegroundColor Green } else { Write-Host "Task registered." -ForegroundColor Green }
Write-Host "Check it in Task Scheduler (taskschd.msc), or run it now with: Start-ScheduledTask -TaskName '$TaskName'"
