# Removes the Windows scheduled task registered by register-task.ps1 (see deploy/updater/README.md).
#
#   .\deploy\updater\unregister-task.ps1
#   .\deploy\updater\unregister-task.ps1 -WhatIf      # says what would be removed, changes nothing
#
# Idempotent: nothing to do if the task is not registered. Compatible with Windows PowerShell 5.1.

param(
    [switch]$WhatIf,
    [string]$TaskName = "TSI monthly data update"
)

$ErrorActionPreference = "Stop"
$existing = Get-ScheduledTask -TaskName $TaskName -ErrorAction SilentlyContinue
if (-not $existing) {
    Write-Host "Task '$TaskName' is not registered: nothing to do."
    exit 0
}
if ($WhatIf) {
    Write-Host "WhatIf: the task '$TaskName' would be removed (state: $($existing.State))."
    exit 0
}
Unregister-ScheduledTask -TaskName $TaskName -Confirm:$false
Write-Host "Task '$TaskName' removed. The logs (logs\update-*.log) are kept." -ForegroundColor Green
