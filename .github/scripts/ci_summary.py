"""Markdown summary of the monthly data update (GitHub Actions job summary). Standard library only.

    python .github/scripts/ci_summary.py --runs-dir logs/runs --work-dir ci-work \
        --manifest deploy/prod/build/manifest.json --since 2026-11-01T03:00:00Z --mode incremental \
        --state-tag data-2026-10-01 --release-tag data-2026-11-01 --release-url https://... >> "$GITHUB_STEP_SUMMARY"

Inputs (all optional, a missing or unreadable file is reported, never fatal):
- `<runs-dir>/*.summary.json`: one per run of the agent (run, deliverables, csr-link, seed...);
- `<work-dir>/steps.tsv`: `step<TAB>exit code<TAB>duration in s<TAB>policy`, one line per step (written by
  run_step.sh; exit 1 is labelled as a quota only for the `quota` policy, 124 is the time budget);
- `<work-dir>/csr-build.out`, `<work-dir>/supabase-sync.out`: JSON printed by these commands;
- the manifest of prod-export.
"""

import argparse
import json
import sys
from datetime import datetime, timezone
from pathlib import Path


def _read_json(path: Path):
    """First JSON value of the file (commands may print text after it); None if missing or unreadable."""
    try:
        text = path.read_text(encoding="utf-8").lstrip()
        return json.JSONDecoder().raw_decode(text)[0]
    except (OSError, ValueError):
        return None


def _instant(value):
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(str(value).strip().replace("Z", "+00:00"))
    except ValueError:
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)


def _minutes(seconds) -> str:
    try:
        s = float(seconds)
    except (TypeError, ValueError):
        return "-"
    return f"{s:.0f} s" if s < 60 else f"{s / 60:.0f} min"


def _money(value) -> str:
    try:
        return f"${float(value):.2f}"
    except (TypeError, ValueError):
        return "-"


def load_runs(runs_dir: Path, since: str | None = None) -> list[dict]:
    """Run summaries started at or after `since`, oldest first."""
    start = _instant(since)
    runs = []
    for path in sorted(Path(runs_dir).glob("*.summary.json")) if Path(runs_dir).is_dir() else []:
        data = _read_json(path)
        if not isinstance(data, dict):
            continue
        started = _instant(data.get("started_at"))
        if start is not None and (started is None or started < start):
            continue
        runs.append(data)
    return sorted(runs, key=lambda r: (str(r.get("started_at")), str(r.get("run_id"))))


def step_of(run: dict) -> str:
    mode = run.get("mode")
    if mode == "seed_sitemap_lastmod":
        return "seed"
    if mode == "csr_link":
        return "csr-link"
    if mode:
        return str(mode)
    return "run" if "llm_pages" in run or "skipped_by_sitemap" in run else "other"


def _details(run: dict, step: str) -> tuple[str, str, str]:
    """(new or updated projects, pages skipped thanks to the sitemap, details)."""
    if step == "run":
        w = run.get("written") or {}
        parts = [f"{w.get('detail', 0)} detail", f"{w.get('thin', 0)} title-only",
                 f"{w.get('refreshed', 0)} refreshed", f"{w.get('delisted', 0)} delisted"]
        if w.get("write_failed"):
            parts.append(f"{w['write_failed']} write errors")
        if run.get("failed"):
            parts.append(f"{run['failed']} failed ({run.get('fetch_failed', 0)} fetch, "
                         f"{run.get('extract_failed', 0)} extraction)")
        if run.get("detail_without_sitemap_date"):
            parts.append(f"{run['detail_without_sitemap_date']} without sitemap date")
        if run.get("remaining_detail"):
            parts.append(f"{run['remaining_detail']} detail pages left")
        new = int(w.get("detail", 0) or 0) + int(w.get("thin", 0) or 0)
        return str(new), str(run.get("skipped_by_sitemap", 0)), ", ".join(parts)
    if step == "seed":
        return "-", "-", f"{run.get('seeded', 0)} of {run.get('detail_known', 0)} detail pages dated"
    if step == "deliverables":
        w = run.get("written") or {}
        parts = [f"{w.get('publications', 0)} publications", f"{w.get('projects_updated', 0)} projects updated",
                 f"{run.get('remaining_publications', 0)} remaining", f"{len(run.get('failed') or [])} failed"]
        return str(w.get("projects_updated", 0)), "-", ", ".join(parts)
    if step == "csr-link":
        failed = run.get("failed")
        n_failed = len(failed) if isinstance(failed, list) else int(failed or 0)
        return "-", "-", (f"{run.get('linked', 0)} newly linked, {run.get('cached', 0)} cached, "
                          f"{run.get('deferred', 0)} deferred, {n_failed} failed")
    return "-", "-", step


def read_steps(work_dir: Path) -> list[tuple[str, str, str, str]] | None:
    """(step, exit code, duration in s, policy) per line of steps.tsv; None if the file is missing."""
    try:
        lines = (Path(work_dir) / "steps.tsv").read_text(encoding="utf-8").splitlines()
    except OSError:
        return None
    return [tuple((line.split("\t") + ["", "", "", ""])[:4]) for line in lines if line.strip()]


def step_result(code: str, policy: str) -> str:
    """Label of a step result. Exit 1 is a quota only for the LLM steps (policy `quota`); 124 is the time budget."""
    if code == "0":
        return "ok"
    if code == "124" and policy in ("quota", "soft"):
        return "time budget reached, continued"
    if code == "1" and policy == "quota":
        return "quota (exit 1), continued"
    if policy == "soft":
        return f"failed (exit {code}), continued"
    return f"failed (exit {code})"


def _steps_table(steps: list[tuple[str, str, str, str]] | None) -> list[str]:
    if steps is None:
        return ["No step result recorded."]
    out = ["| Step | Result | Duration |", "|---|---|---|"]
    for name, code, duration, policy in steps:
        out.append(f"| {name} | {step_result(code, policy)} | {_minutes(duration)} |")
    return out


def render(runs_dir: Path, work_dir: Path, manifest: Path, since: str | None = None, mode: str | None = None,
           state_tag: str | None = None, release_tag: str | None = None, release_url: str | None = None,
           partial_artifact: str | None = None) -> str:
    lines = ["## TSI data update", ""]
    head = [f"Mode: **{mode}**"] if mode else []
    if state_tag:
        head.append(f"state restored from `{state_tag}`")
    if head:
        lines += [" · ".join(head), ""]

    steps = read_steps(work_dir)
    lines += ["### Steps", "", *_steps_table(steps), ""]
    if any(name == "supabase-sync" and code != "0" for name, code, _, _ in steps or []):
        lines += ["> **Supabase tables may be partial** (supabase-sync failed twice). Run the workflow again with "
                  "`publish_release=false`, or run `python -m tsi_agent supabase-sync` locally.", ""]
    if partial_artifact:
        lines += [f"> **The job did not finish.** The work done so far (state of `output/`) is kept in the artifact "
                  f"`{partial_artifact}` for 30 days: restore it as described in deploy/updater/README.md "
                  "(\"Automatic monthly update\", failed run).", ""]

    lines += ["### Runs of the agent", ""]
    runs = load_runs(runs_dir, since)
    if not runs:
        lines += ["No run summary found.", ""]
    else:
        lines += ["| Run | Step | Status | New / updated projects | Skipped (sitemap) | Details | LLM calls | Cost "
                  "| Duration |", "|---|---|---|---|---|---|---|---|---|"]
        total = 0.0
        for run in runs:
            step = step_of(run)
            new, skipped, details = _details(run, step)
            status = str(run.get("status", "-"))
            if run.get("quota_exceeded"):
                status += ", quota"
            lines.append(f"| {run.get('run_id', '-')} | {step} | {status} | {new} | {skipped} | {details} "
                         f"| {run.get('llm_calls', 0)} | {_money(run.get('cost_usd'))} "
                         f"| {_minutes(run.get('duration_s'))} |")
            try:
                total += float(run.get("cost_usd") or 0)
            except (TypeError, ValueError):
                pass
        lines += ["", f"**Total LLM cost: {_money(total)}**", ""]

    work = Path(work_dir)
    csr = _read_json(work / "csr-build.out")
    sync = _read_json(work / "supabase-sync.out")
    if isinstance(csr, dict) or isinstance(sync, dict):
        lines += ["### CSR and Supabase", ""]
        if isinstance(csr, dict):
            lines.append(f"- CSR files: {csr.get('csr_files', '-')} (from {csr.get('rows', '-')} rows of the ECFIN "
                         "export)")
        if isinstance(sync, dict):
            lines.append("- Supabase rows: " + ", ".join(f"{k} {v}" for k, v in sync.items()))
        lines.append("")

    lines += ["### Release", ""]
    data = _read_json(Path(manifest))
    if isinstance(data, dict):
        lines.append(f"- Production export: {data.get('files', '-')} files, {data.get('chunks', '-')} chunks expected")
        files, chunks = data.get("files_by_type") or {}, data.get("chunks_by_type") or {}
        for kind, n in files.items():
            lines.append(f"  - {kind} {n} files / {chunks.get(kind, '-')} chunks")
    else:
        lines.append("- No manifest (prod-export did not run).")
    if release_tag and release_url:
        lines.append(f"- Release: [{release_tag}]({release_url})")
    else:
        lines.append("- Release: not published.")
    return "\n".join(lines) + "\n"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--runs-dir", default="logs/runs")
    parser.add_argument("--work-dir", default="ci-work")
    parser.add_argument("--manifest", default="deploy/prod/build/manifest.json")
    parser.add_argument("--since", help="ISO time: ignore runs started before")
    parser.add_argument("--mode")
    parser.add_argument("--state-tag")
    parser.add_argument("--release-tag")
    parser.add_argument("--release-url")
    parser.add_argument("--partial-artifact", help="artifact holding the state of a failed or cancelled run")
    args = parser.parse_args(argv)
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    sys.stdout.write(render(Path(args.runs_dir), Path(args.work_dir), Path(args.manifest), since=args.since,
                            mode=args.mode, state_tag=args.state_tag, release_tag=args.release_tag,
                            release_url=args.release_url, partial_artifact=args.partial_artifact))
    return 0


if __name__ == "__main__":
    sys.exit(main())
