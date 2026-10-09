# Televerse deploy/prod/build/files (tsi-*.md et leurs .meta.json) dans le workspace de production.
# Aucune cle dans ce fichier : API_KEY, API_URL et DEFAULT_WORKSPACE_NAME sont lus dans l'environnement de la
# session (ou dans la configuration de `haystack-enterprise login`). Prerequis : `pip install haystack-enterprise-sdk`
# et `python -m tsi_agent prod-export`.
#
#   $s = Read-Host -AsSecureString "Cle API du workspace"     # saisie masquee, absente de l'historique
#   $env:API_KEY = [Runtime.InteropServices.Marshal]::PtrToStringAuto([Runtime.InteropServices.Marshal]::SecureStringToBSTR($s))
#   $env:API_URL = "<URL de l'API de la plateforme du client>"
#   $env:DEFAULT_WORKSPACE_NAME = "tsi-reform"
#   .\deploy\prod\upload.ps1

$ErrorActionPreference = "Stop"
$root = Split-Path -Parent $MyInvocation.MyCommand.Path
$files = Join-Path $root "build\files"
$manifestPath = Join-Path $root "build\manifest.json"

if (-not (Test-Path $manifestPath)) { throw "manifest.json introuvable : lancer d'abord python -m tsi_agent prod-export" }
foreach ($name in "API_KEY", "DEFAULT_WORKSPACE_NAME") {
    if (-not [Environment]::GetEnvironmentVariable($name)) { throw "variable d'environnement $name absente" }
}
$manifest = Get-Content $manifestPath -Raw -Encoding utf8 | ConvertFrom-Json
if ($manifest.meta_type_problems.Count -gt 0) { throw "types de metadonnees incoherents : voir manifest.json" }
$count = (Get-ChildItem $files -Filter "tsi-*.md").Count
if ($count -ne $manifest.files) { throw "$count fichiers .md dans build\files, $($manifest.files) attendus" }

Write-Host "Workspace $env:DEFAULT_WORKSPACE_NAME : $count fichiers, $($manifest.chunks) morceaux attendus dans l'index."
haystack-enterprise upload $files --write-mode OVERWRITE --enable-parallel-processing
if ($LASTEXITCODE -ne 0) { throw "echec du televersement (code $LASTEXITCODE)" }
Write-Host "Termine. Verifier dans l'index : Indexed files = $count, Documents in index = $($manifest.chunks)."
