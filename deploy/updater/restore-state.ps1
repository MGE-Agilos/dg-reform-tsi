# Restores the state of the data (output\) from a GitHub release of the repository (see deploy/updater/README.md).
# Downloads tsi-output-state.zip with GitHub CLI (gh auth login done beforehand) and unzips it into output\.
#
#   .\deploy\updater\restore-state.ps1                       # latest release
#   .\deploy\updater\restore-state.ps1 -Tag data-2026-10-14
#   .\deploy\updater\restore-state.ps1 -Force                # output\ already exists: replace the files with the same name
#
# Files of output\ that are not in the zip are kept. The zip contains neither output\vectors\ nor output\reports\, nor
# the HTTP cache (.cache\http): the first run on this PC therefore reloads the detail pages of the site (about 2 hours).
# Compatible with Windows PowerShell 5.1, no interactive question.

param(
    [string]$Tag = "",
    [string]$Repo = "MGE-Agilos/dg-reform-tsi",
    [switch]$Force
)

$ErrorActionPreference = "Stop"
$root = Split-Path -Parent (Split-Path -Parent (Split-Path -Parent $MyInvocation.MyCommand.Path))
$output = Join-Path $root "output"
$asset = "tsi-output-state.zip"

if (-not (Get-Command gh -ErrorAction SilentlyContinue)) {
    throw "GitHub CLI (gh) not found: install it (https://cli.github.com), then run gh auth login"
}
if ((Test-Path $output) -and (Get-ChildItem $output -Force | Select-Object -First 1) -and (-not $Force)) {
    throw "$output is not empty: run again with -Force to unzip the release state into it (files with the same name will be replaced, the others kept)"
}

$temp = Join-Path ([IO.Path]::GetTempPath()) ("tsi-state-" + [Guid]::NewGuid().ToString("N"))
New-Item -ItemType Directory -Path $temp | Out-Null
try {
    # gh writes its progress to stderr: do not stop on stderr (ISE); the result is judged on the exit code.
    $ErrorActionPreference = "Continue"
    if ($Tag) {
        Write-Host "Downloading $asset (release $Tag, $Repo)..."
        gh release download $Tag -p $asset -R $Repo -D $temp
    } else {
        Write-Host "Downloading $asset (latest release, $Repo)..."
        gh release download -p $asset -R $Repo -D $temp
    }
    $ghCode = $LASTEXITCODE
    $ErrorActionPreference = "Stop"
    if ($ghCode -ne 0) { throw "gh release download failed (exit code $ghCode): unknown tag, or gh auth login not done?" }
    $zip = Join-Path $temp $asset
    if (-not (Test-Path $zip)) { throw "$asset is missing from the release" }
    $mb = [math]::Round((Get-Item $zip).Length / 1MB, 1)
    Write-Host "Unzipping ($mb MB) into $output ..."
    if (-not (Test-Path $output)) { New-Item -ItemType Directory -Path $output | Out-Null }
    Expand-Archive -Path $zip -DestinationPath $output -Force
    $projects = (Get-ChildItem (Join-Path $output "projects") -Filter "*.md" -ErrorAction SilentlyContinue).Count
    Write-Host "State restored: $projects project files in output\projects." -ForegroundColor Green
    Write-Host "Next: .venv\Scripts\python.exe -m tsi_agent llm-check (after a first run), then deploy\updater\update.ps1"
} finally {
    Remove-Item -Recurse -Force $temp -ErrorAction SilentlyContinue
}
