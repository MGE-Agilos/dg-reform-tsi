# Lists the tsi-* files of the workspace that no longer exist in the new release (to delete in *Files*).
# With the SDK (OVERWRITE), changed files are replaced, but a document that has left the corpus stays in the
# workspace and in the index: this script gives their names. It deletes nothing and calls no service.
#
# -Current : what is currently in the workspace, either
#            - a text file with one file name per line (list copied from *Files*, or exported from the API);
#            - the files folder of the previous release (the one currently online).
# -New     : files folder of the new release (default: deploy\prod\build\files).
#
#   .\deploy\prod\obsolete-files.ps1 -Current C:\tsi-release-old\files
#   .\deploy\prod\obsolete-files.ps1 -Current workspace-files.txt -New C:\tsi-release\files -Out to-delete.txt
#
# Compatible with Windows PowerShell 5.1.

param(
    [Parameter(Mandatory = $true)][string]$Current,
    [string]$New = "",
    [string]$Out = ""
)

$ErrorActionPreference = "Stop"
if (-not $New) { $New = Join-Path (Split-Path -Parent $MyInvocation.MyCommand.Path) "build\files" }
if (-not (Test-Path $New -PathType Container)) { throw "new release folder not found: $New (pass -New)" }

function Get-DocNames([string[]]$names) {
    # Documents only: the .meta.json files go with their file and are not documents of the workspace.
    $names | ForEach-Object { $_.Trim() } | Where-Object { $_ -like "tsi-*" -and $_ -notlike "*.meta.json" }
}

if (Test-Path $Current -PathType Container) {
    $currentNames = Get-DocNames (Get-ChildItem $Current -File | ForEach-Object { $_.Name })
} elseif (Test-Path $Current -PathType Leaf) {
    $currentNames = Get-DocNames (Get-Content $Current -Encoding utf8)
} else {
    throw "not found: $Current (text file with one name per line, or files folder of the previous release)"
}
$newNames = @{}
foreach ($n in (Get-DocNames (Get-ChildItem $New -File | ForEach-Object { $_.Name }))) { $newNames[$n] = $true }
if ($newNames.Count -eq 0) { throw "no tsi-* file in $New" }

$obsolete = @($currentNames | Sort-Object -Unique | Where-Object { -not $newNames.ContainsKey($_) })
Write-Host "Current workspace: $(@($currentNames).Count) tsi-* files; new release: $($newNames.Count); to delete: $($obsolete.Count)"
if ($obsolete.Count -gt 0) {
    $obsolete | ForEach-Object { Write-Output $_ }
    if ($Out) {
        $obsolete | Set-Content -Path $Out -Encoding utf8
        Write-Host "List written to $Out"
    }
}
