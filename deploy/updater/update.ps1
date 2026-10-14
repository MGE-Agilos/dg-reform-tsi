# Mise a jour complete des donnees TSI sur un PC Windows (voir deploy/updater/README.md).
# Enchaine : run -> deliverables -> csr-fetch -> csr-build -> csr-link -> anomalies -> prod-export,
# et s'arrete a la premiere erreur. Aucune cle dans ce fichier : OPENAI_API_KEY est lue dans l'environnement
# ou dans .env a la racine du depot.
#
#   .\deploy\updater\update.ps1
#   .\deploy\updater\update.ps1 -Settings deploy\updater\settings.updater.yaml -SkipDeliverables
#   .\deploy\updater\update.ps1 -SkipCsr
#   .\deploy\updater\update.ps1 -DetailOnly        # csr-link : seulement les projets avec page detail
#
# Compatible Windows PowerShell 5.1. Lance depuis n'importe quel dossier : les commandes tournent a la racine du depot.

param(
    [string]$Settings = "config\settings.yaml",
    [switch]$SkipDeliverables,
    [switch]$SkipCsr,
    [switch]$DetailOnly
)

$ErrorActionPreference = "Stop"
$root = Split-Path -Parent (Split-Path -Parent (Split-Path -Parent $MyInvocation.MyCommand.Path))
$python = Join-Path $root ".venv\Scripts\python.exe"

function Get-YamlValue([string]$text, [string]$key) {
    $m = [regex]::Match($text, "(?m)^" + [regex]::Escape($key) + ":[ \t]*([^#\r\n]*)")
    if ($m.Success) { return $m.Groups[1].Value.Trim().Trim('"').Trim("'") }
    return ""
}

function Test-KeyAvailable([string]$name) {
    if ([Environment]::GetEnvironmentVariable($name)) { return $true }
    $dotenv = Join-Path $root ".env"
    if (-not (Test-Path $dotenv)) { return $false }
    foreach ($line in (Get-Content $dotenv -Encoding utf8)) {
        $m = [regex]::Match($line, "^\s*(export\s+)?" + [regex]::Escape($name) + "\s*=\s*(.*)$")
        if ($m.Success -and $m.Groups[2].Value.Trim().Trim('"').Trim("'")) { return $true }
    }
    return $false
}

function Invoke-Step([string]$label, [string[]]$stepArgs) {
    Write-Host ""
    Write-Host ("=== " + $label + " : python -m tsi_agent " + ($stepArgs -join " ") + "  (" + (Get-Date -Format "HH:mm:ss") + ")")
    & $python -m tsi_agent --settings $settingsPath @stepArgs | Out-Host
    return $LASTEXITCODE
}

function Stop-Update([string]$message, [int]$code) {
    Write-Host ""
    Write-Host ("ARRET : " + $message) -ForegroundColor Red
    Pop-Location
    exit $code
}

# Chemin des reglages : relatif au dossier courant s'il y existe, sinon a la racine du depot.
if (Test-Path $Settings) { $settingsPath = (Resolve-Path $Settings).Path } else { $settingsPath = Join-Path $root $Settings }
if (-not (Test-Path $python)) {
    throw "Python du projet introuvable ($python) : creer d'abord l'environnement (deploy/updater/README.md, etape 3)"
}
Push-Location $root
try {
    if (-not (Test-Path $settingsPath)) {
        throw "reglages introuvables : $settingsPath (copier deploy\updater\settings.updater.yaml vers config\settings.yaml)"
    }
    $settingsText = Get-Content $settingsPath -Raw -Encoding utf8
    $provider = Get-YamlValue $settingsText "llm_provider"
    if (-not $provider) { $provider = "claude_cli" }
    if ($provider -eq "openai") {
        $keyName = Get-YamlValue $settingsText "openai_api_key_env"
        if (-not $keyName) { $keyName = "OPENAI_API_KEY" }
        if (-not (Test-KeyAvailable $keyName)) {
            throw "cle $keyName absente : la renseigner dans .env a la racine du depot (voir deploy\updater\.env.example)"
        }
        Write-Host "Fournisseur LLM : openai ($(Get-YamlValue $settingsText 'openai_model'), $(Get-YamlValue $settingsText 'openai_base_url'))"
    } else {
        Write-Host "Fournisseur LLM : $provider"
    }
    if (-not (Test-Path (Join-Path $root "output\reports\llm_check.md"))) {
        Write-Host "Conseil : lancer d'abord .venv\Scripts\python.exe -m tsi_agent llm-check (controle de la qualite" -ForegroundColor Yellow
        Write-Host "de l'extraction avec ce fournisseur, quelques centimes) ; la mise a jour continue sans." -ForegroundColor Yellow
    }
} catch {
    Pop-Location
    throw
}

$start = Get-Date

$code = Invoke-Step "1/7 projets" @("run")
if ($code -eq 1) { Stop-Update "quota du LLM atteint pendant run : relancer plus tard (le run reprend ou il s'etait arrete)" 1 }
if ($code -ne 0) { Stop-Update "run a echoue (code $code) : voir logs\runs\" $code }

if ($SkipDeliverables) {
    Write-Host "=== 2/7 livrables : ignore (-SkipDeliverables)"
} else {
    $code = Invoke-Step "2/7 livrables" @("deliverables")
    if ($code -eq 1) { Stop-Update "quota du LLM atteint pendant deliverables : relancer plus tard (reprise automatique)" 1 }
    if ($code -ne 0) { Stop-Update "deliverables a echoue (code $code) : voir logs\runs\" $code }
}

$csrQuota = $false
if ($SkipCsr) {
    Write-Host "=== 3-5/7 CSR : ignore (-SkipCsr)"
} else {
    $code = Invoke-Step "3/7 base CSR (ECFIN)" @("csr-fetch")
    if ($code -ne 0) { Stop-Update "csr-fetch a echoue (code $code) : site ECFIN indisponible ? relancer plus tard, ou -SkipCsr" $code }
    $code = Invoke-Step "4/7 fiches CSR" @("csr-build")
    if ($code -ne 0) { Stop-Update "csr-build a echoue (code $code)" $code }
    $linkArgs = @("csr-link")
    if ($DetailOnly) { $linkArgs += "--detail-only" }
    $code = Invoke-Step "5/7 liens projet -> CSR" $linkArgs
    if ($code -eq 1) {
        $csrQuota = $true
        Write-Host "csr-link : quota du LLM atteint, liens partiels. Relancer plus tard (cache et reprise) ; on continue." -ForegroundColor Yellow
    } elseif ($code -ne 0) {
        Stop-Update "csr-link a echoue (code $code)" $code
    }
}

$code = Invoke-Step "6/7 anomalies" @("anomalies")
if ($code -ne 0) { Stop-Update "anomalies a echoue (code $code)" $code }

$code = Invoke-Step "7/7 export de production" @("prod-export")
if ($code -ne 0) { Stop-Update "prod-export a echoue (code $code) : types de metadonnees incoherents ou fichier > 10 Mo, voir le JSON ci-dessus" $code }

$build = Join-Path $root "deploy\prod\build"
$manifestPath = Join-Path $build "manifest.json"
$manifest = Get-Content $manifestPath -Raw -Encoding utf8 | ConvertFrom-Json
$elapsed = (Get-Date) - $start
Write-Host ""
Write-Host ("Termine en {0:hh\:mm\:ss}." -f $elapsed) -ForegroundColor Green
Write-Host "Fichiers a importer : $(Join-Path $build 'files')"
Write-Host "Manifeste           : $manifestPath"
Write-Host "  $($manifest.files) fichiers, $($manifest.chunks) morceaux attendus dans l'index"
foreach ($p in $manifest.files_by_type.PSObject.Properties) {
    Write-Host ("  {0,-12} {1,6} fichiers  {2,7} morceaux" -f $p.Name, $p.Value, $manifest.chunks_by_type.($p.Name))
}
if ($csrQuota) { Write-Host "Attention : liens CSR partiels (quota) ; relancer csr-link puis prod-export plus tard." -ForegroundColor Yellow }
Write-Host "Suite : import dans Haystack (deploy\prod\GUIDE.md, ou deploy\prod\upload.ps1)."
Pop-Location
exit 0
