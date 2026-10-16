# Updating the TSI data on a Windows PC

This guide sets up, on a PC, the search for new documents published by the Commission (TSI and SRSP projects,
project publications, country-specific recommendations (CSRs) and project -> CSR links), with an
OpenAI-compatible LLM: the Commission's **EC GPT** gateway or the **OpenAI** API. The result is a folder of files
ready to import into Haystack Enterprise.

No knowledge of Python is needed: you only copy the commands into **PowerShell**, from the repository folder. Use
the normal PowerShell console (Start menu, "Windows PowerShell" or "Terminal"), not **PowerShell ISE**, which
treats the tools' progress messages as errors.

The commands of the agent (`python -m tsi_agent ...`) print their messages in French. Where this guide quotes
one, the English meaning is given in parentheses.

## Installation (once)

1. **Prerequisites.** Install:
   - **Python 3.12** (<https://www.python.org/downloads/windows/>), ticking "Add python.exe to PATH";
   - **Git** (<https://git-scm.com/download/win>);
   - **GitHub CLI** (<https://cli.github.com>), then sign in once:
     ```powershell
     gh auth login
     ```
     (choose *GitHub.com*, *HTTPS*, then sign-in with the browser).

2. **Clone the repository** into a working folder, preferably outside OneDrive (updates write thousands of small
   files):
   ```powershell
   cd C:\
   git clone https://github.com/MGE-Agilos/dg-reform-tsi.git tsi
   cd C:\tsi
   ```
   All the following commands are run from this folder.

3. **Create the Python environment**:
   ```powershell
   python -m venv .venv
   .venv\Scripts\pip install -e .
   ```
   The update commands do not need the dependencies of the local chatbot (`.[local]`: PyTorch, Qdrant).

4. **Settings.** Copy the settings prepared for this mode:
   ```powershell
   Copy-Item deploy\updater\settings.updater.yaml config\settings.yaml
   ```
   They select `llm_provider: openai`, the EC GPT gateway (`openai_base_url`) and the `gpt-5.1` model. For the
   public OpenAI API, empty the `openai_base_url:` line. Alternative without copying: add
   `--settings deploy\updater\settings.updater.yaml` to each command, and `-Settings ...` to `update.ps1`.
   Two optional settings: `openai_max_retries` (retries by the client after a network error, a 429 or a 5xx; 2 by
   default) and `openai_max_completion_tokens` (cap on output tokens, reasoning included; empty by default, no
   cap).

5. **LLM key.** Copy the key file template, then open it in Notepad:
   ```powershell
   Copy-Item deploy\updater\.env.example .env
   notepad .env
   ```
   Fill in `OPENAI_API_KEY=` with the EC GPT key (or the OpenAI key). **Never commit `.env`** (Git ignores it) and
   never copy the key into another file.

6. **Get the current state of the data** (projects already extracted, publications, CSRs, links) from the latest
   release of the repository, so as not to redo everything:
   ```powershell
   Set-ExecutionPolicy -Scope Process Bypass    # allows .ps1 scripts in this window only
   .\deploy\updater\restore-state.ps1
   ```
   Repeat the first line in each new PowerShell window before running a `.ps1` script (otherwise: "running scripts
   is disabled on this system"). Another way, without changing the policy:
   `powershell -ExecutionPolicy Bypass -File .\deploy\updater\restore-state.ps1`. If you got the repository as a
   ZIP rather than with `git clone`, unblock the scripts once: `Get-ChildItem -Recurse -Filter *.ps1 | Unblock-File`.
   Option `-Tag data-YYYY-MM-DD` for a specific release. If `output\` already exists, the script refuses to
   continue without `-Force` (files with the same name are then replaced, the others kept).

7. **Check the quality with this LLM** before the first large batch. `llm-check` redoes the extraction of a few
   projects that were already processed and puts the old and new results side by side:
   ```powershell
   .venv\Scripts\python.exe -m tsi_agent run --limit 3
   .venv\Scripts\python.exe -m tsi_agent llm-check --n 3
   ```
   `llm-check` reads the pages from the HTTP cache (`.cache\http`), which is not in the release: on a new PC it
   answers « aucune page detail … dans le cache » (no detail page … in the cache, exit code 1) until a `run` has
   reloaded some pages. The first `run` reloads **all** the detail pages of the site (see *Durations*): you can
   stop it with Ctrl+C after a few minutes, `llm-check` then has enough pages, and the run later resumes where it
   stopped. Then read `output\reports\llm_check.md`: summaries, sections and results should be comparable.

## Update (each time)

8. **Run the full update**:
   ```powershell
   .\deploy\updater\update.ps1
   ```
   The script checks the settings and the key, runs the seven steps below and stops at the first error. At the
   end, it shows the folder to import (`deploy\prod\build\files`) and the figures of the manifest
   (`deploy\prod\build\manifest.json`).

   Options:
   - `-SkipDeliverables`: do not process the publications (the longest and most expensive step);
   - `-SkipCsr`: do not update the CSRs or the project -> CSR links;
   - `-DetailOnly`: `csr-link` only sends the projects with a detail page to the LLM (the others later);
   - `-Settings <file>`: settings other than `config\settings.yaml`.

   The steps can also be run one by one, in the same order, with
   `.venv\Scripts\python.exe -m tsi_agent <command>`.

9. **Import into Haystack Enterprise**, either:
   - through the web interface: follow [deploy/prod/GUIDE.md](../prod/GUIDE.md), with the files of
     `deploy\prod\build\files` instead of the release zip (4 batches with their metadata);
   - with the SDK (recommended: keeps the metadata of each file): `deploy\prod\upload.ps1`. It reads
     `API_KEY`, `API_URL` and `DEFAULT_WORKSPACE_NAME` from the PowerShell session. To take them from `.env`:
     ```powershell
     Get-Content .env | Where-Object { $_ -match '^\s*(API_KEY|API_URL|DEFAULT_WORKSPACE_NAME)\s*=\s*(.+)$' } | ForEach-Object { Set-Item "env:$($Matches[1])" $Matches[2].Trim() }
     .\deploy\prod\upload.ps1
     ```
     The SDK is installed in its own Python environment, to activate before `upload.ps1` (exact commands in
     [deploy/prod/GUIDE.md](../prod/GUIDE.md#sdk-only-prepare-the-computer-once), "SDK only: prepare the
     computer"). To delete the documents that have left the corpus: `deploy\prod\obsolete-files.ps1` (GUIDE,
     step 2).

10. **Optional: publish a data release**, if you have write access to the repository. Colleagues then get this
    state with `restore-state.ps1`, and the deployment through the web interface starts from this release:
    ```powershell
    .venv\Scripts\python.exe -m tsi_agent release-data --publish
    ```
    Default tag `data-YYYY-MM-DD` (option `--tag` if a release of the day already exists; nothing is
    overwritten).

## What each step does

| Step | Command | What it does | LLM |
|---|---|---|---|
| 1 | `run` | reads the country pages, the policy areas, the SRSP PDF and the sitemap again; extracts new or changed projects into `output\projects` | yes, one call per changed page (and one sitemap arbitration if needed) |
| 2 | `deliverables` | downloads the project publications (PDF), extracts their text and summarises it into `output\deliverables` | yes, one call per new publication |
| 3 | `csr-fetch` | downloads the ECFIN CSR database (full export, `output\csr\csr_full.xlsx`) | no |
| 4 | `csr-build` | writes one file per CSR (2016-2025) into `output\csr` | no |
| 5 | `csr-link` | assesses which CSRs each project supports (`output\csr_links`), with a cache: only new or changed projects are sent | yes |
| 6 | `anomalies` | report of publication anomalies (`output\reports\anomalies.csv` and `.md`) | no |
| 7 | `prod-export` | writes one file per project, programme, publication and CSR, with its metadata, into `deploy\prod\build\files`, and the manifest | no |

Exit codes: `0` done; `1` LLM quota reached (the next run resumes where it stopped); `2` fatal error (details in
the `ERREUR` (error) message and in `logs\runs\`).

**LLM check before any work.** `run`, `deliverables`, `csr-link` and `llm-check` first check the provider: known
`llm_provider` setting, key present and, in `openai` mode, a very small test call. A refused key, URL
(`openai_base_url`) or model (`openai_model`) therefore stops the command **straight away**, with the message
`ERREUR (fournisseur LLM) : ...` (LLM provider error) and exit code `2`, without processing anything; `update.ps1`
then stops and reminds you what to fix. If the key or the model is refused along the way (401, 403, 404), the
batch stops as on a quota (exit code `1`) and the log says it is a configuration error. For `csr-link` only, the
script continues after a quota ("run again later") and goes on to `prod-export`: the links already made are
kept.

## Durations and costs

- **Politeness towards the Commission's website.** At least 2 s between two requests; after a "too many
  requests" response (HTTP 429), the agent slows down to up to 15 s between two requests and only speeds up again
  gradually. This is the main reason for the duration.
- **Incremental `run`**: the sitemap of the site gives the date of last change (`<lastmod>`) of each page. A
  known detail page whose date has not changed since it was last read is **not downloaded again** (counted as
  `skipped_by_sitemap` in `logs\runs\<run>.summary.json`). New pages, pages with a newer date and pages without a
  date are read as before. The listings, the SRSP PDF and the sitemap itself are always read.
- **First `run` on a PC**: if the restored state has no sitemap dates yet (releases before October 2026), record
  them once without downloading anything (the data of the release is assumed to be current):
  `.venv\Scripts\python.exe -m tsi_agent run --seed-sitemap-lastmod`. Otherwise all the detail pages are
  reloaded, about **2 hours**. Later runs only reload the listings and the changed pages: a few minutes to half an
  hour. `run --recheck-all` reads every detail page again whatever its date (only changed pages go to the LLM);
  `run --full` also re-extracts every page with the LLM (long and costly).
- **Publications**: download of the PDFs (up to 50 MB) and one long LLM call per publication; allow several
  minutes per ten new publications.
- **Costs** (order of magnitude, OpenAI public price for gpt-5.1: $1.25 per million input tokens, $10 per million
  output tokens): about 1 cent per extracted project, 5 to 10 cents per summarised publication, 4 cents per
  project linked to the CSRs. A usual weekly update costs less than a few dollars; redoing everything
  (`run --full`, all the publications, all the links) about $100. With EC GPT, billing is internal to the
  Commission. The cost of each run is in `logs\runs\<run>.summary.json` (`cost_usd`), computed with the `pricing`
  section of the settings.

## Automatic monthly update (GitHub Actions)

The workflow [`.github/workflows/update-data.yml`](../../.github/workflows/update-data.yml) runs the whole update
in GitHub, without a PC. The LLM is the Claude Code CLI (`llm_provider: claude_cli`, `model: sonnet` in
`config/settings.yaml`), authenticated by an Anthropic API key (no Claude subscription is used).

**What it does**, in order:

1. installs the agent (`pip install .`) and the Claude Code CLI, then makes a tiny test call (Haiku) to check the
   key before the long steps;
2. restores the state (`output/`) from `tsi-output-state.zip` of the **latest `data-*` release** of the
   repository; with no such release, the job stops with a clear message;
3. if the state has no sitemap dates yet, records them once (`run --seed-sitemap-lastmod`, no download);
4. `run` (incremental), `deliverables`, `csr-fetch`, `csr-build`, `csr-link --detail-only`, `anomalies`,
   `supabase-sync`, `prod-export`;
5. publishes a release `data-YYYY-MM-DD` (`release-data --publish`; `data-YYYY-MM-DD-HHMM` for a second run on
   the same day). The next run restores its state from it.

**Schedule.** The 1st of each month at 03:00 UTC. In January, April, July and October, the scheduled run is a
**full check** (`run --recheck-all`): every detail page is read again whatever its sitemap date, in case the
site changed a page without changing its date; only the pages whose text changed go to the LLM.

**Run it by hand.** GitHub > *Actions* > *Monthly data update* > *Run workflow* (or
`gh workflow run update-data.yml -f skip_deliverables=true`). Inputs:

| Input | Default | Effect |
|---|---|---|
| `full` | false | `run --full`: re-read **and re-extract with the LLM** every detail page (hours, tens of dollars; every project is then sent again to `csr-link`) |
| `recheck_all` | false | `run --recheck-all`: the quarterly full check |
| `detail_only` | true | `csr-link --detail-only`: only the projects with a detail page go to the LLM |
| `skip_deliverables` | false | skip the publications step |
| `publish_release` | true | publish the `data-*` release at the end (without it, the next run starts again from the previous release) |
| `sync_supabase` | true | publish projects, CSRs, links and anomalies to Supabase (web explorer) |

Only one update runs at a time (`concurrency: update-data`); the job stops after 340 minutes.

**Errors.** A step that stops on the LLM quota (exit code 1: `run`, `deliverables`, `csr-link`), a failed
`csr-fetch` (ECFIN site down: the previous CSR export is kept) or a failed `supabase-sync` does not stop the
following steps: the release is still published with what was done, then the job ends **in failure** so that
GitHub sends an e-mail. Run the workflow again later: each step resumes where it stopped. A configuration error
(exit code 2, for example a refused key) or a failed `csr-build`, `anomalies`, `prod-export` or `release-data`
stops the job at once, without a release.

**Secrets** (GitHub > *Settings* > *Secrets and variables* > *Actions* > *New repository secret*, or
`gh secret set NAME`): never in a file, never printed in the log.

| Secret | Used by |
|---|---|
| `ANTHROPIC_API_KEY` | the Claude Code CLI (`run`, `deliverables`, `csr-link`, test call) |
| `SUPABASE_URL` | `supabase-sync` |
| `SUPABASE_SERVICE_KEY` | `supabase-sync` (service_role key) |

The release uses the token that GitHub gives to each job (`permissions: contents: write`): no secret needed.

**Where to see the result.** The page of the run (*Actions* > the run) shows a **summary**: result and duration
of each step; for each run of the agent, the projects new or updated, the pages skipped thanks to the sitemap,
the publications, the CSR links, the LLM calls and their cost; the CSR files, the rows sent to Supabase, the
files and chunks of the export, and the link to the release. The logs of the agent (`logs/runs/`) are attached
to the run as the artifact `run-logs-<id>` for 30 days.

**Costs.**
- GitHub: a public repository has unlimited minutes on `ubuntu-latest`; for a private one, a monthly run of 1
  to 4 hours fits in the free minutes of most plans.
- Anthropic API (Claude Sonnet: $3 per million input tokens, $15 per million output tokens, prices of 2026):
  roughly 2 to 3 cents per extracted project, 5 to 15 cents per summarised publication, 4 to 5 cents per project
  linked to the CSRs. A usual monthly update costs **a few dollars**; the quarterly full check costs about the
  same (only changed pages go to the LLM). The exact cost of each run is in the summary (`cost_usd`, as reported
  by the CLI). Set a spending limit on the key in the Anthropic console.

## Troubleshooting

| Message | Cause and solution |
|---|---|
| `key OPENAI_API_KEY is missing` (update.ps1) | `.env` missing from the root of the repository, or empty `OPENAI_API_KEY=` line: step 5. |
| `ERREUR (fournisseur LLM) : cle API absente` (LLM provider error: API key missing; exit code 2) | `.env` missing from the root of the repository, or empty `OPENAI_API_KEY=` line: step 5. |
| `ERREUR (fournisseur LLM) : erreur de configuration ...` (LLM provider error: configuration error; exit code 2), or 401, 403, 404 in the log | invalid or expired key, OpenAI key used with the EC GPT URL (or the other way round), wrong URL or model name: check `.env`, `openai_base_url` and `openai_model`. |
| `reponse tronquee (finish_reason=length)` (truncated response) | the response reached `openai_max_completion_tokens`: increase this setting (for example 16000) or empty it. |
| Stop with exit code 1, "quota" | rate or budget limit of the key reached: run again later, it resumes automatically (`openai_max_retries` sets the immediate retries). |
| Timeout on publications | slow gateway: increase `openai_timeout_s` (or `deliverables_llm_timeout_s`) in the settings, then run again. |
| HTTP 429 from the Commission's website | normal: the agent slows down by itself. Do not run two updates at the same time. |
| `csr-fetch failed` | ECFIN site unavailable: run again later, or `update.ps1 -SkipCsr`. |
| `prod-export failed` | inconsistent metadata types or a file over 10 MB: the JSON shown gives the files concerned. |
| `llm-check`: « aucune page detail … dans le cache » (no detail page … in the cache) | run a `run` first (step 7). |
| `restore-state.ps1`: `gh` not found or not signed in | install GitHub CLI and run `gh auth login` (step 1). |
| `project Python not found` | the `.venv` environment has not been created in the repository folder: step 3. |
| Scripts blocked ("running scripts is disabled on this system") | `Set-ExecutionPolicy -Scope Process Bypass` in the window (step 6), or `powershell -ExecutionPolicy Bypass -File .\deploy\updater\update.ps1`; repository downloaded as a ZIP: `Unblock-File` (step 6). |
| The script stops on a progress message in red | started from PowerShell ISE: use the normal PowerShell console. |

Never run two update commands at the same time on the same folder (`run`, `deliverables` and `csr-link` write
into `output\`).
