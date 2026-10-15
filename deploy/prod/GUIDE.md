# Updating PROD on Haystack Enterprise Platform

This guide gives the order of operations to replace the corpus of the `tsi-reform` workspace with a new data release, then republish the pipelines. It does not repeat the details, which are in [README.md](README.md): the role of each file, workspace isolation, secrets and the full acceptance tests.

**First installation.** Before anything else, follow steps 1 and 3 of the [README](README.md#steps): workspace, `GPT_EC` secret, SearchApi, API key and index creation.

## Before you start

- **Access:** you need access to the **`tsi-reform`** workspace on the client's platform.
- **Data release:** pick the release to deploy from the repository's *Releases* page (<https://github.com/MGE-Agilos/dg-reform-tsi/releases>). Its tag looks like `data-YYYY-MM-DD`, and its description (`NOTES.md`) gives the expected figures.
- **Upload method (step 3):** choose either the **SDK** (recommended, full chatbot) or the **web interface** (nothing to install, reduced chatbot).

### SDK only: prepare the computer (once)

Python 3.12 and Git must be installed. Open **PowerShell** (the normal console, not PowerShell ISE):

```powershell
# Repository scripts (upload.ps1, obsolete-files.ps1) and pipeline YAMLs
cd C:\
git clone https://github.com/MGE-Agilos/dg-reform-tsi.git tsi
# Haystack Enterprise SDK, in its own Python environment
python -m venv C:\tsi-sdk
C:\tsi-sdk\Scripts\pip install haystack-enterprise-sdk
```

In each session, before using the SDK:

```powershell
Set-ExecutionPolicy -Scope Process Bypass      # allows .ps1 scripts in this window only
C:\tsi-sdk\Scripts\Activate.ps1                # activates the SDK environment (haystack-enterprise command)
cd C:\tsi
git pull
```

Windows may still block a downloaded script, for example if you got the repository as a ZIP rather than with `git clone`. In that case, run `Get-ChildItem C:\tsi -Recurse -Filter *.ps1 | Unblock-File`. You can also run a script without changing the policy: `powershell -ExecutionPolicy Bypass -File .\deploy\prod\upload.ps1 ...`.

## 1. Download

1. From the release, download **`tsi-prod-files.zip`** and **`manifest.json`**.
2. Unzip the archive into an **empty folder**, for example `C:\tsi-release\files`, and put `manifest.json` in `C:\tsi-release`.

The zip contains the `tsi-*.md` files and their `.meta.json`, all at the same level. A folder that already holds old files makes the checks fail.

From the command line (optional, with GitHub CLI signed in):

```powershell
gh release download data-YYYY-MM-DD -p tsi-prod-files.zip -p manifest.json -R MGE-Agilos/dg-reform-tsi -D C:\tsi-release
Expand-Archive C:\tsi-release\tsi-prod-files.zip -DestinationPath C:\tsi-release\files
```

## 2. Prepare the workspace

While indexing runs (steps 3 and 4), the chatbot and the impact analysis answer from a partial corpus, or return nothing right after a deletion. Warn the testers, or update outside working hours.

- **SDK: do not delete everything.** An `OVERWRITE` upload replaces the changed files, and the platform re-indexes only those. You only need to delete the documents that have left the corpus. To get their names:
  ```powershell
  # Files of the previous release (the one currently online), unzipped separately:
  .\deploy\prod\obsolete-files.ps1 -Current C:\tsi-release-previous\files -New C:\tsi-release\files
  # or a list of names copied from Files (one name per line):
  .\deploy\prod\obsolete-files.ps1 -Current workspace-names.txt -New C:\tsi-release\files -Out to-delete.txt
  ```
  Delete these files in *Files*, before or after the upload. The script only prints the names: it deletes nothing itself.
- **Web interface: first delete all `tsi-*` files.** In *Files*, filter on `tsi-` and delete everything. Leave files that do not start with `tsi-` alone: they belong to other corpora.

## 3. Upload

The two methods do not give the same chatbot:

| Method | Metadata | Chat pipeline to use |
|---|---|---|
| **SDK**: `deploy\prod\upload.ps1` (recommended) | Complete: each `.meta.json` is attached to its file (countries, programme, year, linked CSRs…). | `pipeline-tsi-chat.yaml` (with `list_projects`, `find_projects` and the `csr` filter) |
| **Web interface**: *Files > Upload*, in 4 batches | Only what is common to each batch (`Topic`, `corpus`, `doc_type`). | `pipeline-tsi-chat-ui.yaml` (search only: no `list_projects`, no `find_projects`, no `csr` filter) |

### With the SDK

In the session prepared above (SDK environment activated, folder `C:\tsi`):

```powershell
$s = Read-Host -AsSecureString "Workspace API key"
$env:API_KEY = [Runtime.InteropServices.Marshal]::PtrToStringAuto([Runtime.InteropServices.Marshal]::SecureStringToBSTR($s))
$env:API_URL = "<API URL of the client's platform>"
$env:DEFAULT_WORKSPACE_NAME = "tsi-reform"
.\deploy\prod\upload.ps1 -Files C:\tsi-release\files -Manifest C:\tsi-release\manifest.json
```

The script checks the variables, the manifest and the number of files. It then shows the target URL and workspace, and uploads.

Without `-Files` and `-Manifest`, the script uses `deploy\prod\build\files` and `deploy\prod\build\manifest.json`, which is the output of `prod-export` on the update PC. That folder must then hold only the current export.

### With the web interface

The interface does not attach the `.meta.json` files: they would become ordinary documents. So upload **only the `.md` files**, in **4 batches**, and type each batch's common metadata in the upload window:

| Batch | Files (name filter in Windows Explorer) | Batch metadata |
|---|---|---|
| 1 | `tsi-project-*.md` | `Topic` = `TSI-BDG`, `corpus` = `dg-reform-tsi`, `doc_type` = `project` |
| 2 | `tsi-programme-*.md` | `Topic` = `TSI-BDG`, `corpus` = `dg-reform-tsi`, `doc_type` = `programme` |
| 3 | `tsi-deliverable-*.md` | `Topic` = `TSI-BDG`, `corpus` = `dg-reform-tsi`, `doc_type` = `deliverable` |
| 4 | `tsi-csr-*.md` | `Topic` = `TSI-BDG`, `corpus` = `dg-reform-tsi`, `doc_type` = `csr` |

For each batch:
1. In Windows Explorer, type the filter (for example `tsi-project-*.md`) in the folder's search box.
2. Select everything (Ctrl+A) and drag it into the upload window.
3. Check that no `.meta.json` file is selected.

`Topic` and `corpus` are required: without them, the index router and filters discard the files. `doc_type` is used by the `search_projects`, `search_deliverables` and `search_csrs` tools.

Check in *Files*:
- the number of `tsi-*.md` files equals `files` in `manifest.json`, with the breakdown by type in `files_by_type`;
- the metadata shows on one file from each batch.

## 4. Check the index

In *Indexes > tsi-corpus*, the platform indexes the new files. When the status is *Indexed*, check:

- *Indexed files* = `files` in the manifest;
- *Documents in index* = `chunks` in the manifest, with the breakdown by type in `chunks_by_type`;
- *Failed files* = 0.

If the index does not exist yet, create it from `index-tsi-corpus.yaml`, with its filters (prefix `tsi-`, `corpus` = `dg-reform-tsi`), before clicking *Enable*. See step 3 of the [README](README.md#3-index).

## 5. Pipelines

Take the YAML files **at the release tag**, so that they match the data: `https://github.com/MGE-Agilos/dg-reform-tsi/tree/data-YYYY-MM-DD/deploy/prod`. In the cloned repository, you can instead run `git checkout data-YYYY-MM-DD`, then `git checkout main` when done.

- `tsi-chat`: `pipeline-tsi-chat.yaml` (SDK upload) or `pipeline-tsi-chat-ui.yaml` (web interface upload);
- `tsi-impact`: `pipeline-tsi-impact.yaml`.

For each pipeline:
1. Open the pipeline, switch to the YAML view and replace its whole content with the file.
2. Click *Save*.
3. Click **Publish > Publish through Gateway** on the existing service. The first time, use *Publish to new service* instead (see step 4 of the [README](README.md#4-pipelines)).

If the YAML files have not changed since the last deployment and you used the same upload method, you don't need to publish again: the new index is enough.

## 6. Acceptance tests

In each service's Playground, start a new conversation for each question:
1. Use the questions from the [README acceptance tests](README.md#5-acceptance-tests): chat, aggregation, CSR and impact analysis.
2. Compare the answers with the expected ones.
3. Note the duration and the tokens in *Analytics*.

After a web interface upload (`pipeline-tsi-chat-ui.yaml`), aggregation and linked-CSR questions return non-exhaustive lists. This is expected.

## Troubleshooting

- **Indexed files = 0, or far fewer than expected.** Metadata is missing on the batch (`Topic` = `TSI-BDG`, `corpus` = `dg-reform-tsi`). Delete those files and redo the batch with its metadata. Also check the index filters (prefix `tsi-` and `corpus`).
- **Failed files > 0.** Open the failed file in *Files* and upload it again.
- **`.meta.json` files show up as documents in *Files*.** They were uploaded through the web interface: delete them.
- **`upload.ps1` errors:**
  - "environment variable … is missing": set `API_KEY`, `API_URL` and `DEFAULT_WORKSPACE_NAME` in the same PowerShell window.
  - "haystack-enterprise is not recognized": activate the SDK environment.
- **"Model (custom) is not connected".** The pasted YAML is not the repository version. See the README section [To provide or check on the client side](README.md#to-provide-or-check-on-the-client-side).
