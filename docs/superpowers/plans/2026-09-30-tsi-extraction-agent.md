# TSI Extraction Agent — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** A Haystack pipeline that crawls the public TSI / SRSP sources of the European Commission and writes one Markdown file per project (YAML front matter + structured sections), incrementally, with a JSONL action log and LLM cost tracking.

**Architecture:** One Haystack pipeline of custom components: `SourceListingFetcher → ProjectDiscoverer → ChangeDetector → DetailPageFetcher → ProjectExtractor → MarkdownProjectWriter`. Parsing is deterministic (BeautifulSoup, pdfplumber). The LLM only restructures detail pages, through an injected chat generator: `ClaudeCliChatGenerator` locally (the `claude` CLI on the user's Claude subscription), later `OpenAIChatGenerator` in deepset Studio. A runner re-runs the pipeline in batches so an interrupted run resumes. The run log travels in a contextvar so that components stay YAML-serialisable.

**Tech Stack:** Python 3.11+ (3.13 on the dev PC), haystack-ai 3.1, httpx, beautifulsoup4, pdfplumber, pydantic 2, PyYAML, pytest. Windows, PowerShell.

**Spec:** `docs/superpowers/specs/2026-09-30-tsi-extraction-agent-design.md`

**Provenance of the code below:** every file in this plan was written and run during design (66+1 tests green, real smoke run on Croatia with 2 LLM calls). Copy it verbatim; the expected test counts are the measured ones.

---

## Ground rules for the implementer

- Work from the repo root: `C:\Users\MGE\OneDrive - Agilos\Documents\Claude Code Projects\DG REFORM\Haystack Pipeline`. It is its own git repo (branch `main`, 1 commit with the spec). Do not touch the parent repo in `C:\Users\MGE`.
- The HTML/PDF fixtures in `tests/fixtures/` are already on disk (real pages saved on 2026-09-30). Commit them in Task 1; never edit them.
- All paths in commands are relative to the repo root. Use `.venv\Scripts\python` (the venv), never the global Python.
- Windows path limit: never create deeper folder structures than listed here.
- Commit messages: end them with the line `Co-Authored-By: Claude Opus 5.5 (1M context) <noreply@anthropic.com>` (add it as a second `-m` argument).
- Do **not** call the real `claude` CLI or the EC website in tests. Only Task 14 does real runs.

## File map

| File | Responsibility |
|---|---|
| `pyproject.toml`, `config/settings.yaml`, `config/sources.yaml` | Packaging, run settings, crawled URLs |
| `src/tsi_agent/countries.py` | 27 Member States: ISO code, factsheet slug, names used by the sources |
| `src/tsi_agent/ids.py` | Slugs, title keys (thin project ids), content hash |
| `src/tsi_agent/urls.py` | Which links are project pages; canonical vs to-be-resolved |
| `src/tsi_agent/models.py` | Pydantic objects passed between components |
| `src/tsi_agent/render.py` | Markdown + YAML front matter rendering / parsing |
| `src/tsi_agent/runlog.py` | JSONL run log in a contextvar, summary, `costs.csv` |
| `src/tsi_agent/http.py` | `PoliteFetcher`: throttle, disk cache (ETag), robots.txt, retry/429 backoff, memo |
| `src/tsi_agent/store.py` | `ProjectStore` protocol, `LocalProjectStore` (files + `state/index.json`) |
| `src/tsi_agent/parsers/*.py` | factsheet, spotlight, policy area, detail page, SRSP PDF |
| `src/tsi_agent/components/*.py` | The six pipeline components + `ClaudeCliChatGenerator` |
| `src/tsi_agent/config.py`, `pipeline.py`, `runner.py`, `__main__.py` | Settings, pipeline assembly / YAML, batch loop, CLI |
| `README.md` | Usage, scheduling, deepset / OpenAI migration |

---
### Task 1: Project scaffold and fixtures

Sets up packaging, config and the virtual environment, and commits the real fixtures.

**Files:**
- Create: `pyproject.toml`, `config/settings.yaml`, `config/sources.yaml`, `src/tsi_agent/__init__.py`, `src/tsi_agent/parsers/__init__.py` (placeholder, replaced in Task 7), `src/tsi_agent/components/__init__.py` (placeholder, replaced in Task 13), `tests/conftest.py`
- Commit: `tests/fixtures/*` (already present), `.gitignore` (already present, committed with the spec)

- [ ] **Step 1: Write `pyproject.toml`**

```toml
[build-system]
requires = ["setuptools>=68"]
build-backend = "setuptools.build_meta"

[project]
name = "tsi-agent"
version = "0.1.0"
description = "Agent d'extraction des projets TSI/SRSP (DG REFORM) - pipeline Haystack"
requires-python = ">=3.11"
dependencies = [
    "haystack-ai>=3.1",
    "httpx>=0.27",
    "beautifulsoup4>=4.12",
    "pdfplumber>=0.11",
    "pydantic>=2.7",
    "pyyaml>=6",
]

[project.optional-dependencies]
dev = ["pytest>=8"]

[tool.setuptools.packages.find]
where = ["src"]

[tool.pytest.ini_options]
testpaths = ["tests"]
pythonpath = ["src"]
```

- [ ] **Step 2: Write `config/settings.yaml`**

```yaml
# Réglages d'exécution locale. Chemins relatifs à la racine du dépôt.
output_dir: output
logs_dir: logs
cache_dir: .cache/http
user_agent: "TSI-Extraction-Agent/0.1 (DG REFORM project; polite crawler)"
min_interval_s: 2.0   # le site renvoie des 429 au-delà d'environ 1 req/s soutenue
model: sonnet
llm_timeout_s: 180
max_concurrency: 2
chunk_size: 25
```

- [ ] **Step 3: Write `config/sources.yaml`**

```yaml
# Sources publiques crawlées par l'agent d'extraction.
factsheet_url: "https://reforms-investments.ec.europa.eu/technical-support-instrument-0/our-projects/tsi-country-factsheets/{slug}_en"
spotlight_url: "https://reforms-investments.ec.europa.eu/technical-support-instrument-0/our-projects/projects-spotlight_en"
policy_area_url: "https://reforms-investments.ec.europa.eu/technical-support-instrument-0/{slug}_en"
policy_areas:
  - competitiveness
  - digital-transition
  - financial-sector-and-access-finance
  - green-transition
  - health-and-long-term-care
  - labour-market-and-social-protection
  - migration-management
  - public-administration-and-governance
  - revenue-administration-and-public-financial-management
  - skills-education-and-training
srsp_pdf_url: "https://commission.europa.eu/system/files/2020-03/rss-technical-support-per-country_en.pdf"
programme_docs:
  - id: tsi-performance
    title: Technical Support Instrument - programme performance
    url: "https://commission.europa.eu/strategy-and-policy/eu-budget/performance-and-reporting/programme-performance-statements/technical-support-instrument-performance_en"
```

- [ ] **Step 4: Write `src/tsi_agent/__init__.py`**

```python
"""Agent d'extraction des projets TSI / SRSP (DG REFORM)."""
```

- [ ] **Step 5: Write the two package placeholders**

`src/tsi_agent/parsers/__init__.py` and `src/tsi_agent/components/__init__.py`, each containing only:

```python
"""Placeholder, replaced later in the plan."""
```

- [ ] **Step 6: Write `tests/conftest.py`**

```python
from pathlib import Path

import pytest

FIXTURES = Path(__file__).parent / "fixtures"


def fixture_bytes(name: str) -> bytes:
    return (FIXTURES / name).read_bytes()


@pytest.fixture
def fixtures_dir() -> Path:
    return FIXTURES
```

- [ ] **Step 7: Create the venv and install**

```powershell
python -m venv .venv
.venv\Scripts\python -m pip install -e ".[dev]"
```
Expected: ends with `Successfully installed ... tsi-agent-0.1.0`.

- [ ] **Step 8: Check pytest runs**

Run: `.venv\Scripts\python -m pytest -q`
Expected: `no tests ran` (exit code 5). This is fine at this stage.

- [ ] **Step 9: Check the fixtures are there**

Run: `Get-ChildItem tests/fixtures | Select-Object Name, Length`
Expected: 8 files — `detail_hems_croatia.html`, `factsheet_croatia.html`, `factsheet_france.html`, `factsheets_index.html`, `policy_area_green_transition.html`, `spotlight.html`, `srsp_list.pdf` (≈873 KB), `tsi_performance.html`.

- [ ] **Step 10: Commit**

```powershell
git add pyproject.toml config src tests
git commit -m "chore: squelette du projet, config et fixtures reelles"
```


### Task 2: Countries, ids and URL rules

Pure functions everything else relies on: ISO codes, stable project ids, and the rules that decide which links are project pages.

**Files:**
- Create: `src/tsi_agent/countries.py`
- Create: `src/tsi_agent/ids.py`
- Create: `src/tsi_agent/urls.py`
- Test: `tests/test_ids_urls_countries.py`

- [ ] **Step 1: Write the failing test `tests/test_ids_urls_countries.py`**

```python
from tsi_agent.countries import iso_from_name, iso_from_slug, name_from_iso, slug_from_iso
from tsi_agent.ids import clean_text, sha256_text, slug_from_url, slugify, title_key
from tsi_agent.urls import (absolutize, is_canonical_detail, is_detail_candidate, is_final_detail,
                            policy_area_from_url)

DETAIL = ("https://reforms-investments.ec.europa.eu/technical-support-instrument-0/"
          "health-and-long-term-care/establishment-helicopter-emergency-medical-services-croatia_en")


def test_slugify_strips_accents_pace_prefix_and_punctuation():
    assert slugify("PACE - Réforme  de l'État (EVADE)") == "reforme-de-l-etat-evade"
    assert slugify("PACE – Energy") == "energy"
    assert len(slugify("x" * 200)) == 80


def test_title_key_and_slug_from_url():
    assert title_key("TSI", 2025, "Automation of routine tasks (ARTJP)") == "tsi-2025-automation-of-routine-tasks-artjp"
    assert slug_from_url(DETAIL) == "establishment-helicopter-emergency-medical-services-croatia"


def test_clean_text_and_hash():
    assert clean_text("  a\n  b\t c ") == "a b c"
    assert sha256_text("a").startswith("sha256:") and sha256_text("a") != sha256_text("b")


def test_countries_lookup():
    assert iso_from_slug("czechia") == "CZ"
    assert slug_from_iso("GR") == "greece"
    assert iso_from_name("Czech Republic") == "CZ"
    assert iso_from_name("The  Netherlands") == "NL"
    assert iso_from_name("Europe") is None
    assert name_from_iso("HR") == "Croatia"


def test_detail_url_rules():
    assert is_canonical_detail(DETAIL) and is_final_detail(DETAIL)
    assert policy_area_from_url(DETAIL) == "health-and-long-term-care"
    old = "https://reform-support.ec.europa.eu/what-we-do/health-and-long-term-care/healthier-online-gambling_en"
    assert is_detail_candidate(old) and not is_canonical_detail(old) and not is_final_detail(old)
    odd = absolutize("/technical-support-instrument-0/what-we-do/finance-and-access-finance/support-insolvency-croatia_en")
    assert is_detail_candidate(odd) and not is_canonical_detail(odd) and is_final_detail(odd)
    assert not is_detail_candidate(absolutize("/document/download/70c3_en?filename=TSI.pdf"))
    assert not is_detail_candidate(absolutize("/technical-support-instrument-0/green-transition_en"))
    assert not is_detail_candidate(absolutize("/technical-support-instrument-0/our-projects/tsi-country-factsheets/france_en"))
    assert not is_detail_candidate("https://commission.europa.eu/system/files/2021-02/tsi_2021_country_factsheet_france.pdf")
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `.venv\Scripts\python -m pytest tests/test_ids_urls_countries.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'tsi_agent.countries'`

- [ ] **Step 3: Write `src/tsi_agent/countries.py`**

```python
"""Référentiel des 27 États membres : code ISO, slug de factsheet, noms rencontrés dans les sources."""

COUNTRIES: dict[str, tuple[str, tuple[str, ...]]] = {
    "AT": ("austria", ("Austria",)),
    "BE": ("belgium", ("Belgium",)),
    "BG": ("bulgaria", ("Bulgaria",)),
    "HR": ("croatia", ("Croatia",)),
    "CY": ("cyprus", ("Cyprus",)),
    "CZ": ("czechia", ("Czechia", "Czech Republic")),
    "DK": ("denmark", ("Denmark",)),
    "EE": ("estonia", ("Estonia",)),
    "FI": ("finland", ("Finland",)),
    "FR": ("france", ("France",)),
    "DE": ("germany", ("Germany",)),
    "GR": ("greece", ("Greece",)),
    "HU": ("hungary", ("Hungary",)),
    "IE": ("ireland", ("Ireland",)),
    "IT": ("italy", ("Italy",)),
    "LV": ("latvia", ("Latvia",)),
    "LT": ("lithuania", ("Lithuania",)),
    "LU": ("luxembourg", ("Luxembourg",)),
    "MT": ("malta", ("Malta",)),
    "NL": ("netherlands", ("Netherlands", "The Netherlands")),
    "PL": ("poland", ("Poland",)),
    "PT": ("portugal", ("Portugal",)),
    "RO": ("romania", ("Romania",)),
    "SK": ("slovakia", ("Slovakia",)),
    "SI": ("slovenia", ("Slovenia",)),
    "ES": ("spain", ("Spain",)),
    "SE": ("sweden", ("Sweden",)),
}

_BY_SLUG = {slug: iso for iso, (slug, _) in COUNTRIES.items()}
_BY_NAME = {name.lower(): iso for iso, (_, names) in COUNTRIES.items() for name in names}


def iso_from_slug(slug: str) -> str | None:
    return _BY_SLUG.get(slug)


def slug_from_iso(iso: str) -> str:
    return COUNTRIES[iso][0]


def name_from_iso(iso: str) -> str:
    return COUNTRIES[iso][1][0]


def iso_from_name(name: str) -> str | None:
    return _BY_NAME.get(" ".join(name.split()).lower())
```

- [ ] **Step 4: Write `src/tsi_agent/ids.py`**

```python
"""Identifiants stables : slugs, clés titre, hash de contenu."""

import hashlib
import re
import unicodedata
from urllib.parse import urlsplit

_PACE_PREFIX = re.compile(r"^pace\s*[-–—]\s*")
_NON_ALNUM = re.compile(r"[^a-z0-9]+")


def clean_text(text: str) -> str:
    """Réduit tous les blancs (y compris sauts de ligne) à un espace simple."""
    return " ".join(text.split())


def slugify(text: str, max_len: int = 80) -> str:
    t = _PACE_PREFIX.sub("", clean_text(text).lower())
    t = unicodedata.normalize("NFKD", t).encode("ascii", "ignore").decode()
    t = _NON_ALNUM.sub("-", t).strip("-")
    return t[:max_len].rstrip("-")


def title_key(programme: str, year: int, title: str) -> str:
    """Clé de fusion par titre, et id d'un projet sans page détail."""
    return f"{programme.lower()}-{year}-{slugify(title)}"


def slug_from_url(url: str) -> str:
    last = urlsplit(url).path.rstrip("/").rsplit("/", 1)[-1]
    return last[:-3] if last.endswith("_en") else last


def sha256_text(text: str) -> str:
    return "sha256:" + hashlib.sha256(text.encode("utf-8")).hexdigest()
```

- [ ] **Step 5: Write `src/tsi_agent/urls.py`**

```python
"""Reconnaissance et canonisation des liens vers les pages détail projet."""

import re
from urllib.parse import urljoin, urlsplit

BASE_URL = "https://reforms-investments.ec.europa.eu"
_HOST = "reforms-investments.ec.europa.eu"
_CANDIDATE_HOSTS = {_HOST, "reform-support.ec.europa.eu", "ec.europa.eu"}
_CANONICAL = re.compile(
    r"^https://reforms-investments\.ec\.europa\.eu/technical-support-instrument-0/"
    r"(?!our-projects/|what-we-do/)([a-z0-9-]+)/[a-z0-9-]+_en$"
)
_SECTION_INDEX = re.compile(r"^/technical-support-instrument-0(/[a-z0-9-]+)?_en$")


def absolutize(href: str, base: str = BASE_URL) -> str:
    return urljoin(base + "/", href.strip())


def is_detail_candidate(url: str) -> bool:
    """Lien qui ressemble à une page projet (y compris ancien domaine), avant résolution."""
    parts = urlsplit(url)
    if parts.scheme not in ("http", "https") or parts.netloc not in _CANDIDATE_HOSTS:
        return False
    path = parts.path
    if parts.netloc == "ec.europa.eu" and not path.startswith("/reform-support/"):
        return False
    if not path.endswith("_en") or _SECTION_INDEX.match(path):
        return False
    return not any(bit in path for bit in ("/document/", "/our-projects", "/events/", "/news/", "/publications"))


def is_canonical_detail(url: str) -> bool:
    """URL déjà canonique : pas besoin de suivre les redirections."""
    return bool(_CANONICAL.match(url))


def is_final_detail(url: str) -> bool:
    """URL finale (après redirections) acceptée comme page détail."""
    return urlsplit(url).netloc == _HOST and is_detail_candidate(url)


def policy_area_from_url(url: str) -> str | None:
    m = _CANONICAL.match(url)
    return m.group(1) if m else None
```

- [ ] **Step 6: Run the tests to verify they pass**

Run: `.venv\Scripts\python -m pytest tests/test_ids_urls_countries.py -v`
Expected: 5 passed

- [ ] **Step 7: Commit**

```powershell
git add src/tsi_agent/countries.py src/tsi_agent/ids.py src/tsi_agent/urls.py tests/test_ids_urls_countries.py
git commit -m "feat: referentiel pays, identifiants et regles d'URL"
```


### Task 3: Models and Markdown rendering

The objects exchanged between components, and the exact file format (front matter order, sections, thin sentence).

**Files:**
- Create: `src/tsi_agent/models.py`
- Create: `src/tsi_agent/render.py`
- Test: `tests/test_render.py`

- [ ] **Step 1: Write the failing test `tests/test_render.py`**

```python
from tsi_agent.models import ProgrammeDoc, ProjectContent, ProjectRef
from tsi_agent.render import (NOT_REPORTED, detail_body, front_matter, parse_front_matter, programme_doc_text,
                              render, thin_body)

DETAIL = "https://reforms-investments.ec.europa.eu/technical-support-instrument-0/health-and-long-term-care/hems_en"


def detail_ref():
    return ProjectRef(project_id="hems", title="HEMS in Croatia", programme="TSI", year=2022, countries=["HR"],
                      detail_url=DETAIL, policy_area="health-and-long-term-care", spotlight=True,
                      listing_urls=["https://x/croatia_en", "https://x/spotlight_en"])


def test_front_matter_roundtrip_and_field_order():
    fm = front_matter(detail_ref(), content_hash="sha256:1", first_seen="2026-09-30", last_updated="2026-09-30",
                      run_id="r1", model="claude-cli/sonnet")
    text = render(fm, "# body")
    parsed, body = parse_front_matter(text)
    assert parsed == fm
    assert list(parsed)[:3] == ["project_id", "title", "programme"]
    assert parsed["source_urls"][0] == DETAIL and parsed["detail_page"] is True
    assert body == "# body\n"


def test_detail_body_fills_missing_sections():
    content = ProjectContent(summary="S.", context="C.", support_delivered="", results_achieved="R.")
    body = detail_body(detail_ref(), content)
    assert body.startswith("# HEMS in Croatia\n\n## Summary\n\nS.")
    assert f"## Support delivered\n\n{NOT_REPORTED}" in body
    assert body.endswith(f"## Sources\n\n- {DETAIL}\n- https://x/croatia_en\n- https://x/spotlight_en")


def test_thin_bodies():
    tsi = ProjectRef(project_id="tsi-2025-x", title="X", programme="TSI", year=2025, countries=["FR", "DE"],
                     listing_urls=["https://x/france_en"])
    assert ("Project listed in the TSI country factsheets for France, Germany under TSI 2025 (multi-country). "
            "No detail page is published.") in thin_body(tsi)
    srsp = ProjectRef(project_id="srsp-2019-y", title="Y", programme="SRSP", year=2019, countries=["AT"],
                      listing_urls=["https://x/srsp.pdf"])
    assert "SRSP list of approved technical support requests (Austria, 2019)" in thin_body(srsp)


def test_programme_doc_text():
    doc = ProgrammeDoc(doc_id="country-croatia", title="Croatia", url="https://x/croatia_en", text="138 projects")
    fm, body = parse_front_matter(programme_doc_text(doc, "r1", "2026-09-30"))
    assert fm["doc_id"] == "country-croatia" and fm["content_hash"] == doc.content_hash
    assert "138 projects" in body
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `.venv\Scripts\python -m pytest tests/test_render.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'tsi_agent.models'`

- [ ] **Step 3: Write `src/tsi_agent/models.py`**

```python
"""Objets échangés entre les composants du pipeline."""

import json
from typing import Literal

from pydantic import BaseModel, Field

from tsi_agent.ids import sha256_text

Programme = Literal["TSI", "SRSP"]
PageKind = Literal["factsheet", "spotlight", "policy_area", "srsp_pdf", "programme_doc"]


class RawPage(BaseModel):
    """Page source brute. `key` = slug pays, slug domaine ou id de document."""

    kind: PageKind
    key: str
    url: str
    content: bytes
    title: str | None = None


class ProjectRef(BaseModel):
    """Projet tel que vu dans les listings (avant extraction)."""

    project_id: str
    title: str
    programme: Programme | None = None
    year: int | None = None
    countries: list[str] = Field(default_factory=list)
    multi_country: bool = False
    detail_url: str | None = None
    policy_area: str | None = None
    spotlight: bool = False
    highlighted: bool = False
    listing_urls: list[str] = Field(default_factory=list)
    aliases: list[str] = Field(default_factory=list)

    @property
    def detail_page(self) -> bool:
        return self.detail_url is not None

    def listing_hash(self) -> str:
        """Hash des métadonnées de listing : change si pays, drapeaux ou sources changent."""
        payload = {
            "title": self.title,
            "programme": self.programme,
            "year": self.year,
            "countries": sorted(self.countries),
            "multi_country": self.multi_country,
            "detail_url": self.detail_url,
            "policy_area": self.policy_area,
            "spotlight": self.spotlight,
            "highlighted": self.highlighted,
            "listing_urls": sorted(self.listing_urls),
        }
        return sha256_text(json.dumps(payload, sort_keys=True))


class DetailPage(BaseModel):
    ref: ProjectRef
    url: str
    title: str
    text: str
    content_hash: str
    programme: Programme | None = None
    year: int | None = None


class ProjectContent(BaseModel):
    # Docstring en ASCII : il part dans le JSON schema passé en argument au CLI (via cmd.exe).
    """Structured content extracted from a project page."""

    summary: str
    context: str
    support_delivered: str
    results_achieved: str
    countries: list[str] = Field(default_factory=list)


class ProjectRecord(BaseModel):
    ref: ProjectRef
    content: ProjectContent
    content_hash: str
    model: str


class ProgrammeDoc(BaseModel):
    doc_id: str
    title: str
    url: str
    text: str

    @property
    def content_hash(self) -> str:
        return sha256_text(self.text)
```

- [ ] **Step 4: Write `src/tsi_agent/render.py`**

```python
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
    _, fm, body = text.split("---\n", 2)
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
```

- [ ] **Step 5: Run the tests to verify they pass**

Run: `.venv\Scripts\python -m pytest tests/test_render.py -v`
Expected: 4 passed

- [ ] **Step 6: Commit**

```powershell
git add src/tsi_agent/models.py src/tsi_agent/render.py tests/test_render.py
git commit -m "feat: modeles et rendu Markdown avec front matter"
```


### Task 4: Run log (traceability and costs)

One JSON event per line, a summary, and one `costs.csv` line per run. The current log lives in a contextvar, so components never take it as an init parameter (that keeps them serialisable). Threads must copy the context (see the third test).

**Files:**
- Create: `src/tsi_agent/runlog.py`
- Test: `tests/test_runlog.py`

- [ ] **Step 1: Write the failing test `tests/test_runlog.py`**

```python
import contextvars
import csv
import json
from concurrent.futures import ThreadPoolExecutor

from tsi_agent import runlog


def test_events_summary_and_costs(tmp_path):
    with runlog.open_run(tmp_path, run_id="r1") as run:
        runlog.event("C", "fetch", "u1")
        runlog.event("C", "llm_call", "p1", input_tokens=100, output_tokens=20, cost_usd=0.01)
        runlog.event("C", "llm_call", "p2", input_tokens=50, output_tokens=10, cache_read_tokens=5, cost_usd=0.02)
        runlog.event("C", "alert", "FR", "warning", message="0 projects")
    lines = [json.loads(l) for l in (tmp_path / "runs" / "r1.jsonl").read_text(encoding="utf-8").splitlines()]
    assert [l["action"] for l in lines] == ["fetch", "llm_call", "llm_call", "alert"]
    assert lines[0]["run_id"] == "r1" and lines[0]["target"] == "u1"
    summary = json.loads((tmp_path / "runs" / "r1.summary.json").read_text(encoding="utf-8"))
    assert summary["llm_calls"] == 2
    assert summary["tokens"] == {"input_tokens": 150, "output_tokens": 30, "cache_read_tokens": 5,
                                 "cache_creation_tokens": 0}
    assert summary["cost_usd"] == 0.03
    assert summary["alerts"] == ["0 projects"]
    assert run.final_summary == summary
    rows = list(csv.reader((tmp_path / "costs.csv").open(encoding="utf-8")))
    assert rows[0] == runlog.COSTS_HEADER and rows[1][0] == "r1" and rows[1][2] == "2"


def test_event_outside_run_does_not_fail():
    runlog.event("C", "fetch", "u")  # va au logging standard


def test_threads_need_copied_context(tmp_path):
    with runlog.open_run(tmp_path, run_id="r2") as run:
        with ThreadPoolExecutor(2) as pool:
            for i in range(4):
                pool.submit(contextvars.copy_context().run, runlog.event, "C", "write", f"p{i}")
        assert run.counts["write:ok"] == 4
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `.venv\Scripts\python -m pytest tests/test_runlog.py -v`
Expected: FAIL — `ImportError: cannot import name 'runlog'`

- [ ] **Step 3: Write `src/tsi_agent/runlog.py`**

```python
"""Journal de run : un événement JSON par ligne, résumé et suivi des coûts.

Le journal courant est porté par un contextvar pour que les composants Haystack
restent sérialisables (aucun objet journal dans leurs paramètres d'init).
"""

import contextvars
import csv
import json
import logging
import threading
import time
from collections import Counter
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterator

_logger = logging.getLogger("tsi_agent")
_current: contextvars.ContextVar["RunLog | None"] = contextvars.ContextVar("tsi_runlog", default=None)

LLM_TOKEN_FIELDS = ("input_tokens", "output_tokens", "cache_read_tokens", "cache_creation_tokens")
COSTS_HEADER = ["run_id", "started_at", "llm_calls", *LLM_TOKEN_FIELDS, "cost_usd", "duration_s"]


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds")


class RunLog:
    def __init__(self, logs_dir: str | Path, run_id: str | None = None):
        self.run_id = run_id or datetime.now().strftime("%Y%m%d-%H%M%S")
        self.logs_dir = Path(logs_dir)
        (self.logs_dir / "runs").mkdir(parents=True, exist_ok=True)
        self.path = self.logs_dir / "runs" / f"{self.run_id}.jsonl"
        self.started_at = datetime.now(timezone.utc).isoformat(timespec="seconds")
        self._t0 = time.monotonic()
        self.counts: Counter[str] = Counter()
        self.alerts: list[str] = []
        self.llm_calls = 0
        self.llm_tokens: Counter[str] = Counter()
        self.cost_usd = 0.0
        self.extra: dict[str, Any] = {}
        self.final_summary: dict[str, Any] | None = None
        self._lock = threading.Lock()
        self._fh = self.path.open("a", encoding="utf-8")

    def event(self, component: str, action: str, target: str | None = None, result: str = "ok", **fields: Any) -> None:
        record = {"ts": _now(), "run_id": self.run_id, "component": component, "action": action,
                  "target": target, "result": result, **fields}
        with self._lock:
            self._fh.write(json.dumps(record, ensure_ascii=False, default=str) + "\n")
            self._fh.flush()
            self.counts[f"{action}:{result}"] += 1
            if action == "llm_call":
                self.llm_calls += 1
                for name in LLM_TOKEN_FIELDS:
                    self.llm_tokens[name] += int(fields.get(name) or 0)
                self.cost_usd += float(fields.get("cost_usd") or 0.0)
            if action == "alert":
                self.alerts.append(str(fields.get("message") or target))

    def summary(self) -> dict[str, Any]:
        return {
            "run_id": self.run_id,
            "started_at": self.started_at,
            "duration_s": round(time.monotonic() - self._t0, 1),
            "counts": dict(sorted(self.counts.items())),
            "alerts": list(self.alerts),
            "llm_calls": self.llm_calls,
            "tokens": {name: self.llm_tokens[name] for name in LLM_TOKEN_FIELDS},
            "cost_usd": round(self.cost_usd, 4),
            **self.extra,
        }

    def close(self) -> dict[str, Any]:
        summary = self.summary()
        self._fh.close()
        (self.logs_dir / "runs" / f"{self.run_id}.summary.json").write_text(
            json.dumps(summary, indent=2, ensure_ascii=False), encoding="utf-8")
        costs = self.logs_dir / "costs.csv"
        new_file = not costs.exists()
        with costs.open("a", newline="", encoding="utf-8") as fh:
            writer = csv.writer(fh)
            if new_file:
                writer.writerow(COSTS_HEADER)
            writer.writerow([self.run_id, self.started_at, self.llm_calls,
                             *(summary["tokens"][n] for n in LLM_TOKEN_FIELDS),
                             summary["cost_usd"], summary["duration_s"]])
        return summary


@contextmanager
def open_run(logs_dir: str | Path, run_id: str | None = None) -> Iterator[RunLog]:
    run = RunLog(logs_dir, run_id)
    token = _current.set(run)
    try:
        yield run
    finally:
        _current.reset(token)
        run.final_summary = run.close()


def current() -> RunLog | None:
    return _current.get()


def event(component: str, action: str, target: str | None = None, result: str = "ok", **fields: Any) -> None:
    run = _current.get()
    if run is None:
        _logger.info("%s %s %s %s %s", component, action, target, result, fields)
        return
    run.event(component, action, target, result, **fields)
```

- [ ] **Step 4: Run the tests to verify they pass**

Run: `.venv\Scripts\python -m pytest tests/test_runlog.py -v`
Expected: 3 passed

- [ ] **Step 5: Commit**

```powershell
git add src/tsi_agent/runlog.py tests/test_runlog.py
git commit -m "feat: journal de run JSONL, resume et suivi des couts"
```


### Task 5: Polite HTTP client

Throttling (2 s by default: the EC site answers HTTP 429 above ~1 req/s), disk cache with ETag / Last-Modified, robots.txt, retries (5xx, 429 with `Retry-After` and a growing wait, no retry on 404), and an in-process memo so later batches do not re-fetch listings. `shared_fetcher` returns one instance per configuration so components share throttle and memo.

**Files:**
- Create: `src/tsi_agent/http.py`
- Test: `tests/test_http.py`

- [ ] **Step 1: Write the failing test `tests/test_http.py`**

```python
import httpx
import pytest

from tsi_agent.http import FetchError, PoliteFetcher

URL = "https://example.test/page_en"


def make(tmp_path, handler, **kw):
    return PoliteFetcher(tmp_path / "cache", min_interval_s=0, transport=httpx.MockTransport(handler),
                         sleep=lambda s: None, **kw)


def robots_ok(request):
    return httpx.Response(200, text="User-agent: *\nDisallow: /private/\n")


def test_fetch_caches_and_uses_etag(tmp_path):
    seen = []

    def handler(request):
        if request.url.path == "/robots.txt":
            return robots_ok(request)
        seen.append(request.headers.get("if-none-match"))
        if request.headers.get("if-none-match") == '"v1"':
            return httpx.Response(304)
        return httpx.Response(200, content=b"hello", headers={"ETag": '"v1"'})

    first = make(tmp_path, handler).get(URL)
    assert (first.content, first.from_cache) == (b"hello", False)
    second = make(tmp_path, handler).get(URL)  # nouveau processus : cache disque
    assert (second.content, second.from_cache) == (b"hello", True)
    assert seen == [None, '"v1"']


def test_memo_avoids_second_request(tmp_path):
    calls = []

    def handler(request):
        calls.append(request.url.path)
        return robots_ok(request) if request.url.path == "/robots.txt" else httpx.Response(200, content=b"x")

    f = make(tmp_path, handler)
    f.get(URL)
    f.get(URL)
    assert calls == ["/robots.txt", "/page_en"]


def test_retries_on_5xx_then_succeeds(tmp_path):
    attempts = []

    def handler(request):
        if request.url.path == "/robots.txt":
            return robots_ok(request)
        attempts.append(1)
        return httpx.Response(503) if len(attempts) < 3 else httpx.Response(200, content=b"ok")

    assert make(tmp_path, handler).get(URL).content == b"ok"
    assert len(attempts) == 3


def test_404_is_not_retried(tmp_path):
    attempts = []

    def handler(request):
        if request.url.path == "/robots.txt":
            return robots_ok(request)
        attempts.append(1)
        return httpx.Response(404)

    with pytest.raises(FetchError) as err:
        make(tmp_path, handler).get(URL)
    assert err.value.status == 404 and len(attempts) == 1


def test_robots_disallow(tmp_path):
    f = make(tmp_path, lambda r: robots_ok(r) if r.url.path == "/robots.txt" else httpx.Response(200))
    with pytest.raises(FetchError, match="robots"):
        f.get("https://example.test/private/x_en")


def test_redirect_final_url(tmp_path):
    def handler(request):
        if request.url.path == "/robots.txt":
            return robots_ok(request)
        if request.url.path == "/old_en":
            return httpx.Response(301, headers={"Location": "https://example.test/new_en"})
        return httpx.Response(200, content=b"new")

    result = make(tmp_path, handler).get("https://example.test/old_en")
    assert result.final_url == "https://example.test/new_en"


def test_429_waits_long_and_honours_retry_after(tmp_path):
    waits, attempts = [], []

    def handler(request):
        if request.url.path == "/robots.txt":
            return robots_ok(request)
        attempts.append(1)
        if len(attempts) == 1:
            return httpx.Response(429, headers={"Retry-After": "90"})
        if len(attempts) == 2:
            return httpx.Response(429)
        return httpx.Response(200, content=b"ok")

    f = PoliteFetcher(tmp_path / "cache", min_interval_s=0, transport=httpx.MockTransport(handler),
                      sleep=waits.append, rate_limit_backoff_s=30)
    assert f.get(URL).content == b"ok"
    assert waits == [90, 180]  # Retry-After, puis le délai double (jamais de retour en arrière)
```

- [ ] **Step 2: Extend `tests/conftest.py` with `FakeFetcher`** (used by later tasks)

Replace the whole file with:

```python
from pathlib import Path
from typing import Callable

import pytest

from tsi_agent.http import FetchError, FetchResult

FIXTURES = Path(__file__).parent / "fixtures"


def fixture_bytes(name: str) -> bytes:
    return (FIXTURES / name).read_bytes()


@pytest.fixture
def fixtures_dir() -> Path:
    return FIXTURES


class FakeFetcher:
    """Remplace PoliteFetcher : `pages` url -> contenu, `redirects` url -> url finale, `default` pour le reste."""

    def __init__(self, pages: dict[str, bytes] | None = None, redirects: dict[str, str] | None = None,
                 default: Callable[[str], bytes | None] | None = None):
        self.pages = pages or {}
        self.redirects = redirects or {}
        self.default = default
        self.calls: list[str] = []

    def get(self, url: str) -> FetchResult:
        self.calls.append(url)
        final = self.redirects.get(url, url)
        content = self.pages.get(final)
        if content is None and self.default is not None:
            content = self.default(final)
        if content is None:
            raise FetchError(url, "HTTP 404", 404)
        return FetchResult(url, final, 200, content, False)
```

- [ ] **Step 2b: Run the tests to verify they fail**

Run: `.venv\Scripts\python -m pytest tests/test_http.py -v`
Expected: FAIL — `ImportError while loading conftest` (`No module named 'tsi_agent.http'`)

- [ ] **Step 3: Write `src/tsi_agent/http.py`**

```python
"""Client HTTP poli : débit limité, cache disque ETag/Last-Modified, robots.txt, retry, mémo."""

import hashlib
import json
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Callable
from urllib.parse import urlsplit
from urllib.robotparser import RobotFileParser

import httpx

from tsi_agent import runlog

DEFAULT_USER_AGENT = "TSI-Extraction-Agent/0.1 (DG REFORM project; polite crawler)"
COMPONENT = "PoliteFetcher"


@dataclass
class FetchResult:
    url: str
    final_url: str
    status: int
    content: bytes
    from_cache: bool


class FetchError(Exception):
    def __init__(self, url: str, reason: str, status: int | None = None):
        super().__init__(f"{url}: {reason}")
        self.url = url
        self.reason = reason
        self.status = status


def _retry_after(resp: httpx.Response) -> float | None:
    value = resp.headers.get("retry-after", "")
    return float(value) if value.strip().isdigit() else None


class PoliteFetcher:
    def __init__(self, cache_dir: str | Path, min_interval_s: float = 1.0, user_agent: str = DEFAULT_USER_AGENT,
                 retries: int = 3, backoff_s: float = 2.0, rate_limit_backoff_s: float = 30.0, timeout_s: float = 30.0,
                 transport: httpx.BaseTransport | None = None, respect_robots: bool = True,
                 sleep: Callable[[float], None] = time.sleep):
        self.cache_dir = Path(cache_dir)
        self.cache_dir.mkdir(parents=True, exist_ok=True)
        self.min_interval_s = min_interval_s
        self.user_agent = user_agent
        self.retries = retries
        self.backoff_s = backoff_s
        self.rate_limit_backoff_s = rate_limit_backoff_s
        self.respect_robots = respect_robots
        self._sleep = sleep
        self._client = httpx.Client(headers={"User-Agent": user_agent}, follow_redirects=True,
                                    timeout=timeout_s, transport=transport)
        self._memo: dict[str, FetchResult] = {}
        self._robots: dict[str, RobotFileParser | None] = {}
        self._last_request = 0.0
        self._lock = threading.Lock()

    # --- API publique -------------------------------------------------------------------
    def get(self, url: str) -> FetchResult:
        if url in self._memo:
            runlog.event(COMPONENT, "cache_hit", url, "memo")
            return self._memo[url]
        if not self._allowed(url):
            runlog.event(COMPONENT, "fetch", url, "robots_disallowed")
            raise FetchError(url, "disallowed by robots.txt")
        meta, body = self._cache_read(url)
        headers: dict[str, str] = {}
        if meta and body is not None:
            if meta.get("etag"):
                headers["If-None-Match"] = meta["etag"]
            if meta.get("last_modified"):
                headers["If-Modified-Since"] = meta["last_modified"]
        last_error = "unknown error"
        wait_s = self.backoff_s
        for attempt in range(1, self.retries + 1):
            t0 = time.monotonic()
            try:
                resp = self._request(url, headers)
            except httpx.HTTPError as exc:
                resp, last_error = None, f"{type(exc).__name__}: {exc}"
            ms = int((time.monotonic() - t0) * 1000)
            if resp is not None:
                if resp.status_code == 304 and meta and body is not None:
                    result = FetchResult(url, meta["final_url"], 200, body, True)
                    runlog.event(COMPONENT, "fetch", url, "not_modified", duration_ms=ms)
                    self._memo[url] = result
                    return result
                if resp.status_code < 400:
                    result = FetchResult(url, str(resp.url), resp.status_code, resp.content, False)
                    self._cache_write(url, result, resp.headers)
                    runlog.event(COMPONENT, "fetch", url, "ok", duration_ms=ms, status=resp.status_code,
                                 final_url=result.final_url, bytes=len(resp.content))
                    self._memo[url] = result
                    return result
                if resp.status_code != 429 and resp.status_code < 500:
                    runlog.event(COMPONENT, "fetch", url, f"http_{resp.status_code}", duration_ms=ms)
                    raise FetchError(url, f"HTTP {resp.status_code}", resp.status_code)
                last_error = f"HTTP {resp.status_code}"
                if resp.status_code == 429:
                    wait_s = max(wait_s, self.rate_limit_backoff_s, _retry_after(resp) or 0)
            if attempt < self.retries:
                runlog.event(COMPONENT, "fetch", url, "retry", attempt=attempt, wait_s=wait_s, error=last_error)
                self._sleep(wait_s)
                wait_s *= 2
        runlog.event(COMPONENT, "fetch", url, "error", error=last_error, attempts=self.retries)
        raise FetchError(url, last_error)

    # --- internes -----------------------------------------------------------------------
    def _request(self, url: str, headers: dict[str, str]) -> httpx.Response:
        with self._lock:
            wait = self.min_interval_s - (time.monotonic() - self._last_request)
            if wait > 0:
                self._sleep(wait)
            self._last_request = time.monotonic()
        return self._client.get(url, headers=headers)

    def _allowed(self, url: str) -> bool:
        if not self.respect_robots:
            return True
        parts = urlsplit(url)
        root = f"{parts.scheme}://{parts.netloc}"
        if root not in self._robots:
            parser: RobotFileParser | None = None
            try:
                resp = self._request(root + "/robots.txt", {})
                if resp.status_code == 200:
                    parser = RobotFileParser()
                    parser.parse(resp.text.splitlines())
            except httpx.HTTPError:
                parser = None
            self._robots[root] = parser
        parser = self._robots[root]
        return True if parser is None else parser.can_fetch(self.user_agent, url)

    def _cache_paths(self, url: str) -> tuple[Path, Path]:
        key = hashlib.sha1(url.encode("utf-8")).hexdigest()
        return self.cache_dir / f"{key}.json", self.cache_dir / f"{key}.bin"

    def _cache_read(self, url: str) -> tuple[dict | None, bytes | None]:
        meta_path, body_path = self._cache_paths(url)
        if not meta_path.exists() or not body_path.exists():
            return None, None
        return json.loads(meta_path.read_text(encoding="utf-8")), body_path.read_bytes()

    def _cache_write(self, url: str, result: FetchResult, headers: httpx.Headers) -> None:
        meta_path, body_path = self._cache_paths(url)
        body_path.write_bytes(result.content)
        meta = {"url": url, "final_url": result.final_url, "etag": headers.get("etag"),
                "last_modified": headers.get("last-modified")}
        meta_path.write_text(json.dumps(meta), encoding="utf-8")


_shared: dict[tuple, PoliteFetcher] = {}


def shared_fetcher(cache_dir: str, min_interval_s: float, user_agent: str) -> PoliteFetcher:
    """Une instance par configuration dans le processus : mémo et débit partagés entre composants."""
    key = (str(cache_dir), min_interval_s, user_agent)
    if key not in _shared:
        _shared[key] = PoliteFetcher(cache_dir, min_interval_s=min_interval_s, user_agent=user_agent)
    return _shared[key]
```

- [ ] **Step 4: Run the tests to verify they pass**

Run: `.venv\Scripts\python -m pytest tests/test_http.py -v`
Expected: 7 passed

- [ ] **Step 5: Commit**

```powershell
git add src/tsi_agent/http.py tests/test_http.py tests/conftest.py
git commit -m "feat: client HTTP poli (cache, robots, retry, backoff 429)"
```


### Task 6: Project store

Files under `output/projects` and `output/programme`, state in `output/state/index.json` (atomic rewrite). `put(save=False)` + `flush()` let the writer batch thin projects. File names are capped at 100 characters because of the Windows 260-character path limit. `shared_store` gives the detector and the writer the same in-memory state across batches.

**Files:**
- Create: `src/tsi_agent/store.py`
- Test: `tests/test_store.py`

- [ ] **Step 1: Write the failing test `tests/test_store.py`**

```python
from tsi_agent.store import LocalProjectStore, StoreEntry


def entry(pid, **kw):
    return StoreEntry(project_id=pid, file=f"projects/{pid}.md", first_seen="2026-09-30", last_updated="2026-09-30", **kw)


def test_put_get_persist_and_remove(tmp_path):
    store = LocalProjectStore(tmp_path)
    store.put(entry("a", content_hash="sha256:1", countries=["FR"]))
    reloaded = LocalProjectStore(tmp_path)
    assert reloaded.get("a").content_hash == "sha256:1"
    assert list(reloaded.all()) == ["a"]
    reloaded.remove("a")
    assert LocalProjectStore(tmp_path).get("a") is None


def test_get_returns_a_copy(tmp_path):
    store = LocalProjectStore(tmp_path)
    store.put(entry("a"))
    e = store.get("a")
    e.listed = False
    assert store.get("a").listed is True


def test_project_and_programme_files(tmp_path):
    store = LocalProjectStore(tmp_path)
    assert store.write_project_file("a", "hello") == "projects/a.md"
    assert store.read_project_file("a") == "hello"
    store.delete_project_file("a")
    assert store.read_project_file("a") is None
    assert store.write_programme_doc("tsi-performance", "text", "sha256:x") == "programme/tsi-performance.md"
    assert LocalProjectStore(tmp_path).programme_hash("tsi-performance") == "sha256:x"


def test_long_ids_get_a_bounded_file_name(tmp_path):
    from tsi_agent.store import MAX_FILE_STEM, file_stem
    long_id = "x" * 150
    stem = file_stem(long_id)
    assert len(stem) == MAX_FILE_STEM and stem != file_stem("x" * 151)
    assert file_stem("short") == "short"
    store = LocalProjectStore(tmp_path)
    assert store.write_project_file(long_id, "t") == f"projects/{stem}.md"
    assert store.read_project_file(long_id) == "t"
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `.venv\Scripts\python -m pytest tests/test_store.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'tsi_agent.store'`

- [ ] **Step 3: Write `src/tsi_agent/store.py`**

```python
"""État persistant : quels projets sont connus, avec quels hash, et où sont leurs fichiers."""

import hashlib
import json
import os
from pathlib import Path
from typing import Protocol

from pydantic import BaseModel, Field


MAX_FILE_STEM = 100  # limite de chemin Windows (260) : le dossier du dépôt en consomme déjà ~110


def file_stem(project_id: str) -> str:
    """Nom de fichier sûr : l'id complet reste dans le front matter."""
    if len(project_id) <= MAX_FILE_STEM:
        return project_id
    digest = hashlib.sha1(project_id.encode("utf-8")).hexdigest()[:8]
    return f"{project_id[:MAX_FILE_STEM - 9].rstrip('-')}-{digest}"


class StoreEntry(BaseModel):
    project_id: str
    file: str
    programme: str | None = None
    detail_page: bool = False
    content_hash: str | None = None
    listing_hash: str | None = None
    source_urls: list[str] = Field(default_factory=list)
    countries: list[str] = Field(default_factory=list)
    first_seen: str
    last_updated: str
    listed: bool = True
    aliases: list[str] = Field(default_factory=list)


class ProjectStore(Protocol):
    def get(self, project_id: str) -> StoreEntry | None: ...
    def all(self) -> dict[str, StoreEntry]: ...
    def put(self, entry: StoreEntry, save: bool = True) -> None: ...
    def flush(self) -> None: ...
    def remove(self, project_id: str) -> None: ...
    def write_project_file(self, project_id: str, text: str) -> str: ...
    def read_project_file(self, project_id: str) -> str | None: ...
    def delete_project_file(self, project_id: str) -> None: ...
    def programme_hash(self, doc_id: str) -> str | None: ...
    def write_programme_doc(self, doc_id: str, text: str, content_hash: str) -> str: ...


class LocalProjectStore:
    """Fichiers dans `<output_dir>/projects` et `<output_dir>/programme`, index dans `<output_dir>/state/index.json`."""

    def __init__(self, output_dir: str | Path):
        self.root = Path(output_dir)
        self.projects_dir = self.root / "projects"
        self.programme_dir = self.root / "programme"
        self.index_path = self.root / "state" / "index.json"
        for d in (self.projects_dir, self.programme_dir, self.index_path.parent):
            d.mkdir(parents=True, exist_ok=True)
        self._projects: dict[str, StoreEntry] = {}
        self._programme: dict[str, str] = {}
        if self.index_path.exists():
            data = json.loads(self.index_path.read_text(encoding="utf-8"))
            self._projects = {k: StoreEntry.model_validate(v) for k, v in data.get("projects", {}).items()}
            self._programme = dict(data.get("programme_docs", {}))

    def get(self, project_id: str) -> StoreEntry | None:
        entry = self._projects.get(project_id)
        return entry.model_copy(deep=True) if entry else None

    def all(self) -> dict[str, StoreEntry]:
        return {k: v.model_copy(deep=True) for k, v in self._projects.items()}

    def put(self, entry: StoreEntry, save: bool = True) -> None:
        self._projects[entry.project_id] = entry.model_copy(deep=True)
        if save:
            self._save()

    def flush(self) -> None:
        self._save()

    def remove(self, project_id: str) -> None:
        if self._projects.pop(project_id, None) is not None:
            self._save()

    def write_project_file(self, project_id: str, text: str) -> str:
        path = self.projects_dir / f"{file_stem(project_id)}.md"
        path.write_text(text, encoding="utf-8")
        return path.relative_to(self.root).as_posix()

    def read_project_file(self, project_id: str) -> str | None:
        path = self.projects_dir / f"{file_stem(project_id)}.md"
        return path.read_text(encoding="utf-8") if path.exists() else None

    def delete_project_file(self, project_id: str) -> None:
        (self.projects_dir / f"{file_stem(project_id)}.md").unlink(missing_ok=True)

    def programme_hash(self, doc_id: str) -> str | None:
        return self._programme.get(doc_id)

    def write_programme_doc(self, doc_id: str, text: str, content_hash: str) -> str:
        path = self.programme_dir / f"{doc_id}.md"
        path.write_text(text, encoding="utf-8")
        self._programme[doc_id] = content_hash
        self._save()
        return path.relative_to(self.root).as_posix()

    def _save(self) -> None:
        data = {"projects": {k: v.model_dump() for k, v in sorted(self._projects.items())},
                "programme_docs": dict(sorted(self._programme.items()))}
        tmp = self.index_path.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(data, indent=1, ensure_ascii=False), encoding="utf-8")
        os.replace(tmp, self.index_path)


_shared: dict[str, LocalProjectStore] = {}


def shared_store(output_dir: str | Path) -> LocalProjectStore:
    """Une instance par dossier dans le processus : détecteur et writer voient le même état entre les lots."""
    key = str(Path(output_dir).resolve())
    if key not in _shared:
        _shared[key] = LocalProjectStore(output_dir)
    return _shared[key]
```

- [ ] **Step 4: Run the tests to verify they pass**

Run: `.venv\Scripts\python -m pytest tests/test_store.py -v`
Expected: 4 passed

- [ ] **Step 5: Commit**

```powershell
git add src/tsi_agent/store.py tests/test_store.py
git commit -m "feat: stockage local des projets et index d'etat"
```


### Task 7: Source parsers

Deterministic parsers, tested on the real fixtures. Key facts the tests pin down: France has 66 listed projects (37 with a detail page), the yearly counts announced by the site are sometimes off by one (informational only), spotlight countries come from the flag *file name* (the alt text is wrong for Slovenia), the performance page currently has no content, and the SRSP PDF yields 871 projects with wrapped titles rebuilt.

**Files:**
- Create: `src/tsi_agent/parsers/__init__.py`
- Create: `src/tsi_agent/parsers/factsheet.py`
- Create: `src/tsi_agent/parsers/spotlight.py`
- Create: `src/tsi_agent/parsers/policy_area.py`
- Create: `src/tsi_agent/parsers/detail.py`
- Create: `src/tsi_agent/parsers/srsp_pdf.py`
- Test: `tests/test_parsers.py`

- [ ] **Step 1: Write the failing test `tests/test_parsers.py`**

```python
from collections import Counter

from conftest import fixture_bytes
from tsi_agent.parsers import main_text
from tsi_agent.parsers.detail import parse_detail
from tsi_agent.parsers.factsheet import parse_factsheet
from tsi_agent.parsers.policy_area import parse_policy_area
from tsi_agent.parsers.spotlight import parse_spotlight
from tsi_agent.parsers.srsp_pdf import parse_srsp_pdf

HR_URL = "https://reforms-investments.ec.europa.eu/technical-support-instrument-0/our-projects/tsi-country-factsheets/croatia_en"


def test_factsheet_france_lists_every_year_with_single_and_multi_country():
    parsed = parse_factsheet(fixture_bytes("factsheet_france.html"), "https://x/france_en", "FR")
    listed = [r for r in parsed.refs if not r.highlighted]
    assert len(listed) == 66
    assert Counter(r.year for r in listed) == {2021: 4, 2022: 11, 2023: 13, 2024: 10, 2025: 19, 2026: 9}
    assert sum(r.detail_page for r in listed) == 37
    assert sum(r.multi_country for r in listed) == 38
    first = listed[0]
    assert first.title == "PACE - European Victim Architecture Digital Evaluation (EVADE)"
    assert first.project_id == "tsi-2026-european-victim-architecture-digital-evaluation-evade"
    assert (first.programme, first.countries, first.multi_country, first.detail_url) == ("TSI", ["FR"], False, None)
    assert parsed.announced[2026] == 10 and parsed.parsed_counts[2026] == 9


def test_factsheet_detail_refs_carry_their_thin_id_as_alias():
    parsed = parse_factsheet(fixture_bytes("factsheet_france.html"), "https://x/france_en", "FR")
    ref = next(r for r in parsed.refs if r.detail_url and r.detail_url.endswith("/energy-regulation-academy_en"))
    assert ref.project_id == "energy-regulation-academy"
    assert ref.aliases == ["tsi-2026-energy-regulation-academy"]
    assert ref.multi_country is True


def test_factsheet_croatia_highlights_and_programme_text():
    parsed = parse_factsheet(fixture_bytes("factsheet_croatia.html"), HR_URL, "HR")
    assert len([r for r in parsed.refs if not r.highlighted]) == 77
    highlighted = [r for r in parsed.refs if r.highlighted]
    assert [r.project_id for r in highlighted] == [
        "support-improve-poverty-diagnostic-and-social-policies-monitoring-croatia",
        "supporting-croatias-demographic-revitalisation-through-stronger-governance",
    ]
    assert all(r.year is not None and r.countries == ["HR"] for r in highlighted)
    assert "138 Reform projects" in parsed.programme_text
    assert "Highlighted projects:" in parsed.programme_text
    odd = [r for r in parsed.refs if r.detail_url and "/what-we-do/" in r.detail_url]
    assert len(odd) == 1


def test_spotlight_reads_country_from_flag_file_name():
    refs = parse_spotlight(fixture_bytes("spotlight.html"), "https://x/spotlight_en")
    assert len(refs) == 22
    assert all(r.spotlight for r in refs)
    hems = next(r for r in refs if r.project_id == "establishment-helicopter-emergency-medical-services-croatia")
    assert hems.countries == ["HR"]
    barnahus = next(r for r in refs if r.project_id == "barnahus-slovenia-supporting-children-victims-violence")
    assert barnahus.countries == ["SI"]  # le alt du drapeau dit "Poland", le fichier dit Slovenia
    assert any(r.multi_country and r.countries == [] for r in refs)


def test_policy_area_links_and_text():
    refs, text = parse_policy_area(fixture_bytes("policy_area_green_transition.html"), "https://x/green-transition_en")
    assert len(refs) == 14
    assert any(r.project_id == "improving-cooperation-between-business-and-science-lithuania" for r in refs)
    assert "Climate action and emissions reduction" in text


def test_detail_page():
    parsed = parse_detail(fixture_bytes("detail_hems_croatia.html"))
    assert parsed.title == "Establishment of Helicopter Emergency Medical Services in Croatia"
    assert (parsed.programme, parsed.year) == ("TSI", 2022)
    assert "Four HEMS bases were established" in parsed.text
    assert "Context" in parsed.text.splitlines()


def test_performance_page_is_currently_empty():
    text = main_text(fixture_bytes("tsi_performance.html"))
    assert "PPS_TSI" not in text
    assert len(text) < 300


def test_srsp_pdf():
    refs = parse_srsp_pdf(fixture_bytes("srsp_list.pdf"), "https://x/srsp.pdf")
    assert len(refs) == 871
    assert Counter(r.countries[0] for r in refs)["AT"] == 17
    assert all(r.programme == "SRSP" and r.detail_url is None for r in refs)
    titles = {r.title for r in refs}
    assert ("Development of the Austrian Open Source Intelligence (OSINT) capabilities to fight and prevent "
            "cybercrime, fraud and economic crime") in titles
    first = refs[0]
    assert (first.countries, first.year, first.title) == (["AT"], 2020, "National strategy for financial literacy")
    assert first.project_id == "srsp-2020-national-strategy-for-financial-literacy"
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `.venv\Scripts\python -m pytest tests/test_parsers.py -v`
Expected: FAIL — `ImportError: cannot import name 'main_text'` (the package is still the placeholder)

- [ ] **Step 3: Write `src/tsi_agent/parsers/__init__.py`**

```python
"""Parsers déterministes des sources (HTML et PDF)."""

from bs4 import BeautifulSoup, Tag


def soup_of(html: bytes | str) -> BeautifulSoup:
    return BeautifulSoup(html, "html.parser")


def main_of(soup: BeautifulSoup) -> Tag:
    return soup.find("main") or soup.body or soup


def main_text(html: bytes | str) -> str:
    """Texte du contenu principal, une ligne par bloc, sans les marqueurs techniques du CMS."""
    lines = []
    for line in main_of(soup_of(html)).get_text("\n", strip=True).splitlines():
        line = " ".join(line.split())
        if not line or line == "Page contents" or line.startswith("[notranslate]"):
            continue
        lines.append(line)
    return "\n".join(lines)
```

- [ ] **Step 4: Write `src/tsi_agent/parsers/factsheet.py`**

```python
"""Parser d'une TSI Country Factsheet (un pays)."""

import re
from dataclasses import dataclass, field

from tsi_agent.countries import name_from_iso
from tsi_agent.ids import clean_text, slug_from_url, title_key
from tsi_agent.models import ProjectRef
from tsi_agent.parsers import soup_of
from tsi_agent.urls import absolutize, is_detail_candidate

_YEAR = re.compile(r"^\d{4}$")
_YEAR_PREFIX = re.compile(r"^(\d{4})\s*[-–—]\s*(.+)$")


@dataclass
class FactsheetParse:
    refs: list[ProjectRef] = field(default_factory=list)
    announced: dict[int, int | None] = field(default_factory=dict)
    parsed_counts: dict[int, int] = field(default_factory=dict)
    programme_text: str = ""


def _detail_url(li) -> str | None:
    a = li.find("a", href=True)
    if a is None:
        return None
    url = absolutize(a["href"])
    return url if is_detail_candidate(url) else None


def parse_factsheet(html: bytes | str, url: str, country_iso: str) -> FactsheetParse:
    soup = soup_of(html)
    out = FactsheetParse()

    for item in soup.select("details.ecl-accordion__item"):
        summary = item.find("summary")
        year_text = clean_text(summary.get_text()) if summary else ""
        if not _YEAR.match(year_text):
            continue
        year = int(year_text)
        content = item.select_one(".ecl-accordion__content") or item
        strong = content.find("strong")
        strong_text = clean_text(strong.get_text()) if strong else ""
        out.announced[year] = int(strong_text) if strong_text.isdigit() else None
        multi = False
        count = 0
        for el in content.find_all(["h3", "h4", "h5", "p", "li"]):
            text = clean_text(el.get_text(" "))
            if el.name != "li":
                if text.lower().startswith("multi-country"):
                    multi = True
                continue
            if not text:
                continue
            count += 1
            thin_id = title_key("TSI", year, text)
            detail = _detail_url(el)
            out.refs.append(ProjectRef(
                project_id=slug_from_url(detail) if detail else thin_id,
                title=text, programme="TSI", year=year, countries=[country_iso], multi_country=multi,
                detail_url=detail, listing_urls=[url], aliases=[thin_id] if detail else [],
            ))
        out.parsed_counts[year] = count

    highlights = []
    for card in soup.select("article.ecl-card"):
        a = card.find("a", href=True)
        if a is None:
            continue
        detail = absolutize(a["href"])
        if not is_detail_candidate(detail):
            continue
        raw_title = clean_text(a.get_text(" "))
        m = _YEAR_PREFIX.match(raw_title)
        year, title = (int(m.group(1)), m.group(2)) if m else (None, raw_title)
        desc = card.select_one(".ecl-content-block__description")
        highlights.append(f"- {raw_title}: {clean_text(desc.get_text(' ')) if desc else ''}".rstrip(": "))
        out.refs.append(ProjectRef(
            project_id=slug_from_url(detail), title=title, year=year, countries=[country_iso],
            detail_url=detail, highlighted=True, listing_urls=[url],
        ))

    facts = [clean_text(f.get_text(" ")) for f in soup.select(".ecl-fact-figures__item")]
    name = name_from_iso(country_iso)
    parts = [f"TSI country factsheet - {name}", "", "Completed and ongoing reform projects:"]
    parts += [f"- {f}" for f in facts]
    if highlights:
        parts += ["", "Highlighted projects:", *highlights]
    out.programme_text = "\n".join(parts)
    return out
```

- [ ] **Step 5: Write `src/tsi_agent/parsers/spotlight.py`**

```python
"""Parser de la page « Projects in the spotlight »."""

import re

from tsi_agent.countries import iso_from_name
from tsi_agent.ids import clean_text, slug_from_url
from tsi_agent.models import ProjectRef
from tsi_agent.parsers import main_of, soup_of
from tsi_agent.urls import absolutize, is_detail_candidate

# Le texte alternatif des drapeaux est parfois faux ; le nom de fichier est fiable.
_FLAG = re.compile(r"/([A-Za-z]+)_flag_photo")


def parse_spotlight(html: bytes | str, url: str) -> list[ProjectRef]:
    refs = []
    for card in main_of(soup_of(html)).select("article.ecl-content-item"):
        a = card.find("a", href=True)
        if a is None:
            continue
        detail = absolutize(a["href"])
        if not is_detail_candidate(detail):
            continue
        countries, multi = [], False
        img = card.find("img", src=True)
        m = _FLAG.search(img["src"]) if img else None
        if m and m.group(1) == "Europe":
            multi = True
        elif m and iso_from_name(m.group(1)):
            countries = [iso_from_name(m.group(1))]
        refs.append(ProjectRef(
            project_id=slug_from_url(detail), title=clean_text(a.get_text(" ")), countries=countries,
            multi_country=multi, detail_url=detail, spotlight=True, listing_urls=[url],
        ))
    return refs
```

- [ ] **Step 6: Write `src/tsi_agent/parsers/policy_area.py`**

```python
"""Parser d'une page de domaine politique : projets clés liés + texte descriptif."""

from tsi_agent.ids import clean_text, slug_from_url
from tsi_agent.models import ProjectRef
from tsi_agent.parsers import main_of, main_text, soup_of
from tsi_agent.urls import absolutize, is_canonical_detail


def parse_policy_area(html: bytes | str, url: str) -> tuple[list[ProjectRef], str]:
    titles: dict[str, str] = {}
    for a in main_of(soup_of(html)).find_all("a", href=True):
        detail = absolutize(a["href"])
        if not is_canonical_detail(detail):
            continue
        title = clean_text(a.get_text(" "))
        if len(title) > len(titles.get(detail, "")):
            titles[detail] = title
    refs = [
        ProjectRef(project_id=slug_from_url(d), title=t or slug_from_url(d), detail_url=d, listing_urls=[url])
        for d, t in titles.items()
    ]
    return refs, main_text(html)
```

- [ ] **Step 7: Write `src/tsi_agent/parsers/detail.py`**

```python
"""Parser d'une page détail projet."""

import re
from dataclasses import dataclass

from tsi_agent.ids import clean_text
from tsi_agent.parsers import main_of, main_text, soup_of


@dataclass
class DetailParse:
    title: str
    text: str
    programme: str | None
    year: int | None


def _programme(value: str) -> str | None:
    if "Technical Support Instrument" in value or "(TSI)" in value:
        return "TSI"
    if "Structural Reform Support" in value or "SRSP" in value:
        return "SRSP"
    return None


def parse_detail(html: bytes | str) -> DetailParse:
    soup = soup_of(html)
    h1 = soup.find("h1")
    meta: dict[str, str] = {}
    for dt in main_of(soup).select("dl.ecl-description-list dt"):
        dd = dt.find_next_sibling("dd")
        if dd is not None:
            meta[clean_text(dt.get_text(" "))] = clean_text(dd.get_text(" "))
    year_match = re.search(r"\d{4}", meta.get("Year", ""))
    return DetailParse(
        title=clean_text(h1.get_text(" ")) if h1 else "",
        text=main_text(html),
        programme=_programme(meta.get("Funding Programme", "")),
        year=int(year_match.group(0)) if year_match else None,
    )
```

- [ ] **Step 8: Write `src/tsi_agent/parsers/srsp_pdf.py`**

```python
"""Parser du PDF « List of approved technical support requests under the SRSP »."""

import io
import re

import pdfplumber

from tsi_agent.countries import iso_from_name
from tsi_agent.ids import clean_text, title_key
from tsi_agent.models import ProjectRef

_FUNDING = re.compile(r"^(?:SRSP)?(\d{4})$")
_HEADER_CELLS = {"Funding", "source", "Title of the request", "Title of the project"}


def _country_headings(page) -> list[tuple[float, str]]:
    """Titres de pays en gras, avec leur position verticale."""
    lines: dict[int, list[str]] = {}
    for word in page.extract_words(extra_attrs=["fontname"]):
        if "Bold" in word["fontname"]:
            lines.setdefault(round(word["top"]), []).append(word["text"])
    found = []
    for top, words in sorted(lines.items()):
        iso = iso_from_name(" ".join(words))
        if iso:
            found.append((top, iso))
    return found


def _is_continuation(row: list[str | None]) -> bool:
    """Suite d'un titre sur plusieurs lignes : une seule cellule remplie, toutes les autres à None."""
    filled = [c for c in row if c]
    return row[0] is None and len(filled) == 1 and sum(c is None for c in row) == len(row) - 1


def parse_srsp_rows(pdf_bytes: bytes) -> list[tuple[str, int, str]]:
    """Retourne des tuples (pays ISO, année, titre) dans l'ordre du document."""
    rows: list[tuple[str, int, str]] = []
    country: str | None = None
    year: int | None = None
    with pdfplumber.open(io.BytesIO(pdf_bytes)) as pdf:
        for page in pdf.pages:
            headings = _country_headings(page)
            for table in sorted(page.find_tables(), key=lambda t: t.bbox[1]):
                above = [iso for top, iso in headings if top < table.bbox[1]]
                if above and above[-1] != country:
                    country, year = above[-1], None
                for row in table.extract():
                    if _is_continuation(row) and rows:
                        c, y, t = rows[-1]
                        rows[-1] = (c, y, clean_text(t + " " + next(x for x in row if x)))
                        continue
                    cells = [clean_text(c) for c in row if c and clean_text(c)]
                    if not cells or any(c in _HEADER_CELLS for c in cells):
                        continue
                    m = _FUNDING.match(cells[0])
                    rest = cells[1:] if m else cells
                    if m:
                        year = int(m.group(1))
                    if rest and country and year:
                        rows.append((country, year, clean_text(" ".join(rest))))
    return rows


def parse_srsp_pdf(pdf_bytes: bytes, url: str) -> list[ProjectRef]:
    return [
        ProjectRef(project_id=title_key("SRSP", year, title), title=title, programme="SRSP", year=year,
                   countries=[iso], listing_urls=[url])
        for iso, year, title in parse_srsp_rows(pdf_bytes)
    ]
```

- [ ] **Step 9: Run the tests to verify they pass**

Run: `.venv\Scripts\python -m pytest tests/test_parsers.py -v`
Expected: 8 passed (the SRSP test takes a few seconds)

- [ ] **Step 10: Commit**

```powershell
git add src/tsi_agent/parsers/__init__.py src/tsi_agent/parsers/factsheet.py src/tsi_agent/parsers/spotlight.py src/tsi_agent/parsers/policy_area.py src/tsi_agent/parsers/detail.py src/tsi_agent/parsers/srsp_pdf.py tests/test_parsers.py
git commit -m "feat: parsers factsheets, spotlight, domaines, pages detail et PDF SRSP"
```


### Task 8: ClaudeCliChatGenerator

A Haystack chat generator that shells out to `claude -p` on the user's subscription. Same interface as `OpenAIChatGenerator`; usage is reported in OpenAI format (`prompt_tokens`, `completion_tokens`). The prompt goes through stdin; the system prompt is collapsed to one line because `claude.CMD` goes through `cmd.exe`. `--bare` is not usable (it forces an API key). Quota errors raise `LLMQuotaExceeded` so the run can stop cleanly. Tests mock `subprocess.run` — never call the real CLI here.

**Files:**
- Create: `src/tsi_agent/components/claude_cli.py`
- Test: `tests/test_claude_cli.py`

- [ ] **Step 1: Write the failing test `tests/test_claude_cli.py`**

```python
import json
import subprocess

import pytest
from haystack.dataclasses import ChatMessage

from tsi_agent.components import claude_cli
from tsi_agent.components.claude_cli import ClaudeCliChatGenerator, LLMCallError, LLMQuotaExceeded


def cli_output(**overrides):
    data = {"type": "result", "is_error": False, "api_error_status": None, "result": "plain text",
            "total_cost_usd": 0.0068, "duration_ms": 3500, "num_turns": 2, "session_id": "s1",
            "usage": {"input_tokens": 4272, "output_tokens": 427, "cache_read_input_tokens": 10,
                      "cache_creation_input_tokens": 0},
            "modelUsage": {"claude-haiku-4-5-20251001": {}}}
    data.update(overrides)
    return json.dumps(data)


@pytest.fixture
def fake_run(monkeypatch):
    calls = {}

    def install(stdout="", stderr="", returncode=0, exc=None):
        def run(cmd, **kw):
            calls["cmd"], calls["input"] = cmd, kw["input"]
            if exc:
                raise exc
            return subprocess.CompletedProcess(cmd, returncode, stdout=stdout, stderr=stderr)
        monkeypatch.setattr(claude_cli.subprocess, "run", run)
        monkeypatch.setattr(claude_cli.shutil, "which", lambda exe: "C:/npm/claude.CMD")
        return calls
    return install


MESSAGES = [ChatMessage.from_system("Line one.\nLine two."), ChatMessage.from_user("Hello\nworld")]


def test_builds_isolated_command_and_sends_prompt_on_stdin(fake_run):
    calls = fake_run(stdout=cli_output())
    ClaudeCliChatGenerator(model="haiku", json_schema={"type": "object"}).run(messages=MESSAGES)
    cmd = calls["cmd"]
    assert cmd[:4] == ["C:/npm/claude.CMD", "-p", "--output-format", "json"]
    for flag in ("--strict-mcp-config", "--no-session-persistence", "--setting-sources", "--tools"):
        assert flag in cmd
    assert cmd[cmd.index("--system-prompt") + 1] == "Line one. Line two."
    assert cmd[cmd.index("--json-schema") + 1] == '{"type":"object"}'
    assert calls["input"] == "Hello\nworld"


def test_reply_text_and_openai_style_usage(fake_run):
    fake_run(stdout=cli_output(structured_output={"summary": "S"}))
    reply = ClaudeCliChatGenerator().run(messages=MESSAGES)["replies"][0]
    assert json.loads(reply.text) == {"summary": "S"}
    assert reply.meta["model"] == "claude-cli/claude-haiku-4-5-20251001"
    assert reply.meta["usage"] == {"prompt_tokens": 4272, "completion_tokens": 427,
                                   "cache_read_input_tokens": 10, "cache_creation_input_tokens": 0}
    assert reply.meta["cost_usd"] == 0.0068


def test_plain_result_when_no_schema(fake_run):
    fake_run(stdout=cli_output())
    assert ClaudeCliChatGenerator().run(messages=MESSAGES)["replies"][0].text == "plain text"


def test_quota_error(fake_run):
    fake_run(stdout=cli_output(is_error=True, result="Claude AI usage limit reached|1759230000"), returncode=1)
    with pytest.raises(LLMQuotaExceeded):
        ClaudeCliChatGenerator().run(messages=MESSAGES)


def test_429_status_is_quota(fake_run):
    fake_run(stdout=cli_output(is_error=True, api_error_status=429, result="API Error"), returncode=1)
    with pytest.raises(LLMQuotaExceeded):
        ClaudeCliChatGenerator().run(messages=MESSAGES)


def test_other_error_and_non_json_output(fake_run):
    fake_run(stdout=cli_output(is_error=True, result="Invalid model"), returncode=1)
    with pytest.raises(LLMCallError) as err:
        ClaudeCliChatGenerator().run(messages=MESSAGES)
    assert not isinstance(err.value, LLMQuotaExceeded)
    fake_run(stdout="", stderr="boom", returncode=2)
    with pytest.raises(LLMCallError, match="boom"):
        ClaudeCliChatGenerator().run(messages=MESSAGES)


def test_timeout(fake_run):
    fake_run(exc=subprocess.TimeoutExpired("claude", 1))
    with pytest.raises(LLMCallError, match="timeout"):
        ClaudeCliChatGenerator(timeout_s=1).run(messages=MESSAGES)


def test_missing_executable(monkeypatch):
    monkeypatch.setattr(claude_cli.shutil, "which", lambda exe: None)
    with pytest.raises(LLMCallError, match="introuvable"):
        ClaudeCliChatGenerator().run(messages=MESSAGES)
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `.venv\Scripts\python -m pytest tests/test_claude_cli.py -v`
Expected: FAIL — `ImportError: cannot import name 'claude_cli'`

- [ ] **Step 3: Write `src/tsi_agent/components/claude_cli.py`**

```python
"""Chat generator Haystack qui passe par le CLI `claude` (souscription Claude, sans clé API).

Même interface que les chat generators Haystack (`OpenAIChatGenerator`...) :
`run(messages) -> {"replies": [ChatMessage]}`, avec l'usage au format OpenAI dans `meta["usage"]`.
"""

import json
import re
import shutil
import subprocess
from typing import Any

from haystack import component
from haystack.dataclasses import ChatMessage, ChatRole

DEFAULT_SYSTEM_PROMPT = "You are a precise assistant. Follow the user's instructions exactly."
_QUOTA = re.compile(r"rate.?limit|usage limit|limit reached|quota|too many requests|\b429\b", re.IGNORECASE)


class LLMCallError(RuntimeError):
    pass


class LLMQuotaExceeded(LLMCallError):
    pass


@component
class ClaudeCliChatGenerator:
    def __init__(self, model: str = "sonnet", timeout_s: int = 180, json_schema: dict[str, Any] | None = None,
                 executable: str = "claude"):
        self.model = model
        self.timeout_s = timeout_s
        self.json_schema = json_schema
        self.executable = executable

    def _command(self, system_prompt: str) -> list[str]:
        exe = shutil.which(self.executable)
        if exe is None:
            raise LLMCallError(f"'{self.executable}' introuvable dans le PATH")
        cmd = [exe, "-p", "--output-format", "json", "--model", self.model,
               "--tools", "", "--strict-mcp-config", "--mcp-config", '{"mcpServers":{}}',
               "--setting-sources", "", "--no-session-persistence", "--system-prompt", system_prompt]
        if self.json_schema:
            cmd += ["--json-schema", json.dumps(self.json_schema, separators=(",", ":"))]
        return cmd

    @component.output_types(replies=list[ChatMessage])
    def run(self, messages: list[ChatMessage]) -> dict[str, list[ChatMessage]]:
        # Le prompt système passe en argument (via cmd.exe sous Windows) : une seule ligne.
        system = " ".join(" ".join(m.text.split()) for m in messages if m.is_from(ChatRole.SYSTEM) and m.text)
        prompt = "\n\n".join(m.text for m in messages if not m.is_from(ChatRole.SYSTEM) and m.text)
        try:
            proc = subprocess.run(self._command(system or DEFAULT_SYSTEM_PROMPT), input=prompt,
                                  capture_output=True, text=True, encoding="utf-8", timeout=self.timeout_s)
        except subprocess.TimeoutExpired as exc:
            raise LLMCallError(f"timeout after {self.timeout_s}s") from exc
        try:
            data = json.loads(proc.stdout)
        except json.JSONDecodeError as exc:
            message = (proc.stderr or proc.stdout or "").strip()[:500]
            if _QUOTA.search(message):
                raise LLMQuotaExceeded(message) from exc
            raise LLMCallError(f"exit {proc.returncode}: {message}") from exc
        if data.get("is_error") or proc.returncode != 0:
            message = str(data.get("result") or proc.stderr or "unknown CLI error")[:500]
            if data.get("api_error_status") == 429 or _QUOTA.search(message):
                raise LLMQuotaExceeded(message)
            raise LLMCallError(message)
        structured = data.get("structured_output")
        text = json.dumps(structured, ensure_ascii=False) if structured is not None else data.get("result", "")
        usage = data.get("usage") or {}
        model_used = next(iter(data.get("modelUsage") or {}), self.model)
        meta = {
            "model": f"claude-cli/{model_used}",
            "usage": {
                "prompt_tokens": int(usage.get("input_tokens") or 0),
                "completion_tokens": int(usage.get("output_tokens") or 0),
                "cache_read_input_tokens": int(usage.get("cache_read_input_tokens") or 0),
                "cache_creation_input_tokens": int(usage.get("cache_creation_input_tokens") or 0),
            },
            "cost_usd": data.get("total_cost_usd"),
            "duration_ms": data.get("duration_ms"),
            "num_turns": data.get("num_turns"),
            "session_id": data.get("session_id"),
        }
        return {"replies": [ChatMessage.from_assistant(text, meta=meta)]}
```

- [ ] **Step 4: Run the tests to verify they pass**

Run: `.venv\Scripts\python -m pytest tests/test_claude_cli.py -v`
Expected: 8 passed

- [ ] **Step 5: Commit**

```powershell
git add src/tsi_agent/components/claude_cli.py tests/test_claude_cli.py
git commit -m "feat: chat generator Haystack via le CLI claude (souscription)"
```


### Task 9: ProjectExtractor

Builds the prompt, calls the injected chat generator, validates the JSON with Pydantic (one retry), and merges the detail page's programme, year and title into the record. It runs with bounded concurrency (`contextvars.copy_context()` per task, so log events reach the run) and stops on quota. When the generator gives no cost (OpenAI API), it computes one from `pricing`. It serialises its generator with `component_to_dict`, the same way Haystack's `Agent` does.

**Files:**
- Create: `src/tsi_agent/components/extractor.py`
- Test: `tests/test_extractor.py`

- [ ] **Step 1: Write the failing test `tests/test_extractor.py`**

````python
from conftest import CONTENT_JSON, FakeChatGenerator
from tsi_agent import runlog
from tsi_agent.components.claude_cli import LLMQuotaExceeded
from tsi_agent.components.extractor import ProjectExtractor, parse_content
from tsi_agent.models import DetailPage, ProjectRef


def page(pid="hems", countries=None):
    ref = ProjectRef(project_id=pid, title="Listing title", programme="TSI", year=2021,
                     countries=countries if countries is not None else ["HR"],
                     detail_url=f"https://x/{pid}_en")
    return DetailPage(ref=ref, url=f"https://x/{pid}_en", title="Page title", text="Four HEMS bases.",
                      content_hash="sha256:1", programme="TSI", year=2022)


def test_parse_content_accepts_fences_and_filters_countries():
    content = parse_content('```json\n{"summary":"s","context":"c","support_delivered":"d",'
                            '"results_achieved":"r","countries":["hr","XX","GR"]}\n```')
    assert content.countries == ["HR", "GR"]


def test_record_merges_detail_metadata(tmp_path):
    gen = FakeChatGenerator()
    with runlog.open_run(tmp_path, "r1") as run:
        out = ProjectExtractor(chat_generator=gen).run(pages=[page()])
    rec = out["records"][0]
    assert (rec.ref.title, rec.ref.year, rec.ref.programme) == ("Page title", 2022, "TSI")
    assert rec.content.results_achieved == "Four HEMS bases were established."
    assert rec.model == "fake/model" and out["failed"] == [] and out["quota_exceeded"] is False
    assert run.llm_calls == 1 and run.cost_usd == 0.01
    prompt = gen.calls[0][1].text
    assert "Four HEMS bases." in prompt and "https://x/hems_en" in prompt


def test_countries_fallback_to_llm_when_listing_has_none(tmp_path):
    out = ProjectExtractor(chat_generator=FakeChatGenerator()).run(pages=[page(countries=[])])
    assert out["records"][0].ref.countries == ["HR"]


def test_one_retry_on_invalid_json_then_failure():
    ok = ProjectExtractor(chat_generator=FakeChatGenerator(replies=["not json", CONTENT_JSON])).run(pages=[page()])
    assert len(ok["records"]) == 1
    gen = FakeChatGenerator(replies=["not json"])
    ko = ProjectExtractor(chat_generator=gen).run(pages=[page()])
    assert ko["records"] == [] and ko["failed"] == ["hems"] and len(gen.calls) == 2


def test_quota_stops_the_batch():
    gen = FakeChatGenerator(error=LLMQuotaExceeded("usage limit reached"))
    out = ProjectExtractor(chat_generator=gen, max_concurrency=1).run(pages=[page("a"), page("b"), page("c")])
    assert out["quota_exceeded"] is True
    assert len(gen.calls) == 1 and out["records"] == []


def test_cost_from_pricing_when_generator_gives_none(tmp_path):
    gen = FakeChatGenerator(cost_usd=None)
    extractor = ProjectExtractor(chat_generator=gen, pricing={"fake/": {"input": 1.0, "output": 10.0}})
    with runlog.open_run(tmp_path, "r2") as run:
        extractor.run(pages=[page()])
    assert abs(run.cost_usd - (100 * 1.0 + 20 * 10.0) / 1_000_000) < 1e-12


def test_openai_style_meta_is_logged(tmp_path):
    from haystack import component
    from haystack.dataclasses import ChatMessage

    from conftest import CONTENT_JSON

    @component
    class OpenAILike:
        @component.output_types(replies=list[ChatMessage])
        def run(self, messages: list[ChatMessage]):
            meta = {"model": "gpt-4o-mini-2024-07-18", "usage": {
                "prompt_tokens": 1000, "completion_tokens": 200, "total_tokens": 1200,
                "prompt_tokens_details": {"cached_tokens": 400}}}
            return {"replies": [ChatMessage.from_assistant(CONTENT_JSON, meta=meta)]}

    extractor = ProjectExtractor(chat_generator=OpenAILike(), pricing={"gpt-4o-mini": {"input": 0.15, "output": 0.6}})
    with runlog.open_run(tmp_path, "r3") as run:
        out = extractor.run(pages=[page()])
    assert out["records"][0].model == "gpt-4o-mini-2024-07-18"
    assert run.llm_tokens["cache_read_tokens"] == 400
    assert abs(run.cost_usd - (1000 * 0.15 + 200 * 0.6) / 1_000_000) < 1e-12
````

- [ ] **Step 2: Replace `tests/conftest.py` with its final version** (adds `CONTENT_JSON` and `FakeChatGenerator`)

```python
import json
from pathlib import Path
from typing import Callable

import pytest
from haystack import component
from haystack.dataclasses import ChatMessage

from tsi_agent.http import FetchError, FetchResult

FIXTURES = Path(__file__).parent / "fixtures"


def fixture_bytes(name: str) -> bytes:
    return (FIXTURES / name).read_bytes()


@pytest.fixture
def fixtures_dir() -> Path:
    return FIXTURES


class FakeFetcher:
    """Remplace PoliteFetcher : `pages` url -> contenu, `redirects` url -> url finale, `default` pour le reste."""

    def __init__(self, pages: dict[str, bytes] | None = None, redirects: dict[str, str] | None = None,
                 default: Callable[[str], bytes | None] | None = None):
        self.pages = pages or {}
        self.redirects = redirects or {}
        self.default = default
        self.calls: list[str] = []

    def get(self, url: str) -> FetchResult:
        self.calls.append(url)
        final = self.redirects.get(url, url)
        content = self.pages.get(final)
        if content is None and self.default is not None:
            content = self.default(final)
        if content is None:
            raise FetchError(url, "HTTP 404", 404)
        return FetchResult(url, final, 200, content, False)


CONTENT_JSON = json.dumps({
    "summary": "The Commission supported HEMS in Croatia.",
    "context": "Croatia has many islands.",
    "support_delivered": "Workshops and guidance.",
    "results_achieved": "Four HEMS bases were established.",
    "countries": ["HR"],
})


@component
class FakeChatGenerator:
    """Chat generator de test : renvoie les réponses prévues, dans l'ordre (la dernière est répétée)."""

    def __init__(self, replies: list[str] | None = None, cost_usd: float | None = 0.01, error: Exception | None = None):
        self.replies = replies or [CONTENT_JSON]
        self.cost_usd = cost_usd
        self.error = error
        self.calls: list[list[ChatMessage]] = []

    @component.output_types(replies=list[ChatMessage])
    def run(self, messages: list[ChatMessage]):
        self.calls.append(messages)
        if self.error is not None:
            raise self.error
        text = self.replies[min(len(self.calls), len(self.replies)) - 1]
        meta = {"model": "fake/model", "usage": {"prompt_tokens": 100, "completion_tokens": 20},
                "cost_usd": self.cost_usd}
        return {"replies": [ChatMessage.from_assistant(text, meta=meta)]}
```

- [ ] **Step 2b: Run the tests to verify they fail**

Run: `.venv\Scripts\python -m pytest tests/test_extractor.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'tsi_agent.components.extractor'`

- [ ] **Step 3: Write `src/tsi_agent/components/extractor.py`**

````python
"""Extraction structurée d'une page projet par un chat generator Haystack injecté."""

import contextvars
import json
import re
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from typing import Any

from haystack import component, default_from_dict, default_to_dict
from haystack.core.serialization import component_to_dict
from haystack.dataclasses import ChatMessage
from haystack.utils import deserialize_chatgenerator_inplace
from pydantic import ValidationError

from tsi_agent import runlog
from tsi_agent.components.claude_cli import LLMQuotaExceeded
from tsi_agent.countries import COUNTRIES
from tsi_agent.models import DetailPage, ProjectContent, ProjectRecord

NAME = "ProjectExtractor"

SYSTEM_PROMPT = ("You restructure European Commission reform project pages into JSON. "
                 "You never add facts that are not in the page.")

USER_TEMPLATE = """You are given the text of a public project page of the European Commission \
(Technical Support Instrument or Structural Reform Support Programme).

Return a JSON object with exactly these keys:
- "summary": 3 to 5 sentences summarising the project, using only facts stated in the page.
- "context": the context / problem the project addresses, as clear prose.
- "support_delivered": the support the Commission delivered (activities, deliverables).
- "results_achieved": the results reported on the page.
- "countries": ISO 3166-1 alpha-2 codes of the EU Member States benefiting from the project \
(use "GR" for Greece); an empty list if the page does not say.

Rules:
- Use only information present in the page text.
- If the page does not cover a section, use exactly "Not reported on the source page."
- Write in English. Output only the JSON object, without code fences.

Project title: {title}
Source URL: {url}

--- PAGE TEXT START ---
{text}
--- PAGE TEXT END ---"""

RETRY_MESSAGE = ("Your previous answer could not be parsed ({error}). "
                 "Return only the JSON object with the keys summary, context, support_delivered, "
                 "results_achieved, countries.")

_FENCE = re.compile(r"^```(?:json)?\s*|\s*```$")


class ContentParseError(ValueError):
    pass


def parse_content(text: str) -> ProjectContent:
    cleaned = _FENCE.sub("", text.strip())
    start, end = cleaned.find("{"), cleaned.rfind("}")
    if start < 0 or end < start:
        raise ContentParseError("no JSON object in reply")
    try:
        content = ProjectContent.model_validate(json.loads(cleaned[start:end + 1]))
    except (json.JSONDecodeError, ValidationError) as exc:
        raise ContentParseError(str(exc).splitlines()[0]) from exc
    content.countries = [c.upper() for c in content.countries if c.upper() in COUNTRIES]
    return content


def _is_quota_error(exc: Exception) -> bool:
    return isinstance(exc, LLMQuotaExceeded) or "RateLimit" in type(exc).__name__


@component
class ProjectExtractor:
    def __init__(self, chat_generator: Any, max_concurrency: int = 2, max_retries: int = 1,
                 pricing: dict[str, dict[str, float]] | None = None):
        """
        :param chat_generator: tout chat generator Haystack (ClaudeCliChatGenerator, OpenAIChatGenerator...).
        :param pricing: prix en $ par million de tokens, par préfixe de modèle, utilisé quand le
            generator ne renvoie pas de coût (API OpenAI). Ex. {"gpt-4o-mini": {"input": 0.15, "output": 0.6}}.
        """
        self.chat_generator = chat_generator
        self.max_concurrency = max_concurrency
        self.max_retries = max_retries
        self.pricing = pricing or {}
        self._warmed = False

    def warm_up(self) -> None:
        if not self._warmed and hasattr(self.chat_generator, "warm_up"):
            self.chat_generator.warm_up()
        self._warmed = True

    def to_dict(self) -> dict[str, Any]:
        return default_to_dict(self, chat_generator=component_to_dict(obj=self.chat_generator, name="chat_generator"),
                               max_concurrency=self.max_concurrency, max_retries=self.max_retries,
                               pricing=self.pricing)

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "ProjectExtractor":
        deserialize_chatgenerator_inplace(data["init_parameters"], key="chat_generator")
        return default_from_dict(cls, data)

    def _cost(self, meta: dict[str, Any]) -> float | None:
        if meta.get("cost_usd") is not None:
            return float(meta["cost_usd"])
        usage = meta.get("usage") or {}
        model = str(meta.get("model", ""))
        for prefix, price in self.pricing.items():
            if model.startswith(prefix):
                return (usage.get("prompt_tokens", 0) * price.get("input", 0)
                        + usage.get("completion_tokens", 0) * price.get("output", 0)) / 1_000_000
        return None

    def _log_call(self, page: DetailPage, reply: ChatMessage, attempt: int, ms: int, result: str) -> None:
        meta = reply.meta or {}
        usage = meta.get("usage") or {}
        runlog.event(NAME, "llm_call", page.ref.project_id, result, duration_ms=ms, attempt=attempt,
                     model=meta.get("model"), input_tokens=usage.get("prompt_tokens", 0),
                     output_tokens=usage.get("completion_tokens", 0),
                     cache_read_tokens=usage.get("cache_read_input_tokens")
                     or (usage.get("prompt_tokens_details") or {}).get("cached_tokens", 0),  # format OpenAI
                     cache_creation_tokens=usage.get("cache_creation_input_tokens", 0),
                     cost_usd=self._cost(meta))

    def _extract_one(self, page: DetailPage) -> ProjectRecord:
        messages = [ChatMessage.from_system(SYSTEM_PROMPT),
                    ChatMessage.from_user(USER_TEMPLATE.format(title=page.title, url=page.url, text=page.text))]
        error = "no attempt"
        for attempt in range(1, self.max_retries + 2):
            t0 = time.monotonic()
            reply = self.chat_generator.run(messages=messages)["replies"][0]
            ms = int((time.monotonic() - t0) * 1000)
            try:
                content = parse_content(reply.text or "")
            except ContentParseError as exc:
                error = str(exc)
                self._log_call(page, reply, attempt, ms, "invalid_json")
                messages = [*messages, reply, ChatMessage.from_user(RETRY_MESSAGE.format(error=error))]
                continue
            self._log_call(page, reply, attempt, ms, "ok")
            ref = page.ref.model_copy(update={
                "title": page.title or page.ref.title,
                "programme": page.programme or page.ref.programme,
                "year": page.year or page.ref.year,
                "countries": page.ref.countries or content.countries,
                "detail_url": page.url,
            })
            model = str((reply.meta or {}).get("model", "unknown"))
            return ProjectRecord(ref=ref, content=content, content_hash=page.content_hash, model=model)
        raise ContentParseError(error)

    @component.output_types(records=list[ProjectRecord], failed=list[str], quota_exceeded=bool)
    def run(self, pages: list[DetailPage]) -> dict[str, Any]:
        self.warm_up()
        stop = threading.Event()
        results: dict[int, ProjectRecord] = {}
        failed: list[str] = []

        def work(i: int, page: DetailPage) -> None:
            if stop.is_set():
                return
            try:
                results[i] = self._extract_one(page)
            except Exception as exc:
                if _is_quota_error(exc):
                    stop.set()
                    runlog.event(NAME, "error", page.ref.project_id, "quota_exceeded", error=str(exc)[:300])
                else:
                    runlog.event(NAME, "error", page.ref.project_id, "extract_failed", error=str(exc)[:300])
                failed.append(page.ref.project_id)

        with ThreadPoolExecutor(max_workers=max(1, self.max_concurrency)) as pool:
            for i, page in enumerate(pages):
                pool.submit(contextvars.copy_context().run, work, i, page)
        records = [results[i] for i in sorted(results)]
        return {"records": records, "failed": failed, "quota_exceeded": stop.is_set()}
````

- [ ] **Step 4: Run the tests to verify they pass**

Run: `.venv\Scripts\python -m pytest tests/test_extractor.py -v`
Expected: 7 passed

- [ ] **Step 5: Commit**

```powershell
git add src/tsi_agent/components/extractor.py tests/test_extractor.py tests/conftest.py
git commit -m "feat: extracteur structure avec generator injecte, retry et suivi des couts"
```


### Task 10: SourceListingFetcher and ProjectDiscoverer

The listing fetcher builds the list of source URLs; with `countries` set (pilot runs) it skips the SRSP PDF. The discoverer parses every page, resolves the ~24 non-canonical links (old domain, odd paths) by following redirects, and merges duplicates by detail URL **or** by title key, because a multi-country project may have a link in one factsheet and not in another. A temporary resolve failure (429/5xx/network) defers the project (`deferred_ids`), and a 404 turns it into a thin project. `scope` records which countries and whether the PDF were actually fetched, so delisting stays within that scope.

**Files:**
- Create: `src/tsi_agent/components/listing.py`
- Create: `src/tsi_agent/components/discoverer.py`
- Test: `tests/test_discoverer.py`

- [ ] **Step 1: Write the failing test `tests/test_discoverer.py`**

```python
from conftest import FakeFetcher, fixture_bytes
from tsi_agent.components.discoverer import ProjectDiscoverer, merge_refs
from tsi_agent.models import ProjectRef, RawPage

BASE = "https://reforms-investments.ec.europa.eu/technical-support-instrument-0"


def test_merge_by_url_and_by_title_key():
    fr = ProjectRef(project_id="tsi-2025-academy", title="Academy", programme="TSI", year=2025, countries=["FR"],
                    multi_country=True, listing_urls=["https://x/france_en"])
    de = ProjectRef(project_id="academy", title="Academy", programme="TSI", year=2025, countries=["DE"],
                    multi_country=True, detail_url=f"{BASE}/green-transition/academy_en",
                    listing_urls=["https://x/germany_en"], aliases=["tsi-2025-academy"])
    spot = ProjectRef(project_id="academy", title="The Academy", detail_url=f"{BASE}/green-transition/academy_en",
                      spotlight=True, listing_urls=["https://x/spotlight_en"])
    merged = merge_refs([fr, de, spot])
    assert len(merged) == 1
    m = merged[0]
    assert m.project_id == "academy" and m.detail_url.endswith("/academy_en")
    assert m.countries == ["FR", "DE"] and m.multi_country and m.spotlight
    assert m.aliases == ["tsi-2025-academy"]
    assert m.listing_urls == ["https://x/france_en", "https://x/germany_en", "https://x/spotlight_en"]


def test_distinct_projects_stay_distinct():
    a = ProjectRef(project_id="tsi-2025-a", title="A", programme="TSI", year=2025, countries=["FR"])
    b = ProjectRef(project_id="tsi-2024-a", title="A", programme="TSI", year=2024, countries=["FR"])
    assert len(merge_refs([a, b])) == 2


def test_resolves_non_canonical_links_and_builds_programme_docs():
    hr_url = f"{BASE}/our-projects/tsi-country-factsheets/croatia_en"
    odd = f"{BASE}/what-we-do/finance-and-access-finance/support-improve-data-collection-insolvency-croatia_en"
    final = f"{BASE}/financial-sector-and-access-finance/support-improve-data-collection-insolvency-croatia_en"
    disc = ProjectDiscoverer()
    disc._fetcher = FakeFetcher(redirects={odd: final}, default=lambda url: b"<html></html>")
    pages = [
        RawPage(kind="factsheet", key="HR", url=hr_url, content=fixture_bytes("factsheet_croatia.html")),
        RawPage(kind="programme_doc", key="tsi-performance", url="https://x/perf_en",
                content=fixture_bytes("tsi_performance.html"), title="TSI performance"),
    ]
    out = disc.run(pages=pages)
    ids = {r.project_id for r in out["refs"]}
    assert "support-improve-data-collection-insolvency-croatia" in ids
    resolved = next(r for r in out["refs"] if r.project_id == "support-improve-data-collection-insolvency-croatia")
    assert resolved.policy_area == "financial-sector-and-access-finance"
    assert disc._fetcher.calls == [odd]
    assert [d.doc_id for d in out["programme_docs"]] == ["country-croatia"]  # performance vide => écartée
    assert out["scope"] == {"countries": ["HR"], "srsp": False, "all_countries": False}
    assert out["suspect_countries"] == []


def test_broken_link_becomes_thin_and_empty_factsheet_is_suspect():
    html = f"""<details class="ecl-accordion__item"><summary>2025</summary><div class="ecl-accordion__content">
      <p>For 2025 the following <strong>1</strong> project:</p>
      <ul><li><a href="https://reform-support.ec.europa.eu/what-we-do/x/gone_en">Gone project</a></li></ul>
      </div></details>""".encode()
    disc = ProjectDiscoverer()
    disc._fetcher = FakeFetcher()
    out = disc.run(pages=[
        RawPage(kind="factsheet", key="FR", url="https://x/france_en", content=html),
        RawPage(kind="factsheet", key="DE", url="https://x/germany_en", content=b"<html><main></main></html>"),
    ])
    assert [r.project_id for r in out["refs"]] == ["tsi-2025-gone-project"]
    assert out["refs"][0].detail_url is None and out["refs"][0].aliases == []
    assert out["suspect_countries"] == ["DE"]


def test_transient_resolve_failure_is_deferred_not_thin():
    html = f"""<details class="ecl-accordion__item"><summary>2025</summary><div class="ecl-accordion__content">
      <ul><li><a href="https://reform-support.ec.europa.eu/what-we-do/x/busy_en">Busy project</a></li></ul>
      </div></details>""".encode()

    class RateLimited(FakeFetcher):
        def get(self, url):
            from tsi_agent.http import FetchError
            raise FetchError(url, "HTTP 429")

    disc = ProjectDiscoverer()
    disc._fetcher = RateLimited()
    out = disc.run(pages=[RawPage(kind="factsheet", key="FR", url="https://x/france_en", content=html)])
    assert out["refs"] == []
    assert out["deferred_ids"] == ["busy", "tsi-2025-busy-project"]
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `.venv\Scripts\python -m pytest tests/test_discoverer.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'tsi_agent.components.discoverer'`

- [ ] **Step 3: Write `src/tsi_agent/components/listing.py`**

```python
from typing import Any

from haystack import component

from tsi_agent import runlog
from tsi_agent.countries import COUNTRIES, slug_from_iso
from tsi_agent.http import DEFAULT_USER_AGENT, FetchError, PoliteFetcher, shared_fetcher
from tsi_agent.models import RawPage

NAME = "SourceListingFetcher"


@component
class SourceListingFetcher:
    """Récupère toutes les pages de listing et les documents programme décrits dans `sources`."""

    def __init__(self, sources: dict[str, Any], cache_dir: str = ".cache/http", min_interval_s: float = 1.0,
                 user_agent: str = DEFAULT_USER_AGENT):
        self.sources = sources
        self.cache_dir = cache_dir
        self.min_interval_s = min_interval_s
        self.user_agent = user_agent
        self._fetcher: PoliteFetcher | None = None

    def _get_fetcher(self) -> PoliteFetcher:
        if self._fetcher is None:
            self._fetcher = shared_fetcher(self.cache_dir, self.min_interval_s, self.user_agent)
        return self._fetcher

    def _targets(self, countries: list[str] | None) -> list[tuple[str, str, str, str | None]]:
        s = self.sources
        isos = countries or list(COUNTRIES)
        targets = [("factsheet", iso, s["factsheet_url"].format(slug=slug_from_iso(iso)), None) for iso in isos]
        targets.append(("spotlight", "spotlight", s["spotlight_url"], None))
        targets += [("policy_area", slug, s["policy_area_url"].format(slug=slug), None) for slug in s["policy_areas"]]
        if not countries:  # le PDF SRSP couvre tous les pays : exclu des runs pilotes
            targets.append(("srsp_pdf", "srsp", s["srsp_pdf_url"], None))
        targets += [("programme_doc", d["id"], d["url"], d.get("title")) for d in s.get("programme_docs", [])]
        return targets

    @component.output_types(pages=list[RawPage])
    def run(self, countries: list[str] | None = None) -> dict[str, list[RawPage]]:
        pages = []
        for kind, key, url, title in self._targets(countries):
            try:
                result = self._get_fetcher().get(url)
            except FetchError as exc:
                runlog.event(NAME, "error", url, "fetch_failed", kind=kind, key=key, error=str(exc))
                continue
            pages.append(RawPage(kind=kind, key=key, url=url, content=result.content, title=title))
        runlog.event(NAME, "discover", None, "ok", pages=len(pages))
        return {"pages": pages}
```

- [ ] **Step 4: Write `src/tsi_agent/components/discoverer.py`**

```python
from typing import Any

from haystack import component

from tsi_agent import runlog
from tsi_agent.countries import COUNTRIES, name_from_iso, slug_from_iso
from tsi_agent.http import DEFAULT_USER_AGENT, FetchError, PoliteFetcher, shared_fetcher
from tsi_agent.ids import slug_from_url, title_key
from tsi_agent.models import ProgrammeDoc, ProjectRef, RawPage
from tsi_agent.parsers import main_text
from tsi_agent.parsers.factsheet import parse_factsheet
from tsi_agent.parsers.policy_area import parse_policy_area
from tsi_agent.parsers.spotlight import parse_spotlight
from tsi_agent.parsers.srsp_pdf import parse_srsp_pdf
from tsi_agent.urls import is_canonical_detail, is_final_detail, policy_area_from_url

NAME = "ProjectDiscoverer"
MIN_PROGRAMME_TEXT = 300


def ref_title_key(ref: ProjectRef) -> str | None:
    return title_key(ref.programme, ref.year, ref.title) if ref.programme and ref.year else None


def _union(a: list[str], b: list[str]) -> list[str]:
    return list(dict.fromkeys([*a, *b]))


def combine(a: ProjectRef, b: ProjectRef) -> ProjectRef:
    """Fusionne deux vues du même projet."""
    detail_url = a.detail_url or b.detail_url
    title = a.title if a.detail_url or not b.detail_url else b.title
    countries = _union(a.countries, b.countries)
    aliases = _union(a.aliases, b.aliases)
    if detail_url:
        aliases = _union(aliases, [r.project_id for r in (a, b) if not r.detail_url])
    project_id = slug_from_url(detail_url) if detail_url else a.project_id
    return ProjectRef(
        project_id=project_id, title=title, programme=a.programme or b.programme, year=a.year or b.year,
        countries=countries, multi_country=a.multi_country or b.multi_country or len(countries) > 1,
        detail_url=detail_url, policy_area=a.policy_area or b.policy_area,
        spotlight=a.spotlight or b.spotlight, highlighted=a.highlighted or b.highlighted,
        listing_urls=_union(a.listing_urls, b.listing_urls),
        aliases=[x for x in aliases if x != project_id],
    )


def merge_refs(refs: list[ProjectRef]) -> list[ProjectRef]:
    """Même projet = même URL détail ou même clé titre (programme, année, titre)."""
    groups: list[ProjectRef] = []
    by_url: dict[str, int] = {}
    by_key: dict[str, int] = {}
    for ref in refs:
        key = ref_title_key(ref)
        idx = by_url.get(ref.detail_url) if ref.detail_url else None
        if idx is None and key:
            idx = by_key.get(key)
        if idx is None:
            groups.append(ref.model_copy(deep=True))
            idx = len(groups) - 1
        else:
            groups[idx] = combine(groups[idx], ref)
        merged = groups[idx]
        if merged.detail_url:
            by_url[merged.detail_url] = idx
        for k in (key, ref_title_key(merged)):
            if k:
                by_key[k] = idx
    return groups


@component
class ProjectDiscoverer:
    """Parse les listings, résout les liens non canoniques, fusionne, produit les documents programme."""

    def __init__(self, cache_dir: str = ".cache/http", min_interval_s: float = 1.0,
                 user_agent: str = DEFAULT_USER_AGENT):
        self.cache_dir = cache_dir
        self.min_interval_s = min_interval_s
        self.user_agent = user_agent
        self._fetcher: PoliteFetcher | None = None

    def _get_fetcher(self) -> PoliteFetcher:
        if self._fetcher is None:
            self._fetcher = shared_fetcher(self.cache_dir, self.min_interval_s, self.user_agent)
        return self._fetcher

    def _resolve(self, ref: ProjectRef, deferred: list[str]) -> ProjectRef | None:
        """Suit les redirections d'un lien non canonique.

        None si le projet est abandonné (lien mort sans clé titre) ou reporté (échec temporaire :
        ses ids vont dans `deferred` pour qu'il ne soit ni écrit en thin ni dé-listé).
        """
        if ref.detail_url is None or is_canonical_detail(ref.detail_url):
            return ref
        try:
            final = self._get_fetcher().get(ref.detail_url).final_url
        except FetchError as exc:
            if exc.status not in (404, 410):
                deferred.extend([ref.project_id, *ref.aliases])
                runlog.event(NAME, "resolve", ref.detail_url, "deferred", error=str(exc))
                return None
            final = None
        if final and is_final_detail(final):
            runlog.event(NAME, "resolve", ref.detail_url, "ok", final_url=final)
            return ref.model_copy(update={"detail_url": final, "project_id": slug_from_url(final)})
        key = ref_title_key(ref)
        if key is None:
            runlog.event(NAME, "resolve", ref.detail_url, "dropped")
            return None
        runlog.event(NAME, "resolve", ref.detail_url, "thin", final_url=final)
        return ref.model_copy(update={"detail_url": None, "project_id": key,
                                      "aliases": [a for a in ref.aliases if a != key]})

    @component.output_types(refs=list[ProjectRef], programme_docs=list[ProgrammeDoc],
                            suspect_countries=list[str], scope=dict[str, Any], deferred_ids=list[str])
    def run(self, pages: list[RawPage]) -> dict[str, Any]:
        refs: list[ProjectRef] = []
        docs: list[ProgrammeDoc] = []
        suspect: list[str] = []
        scope: dict[str, Any] = {"countries": [], "srsp": False, "all_countries": False}
        for page in pages:
            try:
                found = self._parse(page, docs, scope)
            except Exception as exc:  # une page cassée ne doit pas arrêter le run
                runlog.event(NAME, "error", page.url, "parse_failed", kind=page.kind, error=repr(exc))
                continue
            if page.kind == "factsheet" and not found:
                suspect.append(page.key)
                runlog.event(NAME, "alert", page.key, "warning",
                             message=f"Factsheet {page.key}: 0 project parsed (site structure changed?)")
            refs += found
        scope["all_countries"] = set(scope["countries"]) == set(COUNTRIES)
        deferred: list[str] = []
        resolved = [r for r in (self._resolve(r, deferred) for r in refs) if r is not None]
        for r in resolved:
            if r.detail_url and not r.policy_area:
                r.policy_area = policy_area_from_url(r.detail_url)
        merged = merge_refs(resolved)
        kept_docs = []
        for doc in docs:
            if len(doc.text) < MIN_PROGRAMME_TEXT:
                runlog.event(NAME, "alert", doc.doc_id, "warning",
                             message=f"Programme document {doc.doc_id} has no content ({len(doc.text)} chars)")
            else:
                kept_docs.append(doc)
        runlog.event(NAME, "discover", None, "ok", raw_refs=len(refs), projects=len(merged),
                     detail=sum(r.detail_page for r in merged), programme_docs=len(kept_docs))
        return {"refs": merged, "programme_docs": kept_docs, "suspect_countries": suspect, "scope": scope,
                "deferred_ids": deferred}

    def _parse(self, page: RawPage, docs: list[ProgrammeDoc], scope: dict[str, Any]) -> list[ProjectRef]:
        if page.kind == "factsheet":
            parsed = parse_factsheet(page.content, page.url, page.key)
            scope["countries"].append(page.key)
            for year, announced in parsed.announced.items():
                if announced is not None and announced != parsed.parsed_counts.get(year):
                    runlog.event(NAME, "discover", page.key, "count_mismatch", year=year, announced=announced,
                                 parsed=parsed.parsed_counts.get(year))
            docs.append(ProgrammeDoc(doc_id=f"country-{slug_from_iso(page.key)}",
                                     title=f"TSI country factsheet - {name_from_iso(page.key)}",
                                     url=page.url, text=parsed.programme_text))
            return parsed.refs
        if page.kind == "spotlight":
            return parse_spotlight(page.content, page.url)
        if page.kind == "policy_area":
            found, text = parse_policy_area(page.content, page.url)
            docs.append(ProgrammeDoc(doc_id=f"policy-area-{page.key}", title=f"TSI policy area - {page.key}",
                                     url=page.url, text=text))
            return found
        if page.kind == "srsp_pdf":
            scope["srsp"] = True
            return parse_srsp_pdf(page.content, page.url)
        docs.append(ProgrammeDoc(doc_id=page.key, title=page.title or page.key, url=page.url,
                                 text=main_text(page.content)))
        return []
```

- [ ] **Step 5: Run the tests to verify they pass**

Run: `.venv\Scripts\python -m pytest tests/test_discoverer.py -v`
Expected: 5 passed

- [ ] **Step 6: Commit**

```powershell
git add src/tsi_agent/components/listing.py src/tsi_agent/components/discoverer.py tests/test_discoverer.py
git commit -m "feat: recuperation des listings et decouverte/fusion des projets"
```


### Task 11: ChangeDetector

Decides what to do: new or changed thin projects go straight to the writer; detail projects go to the fetcher, at most `limit` per batch and each only once per `run_id` (an in-memory set, which is what makes the batch loop terminate). Delisting only applies inside `scope`, never to suspect countries (0 projects parsed, or a drop below 70 % of known projects), and never to deferred ids.

**Files:**
- Create: `src/tsi_agent/components/change_detector.py`
- Test: `tests/test_change_detector.py`

- [ ] **Step 1: Write the failing test `tests/test_change_detector.py`**

```python
from tsi_agent.components.change_detector import ChangeDetector
from tsi_agent.models import ProjectRef
from tsi_agent.store import LocalProjectStore, StoreEntry

SCOPE = {"countries": ["FR"], "srsp": False, "all_countries": False}


def thin(pid, country="FR", **kw):
    return ProjectRef(project_id=pid, title=pid, programme="TSI", year=2025, countries=[country], **kw)


def detail(pid, country="FR"):
    return ProjectRef(project_id=pid, title=pid, programme="TSI", year=2025, countries=[country],
                      detail_url=f"https://x/{pid}_en")


def known(store, ref, **kw):
    store.put(StoreEntry(project_id=ref.project_id, file="f", programme=ref.programme, countries=ref.countries,
                         listing_hash=ref.listing_hash(), first_seen="d", last_updated="d", **kw))


def detector(tmp_path):
    d = ChangeDetector(output_dir=str(tmp_path))
    d._store = LocalProjectStore(tmp_path)
    return d


def test_new_changed_and_unchanged_thin(tmp_path):
    d = detector(tmp_path)
    same, changed = thin("same"), thin("changed")
    known(d._store, same)
    known(d._store, changed)
    out = d.run(refs=[same, changed.model_copy(update={"highlighted": True}), thin("new")], run_id="r", scope=SCOPE)
    assert [r.project_id for r in out["thin"]] == ["changed", "new"]
    assert out["detail"] == [] and out["delisted"] == []


def test_detail_limit_and_offered_once_per_run(tmp_path):
    d = detector(tmp_path)
    refs = [detail(f"p{i}") for i in range(5)]
    first = d.run(refs=refs, run_id="r", scope=SCOPE, limit=2)
    assert [r.project_id for r in first["detail"]] == ["p0", "p1"] and first["remaining"] == 3
    second = d.run(refs=refs, run_id="r", scope=SCOPE, limit=2)
    assert [r.project_id for r in second["detail"]] == ["p2", "p3"] and second["remaining"] == 1
    other_run = d.run(refs=refs, run_id="r2", scope=SCOPE)
    assert len(other_run["detail"]) == 5


def test_thin_already_covered_by_detail_alias_is_skipped(tmp_path):
    d = detector(tmp_path)
    known(d._store, detail("academy"), aliases=["tsi-2025-academy"])
    out = d.run(refs=[thin("tsi-2025-academy")], run_id="r", scope=SCOPE)
    assert out["thin"] == []


def test_delist_only_in_scope_and_not_for_suspect_countries(tmp_path):
    d = detector(tmp_path)
    for i in range(6):
        known(d._store, thin(f"fr{i}"))
    known(d._store, thin("de0", country="DE"))
    known(d._store, thin("gone"))
    refs = [thin(f"fr{i}") for i in range(6)]
    out = d.run(refs=refs, run_id="r", scope=SCOPE)
    assert out["delisted"] == ["gone"]  # DE hors périmètre
    out = d.run(refs=refs, run_id="r", scope=SCOPE, suspect_countries=["FR"])
    assert out["delisted"] == []


def test_big_drop_makes_country_suspect(tmp_path):
    d = detector(tmp_path)
    for i in range(10):
        known(d._store, thin(f"fr{i}"))
    out = d.run(refs=[thin("fr0"), thin("fr1")], run_id="r", scope=SCOPE)
    assert out["delisted"] == []


def test_deferred_ids_are_not_delisted(tmp_path):
    d = detector(tmp_path)
    known(d._store, thin("busy"))
    out = d.run(refs=[], run_id="r", scope=SCOPE, deferred_ids=["busy"])
    assert out["delisted"] == []
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `.venv\Scripts\python -m pytest tests/test_change_detector.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'tsi_agent.components.change_detector'`

- [ ] **Step 3: Write `src/tsi_agent/components/change_detector.py`**

```python
from collections import Counter
from typing import Any

from haystack import component

from tsi_agent import runlog
from tsi_agent.models import ProjectRef
from tsi_agent.store import ProjectStore, shared_store, StoreEntry

NAME = "ChangeDetector"
DROP_RATIO = 0.7      # moins de 70 % des projets connus d'un pays => pays suspect
DROP_MIN_KNOWN = 5    # en dessous, on ne juge pas une chute


def _in_scope(entry: StoreEntry, scope: dict[str, Any]) -> bool:
    if entry.programme == "SRSP":
        return bool(scope.get("srsp"))
    if not entry.countries:
        return bool(scope.get("all_countries"))
    return any(c in scope.get("countries", []) for c in entry.countries)


@component
class ChangeDetector:
    """Compare les projets découverts à l'état : que faut-il (ré)extraire, écrire, dé-lister ?"""

    def __init__(self, output_dir: str = "output"):
        self.output_dir = output_dir
        self._store: ProjectStore | None = None
        self._offered: dict[str, set[str]] = {}  # run_id -> projets détaillés déjà proposés dans ce run

    def _get_store(self) -> ProjectStore:
        if self._store is None:
            self._store = shared_store(self.output_dir)
        return self._store

    def _dropped_countries(self, refs: list[ProjectRef], known: dict[str, StoreEntry],
                           scope: dict[str, Any]) -> list[str]:
        now = Counter(c for r in refs if r.programme != "SRSP" for c in r.countries)
        before = Counter(c for e in known.values() if e.listed and e.programme != "SRSP" for c in e.countries)
        dropped = []
        for country in scope.get("countries", []):
            if before[country] >= DROP_MIN_KNOWN and now[country] < DROP_RATIO * before[country]:
                dropped.append(country)
                runlog.event(NAME, "alert", country, "warning",
                             message=f"{country}: {now[country]} projects listed vs {before[country]} known")
        return dropped

    @component.output_types(thin=list[ProjectRef], detail=list[ProjectRef], delisted=list[str], remaining=int)
    def run(self, refs: list[ProjectRef], run_id: str, scope: dict[str, Any],
            suspect_countries: list[str] | None = None, deferred_ids: list[str] | None = None, full: bool = False,
            limit: int | None = None) -> dict[str, Any]:
        store = self._get_store()
        known = store.all()
        alias_owner = {alias: pid for pid, e in known.items() for alias in e.aliases}
        offered = self._offered.setdefault(run_id, set())
        thin, detail, remaining = [], [], 0
        for ref in refs:
            entry = known.get(ref.project_id)
            if ref.detail_url:
                if ref.project_id in offered:
                    continue
                if limit is not None and len(detail) >= limit:
                    remaining += 1
                    continue
                offered.add(ref.project_id)
                detail.append(ref)
                runlog.event(NAME, "classify", ref.project_id, "known_detail" if entry else "new_detail")
            elif entry is None:
                if ref.project_id in alias_owner:
                    continue  # déjà couvert par sa page détail
                thin.append(ref)
                runlog.event(NAME, "classify", ref.project_id, "new_thin")
            elif entry.listing_hash != ref.listing_hash() or not entry.listed:
                thin.append(ref)
                runlog.event(NAME, "classify", ref.project_id, "changed_thin")
        suspect = set(suspect_countries or []) | set(self._dropped_countries(refs, known, scope))
        seen = {r.project_id for r in refs} | {a for r in refs for a in r.aliases} | set(deferred_ids or [])
        delisted = [
            pid for pid, e in known.items()
            if e.listed and pid not in seen and _in_scope(e, scope) and not (set(e.countries) & suspect)
        ]
        for pid in delisted:
            runlog.event(NAME, "classify", pid, "delisted")
        return {"thin": thin, "detail": detail, "delisted": delisted, "remaining": remaining}
```

- [ ] **Step 4: Run the tests to verify they pass**

Run: `.venv\Scripts\python -m pytest tests/test_change_detector.py -v`
Expected: 6 passed

- [ ] **Step 5: Commit**

```powershell
git add src/tsi_agent/components/change_detector.py tests/test_change_detector.py
git commit -m "feat: detection des projets nouveaux, modifies et retires"
```


### Task 12: DetailPageFetcher and MarkdownProjectWriter

The fetcher hashes each detail page's text, and only changed pages go to the LLM (`full=True` forces all). The writer writes detail and thin files, replaces a thin file when its project gains a detail page (keeping `first_seen` and the alias), refreshes only the front matter of unchanged projects whose listing metadata changed, marks delisted projects `listed: false` (never deletes), and writes programme documents when their hash changes.

**Files:**
- Create: `src/tsi_agent/components/detail_fetcher.py`
- Create: `src/tsi_agent/components/writer.py`
- Test: `tests/test_detail_fetcher_writer.py`

- [ ] **Step 1: Write the failing test `tests/test_detail_fetcher_writer.py`**

```python
from conftest import FakeFetcher, fixture_bytes
from tsi_agent.components.detail_fetcher import DetailPageFetcher
from tsi_agent.components.writer import MarkdownProjectWriter
from tsi_agent.models import ProgrammeDoc, ProjectContent, ProjectRecord, ProjectRef
from tsi_agent.render import parse_front_matter
from tsi_agent.store import LocalProjectStore, StoreEntry

URL = "https://reforms-investments.ec.europa.eu/technical-support-instrument-0/health-and-long-term-care/hems_en"


def ref(**kw):
    base = dict(project_id="hems", title="HEMS", programme="TSI", year=2022, countries=["HR"], detail_url=URL,
                listing_urls=["https://x/croatia_en"])
    return ProjectRef(**{**base, **kw})


def fetcher(tmp_path, store):
    f = DetailPageFetcher(output_dir=str(tmp_path))
    f._fetcher = FakeFetcher(pages={URL: fixture_bytes("detail_hems_croatia.html")})
    f._store = store
    return f


def writer(store):
    w = MarkdownProjectWriter()
    w._store = store
    return w


def test_detail_fetcher_new_then_unchanged_then_full(tmp_path):
    store = LocalProjectStore(tmp_path)
    f = fetcher(tmp_path, store)
    out = f.run(refs=[ref()])
    page = out["pages"][0]
    assert page.title == "Establishment of Helicopter Emergency Medical Services in Croatia"
    assert (page.programme, page.year) == ("TSI", 2022) and page.content_hash.startswith("sha256:")
    store.put(StoreEntry(project_id="hems", file="f", content_hash=page.content_hash, first_seen="d", last_updated="d"))
    assert [r.project_id for r in f.run(refs=[ref()])["unchanged"]] == ["hems"]
    assert len(f.run(refs=[ref()], full=True)["pages"]) == 1


def test_detail_fetcher_failure(tmp_path):
    f = fetcher(tmp_path, LocalProjectStore(tmp_path))
    out = f.run(refs=[ref(project_id="missing", detail_url="https://x/missing_en")])
    assert out["failed"] == ["missing"] and out["pages"] == []


def record(**kw):
    content = ProjectContent(summary="S.", context="C.", support_delivered="D.", results_achieved="R.")
    return ProjectRecord(ref=ref(**kw), content=content, content_hash="sha256:1", model="claude-cli/sonnet")


def test_writer_thin_then_detail_replaces_thin_file(tmp_path):
    store = LocalProjectStore(tmp_path)
    w = writer(store)
    thin_ref = ProjectRef(project_id="tsi-2022-hems", title="HEMS", programme="TSI", year=2022, countries=["HR"],
                          listing_urls=["https://x/croatia_en"])
    w.run(run_id="r1", thin=[thin_ref])
    fm, body = parse_front_matter(store.read_project_file("tsi-2022-hems"))
    assert fm["detail_page"] is False and fm["extraction"] == {"run_id": "r1", "model": "none"}
    assert "No detail page is published." in body
    out = w.run(run_id="r2", records=[record(aliases=["tsi-2022-hems"])])
    assert out["stats"]["detail"] == 1
    assert store.read_project_file("tsi-2022-hems") is None and store.get("tsi-2022-hems") is None
    fm, body = parse_front_matter(store.read_project_file("hems"))
    assert fm["aliases"] == ["tsi-2022-hems"] and fm["content_hash"] == "sha256:1"
    assert fm["extraction"] == {"run_id": "r2", "model": "claude-cli/sonnet"}
    assert "## Results achieved\n\nR." in body
    assert store.get("hems").first_seen == store.get("hems").last_updated


def test_writer_refreshes_front_matter_of_unchanged_and_delists(tmp_path):
    store = LocalProjectStore(tmp_path)
    w = writer(store)
    w.run(run_id="r1", records=[record()])
    out = w.run(run_id="r2", unchanged=[ref(spotlight=True)])
    assert out["stats"]["refreshed"] == 1
    fm, body = parse_front_matter(store.read_project_file("hems"))
    assert fm["spotlight"] is True and fm["extraction"]["run_id"] == "r1" and "## Summary\n\nS." in body
    assert w.run(run_id="r3", unchanged=[ref(spotlight=True)])["stats"]["refreshed"] == 0
    w.run(run_id="r4", delisted=["hems"])
    fm, _ = parse_front_matter(store.read_project_file("hems"))
    assert fm["listed"] is False and store.get("hems").listed is False


def test_writer_programme_docs_only_when_changed(tmp_path):
    store = LocalProjectStore(tmp_path)
    w = writer(store)
    doc = ProgrammeDoc(doc_id="country-croatia", title="Croatia", url="https://x/croatia_en", text="x" * 400)
    assert w.run(run_id="r1", programme_docs=[doc])["stats"]["programme_docs"] == 1
    assert w.run(run_id="r2", programme_docs=[doc])["stats"]["programme_docs"] == 0
    assert (tmp_path / "programme" / "country-croatia.md").exists()
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `.venv\Scripts\python -m pytest tests/test_detail_fetcher_writer.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'tsi_agent.components.detail_fetcher'`

- [ ] **Step 3: Write `src/tsi_agent/components/detail_fetcher.py`**

```python
from typing import Any

from haystack import component

from tsi_agent import runlog
from tsi_agent.http import DEFAULT_USER_AGENT, FetchError, PoliteFetcher, shared_fetcher
from tsi_agent.ids import sha256_text
from tsi_agent.models import DetailPage, ProjectRef
from tsi_agent.parsers.detail import parse_detail
from tsi_agent.store import ProjectStore, shared_store

NAME = "DetailPageFetcher"


@component
class DetailPageFetcher:
    """Récupère les pages détail ; seules celles dont le texte a changé partent vers le LLM."""

    def __init__(self, output_dir: str = "output", cache_dir: str = ".cache/http", min_interval_s: float = 1.0,
                 user_agent: str = DEFAULT_USER_AGENT):
        self.output_dir = output_dir
        self.cache_dir = cache_dir
        self.min_interval_s = min_interval_s
        self.user_agent = user_agent
        self._fetcher: PoliteFetcher | None = None
        self._store: ProjectStore | None = None

    def _get_fetcher(self) -> PoliteFetcher:
        if self._fetcher is None:
            self._fetcher = shared_fetcher(self.cache_dir, self.min_interval_s, self.user_agent)
        return self._fetcher

    def _get_store(self) -> ProjectStore:
        if self._store is None:
            self._store = shared_store(self.output_dir)
        return self._store

    @component.output_types(pages=list[DetailPage], unchanged=list[ProjectRef], failed=list[str])
    def run(self, refs: list[ProjectRef], full: bool = False) -> dict[str, Any]:
        pages, unchanged, failed = [], [], []
        for ref in refs:
            try:
                result = self._get_fetcher().get(ref.detail_url)
                parsed = parse_detail(result.content)
            except FetchError as exc:
                failed.append(ref.project_id)
                runlog.event(NAME, "error", ref.project_id, "fetch_failed", error=str(exc))
                continue
            content_hash = sha256_text(parsed.text)
            entry = self._get_store().get(ref.project_id)
            if not full and entry is not None and entry.content_hash == content_hash:
                unchanged.append(ref)
                runlog.event(NAME, "classify", ref.project_id, "unchanged")
                continue
            pages.append(DetailPage(ref=ref, url=result.final_url, title=parsed.title or ref.title,
                                    text=parsed.text, content_hash=content_hash,
                                    programme=parsed.programme, year=parsed.year))
            runlog.event(NAME, "classify", ref.project_id, "changed" if entry else "new")
        return {"pages": pages, "unchanged": unchanged, "failed": failed}
```

- [ ] **Step 4: Write `src/tsi_agent/components/writer.py`**

```python
from datetime import date
from typing import Any

from haystack import component

from tsi_agent import runlog
from tsi_agent.models import ProgrammeDoc, ProjectRecord, ProjectRef
from tsi_agent.render import (detail_body, front_matter, parse_front_matter, programme_doc_text, render,
                              source_urls, thin_body)
from tsi_agent.store import ProjectStore, shared_store, StoreEntry

NAME = "MarkdownProjectWriter"
THIN_FLUSH_EVERY = 100  # l'index est réécrit en entier : on groupe les projets thin


@component
class MarkdownProjectWriter:
    """Écrit un fichier par projet et met à jour l'état, projet par projet."""

    def __init__(self, output_dir: str = "output"):
        self.output_dir = output_dir
        self._store: ProjectStore | None = None

    def _get_store(self) -> ProjectStore:
        if self._store is None:
            self._store = shared_store(self.output_dir)
        return self._store

    def _first_seen(self, ref: ProjectRef, today: str) -> str:
        store = self._get_store()
        dates = [e.first_seen for pid in (ref.project_id, *ref.aliases) if (e := store.get(pid))]
        return min(dates) if dates else today

    def _entry(self, ref: ProjectRef, file: str, first_seen: str, today: str,
               content_hash: str | None) -> StoreEntry:
        return StoreEntry(project_id=ref.project_id, file=file, programme=ref.programme,
                          detail_page=ref.detail_page, content_hash=content_hash,
                          listing_hash=ref.listing_hash(), source_urls=source_urls(ref),
                          countries=list(ref.countries), first_seen=first_seen, last_updated=today,
                          listed=True, aliases=list(ref.aliases))

    def _replace_aliases(self, ref: ProjectRef) -> None:
        store = self._get_store()
        for alias in ref.aliases:
            if alias != ref.project_id and store.get(alias):
                store.delete_project_file(alias)
                store.remove(alias)
                runlog.event(NAME, "write", alias, "replaced_by_detail", project_id=ref.project_id)

    def _write_record(self, rec: ProjectRecord, run_id: str, today: str) -> str:
        store = self._get_store()
        ref = rec.ref
        first_seen = self._first_seen(ref, today)
        fm = front_matter(ref, content_hash=rec.content_hash, first_seen=first_seen, last_updated=today,
                          run_id=run_id, model=rec.model)
        file = store.write_project_file(ref.project_id, render(fm, detail_body(ref, rec.content)))
        self._replace_aliases(ref)
        store.put(self._entry(ref, file, first_seen, today, rec.content_hash))
        runlog.event(NAME, "write", ref.project_id, "detail", file=file)
        return file

    def _write_thin(self, ref: ProjectRef, run_id: str, today: str) -> str:
        store = self._get_store()
        first_seen = self._first_seen(ref, today)
        fm = front_matter(ref, content_hash=None, first_seen=first_seen, last_updated=today,
                          run_id=run_id, model="none")
        file = store.write_project_file(ref.project_id, render(fm, thin_body(ref)))
        store.put(self._entry(ref, file, first_seen, today, None), save=False)
        runlog.event(NAME, "write", ref.project_id, "thin", file=file)
        return file

    def _refresh(self, project_id: str, today: str, ref: ProjectRef | None = None, listed: bool = True) -> bool:
        """Met à jour le front matter d'un fichier existant sans rappeler le LLM."""
        store = self._get_store()
        entry = store.get(project_id)
        text = store.read_project_file(project_id)
        if entry is None or text is None:
            return False
        if ref is not None and entry.listing_hash == ref.listing_hash() and entry.listed == listed:
            return False
        old, body = parse_front_matter(text)
        if ref is not None:
            fm = front_matter(ref, content_hash=entry.content_hash, first_seen=entry.first_seen,
                              last_updated=today, run_id=old["extraction"]["run_id"],
                              model=old["extraction"]["model"], listed=listed)
            fm["title"], fm["programme"], fm["year"] = old["title"], old["programme"], old["year"]
            entry.listing_hash, entry.countries, entry.source_urls = ref.listing_hash(), list(ref.countries), source_urls(ref)
        else:
            fm = {**old, "listed": listed, "last_updated": today}
        store.write_project_file(project_id, render(fm, body))
        entry.listed, entry.last_updated = listed, today
        store.put(entry)
        return True

    @component.output_types(written=list[str], stats=dict[str, int])
    def run(self, run_id: str, records: list[ProjectRecord] | None = None, thin: list[ProjectRef] | None = None,
            unchanged: list[ProjectRef] | None = None, delisted: list[str] | None = None,
            programme_docs: list[ProgrammeDoc] | None = None) -> dict[str, Any]:
        today = date.today().isoformat()
        store = self._get_store()
        written: list[str] = []
        stats = {"detail": 0, "thin": 0, "refreshed": 0, "delisted": 0, "programme_docs": 0}
        for rec in records or []:
            written.append(self._write_record(rec, run_id, today))
            stats["detail"] += 1
        for i, ref in enumerate(thin or [], start=1):
            written.append(self._write_thin(ref, run_id, today))
            stats["thin"] += 1
            if i % THIN_FLUSH_EVERY == 0:
                store.flush()
        store.flush()
        for ref in unchanged or []:
            if self._refresh(ref.project_id, today, ref=ref):
                stats["refreshed"] += 1
                runlog.event(NAME, "write", ref.project_id, "refreshed_front_matter")
        for pid in delisted or []:
            if self._refresh(pid, today, listed=False):
                stats["delisted"] += 1
                runlog.event(NAME, "delist", pid)
        for doc in programme_docs or []:
            if store.programme_hash(doc.doc_id) != doc.content_hash:
                written.append(store.write_programme_doc(doc.doc_id, programme_doc_text(doc, run_id, today),
                                                         doc.content_hash))
                stats["programme_docs"] += 1
                runlog.event(NAME, "write", doc.doc_id, "programme_doc")
        return {"written": written, "stats": stats}
```

- [ ] **Step 5: Run the tests to verify they pass**

Run: `.venv\Scripts\python -m pytest tests/test_detail_fetcher_writer.py -v`
Expected: 5 passed

- [ ] **Step 6: Commit**

```powershell
git add src/tsi_agent/components/detail_fetcher.py src/tsi_agent/components/writer.py tests/test_detail_fetcher_writer.py
git commit -m "feat: recuperation des pages detail et ecriture des fichiers projet"
```


### Task 13: Pipeline, runner and CLI

Assembles the pipeline (a dry-run variant stops after the detector) and exports / reloads it as YAML. Haystack 3.1 refuses unknown modules, hence `allowed_modules=["tsi_agent"]`. The runner loops over batches until nothing remains, the global `limit` is reached, or the quota is hit. The CLI exposes `run` and `export-yaml`. The end-to-end test runs twice on the Croatia fixture with fakes: the first run writes everything, the second makes zero LLM calls.

**Files:**
- Create: `src/tsi_agent/components/__init__.py`
- Create: `src/tsi_agent/config.py`
- Create: `src/tsi_agent/pipeline.py`
- Create: `src/tsi_agent/runner.py`
- Create: `src/tsi_agent/__main__.py`
- Test: `tests/test_pipeline.py`

- [ ] **Step 1: Write the failing test `tests/test_pipeline.py`**

```python
import json

import pytest

from conftest import FakeChatGenerator, FakeFetcher, fixture_bytes
from tsi_agent.components import ClaudeCliChatGenerator, ProjectExtractor
from tsi_agent.config import Settings, load_sources
from tsi_agent.pipeline import build_pipeline, load_pipeline
from tsi_agent.runner import run_extraction
from tsi_agent.store import LocalProjectStore

SOURCES = load_sources("config/sources.yaml")
HR = SOURCES["factsheet_url"].format(slug="croatia")


def fake_site(url: str) -> bytes | None:
    if url == HR:
        return fixture_bytes("factsheet_croatia.html")
    if url == SOURCES["spotlight_url"]:
        return fixture_bytes("spotlight.html")
    if url.endswith("/green-transition_en"):
        return fixture_bytes("policy_area_green_transition.html")
    if "/technical-support-instrument-0/" in url and url.endswith("_en") and "tsi-country-factsheets" not in url:
        if url.count("/") <= 4:  # autres pages de domaine politique : vides
            return b"<html><main></main></html>"
        return fixture_bytes("detail_hems_croatia.html")
    if url == SOURCES["programme_docs"][0]["url"]:
        return fixture_bytes("tsi_performance.html")
    return None


@pytest.fixture
def settings(tmp_path):
    return Settings(output_dir=str(tmp_path / "output"), logs_dir=str(tmp_path / "logs"),
                    cache_dir=str(tmp_path / "cache"), min_interval_s=0, chunk_size=20)


def factory(generator, fetcher):
    def make(settings, sources, dry_run=False, model=None):
        pipe = build_pipeline(settings, sources, dry_run=dry_run, chat_generator=generator)
        for name in ("listing", "discoverer", "detail_fetcher"):
            if name in pipe.graph.nodes:
                pipe.get_component(name)._fetcher = fetcher
        return pipe
    return make


def test_two_runs_first_writes_second_is_incremental(settings):
    gen, fetcher = FakeChatGenerator(), FakeFetcher(default=fake_site)
    first = run_extraction(settings, SOURCES, countries=["HR"], run_id="run1", pipeline_factory=factory(gen, fetcher))
    store = LocalProjectStore(settings.output_dir)
    projects = store.all()
    n_detail = sum(e.detail_page for e in projects.values())
    assert n_detail > 40 and len(projects) > 60
    assert first["llm_calls"] == len(gen.calls) == n_detail
    assert first["chunks"] >= 3 and first["quota_exceeded"] is False
    assert "Programme document tsi-performance has no content (0 chars)" in " ".join(first["alerts"]) or \
        any("tsi-performance" in a for a in first["alerts"])
    assert (settings_path := (store.root / "programme" / "country-croatia.md")).exists()
    summary_file = json.loads((store.root.parent / "logs" / "runs" / "run1.summary.json").read_text(encoding="utf-8"))
    assert summary_file["llm_calls"] == n_detail

    gen2 = FakeChatGenerator()
    second = run_extraction(settings, SOURCES, countries=["HR"], run_id="run2",
                            pipeline_factory=factory(gen2, FakeFetcher(default=fake_site)))
    assert second["llm_calls"] == 0 and gen2.calls == []
    assert second["written"]["thin"] == 0 and second["written"]["detail"] == 0


def test_limit_caps_llm_calls(settings):
    gen = FakeChatGenerator()
    summary = run_extraction(settings, SOURCES, countries=["HR"], limit=3, run_id="r",
                             pipeline_factory=factory(gen, FakeFetcher(default=fake_site)))
    assert summary["llm_calls"] == 3 and summary["remaining_detail"] > 0


def test_dry_run_writes_nothing(settings):
    summary = run_extraction(settings, SOURCES, countries=["HR"], dry_run=True, run_id="d",
                             pipeline_factory=factory(FakeChatGenerator(), FakeFetcher(default=fake_site)))
    assert summary["dry_run"]["detail_candidates"] > 40 and summary["llm_calls"] == 0
    assert LocalProjectStore(settings.output_dir).all() == {}


def test_yaml_roundtrip_for_deepset(settings):
    pipe = build_pipeline(settings, SOURCES)
    yaml_text = pipe.dumps()
    assert "tsi_agent.components.claude_cli.ClaudeCliChatGenerator" in yaml_text
    reloaded = load_pipeline(yaml_text)
    extractor = reloaded.get_component("extractor")
    assert isinstance(extractor, ProjectExtractor)
    assert isinstance(extractor.chat_generator, ClaudeCliChatGenerator)
    assert extractor.chat_generator.json_schema["required"] == ["summary", "context", "support_delivered",
                                                                "results_achieved"]
    assert set(reloaded.graph.nodes) == {"listing", "discoverer", "detector", "detail_fetcher", "extractor", "writer"}


def test_llm_schema_is_ascii_for_the_windows_command_line():
    from tsi_agent.models import ProjectContent
    json.dumps(ProjectContent.model_json_schema()).encode("ascii")
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `.venv\Scripts\python -m pytest tests/test_pipeline.py -v`
Expected: FAIL — `ImportError: cannot import name 'ClaudeCliChatGenerator' from 'tsi_agent.components'`

- [ ] **Step 3: Write `src/tsi_agent/components/__init__.py`**

```python
from tsi_agent.components.change_detector import ChangeDetector
from tsi_agent.components.claude_cli import ClaudeCliChatGenerator, LLMCallError, LLMQuotaExceeded
from tsi_agent.components.detail_fetcher import DetailPageFetcher
from tsi_agent.components.discoverer import ProjectDiscoverer
from tsi_agent.components.extractor import ProjectExtractor
from tsi_agent.components.listing import SourceListingFetcher
from tsi_agent.components.writer import MarkdownProjectWriter

__all__ = [
    "ChangeDetector", "ClaudeCliChatGenerator", "DetailPageFetcher", "LLMCallError", "LLMQuotaExceeded",
    "MarkdownProjectWriter", "ProjectDiscoverer", "ProjectExtractor", "SourceListingFetcher",
]
```

- [ ] **Step 4: Write `src/tsi_agent/config.py`**

```python
from pathlib import Path
from typing import Any

import yaml
from pydantic import BaseModel, Field


class Settings(BaseModel):
    output_dir: str = "output"
    logs_dir: str = "logs"
    cache_dir: str = ".cache/http"
    user_agent: str = "TSI-Extraction-Agent/0.1 (DG REFORM project; polite crawler)"
    min_interval_s: float = 2.0
    model: str = "sonnet"
    llm_timeout_s: int = 180
    max_concurrency: int = 2
    chunk_size: int = 25
    # $ par million de tokens, par préfixe de modèle ; sert quand le generator ne renvoie pas de coût (API OpenAI).
    pricing: dict[str, dict[str, float]] = Field(default_factory=dict)


def load_settings(path: str | Path = "config/settings.yaml") -> Settings:
    p = Path(path)
    return Settings.model_validate(yaml.safe_load(p.read_text(encoding="utf-8")) or {}) if p.exists() else Settings()


def load_sources(path: str | Path = "config/sources.yaml") -> dict[str, Any]:
    return yaml.safe_load(Path(path).read_text(encoding="utf-8"))
```

- [ ] **Step 5: Write `src/tsi_agent/pipeline.py`**

```python
"""Assemblage du pipeline Haystack (et export YAML pour deepset)."""

from typing import Any

from haystack import Pipeline

from tsi_agent.components import (ChangeDetector, ClaudeCliChatGenerator, DetailPageFetcher, MarkdownProjectWriter,
                                  ProjectDiscoverer, ProjectExtractor, SourceListingFetcher)
from tsi_agent.config import Settings
from tsi_agent.models import ProjectContent

ALLOWED_MODULES = ["tsi_agent"]


def default_chat_generator(settings: Settings, model: str | None = None) -> Any:
    return ClaudeCliChatGenerator(model=model or settings.model, timeout_s=settings.llm_timeout_s,
                                  json_schema=ProjectContent.model_json_schema())


def build_pipeline(settings: Settings, sources: dict[str, Any], *, dry_run: bool = False,
                   chat_generator: Any = None, model: str | None = None) -> Pipeline:
    http = {"cache_dir": settings.cache_dir, "min_interval_s": settings.min_interval_s,
            "user_agent": settings.user_agent}
    p = Pipeline()
    p.add_component("listing", SourceListingFetcher(sources=sources, **http))
    p.add_component("discoverer", ProjectDiscoverer(**http))
    p.add_component("detector", ChangeDetector(output_dir=settings.output_dir))
    p.connect("listing.pages", "discoverer.pages")
    p.connect("discoverer.refs", "detector.refs")
    p.connect("discoverer.scope", "detector.scope")
    p.connect("discoverer.suspect_countries", "detector.suspect_countries")
    p.connect("discoverer.deferred_ids", "detector.deferred_ids")
    if dry_run:
        return p
    p.add_component("detail_fetcher", DetailPageFetcher(output_dir=settings.output_dir, **http))
    p.add_component("extractor", ProjectExtractor(
        chat_generator=chat_generator or default_chat_generator(settings, model),
        max_concurrency=settings.max_concurrency, pricing=settings.pricing))
    p.add_component("writer", MarkdownProjectWriter(output_dir=settings.output_dir))
    p.connect("detector.detail", "detail_fetcher.refs")
    p.connect("detail_fetcher.pages", "extractor.pages")
    p.connect("extractor.records", "writer.records")
    p.connect("detector.thin", "writer.thin")
    p.connect("detail_fetcher.unchanged", "writer.unchanged")
    p.connect("detector.delisted", "writer.delisted")
    p.connect("discoverer.programme_docs", "writer.programme_docs")
    return p


def load_pipeline(yaml_text: str) -> Pipeline:
    return Pipeline.loads(yaml_text, allowed_modules=ALLOWED_MODULES)
```

- [ ] **Step 6: Write `src/tsi_agent/runner.py`**

```python
"""Exécution par lots : le pipeline est relancé jusqu'à épuisement des projets à traiter."""

from typing import Any, Callable

from haystack import Pipeline

from tsi_agent import runlog
from tsi_agent.config import Settings
from tsi_agent.pipeline import build_pipeline

PipelineFactory = Callable[..., Pipeline]


def run_extraction(settings: Settings, sources: dict[str, Any], *, full: bool = False,
                   countries: list[str] | None = None, limit: int | None = None, chunk_size: int | None = None,
                   dry_run: bool = False, model: str | None = None, run_id: str | None = None,
                   pipeline_factory: PipelineFactory = build_pipeline) -> dict[str, Any]:
    chunk = chunk_size or settings.chunk_size
    with runlog.open_run(settings.logs_dir, run_id) as run:
        pipeline = pipeline_factory(settings, sources, dry_run=dry_run, model=model)
        budget = limit
        chunks = 0
        quota = False
        written: dict[str, int] = {}
        while True:
            chunk_limit = None if dry_run else (chunk if budget is None else min(chunk, budget))
            data: dict[str, Any] = {
                "listing": {"countries": countries},
                "detector": {"run_id": run.run_id, "full": full, "limit": chunk_limit},
            }
            if not dry_run:
                data["detail_fetcher"] = {"full": full}
                data["writer"] = {"run_id": run.run_id}
            out = pipeline.run(data, include_outputs_from={"detector", "extractor", "writer"})
            chunks += 1
            det = out["detector"]
            if dry_run:
                run.extra["dry_run"] = {"detail_candidates": len(det["detail"]), "thin_to_write": len(det["thin"]),
                                        "delisted": len(det["delisted"])}
                break
            for key, value in out.get("writer", {}).get("stats", {}).items():
                written[key] = written.get(key, 0) + value
            processed = len(det["detail"])
            if budget is not None:
                budget -= processed
            quota = bool(out.get("extractor", {}).get("quota_exceeded"))
            if quota or det["remaining"] == 0 or processed == 0 or (budget is not None and budget <= 0):
                run.extra["remaining_detail"] = det["remaining"]
                break
        run.extra.update({"chunks": chunks, "quota_exceeded": quota, "written": written})
    return run.final_summary
```

- [ ] **Step 7: Write `src/tsi_agent/__main__.py`**

```python
"""CLI : `python -m tsi_agent run ...` et `python -m tsi_agent export-yaml pipeline.yaml`."""

import argparse
import json
import logging
import sys
from pathlib import Path

from tsi_agent.config import load_settings, load_sources
from tsi_agent.countries import COUNTRIES
from tsi_agent.pipeline import build_pipeline
from tsi_agent.runner import run_extraction


def _countries(value: str) -> list[str]:
    isos = [c.strip().upper() for c in value.split(",") if c.strip()]
    unknown = [c for c in isos if c not in COUNTRIES]
    if unknown:
        raise argparse.ArgumentTypeError(f"codes pays inconnus : {', '.join(unknown)}")
    return isos


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="tsi_agent")
    parser.add_argument("--settings", default="config/settings.yaml")
    parser.add_argument("--sources", default="config/sources.yaml")
    sub = parser.add_subparsers(dest="command", required=True)
    run = sub.add_parser("run", help="extraire les projets nouveaux ou modifiés")
    run.add_argument("--full", action="store_true", help="ré-extraire toutes les pages détail")
    run.add_argument("--countries", type=_countries, help="codes ISO, ex. FR,HR (exclut le PDF SRSP)")
    run.add_argument("--limit", type=int, help="nombre maximal d'appels LLM (projets détaillés)")
    run.add_argument("--chunk-size", type=int)
    run.add_argument("--dry-run", action="store_true", help="découverte seule, sans LLM ni écriture")
    run.add_argument("--model")
    export = sub.add_parser("export-yaml", help="exporter le pipeline en YAML (import deepset)")
    export.add_argument("path")
    args = parser.parse_args(argv)
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")

    logging.basicConfig(level=logging.WARNING)
    settings = load_settings(args.settings)
    sources = load_sources(args.sources)
    if args.command == "export-yaml":
        Path(args.path).write_text(build_pipeline(settings, sources).dumps(), encoding="utf-8")
        print(f"Pipeline exporté vers {args.path}")
        return 0
    summary = run_extraction(settings, sources, full=args.full, countries=args.countries, limit=args.limit,
                             chunk_size=args.chunk_size, dry_run=args.dry_run, model=args.model)
    print(json.dumps(summary, indent=2, ensure_ascii=False))
    return 1 if summary.get("quota_exceeded") else 0


if __name__ == "__main__":
    sys.exit(main())
```

- [ ] **Step 8: Run the tests to verify they pass**

Run: `.venv\Scripts\python -m pytest tests/test_pipeline.py -v`
Expected: 5 passed

- [ ] **Step 9: Commit**

```powershell
git add src/tsi_agent/components/__init__.py src/tsi_agent/config.py src/tsi_agent/pipeline.py src/tsi_agent/runner.py src/tsi_agent/__main__.py tests/test_pipeline.py
git commit -m "feat: assemblage du pipeline, execution par lots et CLI"
```


### Task 14: README, full suite and real smoke run

**Files:**
- Create: `README.md`

- [ ] **Step 1: Write `README.md`**

````markdown
# TSI Extraction Agent (DG REFORM)

Premier agent du pipeline Haystack pour la Commission européenne. Il crawle les sites publics du
Technical Support Instrument (TSI) et de son prédécesseur, le SRSP, et produit **un fichier Markdown
par projet** dans `output/projects/`, avec les métadonnées dans un front matter YAML. On peut le relancer
régulièrement : il ne traite que les projets nouveaux ou modifiés.

Design : [docs/superpowers/specs/2026-09-30-tsi-extraction-agent-design.md](docs/superpowers/specs/2026-09-30-tsi-extraction-agent-design.md)

## Installation

```powershell
python -m venv .venv
.venv\Scripts\python -m pip install -e ".[dev]"
```

Pour l'exécution locale, le CLI `claude` doit être installé et connecté à une souscription Claude
(`claude --version`). Aucune clé API n'est nécessaire.

## Utilisation

```powershell
# Pilote : Croatie seulement, 3 appels LLM au maximum
.venv\Scripts\python -m tsi_agent run --countries HR --limit 3

# Voir ce qui serait extrait, sans LLM ni écriture
.venv\Scripts\python -m tsi_agent run --dry-run

# Run complet incrémental (le premier fait environ 620 appels LLM et dure plusieurs heures)
.venv\Scripts\python -m tsi_agent run

# Tout ré-extraire, même les pages inchangées
.venv\Scripts\python -m tsi_agent run --full

# Exporter le pipeline pour deepset Studio
.venv\Scripts\python -m tsi_agent export-yaml pipeline.yaml
```

Un run interrompu (Ctrl-C, limite de souscription) reprend là où il s'était arrêté au run suivant.

## Sorties

| Chemin | Contenu |
|---|---|
| `output/projects/<project_id>.md` | Un fichier par projet : front matter (pays, année, programme, sources, hash, run) + Summary / Context / Support delivered / Results achieved |
| `output/programme/*.md` | Documents programme : chiffres par pays, domaines politiques, performance TSI |
| `output/state/index.json` | État utilisé pour l'incrémental. Ne pas éditer à la main. |
| `logs/runs/<run_id>.jsonl` | Journal : une action par ligne (fetch, classification, appel LLM avec tokens et coût, écriture…) |
| `logs/runs/<run_id>.summary.json` | Résumé du run, aussi affiché en fin de run |
| `logs/costs.csv` | Une ligne par run : appels LLM, tokens, coût |

Avec la souscription, `cost_usd` est un **coût équivalent API** (notionnel), calculé par le CLI.
Il sert à estimer le budget du passage à l'API.

## Planification (Windows)

```powershell
schtasks /Create /TN "TSI Extraction Agent" /SC WEEKLY /D MON /ST 07:00 /TR "cmd /c cd /d \"%CD%\" && .venv\Scripts\python -m tsi_agent run >> logs\scheduled.log 2>&1"
```

Code de sortie `1` : le run s'est arrêté sur la limite de la souscription. Le suivant reprendra.

## Passage à deepset Studio (API OpenAI)

Le pipeline est un pipeline Haystack standard (`export-yaml`). En production, le LLM passe par l'API
OpenAI : dans le YAML, on remplace le `chat_generator` de `extractor` par
`haystack.components.generators.chat.openai.OpenAIChatGenerator` :

```yaml
extractor:
  type: tsi_agent.components.extractor.ProjectExtractor
  init_parameters:
    chat_generator:
      type: haystack.components.generators.chat.openai.OpenAIChatGenerator
      init_parameters:
        model: gpt-4o-mini
        api_key: {type: env_var, env_vars: [OPENAI_API_KEY], strict: true}
        generation_kwargs:
          response_format: {type: json_object}
    pricing:
      gpt-4o-mini: {input: 0.15, output: 0.60}   # $ / million de tokens, pour journaliser le coût
```

Le reste ne change pas. Le composant de stockage local (`LocalProjectStore`) sera remplacé par une
implémentation qui écrit dans l'espace de travail deepset (hors périmètre de cette version).
Le package `tsi_agent` doit être installé côté deepset en tant que composant custom. Au rechargement
d'un YAML, Haystack 3 exige `allowed_modules=["tsi_agent"]` (voir `tsi_agent.pipeline.load_pipeline`).

## Tests

```powershell
.venv\Scripts\python -m pytest -q
```

Les fixtures de `tests/fixtures/` sont des copies réelles des pages du site (septembre 2026).
````

- [ ] **Step 2: Run the whole suite**

Run: `.venv\Scripts\python -m pytest -q`
Expected: `67 passed`

- [ ] **Step 3: Real dry run on all sources (no LLM, ~4 min at 2 s/request)**

Run: `.venv\Scripts\python -m tsi_agent run --dry-run`
Expected: a JSON summary with `dry_run.detail_candidates` ≈ 617, `dry_run.thin_to_write` ≈ 1423, `llm_calls: 0`, and one alert `Programme document tsi-performance has no content`. A few `resolve:deferred` or `fetch:retry` counts are acceptable if the site rate-limits; `resolve:thin` should not come from HTTP 429 errors.

- [ ] **Step 4: Real pilot run (2 LLM calls on the subscription)**

Run: `.venv\Scripts\python -m tsi_agent run --countries HR --limit 2 --model haiku`
Expected: `llm_calls: 2`, `written.detail: 2`, `written.thin` ≈ 31, `written.programme_docs` ≈ 11, `cost_usd` ≈ 0.04, exit code 0. Then open one `output/projects/*.md` with `detail_page: true` and check the front matter and the four sections read correctly.

- [ ] **Step 5: Check the run is incremental**

Run: `.venv\Scripts\python -m tsi_agent run --countries HR --dry-run`
Expected: `dry_run.thin_to_write: 0`.

- [ ] **Step 6: Export the pipeline YAML**

Run: `.venv\Scripts\python -m tsi_agent export-yaml pipeline.yaml`
Expected: `pipeline.yaml` exists and contains `tsi_agent.components.claude_cli.ClaudeCliChatGenerator`. Delete it afterwards; it is regenerated on demand.

- [ ] **Step 7: Commit**

```powershell
git add README.md
git commit -m "docs: README (usage, planification, passage a deepset/OpenAI)"
```

---

## After the plan

- First full run: `python -m tsi_agent run` (about 620 LLM calls; it takes hours and may hit subscription limits — it resumes on the next run). Check `logs/costs.csv` for the notional cost, to budget the OpenAI API.
- Out of scope, next specs: `DeepsetWorkspaceStore` (writing to the deepset workspace), the RAG chatbot agent, the impact analysis agent.
