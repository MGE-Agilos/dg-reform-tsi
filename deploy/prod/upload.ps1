# Uploads deploy/prod/build/files (tsi-*.md and their .meta.json) to the production workspace.
# No key in this file: API_KEY, API_URL and DEFAULT_WORKSPACE_NAME are read from the environment of the
# session. Prerequisites: `pip install haystack-enterprise-sdk` (environment activated) and `python -m tsi_agent prod-export`,
# or an unzipped data release (see deploy/prod/GUIDE.md).
#
#   $s = Read-Host -AsSecureString "Workspace API key"     # hidden input, not kept in the history
#   $env:API_KEY = [Runtime.InteropServices.Marshal]::PtrToStringAuto([Runtime.InteropServices.Marshal]::SecureStringToBSTR($s))
#   $env:API_URL = "<API URL of the client's platform>"
#   $env:DEFAULT_WORKSPACE_NAME = "tsi-reform"
#   .\deploy\prod\upload.ps1
#
# From an unzipped release (tsi-prod-files.zip extracted into an empty folder, manifest.json downloaded separately):
#   .\deploy\prod\upload.ps1 -Files C:\tsi-release\files -Manifest C:\tsi-release\manifest.json
#
# Compatible with Windows PowerShell 5.1.

param(
    [string]$Files = "",      # folder of the tsi-*.md and .meta.json files (default: deploy\prod\build\files)
    [string]$Manifest = ""    # matching manifest.json (default: deploy\prod\build\manifest.json)
)

$ErrorActionPreference = "Stop"
$root = Split-Path -Parent $MyInvocation.MyCommand.Path
if ($Files) { $filesDir = $Files } else { $filesDir = Join-Path $root "build\files" }
if ($Manifest) { $manifestPath = $Manifest } else { $manifestPath = Join-Path $root "build\manifest.json" }

if (-not (Test-Path $manifestPath -PathType Leaf)) {
    throw "manifest.json not found ($manifestPath): run python -m tsi_agent prod-export first, or pass -Manifest"
}
if (-not (Test-Path $filesDir -PathType Container)) { throw "files folder not found: $filesDir (pass -Files)" }
foreach ($name in "API_KEY", "API_URL", "DEFAULT_WORKSPACE_NAME") {
    if (-not [Environment]::GetEnvironmentVariable($name)) {
        throw "environment variable $name is missing: set it in this session (see the header of this script)"
    }
}
$manifestData = Get-Content $manifestPath -Raw -Encoding utf8 | ConvertFrom-Json
if ($manifestData.meta_type_problems.Count -gt 0) { throw "inconsistent metadata types: see $manifestPath" }
$count = (Get-ChildItem $filesDir -Filter "tsi-*.md").Count
if ($count -ne $manifestData.files) {
    throw "$count tsi-*.md files in $filesDir, $($manifestData.files) expected (was the folder empty to begin with? right manifest?)"
}
$metaCount = (Get-ChildItem $filesDir -Filter "tsi-*.md.meta.json").Count
if ($metaCount -lt $count) {
    Write-Host "Warning: $metaCount .meta.json files for $count .md files in $filesDir" -ForegroundColor Yellow
}

Write-Host "Target    : $env:API_URL"
Write-Host "Workspace : $env:DEFAULT_WORKSPACE_NAME"
Write-Host "Files     : $filesDir ($count files, $($manifestData.chunks) chunks expected in the index)"
# The SDK may write to stderr: do not stop on stderr (ISE); the result is judged on the exit code.
$ErrorActionPreference = "Continue"
haystack-enterprise upload $filesDir --write-mode OVERWRITE --enable-parallel-processing
$code = $LASTEXITCODE
$ErrorActionPreference = "Stop"
if ($code -ne 0) { throw "upload failed (exit code $code)" }
Write-Host "Done. Check in the index: Indexed files = $count, Documents in index = $($manifestData.chunks)."
