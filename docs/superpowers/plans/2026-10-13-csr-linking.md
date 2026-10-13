# Liaison projets → CSR : plan d'implémentation (plan 3 sur 3)

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Objectif :** relier chaque projet aux sous-parties de CSR qu'il soutient, avec un niveau (`direct` ou `partial`) et une justification, grâce au CLI Claude en lot. Ces liens deviennent ensuite :
- une section du texte du projet ;
- une métadonnée `csrs` ;
- un filtre `csr` de `list_projects`, en local et sur la plateforme ;
- une anomalie `no_csr_link` ;
- un échantillon CSV à faire valider par les métiers.

**Architecture :** un module `src/tsi_agent/csr_link.py`, construit sur le modèle de `components/merge_judge.py`, qui fait trois choses :
1. **Choix des candidates.** Pour chaque projet, il retient les sous-parties des CSR de ses pays adoptées entre y−3 et y−1. Les CSR sont reconstruites depuis `output/csr/csr_full.xlsx` avec `csr.build_csrs`.
2. **Appel au LLM.** Un appel `ClaudeCliChatGenerator` à sortie JSON par projet, enveloppé dans `LoggedChatGenerator` pour que les coûts soient journalisés. Le cache est `output/csr_links/<stem>.json` : un projet est sauté si `content_hash` et `candidates_hash` n'ont pas changé. Si le quota est atteint, le lot s'arrête proprement.
3. **Écriture dans le fichier projet.** La section `## Supported CSRs (AI-assessed)` et le front matter `csrs` sont écrits de façon idempotente, à la manière de la section des livrables.

Le reste se branche sur ce module : le découpage (`AI_SECTIONS`, `PROJECT_FIELDS`), `prod_export` (`META_TYPES`), `ProjectLister` et le composant de plateforme `list_summary`, les prompts, `anomalies` et la CLI `csr-link`.

**Tech stack :** Python 3.12, Haystack (`ChatMessage`), CLI Claude, pytest.

**Spec :** `docs/superpowers/specs/2026-10-12-csr-linking-anomalies-design.md`, section B.

**Précisions par rapport à la spec :**
- **Classement des candidates pour un projet de plus de 2 pays.** On garde les 15 sous-parties les plus proches par pays selon la similarité de Jaccard des mots du titre et du début du texte (`fuzzy.prefix_tokens`), et non selon l'embedding e5 : c'est déterministe et ne demande ni modèle ni Qdrant. Ce classement ne sert qu'à réduire le prompt, c'est le LLM qui juge.
- **Un projet sans année ou sans pays** reçoit le statut `no_candidates` et n'est pas envoyé au LLM.
- **Une CSR sans sous-partie** devient elle-même une candidate, avec `part_id = csr_id`. Cela n'arrive pas dans les données réelles.

**Conventions :**
- Tests : `.venv/Scripts/python -m pytest -q -p no:cacheprovider`. Ils écrivent dans `tmp_path`, jamais dans `output/`, et n'appellent jamais le vrai CLI : un faux générateur le remplace.
- Commits en français, terminés par `Co-Authored-By: Claude Opus 5.5 (1M context) <noreply@anthropic.com>`.
- Le code des modules de plateforme (`src/tsi_agent/platform_code/*.py`) doit définir une seule classe `Code`, sans fonction imbriquée dans `run()`, et n'importer que `haystack`, `typing`, `collections` et `re`. Ces règles sont vérifiées par `tests/test_platform_code.py`.

---

### Task 1 : candidates et entrée du projet

**Fichiers :**
- à créer : `src/tsi_agent/csr_link.py`
- test : `tests/test_csr_link.py`

- [ ] **Step 1 : écrire les tests, qui doivent échouer**

```python
import json

from haystack.dataclasses import ChatMessage

from tsi_agent.csr import build_csrs
from tsi_agent.csr_link import (SECTION_HEADING, Candidate, candidates_hash, index_parts, project_input,
                                select_candidates)


def row(country="Croatia", year="2022", nbr="1.1", text="Reform the public administration.", areas="Public administration",
        version="2023 - Semester Cycle (May)", assessment="Some Progress"):
    return {"Country": country, "Year": year, "CSR Nbr": nbr, "Version": version, "Text": text,
            "Policy Area(s)": areas, "Assessment": assessment}


ROWS = [
    row(nbr="1", text="Reform the public administration. Improve the business environment."),
    row(nbr="1.1", text="Reform the public administration."),
    row(nbr="1.2", text="Improve the business environment.", areas="Business environment"),
    row(year="2020", nbr="2.1", text="Strengthen the health system.", areas="Healthcare"),
    row(year="2024", nbr="1.1", text="Too recent for a 2024 project."),
    row(country="Slovenia", year="2023", nbr="3.1", text="Reform pensions.", areas="Pensions"),
]


def parts():
    return index_parts(build_csrs(ROWS, year_from=2016, year_to=2025))


def test_index_parts_by_country_and_year():
    idx = parts()
    assert sorted(idx) == [("HR", 2020), ("HR", 2022), ("HR", 2024), ("SI", 2023)]
    hr = idx[("HR", 2022)]
    assert [c.part_id for c in hr] == ["csr-HR-2022-1.1", "csr-HR-2022-1.2"]
    assert hr[0] == Candidate(part_id="csr-HR-2022-1.1", csr_id="csr-HR-2022-1", country="HR", year=2022,
                              text="Reform the public administration.", policy_areas=["Public administration"],
                              assessment="Some Progress")


def test_window_is_three_years_before_the_project():
    fm = {"project_id": "p", "title": "Public administration reform", "year": 2024, "countries": ["HR", "SI"]}
    ids = [c.part_id for c in select_candidates(fm, "", parts())]
    assert ids == ["csr-HR-2022-1.1", "csr-HR-2022-1.2", "csr-SI-2023-3.1"]  # 2021-2023 ; ni 2020 ni 2024


def test_no_year_or_country_gives_no_candidates():
    assert select_candidates({"project_id": "p", "title": "x", "year": None, "countries": ["HR"]}, "", parts()) == []
    assert select_candidates({"project_id": "p", "title": "x", "year": 2024, "countries": []}, "", parts()) == []


def test_many_countries_keep_closest_parts_per_country(monkeypatch):
    import tsi_agent.csr_link as mod

    monkeypatch.setattr(mod, "MAX_PER_COUNTRY", 1)
    fm = {"project_id": "p", "title": "Business environment", "year": 2024, "countries": ["HR", "SI", "GR"]}
    ids = [c.part_id for c in select_candidates(fm, "improve the business environment", parts())]
    assert ids == ["csr-HR-2022-1.2", "csr-SI-2023-3.1"]


def test_candidates_hash_ignores_order():
    a, b = parts()[("HR", 2022)]
    assert candidates_hash([a, b]) == candidates_hash([b, a]) != candidates_hash([a])


def test_project_input_detail_and_title_only():
    body = ("# Title\n\nIntro text.\n\n## Summary (AI-generated)\n\nSummary.\n\n## Objectives\n\nGoals.\n\n"
            f"{SECTION_HEADING}\n\n- old link\n\n## Deliverables (AI-generated summary)\n\nDeliv.\n\n"
            "## Sources\n\n- https://x")
    text, basis = project_input({"title": "Title", "detail_page": True}, body)
    assert basis == "full_text"
    assert "Intro text." in text and "Goals." in text and "Summary." in text
    assert "old link" not in text and "https://x" not in text and "Deliv." not in text
    text, basis = project_input({"title": "Thin", "detail_page": False, "listing_titles": ["Thin", "Thin bis"]},
                                "# Thin\n\n## Sources\n\n- u")
    assert basis == "title_only" and text == "Title: Thin\nListed as: Thin; Thin bis"
```

- [ ] **Step 2 : vérifier qu'ils échouent**

Run : `.venv/Scripts/python -m pytest tests/test_csr_link.py -q -p no:cacheprovider`
Expected : FAIL, `ModuleNotFoundError: No module named 'tsi_agent.csr_link'`.

- [ ] **Step 3 : implémenter le début de `src/tsi_agent/csr_link.py`**

```python
"""Liaison projet -> sous-parties de CSR par LLM (CLI Claude, sortie JSON), en lot avec cache et reprise.

Candidates d'un projet d'annee y : sous-parties des CSR de ses pays adoptees de y-WINDOW a y-1 (une demande TSI
de l'annee y est deposee a l'automne de y-1, sur les CSR des annees precedentes). Au-dela de 2 pays : les
MAX_PER_COUNTRY plus proches par pays (Jaccard des mots, deterministe) pour borner le prompt ; le LLM juge.
Decisions dans output/csr_links/<stem>.json (rejouees sans LLM tant que le projet et ses candidates ne changent
pas) ; liens ecrits dans le fichier projet : front matter `csrs` et section SECTION_HEADING.
"""

import hashlib
import re
from collections import defaultdict
from dataclasses import dataclass

from tsi_agent.chunking import split_sections
from tsi_agent.csr import Csr
from tsi_agent.fuzzy import prefix_tokens

WINDOW = 3
MAX_PER_COUNTRY = 15
MANY_COUNTRIES = 2
MAX_LINKS = 5
PROJECT_CHARS = 6000
LINKS_DIR = "csr_links"
SECTION_HEADING = "## Supported CSRs (AI-assessed)"
SECTION_NAME = SECTION_HEADING.removeprefix("## ")
# Sections du fichier projet exclues de l'entree du LLM : listing, livrables et liens CSR deja ecrits.
_SKIP = {"Sources", "Listed in country factsheets as", "Deliverables (AI-generated summary)", SECTION_NAME}


@dataclass(frozen=True)
class Candidate:
    part_id: str
    csr_id: str
    country: str
    year: int
    text: str
    policy_areas: list[str]
    assessment: str | None


def index_parts(csrs: list[Csr]) -> dict[tuple[str, int], list[Candidate]]:
    """Sous-parties candidates par (pays, annee) ; une CSR sans sous-partie est sa propre candidate."""
    idx: dict[tuple[str, int], list[Candidate]] = defaultdict(list)
    for csr in csrs:
        parts = csr.subparts or ([csr.full] if csr.full else [])
        for p in parts:
            idx[(csr.country, csr.year)].append(Candidate(p.part_id, csr.csr_id, csr.country, csr.year, p.text,
                                                          list(p.policy_areas), p.assessment))
    return dict(idx)


def _jaccard(a: frozenset[str], b: frozenset[str]) -> float:
    return len(a & b) / len(a | b) if a and b else 0.0


def select_candidates(fm: dict, text: str, idx: dict[tuple[str, int], list[Candidate]]) -> list[Candidate]:
    """Candidates d'un projet (front matter + texte d'entree), par pays puis annee puis numero."""
    year, countries = fm.get("year"), list(dict.fromkeys(fm.get("countries") or []))
    if not isinstance(year, int) or not countries:
        return []
    tokens = prefix_tokens(f"{fm.get('title') or ''} {text[:2000]}")
    out: list[Candidate] = []
    for country in countries:
        found = [c for y in range(year - WINDOW, year) for c in idx.get((country, y), [])]
        if len(countries) > MANY_COUNTRIES and len(found) > MAX_PER_COUNTRY:
            ranked = sorted(found, key=lambda c: -_jaccard(tokens, prefix_tokens(c.text)))
            keep = {c.part_id for c in ranked[:MAX_PER_COUNTRY]}
            found = [c for c in found if c.part_id in keep]
        out.extend(found)
    return out


def candidates_hash(cands: list[Candidate]) -> str:
    return "sha1:" + hashlib.sha1("|".join(sorted(c.part_id for c in cands)).encode("utf-8")).hexdigest()


def project_input(fm: dict, body: str) -> tuple[str, str]:
    """(texte transmis au LLM, base) : sections officielles et resume si page detail, sinon titres seuls."""
    if not fm.get("detail_page"):
        listed = [t for t in fm.get("listing_titles") or [] if t]
        lines = [f"Title: {fm.get('title')}"] + ([f"Listed as: {'; '.join(listed)}"] if listed else [])
        return "\n".join(lines), "title_only"
    parts = [f"{name}: {text}" for name, text in split_sections(body) if name not in _SKIP]
    return re.sub(r"[ \t]+", " ", "\n\n".join(parts))[:PROJECT_CHARS], "full_text"
```

Dans `test_project_input_detail_and_title_only`, le texte de l'introduction est précédé de `Introduction: `. L'assertion `"Intro text." in text` reste donc vraie.

- [ ] **Step 4 : vérifier qu'ils passent**

Run : `.venv/Scripts/python -m pytest tests/test_csr_link.py -q -p no:cacheprovider`
Expected : les 6 tests passent.

Dans `test_many_countries_keep_closest_parts_per_country`, HR a 2 candidates pour un maximum de 1, et on garde « business environment ». SI n'en a qu'une, gardée telle quelle. GR n'en a aucune.

- [ ] **Step 5 : commit**

```bash
git add src/tsi_agent/csr_link.py tests/test_csr_link.py
git commit -m "feat(csr-link): candidates CSR d'un projet (fenetre y-3..y-1, pays, plafond multi-pays) et entree du LLM"
```

---

### Task 2 : appel au LLM, validation, cache et section du projet

**Fichiers :**
- modifié : `src/tsi_agent/csr_link.py`
- test : `tests/test_csr_link.py`

- [ ] **Step 1 : ajouter les tests**

Ajouter à l'import du test : `LINK_SCHEMA, apply_links, build_prompt, link_project, links_section, parse_links`.

```python
class FakeGenerator:
    def __init__(self, replies):
        self.replies = list(replies)
        self.prompts = []

    def run(self, messages):
        self.prompts.append(messages[-1].text)
        reply = self.replies.pop(0)
        if isinstance(reply, Exception):
            raise reply
        return {"replies": [ChatMessage.from_assistant(json.dumps(reply), meta={"model": "claude-cli/sonnet"})]}


def hr_candidates():
    return parts()[("HR", 2022)]


def test_schema_and_prompt():
    assert LINK_SCHEMA["required"] == ["links"]
    prompt = build_prompt({"title": "Public administration reform", "programme": "TSI", "year": 2024,
                           "countries": ["HR"]}, "Intro: x", "full_text", hr_candidates())
    assert "csr-HR-2022-1.1 (Croatia 2022; Public administration; latest assessment: Some Progress): " \
           "Reform the public administration." in prompt
    assert "Title: Public administration reform" in prompt and "TSI 2024" in prompt and "Intro: x" in prompt
    assert "only the title" not in prompt
    assert "only the title" in build_prompt({"title": "T", "year": 2024, "countries": ["HR"]}, "Title: T",
                                            "title_only", hr_candidates())


def test_parse_links_keeps_valid_unique_and_caps():
    cands = hr_candidates()
    raw = {"links": [
        {"csr_id": "csr-HR-2022-1.1", "level": "direct", "rationale": "Reforms the administration."},
        {"csr_id": "csr-HR-2022-1.1", "level": "partial", "rationale": "dup"},
        {"csr_id": "csr-XX-2022-9.9", "level": "direct", "rationale": "unknown"},
        {"csr_id": "csr-HR-2022-1.2", "level": "weak", "rationale": "bad level"},
        {"csr_id": "csr-HR-2022-1.2", "level": "partial", "rationale": " Helps firms. "}]}
    assert parse_links(json.dumps(raw), cands) == [
        {"csr_id": "csr-HR-2022-1.1", "level": "direct", "rationale": "Reforms the administration."},
        {"csr_id": "csr-HR-2022-1.2", "level": "partial", "rationale": "Helps firms."}]
    cands6 = index_parts(build_csrs([row(nbr=f"1.{i}", text=f"T{i}.") for i in range(1, 8)]))[("HR", 2022)]
    many = {"links": [{"csr_id": c.part_id, "level": "partial", "rationale": "r"} for c in cands6]}
    assert len(parse_links(json.dumps(many), cands6)) == 5


def test_link_project_record():
    gen = FakeGenerator([{"links": [{"csr_id": "csr-HR-2022-1.1", "level": "direct", "rationale": "R."}]}])
    fm = {"project_id": "p", "title": "Public administration reform", "year": 2024, "countries": ["HR"],
          "detail_page": True, "content_hash": "sha256:abc"}
    rec = link_project(gen, fm, "# P\n\nIntro.", hr_candidates())
    assert rec["status"] == "ok" and rec["basis"] == "full_text" and rec["content_hash"] == "sha256:abc"
    assert rec["candidates_hash"] == candidates_hash(hr_candidates()) and rec["model"] == "claude-cli/sonnet"
    assert rec["links"] == [{"csr_id": "csr-HR-2022-1.1", "level": "direct", "rationale": "R."}]
    assert rec["candidates"] == ["csr-HR-2022-1.1", "csr-HR-2022-1.2"]


def test_links_section_and_apply_is_idempotent():
    rec = {"basis": "title_only", "links": [{"csr_id": "csr-HR-2022-1.1", "level": "direct", "rationale": "R."}]}
    section = links_section(rec)
    assert section.startswith(SECTION_HEADING + "\n\n")
    assert "- csr-HR-2022-1.1 (direct): R." in section and "based on the project title only" in section
    assert links_section({"basis": "full_text", "links": []}) is None
    text = "---\nproject_id: p\ntitle: P\nresults: achieved\n---\n\n# P\n\nIntro.\n\n## Sources\n\n- u\n"
    once = apply_links(text, rec)
    assert once == apply_links(once, rec)
    assert "csrs:\n- csr-HR-2022-1.1\n" in once
    assert once.index(SECTION_HEADING) < once.index("## Sources")
    cleared = apply_links(once, {"basis": "full_text", "links": []})
    assert "csrs" not in cleared and SECTION_HEADING not in cleared and "## Sources" in cleared
```

- [ ] **Step 2 : vérifier qu'ils échouent**

Run : `.venv/Scripts/python -m pytest tests/test_csr_link.py -q -p no:cacheprovider`
Expected : FAIL, `ImportError` sur `LINK_SCHEMA`.

- [ ] **Step 3 : implémenter**

Imports à ajouter en tête de `csr_link.py` :

```python
import json
from datetime import date
from typing import Any

from haystack.dataclasses import ChatMessage

from tsi_agent.countries import name_from_iso
from tsi_agent.render import _TAIL_HEADINGS, parse_front_matter, render
```

Code, à ajouter après `project_input` :

```python
LINK_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {"links": {"type": "array", "items": {
        "type": "object",
        "properties": {"csr_id": {"type": "string"}, "level": {"type": "string", "enum": ["direct", "partial"]},
                       "rationale": {"type": "string"}},
        "required": ["csr_id", "level", "rationale"]}}},
    "required": ["links"],
}

SYSTEM_PROMPT = ("You link European Commission technical support projects (DG REFORM: TSI, SRSP) to the "
                 "country-specific recommendations (CSRs) of the European Semester. You answer only from the "
                 "texts given.")

INSTRUCTIONS = f"""Below are a technical support project and CANDIDATE CSR sub-parts addressed to its \
country or countries in the years before the request. Say which CSR sub-parts the project supports.

- "direct": the project's objective is to design or implement what the CSR sub-part recommends.
- "partial": the project contributes to one aspect of the CSR sub-part, or to a precondition for it.
- Do not link a CSR sub-part that only shares a broad theme (e.g. "public finances") without a concrete \
connection to what the project does. Most projects support 0 to 3 sub-parts; return at most {MAX_LINKS}.

For each link give csr_id (a candidate id exactly as written), level, and a one-sentence rationale that names \
what the project does. Return an empty list when no candidate is supported."""


def build_prompt(fm: dict, text: str, basis: str, cands: list[Candidate]) -> str:
    countries = ", ".join(name_from_iso(c) for c in fm.get("countries") or [])
    head = [f"Title: {fm.get('title')}",
            f"Programme and year: {' '.join(str(v) for v in (fm.get('programme'), fm.get('year')) if v)}",
            f"Countries: {countries}"]
    if basis == "title_only":
        head.append("Note: only the title of this project is published; link only when the title makes the "
                    "connection clear.")
    lines = [f"- {c.part_id} ({name_from_iso(c.country)} {c.year}; {'; '.join(c.policy_areas) or 'no area'}; "
             f"latest assessment: {c.assessment or 'none'}): {c.text}" for c in cands]
    return "\n\n".join([INSTRUCTIONS, "## Project", "\n".join(head), text, "## Candidate CSR sub-parts",
                        "\n".join(lines)])


def parse_links(text: str, cands: list[Candidate]) -> list[dict[str, str]]:
    """Liens valides : id parmi les candidates, niveau connu, sans doublon, au plus MAX_LINKS, ordre du LLM."""
    start, end = text.find("{"), text.rfind("}")
    if start < 0 or end < start:
        raise ValueError("no JSON object in reply")
    raw = json.loads(text[start:end + 1]).get("links")
    if not isinstance(raw, list):
        raise ValueError("no links list")
    known = {c.part_id for c in cands}
    out: list[dict[str, str]] = []
    for item in raw:
        if not isinstance(item, dict):
            continue
        cid, level = item.get("csr_id"), item.get("level")
        if cid in known and level in ("direct", "partial") and cid not in {o["csr_id"] for o in out}:
            out.append({"csr_id": cid, "level": level, "rationale": " ".join(str(item.get("rationale") or "").split())})
    return out[:MAX_LINKS]


def link_project(generator: Any, fm: dict, body: str, cands: list[Candidate]) -> dict[str, Any]:
    """Enregistrement de liaison d'un projet (un appel LLM). Exceptions du generateur propagees (quota...)."""
    text, basis = project_input(fm, body)
    reply = generator.run(messages=[ChatMessage.from_system(SYSTEM_PROMPT),
                                    ChatMessage.from_user(build_prompt(fm, text, basis, cands))])["replies"][0]
    return {"project_id": fm.get("project_id"), "status": "ok", "basis": basis,
            "content_hash": fm.get("content_hash"), "candidates_hash": candidates_hash(cands),
            "candidates": [c.part_id for c in cands], "links": parse_links(reply.text or "", cands),
            "model": (reply.meta or {}).get("model"), "date": date.today().isoformat()}


def links_section(record: dict[str, Any]) -> str | None:
    links = record.get("links") or []
    if not links:
        return None
    lines = [SECTION_HEADING, "",
             "Country-specific recommendations (European Semester) that this project supports, as assessed by an "
             "AI model from the project text" + (" (based on the project title only)" if record.get("basis") ==
                                                 "title_only" else "") + ":", ""]
    lines += [f"- {link['csr_id']} ({link['level']}): {link['rationale']}" for link in links]
    return "\n".join(lines)


def _replace_section(body: str, section: str | None) -> str:
    """Retire la section des liens existante et insere `section` avant la partie listing (ou a la fin)."""
    lines = body.split("\n")
    if SECTION_HEADING in lines:
        start = lines.index(SECTION_HEADING)
        end = next((i for i in range(start + 1, len(lines)) if lines[i].startswith("## ")), len(lines))
        lines = lines[:start] + lines[end:]
    cut = next((i for i, line in enumerate(lines) if line in _TAIL_HEADINGS), len(lines))
    head, tail = "\n".join(lines[:cut]).strip("\n"), "\n".join(lines[cut:]).strip("\n")
    return "\n\n".join(p for p in (head, section, tail) if p)


def apply_links(text: str, record: dict[str, Any]) -> str:
    """Texte du fichier projet avec `csrs` (front matter) et la section des liens a jour ; idempotent."""
    fm, body = parse_front_matter(text.replace("\r\n", "\n"))
    ids = [link["csr_id"] for link in record.get("links") or []]
    fm = {k: v for k, v in fm.items() if k != "csrs"}
    if ids:
        fm["csrs"] = ids
    return render(fm, _replace_section(body, links_section(record)))
```

`_TAIL_HEADINGS` est privé dans `render.py`. On le réutilise tel quel, comme le fait déjà `replace_deliverables_section` dans le même module ; c'est la même règle de placement.

- [ ] **Step 4 : vérifier qu'ils passent**

Run : `.venv/Scripts/python -m pytest tests/test_csr_link.py -q -p no:cacheprovider`
Expected : les 10 tests passent.

- [ ] **Step 5 : commit**

```bash
git add src/tsi_agent/csr_link.py tests/test_csr_link.py
git commit -m "feat(csr-link): appel LLM a sortie JSON, validation des liens, section et front matter csrs du projet"
```

---

### Task 3 : lot avec cache, reprise, quota et échantillon, puis la commande `csr-link`

**Fichiers :**
- modifié : `src/tsi_agent/csr_link.py`, `src/tsi_agent/__main__.py`
- test : `tests/test_csr_link.py`, `tests/test_cli.py`

- [ ] **Step 1 : ajouter les tests**

Ajouter à l'import du test `run_linking, write_sample`, puis `from tsi_agent.components.claude_cli import LLMQuotaExceeded` et `from tsi_agent.config import Settings`.

```python
PROJECT = """---
project_id: {pid}
title: Public administration reform {pid}
programme: TSI
year: 2024
countries:
- HR
detail_page: true
content_hash: sha256:{pid}
---

# Public administration reform {pid}

Intro {pid}.

## Sources

- https://example.eu/{pid}
"""


def setup_output(tmp_path, pids=("a", "b", "c")):
    out = tmp_path / "output"
    (out / "projects").mkdir(parents=True)
    for pid in pids:
        (out / "projects" / f"{pid}.md").write_text(PROJECT.format(pid=pid), encoding="utf-8")
    (out / "projects" / "nocountry.md").write_text(
        PROJECT.format(pid="nocountry").replace("countries:\n- HR\n", ""), encoding="utf-8")
    settings = Settings(output_dir=str(out), logs_dir=str(tmp_path / "logs"))
    return out, settings


def answer(cid="csr-HR-2022-1.1"):
    return {"links": [{"csr_id": cid, "level": "direct", "rationale": "R."}]}


def test_run_links_caches_and_resumes(tmp_path):
    out, settings = setup_output(tmp_path)
    csrs = build_csrs(ROWS, year_from=2016, year_to=2025)
    gen = FakeGenerator([answer(), LLMQuotaExceeded("limit reached")])
    s1 = run_linking(settings, csrs, gen, concurrency=1)
    assert s1["quota_exceeded"] is True and s1["linked"] == 1 and s1["no_candidates"] == 1
    assert sorted(p.name for p in (out / "csr_links").glob("*.json")) == ["a.json", "nocountry.json"]
    assert "csrs:\n- csr-HR-2022-1.1" in (out / "projects" / "a.md").read_text(encoding="utf-8")
    gen2 = FakeGenerator([answer(), answer("csr-HR-2022-1.2")])
    s2 = run_linking(settings, csrs, gen2, concurrency=1)
    assert s2["linked"] == 2 and s2["cached"] == 1 and s2["no_candidates"] == 1 and s2["quota_exceeded"] is False and len(gen2.prompts) == 2
    s3 = run_linking(settings, csrs, FakeGenerator([]), concurrency=1)
    assert s3["linked"] == 0 and s3["cached"] == 3 and s3["no_candidates"] == 1


def test_changed_project_is_relinked_and_limit(tmp_path):
    out, settings = setup_output(tmp_path, pids=("a", "b"))
    csrs = build_csrs(ROWS, year_from=2016, year_to=2025)
    run_linking(settings, csrs, FakeGenerator([answer(), answer()]), concurrency=1)
    path = out / "projects" / "a.md"
    path.write_text(path.read_text(encoding="utf-8").replace("sha256:a", "sha256:a2"), encoding="utf-8")
    gen = FakeGenerator([answer("csr-HR-2022-1.2")])
    s = run_linking(settings, csrs, gen, concurrency=1, limit=1)
    assert s["linked"] == 1 and "csr-HR-2022-1.2" in path.read_text(encoding="utf-8")


def test_bad_reply_is_skipped_not_cached(tmp_path):
    out, settings = setup_output(tmp_path, pids=("a",))
    csrs = build_csrs(ROWS, year_from=2016, year_to=2025)
    s = run_linking(settings, csrs, FakeGenerator([{"oops": 1}, {"oops": 1}]), concurrency=1)
    assert s["failed"] == ["a"] and not (out / "csr_links" / "a.json").exists()


def test_write_sample(tmp_path):
    out, settings = setup_output(tmp_path)
    csrs = build_csrs(ROWS, year_from=2016, year_to=2025)
    run_linking(settings, csrs, FakeGenerator([answer(), answer(), answer()]), concurrency=1)
    path = write_sample(settings, csrs, 2, tmp_path / "sample.csv")
    import csv as _csv
    with path.open(encoding="utf-8-sig", newline="") as fh:
        rows = list(_csv.DictReader(fh))
    assert len({r["project_id"] for r in rows}) == 2
    assert list(rows[0]) == ["project_id", "title", "programme", "year", "countries", "basis", "csr_id", "level",
                             "rationale", "csr_text", "csr_assessment", "verdict"]
    assert rows[0]["csr_text"] == "Reform the public administration." and rows[0]["verdict"] == ""
```

Dans `tests/test_cli.py` :

```python
def test_csr_link_without_export_is_a_clean_error(tmp_path, capsys):
    settings = tmp_path / "settings.yaml"
    settings.write_text(f"output_dir: {(tmp_path / 'output').as_posix()}\n", encoding="utf-8")
    assert cli.main(["--settings", str(settings), "csr-link"]) == 1
    assert "csr-fetch" in capsys.readouterr().err
```

- [ ] **Step 2 : vérifier qu'ils échouent**

Run : `.venv/Scripts/python -m pytest tests/test_csr_link.py tests/test_cli.py -q -p no:cacheprovider`
Expected : FAIL, `ImportError` sur `run_linking`, et `invalid choice: 'csr-link'`.

- [ ] **Step 3 : implémenter**

Imports à ajouter dans `csr_link.py` :

```python
import csv
import os
import random
import threading
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from tsi_agent import runlog
from tsi_agent.components.claude_cli import LLMQuotaExceeded
from tsi_agent.config import Settings
from tsi_agent.store import _retry_on_lock
```

Code :

```python
NAME = "CsrLinker"


def _is_quota(exc: BaseException) -> bool:
    return isinstance(exc, LLMQuotaExceeded) or "RateLimit" in type(exc).__name__


def _write_json(path: Path, data: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    payload = json.dumps(data, indent=1, ensure_ascii=False)

    def write() -> None:
        tmp.write_text(payload, encoding="utf-8")
        os.replace(tmp, path)

    _retry_on_lock(write)


def _read_json(path: Path) -> dict[str, Any] | None:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None


def _apply(path: Path, record: dict[str, Any]) -> bool:
    text = path.read_text(encoding="utf-8")
    new = apply_links(text, record)
    if new == text.replace("\r\n", "\n"):
        return False
    _retry_on_lock(lambda: path.write_text(new, encoding="utf-8"))
    return True


def run_linking(settings: Settings, csrs: list[Csr], generator: Any, *, concurrency: int | None = None,
                limit: int | None = None, project_ids: list[str] | None = None) -> dict[str, Any]:
    """Lie les projets (nouveaux ou modifies) a leurs CSR, puis ecrit les liens de tous les projets deja lies.

    S'arrete proprement sur quota (relancer reprend). Un projet dont la reponse est illisible (2 essais) est
    saute et liste dans `failed`, sans enregistrement : il sera redemande au prochain run.
    """
    root = Path(settings.output_dir)
    links_dir = root / LINKS_DIR
    idx = index_parts(csrs)
    summary: dict[str, Any] = {"projects": 0, "linked": 0, "cached": 0, "no_candidates": 0, "failed": [],
                               "files_updated": 0, "quota_exceeded": False}
    todo: list[tuple[Path, dict, str, list[Candidate]]] = []
    for path in sorted((root / "projects").glob("*.md")):
        try:
            fm, body = parse_front_matter(path.read_text(encoding="utf-8").replace("\r\n", "\n"))
        except ValueError:
            continue
        if not isinstance(fm, dict) or not fm.get("project_id"):
            continue
        if project_ids and fm["project_id"] not in project_ids:
            continue
        summary["projects"] += 1
        text, _ = project_input(fm, body)
        cands = select_candidates(fm, text, idx)
        record_path = links_dir / f"{path.stem}.json"
        stored = _read_json(record_path)
        if not cands:
            if not stored or stored.get("status") != "no_candidates":
                _write_json(record_path, {"project_id": fm["project_id"], "status": "no_candidates",
                                          "content_hash": fm.get("content_hash"), "links": [],
                                          "date": date.today().isoformat()})
            summary["no_candidates"] += 1
            continue
        if stored and stored.get("status") == "ok" and stored.get("content_hash") == fm.get("content_hash") \
                and stored.get("candidates_hash") == candidates_hash(cands):
            summary["cached"] += 1
            continue
        todo.append((path, fm, body, cands))
    if limit is not None:
        todo = todo[:limit]
    stop = threading.Event()
    lock = threading.Lock()

    def work(item: tuple[Path, dict, str, list[Candidate]]) -> None:
        path, fm, body, cands = item
        if stop.is_set():
            return
        for attempt in (1, 2):
            try:
                record = link_project(generator, fm, body, cands)
                break
            except ValueError as exc:  # reponse illisible : un second essai
                if attempt == 2:
                    runlog.event(NAME, "error", fm["project_id"], "bad_reply", error=repr(exc)[:300])
                    with lock:
                        summary["failed"].append(fm["project_id"])
                    return
            except Exception as exc:
                if _is_quota(exc):
                    stop.set()
                    runlog.event(NAME, "error", fm["project_id"], "quota_exceeded", error=str(exc)[:300])
                else:
                    runlog.event(NAME, "error", fm["project_id"], "llm_failed", error=repr(exc)[:300])
                    with lock:
                        summary["failed"].append(fm["project_id"])
                return
        _write_json(links_dir / f"{path.stem}.json", record)
        runlog.event(NAME, "link", fm["project_id"], "ok", links=len(record["links"]), basis=record["basis"])
        with lock:
            summary["linked"] += 1

    with ThreadPoolExecutor(max_workers=max(1, concurrency or settings.max_concurrency)) as pool:
        list(pool.map(work, todo))
    summary["quota_exceeded"] = stop.is_set()
    summary["failed"] = sorted(summary["failed"])
    for path in sorted((root / "projects").glob("*.md")):
        record = _read_json(links_dir / f"{path.stem}.json")
        if record and record.get("status") in ("ok", "no_candidates") and _apply(path, record):
            summary["files_updated"] += 1
    return summary


SAMPLE_COLUMNS = ["project_id", "title", "programme", "year", "countries", "basis", "csr_id", "level",
                  "rationale", "csr_text", "csr_assessment", "verdict"]


def write_sample(settings: Settings, csrs: list[Csr], n: int, out: Path, seed: int = 42) -> Path:
    """Echantillon de n projets lies (au moins un lien), stratifie par programme et annee, graine fixe : une ligne
    par lien, colonne `verdict` vide pour la relecture metier."""
    root = Path(settings.output_dir)
    parts = {c.part_id: c for cands in index_parts(csrs).values() for c in cands}
    linked: dict[tuple, list[tuple[dict, dict]]] = defaultdict(list)
    for path in sorted((root / LINKS_DIR).glob("*.json")):
        record = _read_json(path)
        project = root / "projects" / f"{path.stem}.md"
        if not record or not record.get("links") or not project.exists():
            continue
        fm, _ = parse_front_matter(project.read_text(encoding="utf-8").replace("\r\n", "\n"))
        linked[(fm.get("programme"), fm.get("year"))].append((fm, record))
    rng = random.Random(seed)
    strata = sorted(linked, key=lambda k: (str(k[0]), k[1] or 0))
    for key in strata:
        rng.shuffle(linked[key])
    chosen: list[tuple[dict, dict]] = []
    while len(chosen) < n and any(linked[k] for k in strata):
        for key in strata:
            if linked[key] and len(chosen) < n:
                chosen.append(linked[key].pop())
    out.parent.mkdir(parents=True, exist_ok=True)
    with out.open("w", encoding="utf-8-sig", newline="") as fh:
        writer = csv.DictWriter(fh, fieldnames=SAMPLE_COLUMNS)
        writer.writeheader()
        for fm, record in chosen:
            for link in record["links"]:
                part = parts.get(link["csr_id"])
                writer.writerow({"project_id": fm.get("project_id"), "title": fm.get("title"),
                                 "programme": fm.get("programme"), "year": fm.get("year"),
                                 "countries": ",".join(fm.get("countries") or []), "basis": record.get("basis"),
                                 "csr_id": link["csr_id"], "level": link["level"], "rationale": link["rationale"],
                                 "csr_text": part.text if part else "", "csr_assessment": part.assessment if part else "",
                                 "verdict": ""})
    return out
```

Ensuite, la CLI dans `src/tsi_agent/__main__.py`. Le parseur se place après `csr-build` :

```python
    link = sub.add_parser("csr-link", help="lier les projets aux CSR (LLM, CLI Claude ; cache, reprise sur quota)")
    link.add_argument("--limit", type=int, help="nombre maximal de projets envoyés au LLM")
    link.add_argument("--project", action="append", dest="projects", help="id de projet (répétable)")
    link.add_argument("--sample", type=int, help="écrire un échantillon de N projets liés (relecture métier)")
    link.add_argument("--model", help="modèle du CLI Claude (défaut : réglage model)")
```

Le bloc de traitement se place après celui de `csr-build` :

```python
    if args.command == "csr-link":
        from tsi_agent import runlog
        from tsi_agent.chat.generator import LoggedChatGenerator
        from tsi_agent.components.claude_cli import ClaudeCliChatGenerator
        from tsi_agent.csr import build_csrs, read_rows
        from tsi_agent.csr_link import LINK_SCHEMA, run_linking, write_sample
        source = Path(settings.output_dir) / "csr" / "csr_full.xlsx"
        if not source.exists():
            print(f"ERREUR : {source} introuvable — lancer d'abord `python -m tsi_agent csr-fetch`", file=sys.stderr)
            return 1
        csrs = build_csrs(read_rows(source))
        if args.sample:
            out = Path(settings.output_dir) / "reports" / "csr_links_sample.csv"
            print(f"Échantillon : {write_sample(settings, csrs, args.sample, out)}")
            return 0
        generator = LoggedChatGenerator(chat_generator=ClaudeCliChatGenerator(
            model=args.model or settings.model, json_schema=LINK_SCHEMA, timeout_s=settings.llm_timeout_s))
        with runlog.open_run(settings.logs_dir) as run:
            summary = run_linking(settings, csrs, generator, limit=args.limit, project_ids=args.projects)
            run.extra.update({"mode": "csr_link", **{k: v for k, v in summary.items() if k != "failed"},
                              "failed": summary["failed"][:50]})
        print(json.dumps({**summary, "run": run.final_summary.get("run_id") if run.final_summary else None,
                          "cost_usd": (run.final_summary or {}).get("cost_usd")}, indent=2, ensure_ascii=False))
        return 1 if summary["quota_exceeded"] else 0
```

Vérifier le nom réel de l'attribut de résumé du run dans `runlog.py`. Le plan utilise `run.final_summary`, défini dans `open_run`. Si `run_id` n'est pas une clé du résumé, utiliser `run.run_id`.

- [ ] **Step 4 : vérifier qu'ils passent**

Run : `.venv/Scripts/python -m pytest -q -p no:cacheprovider`
Expected : toute la suite passe.

Dans `test_run_links_caches_and_resumes`, les projets sont traités dans l'ordre des fichiers (a, b, c, nocountry) :
- **1er run** : « nocountry » reçoit `no_candidates` pendant la préparation. a est lié, puis b atteint le quota, et c n'est pas traité.
- **2e run** : b et c sont liés, a est en cache (`cached == 1`) et nocountry reste compté dans `no_candidates`.
- **3e run** : a, b et c sont en cache (`cached == 3`).

- [ ] **Step 5 : commit**

```bash
git add src/tsi_agent/csr_link.py src/tsi_agent/__main__.py tests/test_csr_link.py tests/test_cli.py
git commit -m "feat(csr-link): lot avec cache, reprise sur quota, echantillon CSV et commande csr-link"
```

---

### Task 4 : métadonnée `csrs` dans les morceaux, en production et dans `list_projects`

**Fichiers :**
- modifiés :
  - `src/tsi_agent/chunking.py` : `PROJECT_FIELDS` et `AI_SECTIONS` ;
  - `src/tsi_agent/prod_export.py` : `META_TYPES` et `file_meta` ;
  - `src/tsi_agent/chat/tools.py` : `ProjectLister` ;
  - `src/tsi_agent/chat/prompt.py` ;
  - `src/tsi_agent/platform_code/list_summary.py` ;
  - `deploy/prod/build_pipelines.py`, ainsi que les YAML de chat régénérés.
- tests : `tests/test_chunking.py`, `tests/test_prod_export.py`, `tests/test_chat_store_tools.py`, `tests/test_platform_code.py`

- [ ] **Step 1 : tests**

`tests/test_chunking.py` :

```python
def test_csr_links_section_is_ai_generated_and_csrs_in_meta():
    text = ("---\nproject_id: p\ntitle: P\ncountries: [HR]\nyear: 2024\ncsrs: [csr-HR-2022-1.1]\n---\n\n# P\n\n"
            "Intro.\n\n## Supported CSRs (AI-assessed)\n\n- csr-HR-2022-1.1 (direct): R.\n")
    docs = chunk_file("projects/p.md", text)
    sec = [d for d in docs if d.meta["section"] == "Supported CSRs (AI-assessed)"]
    assert len(sec) == 1 and sec[0].meta["ai_generated"] is True
    assert all(d.meta["csrs"] == ["csr-HR-2022-1.1"] for d in docs)
```

`tests/test_prod_export.py` : ajouter un test qui exporte un projet avec `csrs: [csr-HR-2022-1.1]` dans le front matter. Partir de la constante `PROJECT` du fichier en y insérant la ligne `csrs: [csr-HR-2022-1.1]`. Le test vérifie que `meta["csrs"] == ["csr-HR-2022-1.1"]` et `meta_type_problems == []`, et qu'un projet sans `csrs` n'a pas la clé.

`tests/test_chat_store_tools.py` :

```python
def test_lister_csr_filter(tmp_path):
    store = open_store(str(tmp_path), 8)
    docs = [Document(id=f"projects/{pid}#0", content="x", meta={
        "doc_id": pid, "doc_type": "project", "title": pid, "countries": ["HR"], "year": 2024, "programme": "TSI",
        "csrs": csrs}) for pid, csrs in (("a", ["csr-HR-2022-1.1"]), ("b", ["csr-HR-2022-10.1"]), ("c", None))]
    store.write_documents(MockDocumentEmbedder(model="mock", dimension=8).run(documents=docs)["documents"])
    lister = ProjectLister(document_store=store)
    assert lister.run(csr="csr-HR-2022-1")["result"].startswith("Total matching projects: 1 ")
    assert lister.run(csr="csr-HR-2022")["result"].startswith("Total matching projects: 2 ")
    assert lister.run(csr="csr-HR-2022-1.1")["result"].startswith("Total matching projects: 1 ")
```

`tests/test_platform_code.py` : ajouter, dans le style des tests `list_summary` existants, un appel `run(documents=..., csr="csr-HR-2022-1")`. Il doit renvoyer 1 projet sur des documents dont `meta.csrs` vaut `["csr-HR-2022-1.1"]` et `["csr-HR-2022-10.1"]`.

- [ ] **Step 2 : vérifier qu'ils échouent**

Run : `.venv/Scripts/python -m pytest tests/test_chunking.py tests/test_prod_export.py tests/test_chat_store_tools.py tests/test_platform_code.py -q -p no:cacheprovider`
Expected : FAIL sur les nouveaux tests.

- [ ] **Step 3 : implémenter**

Dans `chunking.py` :

```python
CSR_LINKS = "Supported CSRs (AI-assessed)"
AI_SECTIONS = {SUMMARY, DELIVERABLES, CSR_LINKS}
```

Ajouter aussi `"csrs"` à la fin de `PROJECT_FIELDS`. Commentaire à placer près de `CHUNKER_VERSION` : « csrs et la section des liens CSR ajoutes sans changer CHUNKER_VERSION : presents dans les fichiers revectorises (csr-link reecrit les fichiers lies) ».

Dans `prod_export.py` :
- ajouter `"csrs": list` à `META_TYPES` ;
- dans `file_meta`, ajouter aux `candidates` :

```python
        "csrs": list(first.get("csrs") or []) or None if doc_type == "project" else None,
```

Ajouter les parenthèses explicites si l'analyseur de priorité gêne la lecture : `(list(first.get("csrs") or []) or None) if doc_type == "project" else None`.

Dans `chat/tools.py`, ajouter la fonction au niveau du module :

```python
def csr_matches(csrs: Any, wanted: str) -> bool:
    """`wanted` : id de CSR ou de sous-partie, ou prefixe pays-annee ("csr-HR-2022") ; pas de faux prefixe
    (csr-HR-2022-1 ne trouve pas csr-HR-2022-10.1)."""
    wanted = wanted.strip()
    return any(c == wanted or c.startswith(wanted + ".") or c.startswith(wanted + "-") for c in csrs or [])
```

Dans `ProjectLister.run` :
- ajouter le paramètre `csr: str | None = None`, à placer après `title_contains` ;
- documenter : `:param csr: CSR id or prefix (e.g. "csr-HR-2022-1", "csr-HR-2022-1.2" or "csr-HR-2022" for all 2022 CSRs of Croatia): only projects linked to it (AI-assessed links).` ;
- dans la boucle, ajouter `if csr and not csr_matches(doc.meta.get("csrs"), csr): continue` ;
- ajouter `csr=csr` à `runlog.event`.

Dans `platform_code/list_summary.py`, ajouter la même fonction au niveau du module, sous le nom `_csr_matches`. Le validateur interdit les fonctions imbriquées et les imports autres que `haystack`, `typing`, `collections` et `re`. Puis :
- ajouter le paramètre `csr: Optional[str] = None` à `run` ;
- ajouter le filtre `if csr and not _csr_matches(meta.get("csrs"), csr): continue` après celui de `title_contains`.

Dans `deploy/prod/build_pipelines.py`, `list_projects_tool()` :
- dans `params["properties"]`, ajouter :

```python
        "csr": {"type": "string", "description": "CSR id or prefix, e.g. csr-HR-2022-1, csr-HR-2022-1.2 or "
                                                 "csr-HR-2022: only projects linked to it (AI-assessed links)."},
```

- dans `input_mapping`, ajouter `"csr": ["summary.csr"]`.

Dans `CHAT_PROMPT`, ajouter `csr` à la liste des filtres de `list_projects` et compléter la règle 14 :

```
    To list the projects linked to a CSR, call list_projects with csr (a CSR id or a prefix such as
    csr-HR-2022); these links are assessed by an AI model: say so.
```

Faire de même dans le prompt local, `chat/prompt.py`, règle 16. `CHAT_PROMPT_SEARCH_ONLY` ne change pas : il n'a pas `list_projects`. Le fichier projet y porte de toute façon la section des liens, que `search_projects` peut trouver.

Régénérer : `.venv/Scripts/python deploy/prod/build_pipelines.py`. Valider ensuite `pipeline-tsi-chat.yaml` avec le script de brouillon :

```bash
EXTRA_PKGS='C:\tmp\hk1;C:\tmp\hk2' .venv/Scripts/python "C:/Users/MGE/AppData/Local/Temp/claude/c--Users-MGE-OneDrive---Agilos-Documents-Claude-Code-Projects-DG-REFORM-Haystack-Pipeline/f8efdd32-c9a5-4bb5-90b9-b6388ae91288/scratchpad/check/validate_yaml.py" deploy/prod/pipeline-tsi-chat.yaml
```

- [ ] **Step 4 : vérifier qu'ils passent**

Run : `.venv/Scripts/python -m pytest -q -p no:cacheprovider`
Expected : tout passe. Le validateur YAML répond « OK ».

- [ ] **Step 5 : commit**

```bash
git add src/tsi_agent/chunking.py src/tsi_agent/prod_export.py src/tsi_agent/chat/tools.py src/tsi_agent/chat/prompt.py src/tsi_agent/platform_code/list_summary.py deploy/prod/build_pipelines.py deploy/prod/pipeline-tsi-chat.yaml deploy/prod/pipeline-tsi-chat-ui.yaml tests/
git commit -m "feat(csr-link): metadonnee csrs (morceaux, production) et filtre csr de list_projects (local et plateforme)"
```

---

### Task 5 : anomalie `no_csr_link`

**Fichiers :**
- modifié : `src/tsi_agent/anomalies.py`
- test : `tests/test_anomalies.py`

- [ ] **Step 1 : test**

```python
def test_no_csr_link_only_for_processed_published_tsi():
    projects = [_project("a"), _project("b"), _project("c", programme="SRSP"), _project("d")]
    links = {"a": [], "b": ["csr-GR-2021-1.1"], "c": []}  # d : pas encore traite
    rows = [r for r in detect(projects, [], 2026, csr_links=links) if r.type == "no_csr_link"]
    assert [(r.project_id, r.severity) for r in rows] == [("a", "info")]
```

Un second test passe par `run()` avec un dossier `output/csr_links/a.json` contenant `{"status": "ok", "links": []}`. Il vérifie que `no_csr_link` apparaît dans le résumé.

- [ ] **Step 2 : vérifier qu'il échoue**

Run : `.venv/Scripts/python -m pytest tests/test_anomalies.py -q -p no:cacheprovider`
Expected : FAIL, `unexpected keyword argument 'csr_links'`.

- [ ] **Step 3 : implémenter**

Dans `anomalies.py` :

```python
def load_csr_links(output_dir: Path) -> dict[str, list[str]]:
    """Liens CSR des projets deja traites par csr-link (statut ok), par project_id."""
    out: dict[str, list[str]] = {}
    for path in sorted((output_dir / "csr_links").glob("*.json")):
        try:
            record = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        if isinstance(record, dict) and record.get("status") == "ok" and record.get("project_id"):
            out[record["project_id"]] = [link.get("csr_id") for link in record.get("links") or []]
    return out
```

Dans `detect` :
- ajouter le paramètre `csr_links: dict[str, list[str]] | None = None` ;
- avant le tri, ajouter :

```python
    for p in projects:
        if (csr_links is not None and p["project_id"] in csr_links and not csr_links[p["project_id"]]
                and p.get("programme") == "TSI" and p.get("detail_page")):
            rows.append(_row(p, "no_csr_link", "info", "no CSR sub-part linked by csr-link"))
```

Dans `run`, appeler `detect(..., csr_links=load_csr_links(output_dir))`.

- [ ] **Step 4 : vérifier qu'ils passent**

Run : `.venv/Scripts/python -m pytest -q -p no:cacheprovider`. Expected : tout passe.

- [ ] **Step 5 : commit**

```bash
git add src/tsi_agent/anomalies.py tests/test_anomalies.py
git commit -m "feat(anomalies): no_csr_link (projet TSI publie traite par csr-link sans aucun lien)"
```

---

### Task 6 : exécution réelle (étape d'exploitation, faite par le contrôleur)

- [ ] **Step 1 : pilote.**
  - Lancer `.venv/Scripts/python -m tsi_agent csr-link --limit 20`.
  - Vérifier : 20 projets liés, le coût, aucun échec.
  - Lire 5 enregistrements `output/csr_links/*.json` et les sections correspondantes dans les fichiers projet.
  - Juger la qualité : nombre de liens, part des `direct`, justifications. Ajuster le prompt si nécessaire, **avant** le lot complet.
- [ ] **Step 2 : lot complet, seulement avec l'accord de l'utilisateur.**
  - Il couvre environ 2 150 projets, soit autant d'appels au CLI Claude, à environ 30 à 60 s par appel avec 2 en parallèle : **10 à 18 heures**, en plusieurs passes si le quota est atteint.
  - Lancer `csr-link` en arrière-plan. Le relancer après chaque arrêt sur quota.
- [ ] **Step 3 : échantillon.** Lancer `csr-link --sample 30`, ce qui produit `output/reports/csr_links_sample.csv` pour la relecture métier.
- [ ] **Step 4 : mises à jour.** Lancer `index`, avec le chatbot arrêté puis relancé, puis `anomalies`. Vérifier sur le chatbot local :
  - « Which projects support Croatia's 2022 CSRs? », qui doit passer par `list_projects` avec `csr="csr-HR-2022"` ;
  - « Which CSR does the Barnahus Slovenia project support? ».
- [ ] **Step 5 : README de production.** Ajouter `csrs` à la liste des métadonnées, mentionner le filtre `csr` et ajouter une question de recette.
