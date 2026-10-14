"""`llm-check` : refait l'extraction de quelques projets deja traites avec le fournisseur configure, sans reseau.

Les pages detail viennent du cache HTTP (`PoliteFetcher.cached`) ; l'extraction reprend `ProjectExtractor` (meme
prompt, meme validation `ProjectContent`). Le rapport `output/reports/llm_check.md` met cote a cote l'existant et le
nouveau resultat. Rien n'est ecrit dans `output/projects`.
"""

import random
from pathlib import Path
from typing import Any

from haystack.dataclasses import ChatMessage

from tsi_agent import runlog
from tsi_agent.chunking import CSR_LINKS
from tsi_agent.components.extractor import ProjectExtractor, _is_quota_error
from tsi_agent.config import Settings
from tsi_agent.http import shared_fetcher
from tsi_agent.ids import sha256_text
from tsi_agent.models import DetailPage, ProjectRef
from tsi_agent.parsers.detail import parse_detail
from tsi_agent.render import DELIVERABLES_HEADING, SUMMARY_HEADING, parse_front_matter

NAME = "LLMCheck"
REPORT = Path("reports") / "llm_check.md"
SEED = 20261014
_END_HEADINGS = (DELIVERABLES_HEADING, f"## {CSR_LINKS}", "## Listed in country factsheets as", "## Sources")


class LLMCheckError(RuntimeError):
    pass


def existing_view(text: str) -> dict[str, Any]:
    """Resume, nombre de sections officielles, resultats, pays et modele d'un fichier projet existant."""
    fm, body = parse_front_matter(text.replace("\r\n", "\n"))
    summary: list[str] = []
    sections = 0
    zone = "head"
    for line in body.split("\n"):
        if line.startswith("## "):
            if line.strip() in _END_HEADINGS:
                break
            if line.strip() == SUMMARY_HEADING:
                zone = "summary"
                continue
            zone = "sections"
            if line.strip() != "## Page text":
                sections += 1
            continue
        if zone == "summary":
            summary.append(line)
    return {"title": fm.get("title"), "summary": "\n".join(summary).strip(), "sections": sections,
            "results": fm.get("results"), "countries": list(fm.get("countries") or []),
            "model": (fm.get("extraction") or {}).get("model")}


def _detail_projects(output_dir: Path) -> list[dict[str, Any]]:
    projects = []
    for path in sorted((output_dir / "projects").glob("*.md")):
        try:
            text = path.read_text(encoding="utf-8")
            fm, _ = parse_front_matter(text.replace("\r\n", "\n"))
        except (OSError, ValueError):
            continue
        urls = fm.get("source_urls") or []
        if fm.get("detail_page") and urls:
            projects.append({"project_id": fm.get("project_id") or path.stem, "title": fm.get("title") or path.stem,
                             "url": urls[0], "path": path, "text": text})
    return projects


def select_projects(output_dir: Path, fetcher: Any, n: int, seed: int = SEED) -> list[dict[str, Any]]:
    """N projets avec page detail dont la page est dans le cache HTTP, dans un ordre fixe (graine)."""
    projects = _detail_projects(Path(output_dir))
    random.Random(seed).shuffle(projects)
    chosen = []
    for project in projects:
        if len(chosen) >= n:
            break
        html = fetcher.cached(project["url"])
        if html:
            chosen.append({**project, "html": html})
    return chosen


class _Recorder:
    """Garde les meta (modele, usage, cout) de chaque reponse du generator."""

    def __init__(self, generator: Any):
        self.generator = generator
        self.metas: list[dict[str, Any]] = []

    def run(self, messages: list[ChatMessage]) -> dict[str, Any]:
        result = self.generator.run(messages=messages)
        self.metas.append(dict(result["replies"][0].meta or {}))
        return result


def _page(project: dict[str, Any]) -> DetailPage:
    parsed = parse_detail(project["html"])
    ref = ProjectRef(project_id=project["project_id"], title=project["title"], detail_url=project["url"])
    return DetailPage(ref=ref, url=project["url"], title=parsed.title or project["title"], text=parsed.text,
                      content_hash=sha256_text(parsed.text), programme=parsed.programme, year=parsed.year,
                      intro=parsed.intro, sections=parsed.sections, results=parsed.results)


def _cell(value: Any) -> str:
    if isinstance(value, list):
        value = ", ".join(value) or "-"
    return "-" if value is None or value == "" else str(value).replace("|", "\\|")


def _words(text: str) -> int:
    return len(text.split())


def _section(item: dict[str, Any]) -> str:
    old = item["existing"]
    lines = [f"## {item['title']}", "", f"- Project: `{item['project_id']}`", f"- URL: {item['url']}"]
    if item.get("cost_usd") is not None:
        lines.append(f"- Cost: {item['cost_usd']:.4f} $")
    lines.append("")
    if item.get("error"):
        lines += [f"**New extraction failed:** {item['error']}", "", "### Existing summary", "",
                  old["summary"] or "-", ""]
        return "\n".join(lines)
    new = item["new"]
    lines += ["| Field | Existing | New |", "| --- | --- | --- |",
              f"| Model | {_cell(old['model'])} | {_cell(new['model'])} |",
              f"| Results | {_cell(old['results'])} | {_cell(new['results'])} |",
              f"| Sections | {old['sections']} | {new['sections']} |",
              f"| Countries | {_cell(old['countries'])} | {_cell(new['countries'])} |",
              f"| Summary length (words) | {_words(old['summary'])} | {_words(new['summary'])} |",
              "", "### Existing summary", "", old["summary"] or "-", "",
              "### New summary", "", new["summary"] or "-", ""]
    return "\n".join(lines)


def write_report(path: Path, settings: Settings, items: list[dict[str, Any]], cost: float | None,
                 quota: bool) -> None:
    provider = settings.llm_provider
    model = settings.openai_model if provider == "openai" else settings.model
    head = ["# LLM check: existing extraction vs configured provider", "",
            f"- Provider: `{provider}` (model `{model}`"
            + (f", base URL `{settings.openai_base_url}`" if provider == "openai" and settings.openai_base_url
               else "") + ")",
            f"- Projects: {len(items)} (pages read from the HTTP cache; nothing written to output/projects)",
            f"- Total cost: {cost:.4f} $" if cost is not None else "- Total cost: not available"]
    if quota:
        head.append("- **Stopped: quota or rate limit reached.** Retry later.")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(head) + "\n\n" + "\n".join(_section(i) for i in items), encoding="utf-8")


def run_check(settings: Settings, *, n: int = 3, generator: Any = None, fetcher: Any = None,
              seed: int = SEED) -> dict[str, Any]:
    """Compare l'extraction existante et celle du fournisseur configure pour N projets en cache ; ecrit le rapport."""
    output_dir = Path(settings.output_dir)
    fetcher = fetcher or shared_fetcher(settings.cache_dir, settings.min_interval_s, settings.user_agent)
    chosen = select_projects(output_dir, fetcher, n, seed)
    if not chosen:
        raise LLMCheckError(f"aucune page detail de {output_dir / 'projects'} dans le cache HTTP "
                            f"({settings.cache_dir}) : lancer d'abord `python -m tsi_agent run` ou restaurer le cache")
    if generator is None:
        from tsi_agent.pipeline import default_chat_generator  # meme fabrique et meme schema que `run`
        generator = default_chat_generator(settings)
    recorder = _Recorder(generator)
    extractor = ProjectExtractor(chat_generator=recorder, max_concurrency=1, pricing=settings.pricing)
    items: list[dict[str, Any]] = []
    quota = False
    total: float | None = None
    with runlog.open_run(settings.logs_dir) as run:
        for project in chosen:
            item = {"project_id": project["project_id"], "title": project["title"], "url": project["url"],
                    "existing": existing_view(project["text"]), "error": None, "cost_usd": None}
            start = len(recorder.metas)
            try:
                record = extractor._extract_one(_page(project))
                item["new"] = {"summary": record.content.summary.strip(), "sections": len(record.sections),
                               "results": record.results, "countries": record.content.countries,
                               "model": record.model}
            except Exception as exc:
                item["error"] = f"{type(exc).__name__}: {str(exc)[:300]}"
                quota = _is_quota_error(exc)
                runlog.event(NAME, "error", project["project_id"], "quota_exceeded" if quota else "extract_failed",
                             error=str(exc)[:300])
            costs = [extractor._cost(m) for m in recorder.metas[start:]]
            if any(c is not None for c in costs):
                item["cost_usd"] = sum(c or 0.0 for c in costs)
                total = (total or 0.0) + item["cost_usd"]
            items.append(item)
            if quota:
                break
        report = output_dir / REPORT
        write_report(report, settings, items, total, quota)
        run.extra.update({"mode": "llm_check", "projects": [i["project_id"] for i in items], "quota_exceeded": quota,
                          "report": str(report)})
    return {"report": str(report), "projects": [{"project_id": i["project_id"], "error": i["error"]} for i in items],
            "cost_usd": total, "quota_exceeded": quota}
