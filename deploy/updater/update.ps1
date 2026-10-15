# Full update of the TSI data on a Windows PC (see deploy/updater/README.md).
# Runs: run -> deliverables -> csr-fetch -> csr-build -> csr-link -> anomalies -> prod-export,
# and stops at the first error. No key in this file: OPENAI_API_KEY is read from the environment
# or from .env at the root of the repository.
#
#   .\deploy\updater\update.ps1
#   .\deploy\updater\update.ps1 -Settings deploy\updater\settings.updater.yaml -SkipDeliverables
#   .\deploy\updater\update.ps1 -SkipCsr
#   .\deploy\updater\update.ps1 -DetailOnly        # csr-link: only the projects with a detail page
#
# Compatible with Windows PowerShell 5.1. Can be started from any folder: the commands run at the root of the repository.

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
    # Local to the function: a line on stderr (log, warning) does not stop the script (ISE in particular);
    # the result is judged on the exit code.
    $ErrorActionPreference = "Continue"
    & $python -m tsi_agent --settings $settingsPath @stepArgs | Out-Host
    return $LASTEXITCODE
}

function Stop-Update([string]$message, [int]$code) {
    Write-Host ""
    Write-Host ("STOPPED: " + $message) -ForegroundColor Red
    exit $code   # the main finally block runs Pop-Location
}

# Failed LLM step. Exit code 2: fatal error, including "ERREUR (fournisseur LLM)" (LLM provider error: key, URL
# or model refused, checked before any work): nothing was processed; fix .env or the openai_* settings, then run again.
function Stop-LlmStep([string]$step, [int]$code) {
    if ($code -eq 2) {
        Stop-Update ($step + " failed (exit code 2): see the ERREUR (error) message above. If it is 'ERREUR (fournisseur LLM)' (LLM provider error), " +
                     "fix the key (.env) or openai_base_url / openai_model in the settings, then run again") 2
    }
    Stop-Update ($step + " failed (exit code " + $code + "): see logs\runs\") $code
}

# Settings path: relative to the current folder if it exists there, otherwise to the root of the repository.
if (Test-Path $Settings) { $settingsPath = (Resolve-Path $Settings).Path } else { $settingsPath = Join-Path $root $Settings }
if (-not (Test-Path $python)) {
    throw "project Python not found ($python): create the environment first (deploy/updater/README.md, step 3)"
}
Push-Location $root
try {
    if (-not (Test-Path $settingsPath)) {
        throw "settings not found: $settingsPath (copy deploy\updater\settings.updater.yaml to config\settings.yaml)"
    }
    $settingsText = Get-Content $settingsPath -Raw -Encoding utf8
    $provider = Get-YamlValue $settingsText "llm_provider"
    if (-not $provider) { $provider = "claude_cli" }
    if ($provider -eq "openai") {
        $keyName = Get-YamlValue $settingsText "openai_api_key_env"
        if (-not $keyName) { $keyName = "OPENAI_API_KEY" }
        if (-not (Test-KeyAvailable $keyName)) {
            throw "key $keyName is missing: put it in .env at the root of the repository (see deploy\updater\.env.example)"
        }
        Write-Host "LLM provider: openai ($(Get-YamlValue $settingsText 'openai_model'), $(Get-YamlValue $settingsText 'openai_base_url'))"
    } else {
        Write-Host "LLM provider: $provider"
    }
    if (-not (Test-Path (Join-Path $root "output\reports\llm_check.md"))) {
        Write-Host "Tip: first run .venv\Scripts\python.exe -m tsi_agent llm-check (checks the quality of the" -ForegroundColor Yellow
        Write-Host "extraction with this provider, a few cents); the update continues without it." -ForegroundColor Yellow
    }

    $start = Get-Date

    $code = Invoke-Step "1/7 projects" @("run")
    if ($code -eq 1) { Stop-Update "run: quota reached, or key/model refused along the way: see logs\runs\ (run again later: the run resumes where it stopped)" 1 }
    if ($code -ne 0) { Stop-LlmStep "run" $code }

    if ($SkipDeliverables) {
        Write-Host "=== 2/7 deliverables: skipped (-SkipDeliverables)"
    } else {
        $code = Invoke-Step "2/7 deliverables" @("deliverables")
        if ($code -eq 1) { Stop-Update "deliverables: quota reached, or key/model refused along the way: see logs\runs\ (run again later: it resumes automatically)" 1 }
        if ($code -ne 0) { Stop-LlmStep "deliverables" $code }
    }

    $csrQuota = $false
    if ($SkipCsr) {
        Write-Host "=== 3-5/7 CSR: skipped (-SkipCsr)"
    } else {
        $code = Invoke-Step "3/7 CSR database (ECFIN)" @("csr-fetch")
        if ($code -ne 0) { Stop-Update "csr-fetch failed (exit code $code): ECFIN site unavailable? run again later, or use -SkipCsr" $code }
        $code = Invoke-Step "4/7 CSR files" @("csr-build")
        if ($code -ne 0) { Stop-Update "csr-build failed (exit code $code)" $code }
        $linkArgs = @("csr-link")
        if ($DetailOnly) { $linkArgs += "--detail-only" }
        $code = Invoke-Step "5/7 project -> CSR links" $linkArgs
        if ($code -eq 1) {
            $csrQuota = $true
            Write-Host "csr-link: quota reached, or key/model refused along the way: see logs\runs\. Links are partial, run again later (cache and resume); continuing." -ForegroundColor Yellow
        } elseif ($code -ne 0) {
            Stop-LlmStep "csr-link" $code
        }
    }

    $code = Invoke-Step "6/7 anomalies" @("anomalies")
    if ($code -ne 0) { Stop-Update "anomalies failed (exit code $code)" $code }

    $code = Invoke-Step "7/7 production export" @("prod-export")
    if ($code -ne 0) { Stop-Update "prod-export failed (exit code $code): inconsistent metadata types or a file over 10 MB, see the JSON above" $code }

    $build = Join-Path $root "deploy\prod\build"
    $manifestPath = Join-Path $build "manifest.json"
    $manifest = Get-Content $manifestPath -Raw -Encoding utf8 | ConvertFrom-Json
    $elapsed = (Get-Date) - $start
    Write-Host ""
    Write-Host ("Done in {0:hh\:mm\:ss}." -f $elapsed) -ForegroundColor Green
    Write-Host "Files to import : $(Join-Path $build 'files')"
    Write-Host "Manifest        : $manifestPath"
    Write-Host "  $($manifest.files) files, $($manifest.chunks) chunks expected in the index"
    foreach ($p in $manifest.files_by_type.PSObject.Properties) {
        Write-Host ("  {0,-12} {1,6} files  {2,7} chunks" -f $p.Name, $p.Value, $manifest.chunks_by_type.($p.Name))
    }
    if ($csrQuota) { Write-Host "Warning: partial CSR links (quota); run csr-link then prod-export again later." -ForegroundColor Yellow }
    Write-Host "Next: import into Haystack (deploy\prod\GUIDE.md, or deploy\prod\upload.ps1)."
} finally {
    Pop-Location
}
exit 0
