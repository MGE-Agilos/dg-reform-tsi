# Sert l'explorateur web en local sans Docker : copie web\ dans deploy\frontend-local\dist\, remplace config.js par
# config.local.js et lance le serveur HTTP de Python dans dist\ (Ctrl+C pour arreter).
#
#   Copy-Item deploy\frontend-local\config.local.example.js deploy\frontend-local\config.local.js   # une fois
#   .\deploy\frontend-local\serve.ps1                       # http://localhost:8000/
#   .\deploy\frontend-local\serve.ps1 -Port 8001
#   .\deploy\frontend-local\serve.ps1 -Bind 0.0.0.0         # accessible depuis les autres postes (voir README.md)
#
# Compatible Windows PowerShell 5.1.

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

if (-not (Test-Path $localConfig)) {
    throw "config.local.js introuvable dans $here : copier config.local.example.js vers config.local.js puis l'adapter"
}
if (-not (Test-Path (Join-Path $web "index.html"))) { throw "dossier web introuvable : $web" }

$python = Join-Path $root ".venv\Scripts\python.exe"
if (-not (Test-Path $python)) {
    $cmd = Get-Command python -ErrorAction SilentlyContinue
    if (-not $cmd) { throw "Python introuvable (ni .venv du depot, ni python dans le PATH)" }
    $python = $cmd.Source
}

if (Test-Path $dist) { Remove-Item -Recurse -Force $dist }
New-Item -ItemType Directory -Path $dist | Out-Null
Copy-Item -Path (Join-Path $web "*") -Destination $dist -Recurse -Force
Copy-Item -Path $localConfig -Destination (Join-Path $dist "config.js") -Force

$shown = $Bind
if ($Bind -eq "0.0.0.0" -or $Bind -eq "127.0.0.1") { $shown = "localhost" }
Write-Host "Explorateur : http://$($shown):$Port/  (dossier $dist ; Ctrl+C pour arreter)"
& $python -m http.server $Port --bind $Bind --directory $dist
exit $LASTEXITCODE
