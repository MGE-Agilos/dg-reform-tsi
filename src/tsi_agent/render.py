"""Rendu des fichiers Markdown (front matter YAML + corps)."""

from typing import Any

import yaml

from tsi_agent.countries import COUNTRIES, name_from_iso
from tsi_agent.models import ProgrammeDoc, ProjectContent, ProjectRef

NOT_REPORTED = "Not reported on the source page."


def source_urls(ref: ProjectRef) -> list[str]:
    urls = ([ref.detail_url] if ref.detail_url else []) + ref.listing_urls
    return list(dict.fromkeys(urls))


def front_matter(ref: ProjectRef, *, content_hash: str | None, first_seen: str, last_updated: str,
                 run_id: str, model: str, listed: bool = True) -> dict[str, Any]:
    return {
        "project_id": ref.project_id,
        "title": ref.title,
        "programme": ref.programme,
        "year": ref.year,
        "countries": list(ref.countries),
        "multi_country": ref.multi_country or len(ref.countries) > 1,
        "policy_area": ref.policy_area,
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


def _section(title: str, value: str) -> str:
    return f"## {title}\n\n{value.strip() or NOT_REPORTED}"


def detail_body(ref: ProjectRef, content: ProjectContent) -> str:
    return "\n\n".join([
        f"# {ref.title}",
        _section("Summary", content.summary),
        _section("Context", content.context),
        _section("Support delivered", content.support_delivered),
        _section("Results achieved", content.results_achieved),
        _sources_section(ref),
    ])


def _country_names(ref: ProjectRef) -> str:
    names = [name_from_iso(c) for c in ref.countries if c in COUNTRIES]
    return ", ".join(names) if names else "several Member States"


def thin_sentence(ref: ProjectRef) -> str:
    if ref.programme == "SRSP":
        return (f"Project listed in the SRSP list of approved technical support requests "
                f"({_country_names(ref)}, {ref.year}). No detail page is published.")
    plural = "s" if len(ref.countries) > 1 else ""
    scope = "multi-country" if ref.multi_country or len(ref.countries) > 1 else "single-country"
    return (f"Project listed in the TSI country factsheet{plural} for {_country_names(ref)} "
            f"under TSI {ref.year} ({scope}). No detail page is published.")


def thin_body(ref: ProjectRef) -> str:
    return "\n\n".join([f"# {ref.title}", thin_sentence(ref), _sources_section(ref)])


def programme_doc_text(doc: ProgrammeDoc, run_id: str, fetched: str) -> str:
    front = {"doc_id": doc.doc_id, "title": doc.title, "source_url": doc.url, "content_hash": doc.content_hash,
             "last_updated": fetched, "extraction": {"run_id": run_id, "model": "none"}}
    return render(front, f"# {doc.title}\n\n{doc.text}\n\n## Sources\n\n- {doc.url}")
