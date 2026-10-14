# Recupere l'etat des donnees (output\) depuis une release GitHub du depot (voir deploy/updater/README.md).
# Telecharge tsi-output-state.zip avec GitHub CLI (gh auth login fait au prealable) et le decompresse dans output\.
#
#   .\deploy\updater\restore-state.ps1                       # derniere release
#   .\deploy\updater\restore-state.ps1 -Tag data-2026-10-14
#   .\deploy\updater\restore-state.ps1 -Force                # output\ existe deja : remplacer les fichiers du meme nom
#
# Les fichiers de output\ absents du zip sont gardes. Le zip ne contient ni output\vectors\ ni output\reports\, ni le
# cache HTTP (.cache\http) : le premier run sur ce PC recharge donc les pages detail du site (environ 2 heures).
# Compatible Windows PowerShell 5.1, sans question interactive.

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
    throw "GitHub CLI (gh) introuvable : l'installer (https://cli.github.com) puis lancer gh auth login"
}
if ((Test-Path $output) -and (Get-ChildItem $output -Force | Select-Object -First 1) -and (-not $Force)) {
    throw "$output n'est pas vide : relancer avec -Force pour y decompresser l'etat de la release (les fichiers du meme nom seront remplaces, les autres gardes)"
}

$temp = Join-Path ([IO.Path]::GetTempPath()) ("tsi-state-" + [Guid]::NewGuid().ToString("N"))
New-Item -ItemType Directory -Path $temp | Out-Null
try {
    # gh ecrit sa progression sur stderr : pas d'arret sur stderr (ISE), le resultat est juge sur le code de sortie.
    $ErrorActionPreference = "Continue"
    if ($Tag) {
        Write-Host "Telechargement de $asset (release $Tag, $Repo)..."
        gh release download $Tag -p $asset -R $Repo -D $temp
    } else {
        Write-Host "Telechargement de $asset (derniere release, $Repo)..."
        gh release download -p $asset -R $Repo -D $temp
    }
    $ghCode = $LASTEXITCODE
    $ErrorActionPreference = "Stop"
    if ($ghCode -ne 0) { throw "gh release download a echoue (code $ghCode) : tag inconnu, ou gh auth login a faire ?" }
    $zip = Join-Path $temp $asset
    if (-not (Test-Path $zip)) { throw "$asset absent de la release" }
    $mb = [math]::Round((Get-Item $zip).Length / 1MB, 1)
    Write-Host "Decompression ($mb Mo) dans $output ..."
    if (-not (Test-Path $output)) { New-Item -ItemType Directory -Path $output | Out-Null }
    Expand-Archive -Path $zip -DestinationPath $output -Force
    $projects = (Get-ChildItem (Join-Path $output "projects") -Filter "*.md" -ErrorAction SilentlyContinue).Count
    Write-Host "Etat restaure : $projects fichiers projet dans output\projects." -ForegroundColor Green
    Write-Host "Suite : .venv\Scripts\python.exe -m tsi_agent llm-check (apres un premier run), puis deploy\updater\update.ps1"
} finally {
    Remove-Item -Recurse -Force $temp -ErrorAction SilentlyContinue
}
