# Serves the web explorer locally without Docker: copies web\ to deploy\frontend-local\dist\, replaces config.js with
# config.local.js and starts Python's HTTP server in dist\ (Ctrl+C to stop).
#
#   Copy-Item deploy\frontend-local\config.local.example.js deploy\frontend-local\config.local.js   # once
#   .\deploy\frontend-local\serve.ps1                       # http://localhost:8000/
#   .\deploy\frontend-local\serve.ps1 -Port 8001
#   .\deploy\frontend-local\serve.ps1 -Bind 0.0.0.0         # reachable from other computers (see README.md)
#
# Compatible with Windows PowerShell 5.1.

param(
    [int]$Port = 8000,
    [string]$Bind = "127.0.0.1"
)

$ErrorActionPreference = "Stop"
$here = Split-Path -Parent $MyInvocation.MyCommand.Path
$root = Split-Path -Parent (Split-Path -Parent $here)
$web = Join-Path $root "web"
$dist = Join-Path $here "dist"
$localConfig = Join-Path $here "config.local.js"

if (-not (Test-Path $localConfig -PathType Leaf)) {
    throw "config.local.js not found (or it is a folder, created by Docker) in $($here): copy config.local.example.js to config.local.js, then adapt it"
}
if (-not (Test-Path (Join-Path $web "index.html"))) { throw "web folder not found: $web" }

$python = Join-Path $root ".venv\Scripts\python.exe"
if (-not (Test-Path $python)) {
    $cmd = Get-Command python -ErrorAction SilentlyContinue
    if (-not $cmd) { throw "Python not found (neither the repository's .venv nor python in the PATH)" }
    $python = $cmd.Source
}

if (Test-Path $dist) { Remove-Item -Recurse -Force $dist }
New-Item -ItemType Directory -Path $dist | Out-Null
Copy-Item -Path (Join-Path $web "*") -Destination $dist -Recurse -Force
Copy-Item -Path $localConfig -Destination (Join-Path $dist "config.js") -Force

$shown = $Bind
if ($Bind -eq "0.0.0.0" -or $Bind -eq "127.0.0.1") { $shown = "localhost" }
Write-Host "Explorer: http://$($shown):$Port/  (folder $dist; Ctrl+C to stop)"
& $python -m http.server $Port --bind $Bind --directory $dist
exit $LASTEXITCODE
