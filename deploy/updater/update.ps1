# Full update of the TSI data on a Windows PC (see deploy/updater/README.md).
# Runs: run -> deliverables -> csr-fetch -> csr-build -> csr-link -> anomalies -> [supabase-sync] -> prod-export
# -> [release-data --publish] -> [index]. No key in this file: OPENAI_API_KEY, SUPABASE_URL and
# SUPABASE_SERVICE_KEY are read from the environment or from .env at the root of the repository.
#
#   .\deploy\updater\update.ps1                      # incremental update
#   .\deploy\updater\update.ps1 -RecheckAll          # full check: every detail and publication page re-read
#   .\deploy\updater\update.ps1 -Sync -Publish       # also Supabase (web explorer) and a data-* release
#   .\deploy\updater\update.ps1 -Index               # also the vector index of the local chatbot
#   .\deploy\updater\update.ps1 -SkipDeliverables -SkipCsr -DetailOnly -Settings deploy\updater\settings.updater.yaml
#   -Scheduled: set by the scheduled task (register-task.ps1): full check in January, April, July and October.
#
# Exit codes: 0 done; 1 warning (LLM quota, or a soft step failed: csr-fetch, supabase-sync, release, index);
# 3 warning (the site sitemap could not be read); 2 or another code from a strict step: stopped. Warnings do not
# stop the update: the next steps run and the script ends with the worst code (1 before 3).
# Log of each update: logs\update-YYYYMMDD-HHMM.log (transcript, with the summary of the runs at the end).
#
# Compatible with Windows PowerShell 5.1. Can be started from any folder: the commands run at the root of the repository.

param(
    [string]$Settings = "config\settings.yaml",
    [switch]$SkipDeliverables,
    [switch]$SkipCsr,
    [switch]$DetailOnly,
    [switch]$RecheckAll,
    [switch]$Sync,
    [switch]$Publish,
    [switch]$Index,
    [switch]$Scheduled,
    [string]$Repo = "MGE-Agilos/dg-reform-tsi"
)

$ErrorActionPreference = "Stop"
$root = Split-Path -Parent (Split-Path -Parent (Split-Path -Parent $MyInvocation.MyCommand.Path))
$python = Join-Path $root ".venv\Scripts\python.exe"
$script:worst = 0
$script:warnings = @()

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

# Result of each step for the summary (.github/scripts/ci_summary.py reads steps.tsv).
function Add-StepResult([string]$name, [int]$code, [int]$seconds, [string]$policy) {
    Add-Content -Path (Join-Path $script:work "steps.tsv") -Value ($name + "`t" + $code + "`t" + $seconds + "`t" + $policy) -Encoding ascii
}

function Invoke-Step([string]$label, [string]$name, [string]$policy, [string[]]$stepArgs) {
    Write-Host ""
    Write-Host ("=== " + $label + " : python -m tsi_agent " + ($stepArgs -join " ") + "  (" + (Get-Date -Format "HH:mm:ss") + ")")
    # Local to the function: a line on stderr (log, warning) does not stop the script (ISE in particular);
    # the result is judged on the exit code.
    $ErrorActionPreference = "Continue"
    $t0 = Get-Date
    & $python -m tsi_agent --settings $settingsPath @stepArgs | Out-Host
    $code = $LASTEXITCODE
    Add-StepResult $name $code ([int]((Get-Date) - $t0).TotalSeconds) $policy
    return $code
}

# Warning: the update continues; the final exit code is the worst one (1 before 3).
function Add-Warning([string]$message, [int]$code) {
    Write-Host ("WARNING: " + $message) -ForegroundColor Yellow
    $script:warnings += $message
    if ($code -eq 1) { $script:worst = 1 }
    elseif ($code -eq 3 -and $script:worst -eq 0) { $script:worst = 3 }
}

function Stop-Update([string]$message, [int]$code) {
    Write-Host ""
    Write-Host ("STOPPED: " + $message) -ForegroundColor Red
    $script:stopCode = $code
    throw "update stopped"
}

# LLM step (run, deliverables, csr-link): 1 quota and 3 sitemap unavailable are warnings; anything else stops.
# Exit code 2: fatal error, including "ERREUR (fournisseur LLM)" (LLM provider error: key, URL or model refused,
# checked before any work): nothing was processed; fix .env or the settings, then run again.
function Test-LlmStep([string]$step, [int]$code) {
    if ($code -eq 0) { return }
    if ($code -eq 1) {
        Add-Warning ($step + ": LLM quota reached, or key/model refused along the way (exit code 1): see logs\runs\; run again later, it resumes where it stopped") 1
        return
    }
    if ($code -eq 3) {
        Add-Warning ($step + ": the site sitemap could not be read (exit code 3, HTTP 429?): known pages with a recorded date were not re-read; run again later") 3
        return
    }
    if ($code -eq 2) {
        Stop-Update ($step + " failed (exit code 2): see the ERREUR (error) message above. If it is 'ERREUR (fournisseur LLM)' (LLM provider error), " +
                     "check the Claude CLI login or the key (.env) and the settings, then run again") 2
    }
    Stop-Update ($step + " failed (exit code " + $code + "): see logs\runs\") $code
}

function Test-ChatRunning {
    try {
        $procs = Get-CimInstance Win32_Process -Filter "Name = 'python.exe'" -ErrorAction Stop
    } catch {
        return $false
    }
    foreach ($p in $procs) {
        if ($p.CommandLine -and $p.CommandLine -match "tsi_agent" -and $p.CommandLine -match "(^|\s)chat(\s|$)") { return $true }
    }
    return $false
}

# Settings path: relative to the current folder if it exists there, otherwise to the root of the repository.
if (Test-Path $Settings) { $settingsPath = (Resolve-Path $Settings).Path } else { $settingsPath = Join-Path $root $Settings }
if (-not (Test-Path $python)) {
    throw "project Python not found ($python): create the environment first (deploy/updater/README.md, step 3)"
}

$stamp = Get-Date -Format "yyyyMMdd-HHmm"
$logsDir = Join-Path $root "logs"
New-Item -ItemType Directory -Force -Path $logsDir | Out-Null
$logPath = Join-Path $logsDir ("update-" + $stamp + ".log")
$script:work = Join-Path $logsDir ("update-" + $stamp)
New-Item -ItemType Directory -Force -Path $script:work | Out-Null
$script:stopCode = 0
$since = (Get-Date).ToUniversalTime().ToString("yyyy-MM-ddTHH:mm:ssZ")
$releaseTag = ""
$mode = "incremental"

Start-Transcript -Path $logPath | Out-Null
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
    } elseif ($provider -eq "claude_cli") {
        if (-not (Get-Command claude -ErrorAction SilentlyContinue)) {
            throw "llm_provider is claude_cli but the 'claude' command is not on PATH: install the Claude Code CLI (npm install -g @anthropic-ai/claude-code) or use the openai settings"
        }
        Write-Host "LLM provider: claude_cli (Claude Code CLI). It must be logged in on this PC (run 'claude' once and sign in), or ANTHROPIC_API_KEY must be set." -ForegroundColor Yellow
    } else {
        Write-Host "LLM provider: $provider"
    }

    # Quarterly full check of the scheduled task: every page re-read whatever its sitemap date, only changed
    # pages go to the LLM.
    $recheck = [bool]$RecheckAll
    if ($Scheduled -and @(1, 4, 7, 10) -contains (Get-Date).Month) {
        $recheck = $true
        Write-Host "Scheduled run in January, April, July or October: quarterly full check (--recheck-all)."
    }
    if ($recheck) { $mode = "full check" } else { $mode = "incremental" }
    if ($Scheduled) { $mode = $mode + " (scheduled)" }
    Write-Host "Mode: $mode. Log: $logPath"

    if ($Sync -and -not ((Test-KeyAvailable "SUPABASE_URL") -and (Test-KeyAvailable "SUPABASE_SERVICE_KEY"))) {
        Add-Warning "-Sync: SUPABASE_URL or SUPABASE_SERVICE_KEY missing (environment or .env): supabase-sync will be skipped" 1
        $Sync = $false
    }
    if ($Publish) {
        $gh = Get-Command gh -ErrorAction SilentlyContinue
        $ghOk = $false
        if ($gh) {
            $ErrorActionPreference = "Continue"
            & gh auth status 2>&1 | Out-Null
            $ghOk = ($LASTEXITCODE -eq 0)
            $ErrorActionPreference = "Stop"
        }
        if (-not $ghOk) {
            Add-Warning "-Publish: the GitHub CLI 'gh' is missing or not logged in (gh auth login): no release will be published" 1
            $Publish = $false
        }
    }
    if (-not (Test-Path (Join-Path $root "output\reports\llm_check.md"))) {
        Write-Host "Tip: first run .venv\Scripts\python.exe -m tsi_agent llm-check (checks the quality of the" -ForegroundColor Yellow
        Write-Host "extraction with this provider, a few cents); the update continues without it." -ForegroundColor Yellow
    }

    $start = Get-Date
    $recheckArgs = @()
    if ($recheck) { $recheckArgs = @("--recheck-all") }

    $code = Invoke-Step "projects" "run" "quota" (@("run") + $recheckArgs)
    Test-LlmStep "run" $code

    if ($SkipDeliverables) {
        Write-Host "=== deliverables: skipped (-SkipDeliverables)"
    } else {
        $code = Invoke-Step "deliverables" "deliverables" "quota" (@("deliverables") + $recheckArgs)
        Test-LlmStep "deliverables" $code
    }

    $csrQuota = $false
    if ($SkipCsr) {
        Write-Host "=== CSR: skipped (-SkipCsr)"
    } else {
        $code = Invoke-Step "CSR database (ECFIN)" "csr-fetch" "soft" @("csr-fetch")
        if ($code -ne 0) { Add-Warning "csr-fetch failed (exit code $code): ECFIN site unavailable? the previous CSR export is kept" 1 }
        $code = Invoke-Step "CSR files" "csr-build" "strict" @("csr-build")
        if ($code -ne 0) { Stop-Update "csr-build failed (exit code $code)" $code }
        $linkArgs = @("csr-link")
        if ($DetailOnly) { $linkArgs += "--detail-only" }
        $code = Invoke-Step "project -> CSR links" "csr-link" "quota" $linkArgs
        if ($code -eq 1) { $csrQuota = $true }
        Test-LlmStep "csr-link" $code
    }

    $code = Invoke-Step "anomalies" "anomalies" "strict" @("anomalies")
    if ($code -ne 0) { Stop-Update "anomalies failed (exit code $code)" $code }

    if ($Sync) {
        $code = Invoke-Step "Supabase (web explorer)" "supabase-sync" "soft" @("supabase-sync")
        if ($code -ne 0) {
            Write-Host "supabase-sync failed (exit code $code): new attempt in 60 s" -ForegroundColor Yellow
            Start-Sleep -Seconds 60
            $code = Invoke-Step "Supabase (web explorer), second attempt" "supabase-sync" "soft" @("supabase-sync")
            if ($code -ne 0) { Add-Warning "supabase-sync failed twice (exit code $code): the tables of the web explorer may be partial; run python -m tsi_agent supabase-sync again later" 1 }
        }
    }

    $code = Invoke-Step "production export" "prod-export" "strict" @("prod-export")
    if ($code -ne 0) { Stop-Update "prod-export failed (exit code $code): inconsistent metadata types or a file over 10 MB, see the JSON above" $code }

    if ($Publish) {
        # Same tag as the GitHub workflow: data-YYYY-MM-DD, with -HHMM for a second release on the same day.
        $tag = "data-" + (Get-Date -Format "yyyy-MM-dd")
        $ErrorActionPreference = "Continue"
        & gh release view $tag -R $Repo 2>&1 | Out-Null
        if ($LASTEXITCODE -eq 0) { $tag = $tag + "-" + (Get-Date -Format "HHmm") }
        $ErrorActionPreference = "Stop"
        $code = Invoke-Step "data release" "release-data" "soft" @("release-data", "--publish", "--tag", $tag, "--repo", $Repo)
        if ($code -eq 0) { $releaseTag = $tag } else { Add-Warning "release-data failed (exit code $code): no release published; run python -m tsi_agent release-data --publish later" 1 }
    }

    if ($Index) {
        if (Test-ChatRunning) {
            Add-Warning "-Index: the local chat server (python -m tsi_agent chat) is running and locks the vector store: index skipped; stop the chat server, then run python -m tsi_agent index" 1
        } else {
            Write-Host "Index: the local chat server must be stopped while the vector index is updated."
            $code = Invoke-Step "vector index (local chatbot)" "index" "soft" @("index")
            if ($code -ne 0) { Add-Warning "index failed (exit code $code): run python -m tsi_agent index later" 1 }
        }
    }

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
    if ($releaseTag) { Write-Host "Release: https://github.com/$Repo/releases/tag/$releaseTag" }
    Write-Host "Next: import into Haystack (deploy\prod\GUIDE.md, or deploy\prod\upload.ps1)."
} catch {
    if ($script:stopCode -eq 0) {
        Write-Host ("STOPPED: " + $_.Exception.Message) -ForegroundColor Red
        $script:stopCode = 2
    }
} finally {
    # Summary of the runs of this update (same as the GitHub job summary), into the log.
    try {
        $summaryArgs = @(".github\scripts\ci_summary.py", "--runs-dir", "logs\runs", "--work-dir", $script:work,
                         "--manifest", "deploy\prod\build\manifest.json", "--since", $since, "--mode", $mode)
        if ($releaseTag) { $summaryArgs += @("--release-tag", $releaseTag, "--release-url", "https://github.com/$Repo/releases/tag/$releaseTag") }
        Write-Host ""
        $ErrorActionPreference = "Continue"
        & $python @summaryArgs | Out-Host
    } catch {
        Write-Host "(summary unavailable: $($_.Exception.Message))"
    }
    if ($script:warnings.Count -gt 0) {
        Write-Host ""
        Write-Host "Warnings:" -ForegroundColor Yellow
        foreach ($w in $script:warnings) { Write-Host ("  - " + $w) -ForegroundColor Yellow }
    }
    Pop-Location
    Stop-Transcript | Out-Null
}
if ($script:stopCode -ne 0) { exit $script:stopCode }
exit $script:worst
