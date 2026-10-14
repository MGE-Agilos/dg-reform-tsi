# Televerse deploy/prod/build/files (tsi-*.md et leurs .meta.json) dans le workspace de production.
# Aucune cle dans ce fichier : API_KEY, API_URL et DEFAULT_WORKSPACE_NAME sont lus dans l'environnement de la
# session. Prerequis : `pip install haystack-enterprise-sdk` (environnement active) et `python -m tsi_agent prod-export`,
# ou une release de donnees decompressee (voir deploy/prod/GUIDE.md).
#
#   $s = Read-Host -AsSecureString "Cle API du workspace"     # saisie masquee, absente de l'historique
#   $env:API_KEY = [Runtime.InteropServices.Marshal]::PtrToStringAuto([Runtime.InteropServices.Marshal]::SecureStringToBSTR($s))
#   $env:API_URL = "<URL de l'API de la plateforme du client>"
#   $env:DEFAULT_WORKSPACE_NAME = "tsi-reform"
#   .\deploy\prod\upload.ps1
#
# Depuis une release decompressee (tsi-prod-files.zip extrait dans un dossier vide, manifest.json telecharge a part) :
#   .\deploy\prod\upload.ps1 -Files C:\tsi-release\files -Manifest C:\tsi-release\manifest.json
#
# Compatible Windows PowerShell 5.1.

param(
    [string]$Files = "",      # dossier des tsi-*.md et .meta.json (defaut : deploy\prod\build\files)
    [string]$Manifest = ""    # manifest.json correspondant (defaut : deploy\prod\build\manifest.json)
)

$ErrorActionPreference = "Stop"
$root = Split-Path -Parent $MyInvocation.MyCommand.Path
if ($Files) { $filesDir = $Files } else { $filesDir = Join-Path $root "build\files" }
if ($Manifest) { $manifestPath = $Manifest } else { $manifestPath = Join-Path $root "build\manifest.json" }

if (-not (Test-Path $manifestPath -PathType Leaf)) {
    throw "manifest.json introuvable ($manifestPath) : lancer d'abord python -m tsi_agent prod-export, ou passer -Manifest"
}
if (-not (Test-Path $filesDir -PathType Container)) { throw "dossier des fichiers introuvable : $filesDir (passer -Files)" }
foreach ($name in "API_KEY", "API_URL", "DEFAULT_WORKSPACE_NAME") {
    if (-not [Environment]::GetEnvironmentVariable($name)) {
        throw "variable d'environnement $name absente : la definir dans cette session (voir l'en-tete de ce script)"
    }
}
$manifestData = Get-Content $manifestPath -Raw -Encoding utf8 | ConvertFrom-Json
if ($manifestData.meta_type_problems.Count -gt 0) { throw "types de metadonnees incoherents : voir $manifestPath" }
$count = (Get-ChildItem $filesDir -Filter "tsi-*.md").Count
if ($count -ne $manifestData.files) {
    throw "$count fichiers tsi-*.md dans $filesDir, $($manifestData.files) attendus (dossier vide au depart ? bon manifeste ?)"
}
$metaCount = (Get-ChildItem $filesDir -Filter "tsi-*.md.meta.json").Count
if ($metaCount -lt $count) {
    Write-Host "Attention : $metaCount fichiers .meta.json pour $count fichiers .md dans $filesDir" -ForegroundColor Yellow
}

Write-Host "Cible     : $env:API_URL"
Write-Host "Workspace : $env:DEFAULT_WORKSPACE_NAME"
Write-Host "Fichiers  : $filesDir ($count fichiers, $($manifestData.chunks) morceaux attendus dans l'index)"
# Le SDK peut ecrire sur stderr : pas d'arret sur stderr (ISE), le resultat est juge sur le code de sortie.
$ErrorActionPreference = "Continue"
haystack-enterprise upload $filesDir --write-mode OVERWRITE --enable-parallel-processing
$code = $LASTEXITCODE
$ErrorActionPreference = "Stop"
if ($code -ne 0) { throw "echec du televersement (code $code)" }
Write-Host "Termine. Verifier dans l'index : Indexed files = $count, Documents in index = $($manifestData.chunks)."
