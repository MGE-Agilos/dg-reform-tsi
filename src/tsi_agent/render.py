"""Rendu des fichiers Markdown (front matter YAML + corps)."""

import re
from typing import Any

import yaml

from tsi_agent.countries import COUNTRIES, name_from_iso
from tsi_agent.ids import slugify
from tsi_agent.models import ProgrammeDoc, ProjectRecord, ProjectRef

SUMMARY_HEADING = "## Summary (AI-generated)"
DELIVERABLES_HEADING = "## Deliverables (AI-generated summary)"


def source_urls(ref: ProjectRef) -> list[str]:
    urls = ([ref.detail_url] if ref.detail_url else []) + ref.listing_urls
    return list(dict.fromkeys(urls))


def front_matter(ref: ProjectRef, *, content_hash: str | None, first_seen: str, last_updated: str,
                 run_id: str, model: str, listed: bool = True, results: str | None = None) -> dict[str, Any]:
    """`results` : "achieved" / "expected" selon le titre officiel de la section résultats (None si thin)."""
    return {
        "project_id": ref.project_id,
        "title": ref.title,
        "listing_titles": list(ref.listing_titles),
        "programme": ref.programme,
        "year": ref.year,
        "countries": list(ref.countries),
        "multi_country": ref.multi_country or len(ref.countries) > 1,
        "policy_area": ref.policy_area,
        "results": results,
        "spotlight": ref.spotlight,
        "highlighted": ref.highlighted,
        "detail_page": ref.detail_page,
        "listed": listed,
        "aliases": list(ref.aliases),
        "source_urls": source_urls(ref),
        "content_hash": content_hash,
        "first_seen": first_seen,
        "last_updated": last_updated,
        "extraction": {"run_id": run_id, "model": model},
    }


def render(front: dict[str, Any], body: str) -> str:
    fm = yaml.safe_dump(front, sort_keys=False, allow_unicode=True, width=1000)
    return f"---\n{fm}---\n\n{body.rstrip()}\n"


def parse_front_matter(text: str) -> tuple[dict[str, Any], str]:
    if not text.startswith("---\n"):
        raise ValueError("missing front matter")
    fm, sep, body = text[4:].partition("\n---\n")
    if not sep:
        raise ValueError("unterminated front matter")
    return yaml.safe_load(fm), body.lstrip("\n")


def _sources_section(ref: ProjectRef) -> str:
    return "## Sources\n\n" + "\n".join(f"- {u}" for u in source_urls(ref))


def _title_key(title: str) -> str:
    """Comparaison de titres : sans casse, ponctuation ni article initial (« The … »)."""
    return re.sub(r"^the-", "", slugify(title, max_len=1000))


def _listed_as_section(ref: ProjectRef) -> str | None:
    """Titres de listing réellement différents du titre retenu."""
    own = _title_key(ref.title)
    seen = {own}
    distinct: list[str] = []
    for title in ref.listing_titles:
        key = _title_key(title)
        if key in seen:
            continue
        seen.add(key)
        distinct.append(title)
    if not distinct:
        return None
    return "## Listed in country factsheets as\n\n" + "\n".join(f"- {t}" for t in distinct)


def listing_tail(ref: ProjectRef) -> str:
    """La partie du corps dérivée du listing : section "Listed in..." optionnelle + Sources."""
    parts = []
    listed_as = _listed_as_section(ref)
    if listed_as is not None:
        parts.append(listed_as)
    parts.append(_sources_section(ref))
    return "\n\n".join(parts)


_TAIL_HEADINGS = ("## Listed in country factsheets as", "## Sources")


def replace_listing_tail(body: str, ref: ProjectRef) -> str:
    """Remplace la section listing-dérivée existante (si présente) par celle de `ref`."""
    lines = body.split("\n")
    cut = next((i for i, line in enumerate(lines) if line in _TAIL_HEADINGS), None)
    if cut is None:
        head = body.rstrip("\n")
        return f"{head}\n\n{listing_tail(ref)}"
    head = "\n".join(lines[:cut]).rstrip("\n")
    return f"{head}\n\n{listing_tail(ref)}"


def _file_link(f: dict[str, Any]) -> str:
    """[Document (EN, 12 p.)](url) ; « DOCX » a la place des pages pour un document Word."""
    size = f"{f.get('pages')} p." if f.get("pages") else "DOCX"
    return f"[{f.get('document') or f.get('filename')} ({(f.get('language') or '?').upper()}, {size})]({f.get('url')})"


def deliverables_section(entries: list[Any]) -> str | None:
    """Section des livrables d'un projet (entrees DeliverableEntry, la plus recente d'abord) ; None si aucune."""
    if not entries:
        return None
    parts = [DELIVERABLES_HEADING]
    for e in entries:
        files = " · ".join(_file_link(f) for f in e.files)
        block = [f"### {e.title}" + (f" ({e.date})" if e.date else ""), f"Files: {files}",
                 f"Full text: {e.full_text_path}", e.content.summary.strip()]
        for label, items in (("Key outputs", e.content.key_outputs), ("Results", e.content.results),
                             ("Key figures", e.content.figures)):
            if items:
                block.append(f"{label}:\n" + "\n".join(f"- {item}" for item in items))
        parts.append("\n\n".join(block))
    return "\n\n".join(parts)


def replace_deliverables_section(body: str, section: str | None) -> str:
    """Retire la section livrables existante et insere `section` juste avant la partie listing (ou a la fin)."""
    lines = body.split("\n")
    if DELIVERABLES_HEADING in lines:
        start = lines.index(DELIVERABLES_HEADING)
        end = next((i for i in range(start + 1, len(lines)) if lines[i].startswith("## ")), len(lines))
        lines = lines[:start] + lines[end:]
    cut = next((i for i, line in enumerate(lines) if line in _TAIL_HEADINGS), len(lines))
    head = "\n".join(lines[:cut]).strip("\n")
    tail = "\n".join(lines[cut:]).strip("\n")
    return "\n\n".join(p for p in (head, section, tail) if p)


def with_deliverables_count(fm: dict[str, Any], count: int) -> dict[str, Any]:
    """`deliverables: <n>` place juste apres `results` (ordre fixe du front matter) ; cle retiree si n == 0."""
    out: dict[str, Any] = {}
    for key, value in fm.items():
        if key == "deliverables":
            continue
        out[key] = value
        if key == "results" and count:
            out["deliverables"] = count
    return out


def detail_body(ref: ProjectRef, rec: ProjectRecord) -> str:
    """Titre, accroche officielle, résumé généré (étiqueté), sections officielles verbatim, puis la partie listing.

    Sans section reconnue sur la page, le texte brut complet de la page remplace les sections.
    """
    parts = [f"# {ref.title}"]
    if rec.intro.strip():
        parts.append(rec.intro.strip())
    parts.append(f"{SUMMARY_HEADING}\n\n{rec.content.summary.strip()}")
    if rec.sections:
        parts.extend(f"## {heading}\n\n{body.strip()}".rstrip() for heading, body in rec.sections)
    else:
        parts.append(f"## Page text\n\n{rec.page_text.strip()}".rstrip())
    parts.append(listing_tail(ref))
    return "\n\n".join(parts)


def _country_names(ref: ProjectRef) -> str:
    names = [name_from_iso(c) for c in ref.countries if c in COUNTRIES]
    return ", ".join(names) if names else "several Member States"


def thin_sentence(ref: ProjectRef) -> str:
    if ref.programme == "SRSS":
        return (f"Pre-SRSP technical assistance project ({_country_names(ref)}, {ref.year}), listed in the annex of "
                f"the SRSP list of approved technical support requests (projects financed by a transfer of funds from "
                f"the European Structural and Investment Funds). No detail page is published.")
    if ref.programme == "SRSP":
        return (f"Project listed in the SRSP list of approved technical support requests "
                f"({_country_names(ref)}, {ref.year}). No detail page is published.")
    plural = "s" if len(ref.countries) > 1 else ""
    scope = "multi-country" if ref.multi_country or len(ref.countries) > 1 else "single-country"
    return (f"Project listed in the TSI country factsheet{plural} for {_country_names(ref)} "
            f"under TSI {ref.year} ({scope}). No detail page is published.")


def thin_body(ref: ProjectRef) -> str:
    parts = [f"# {ref.title}", thin_sentence(ref), listing_tail(ref)]
    return "\n\n".join(parts)


def programme_doc_text(doc: ProgrammeDoc, run_id: str, fetched: str) -> str:
    front = {"doc_id": doc.doc_id, "title": doc.title, "source_url": doc.url, "content_hash": doc.content_hash,
             "last_updated": fetched, "extraction": {"run_id": run_id, "model": "none"}}
    return render(front, f"# {doc.title}\n\n{doc.text}\n\n## Sources\n\n- {doc.url}")
