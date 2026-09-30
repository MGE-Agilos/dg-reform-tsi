# Étage « livrables » — Plan d'implémentation

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal :** un pipeline `deliverables` qui, pour chaque projet avec page détail, récupère ses publications (PDF), conserve leur texte intégral dans `output/deliverables/` et ajoute au fichier projet une section « Deliverables (AI-generated summary) », en incrémental.

**Architecture :** `PublicationDiscoverer` lit les pages détail dans le cache HTTP et relève les liens `/publications-0/`. `PublicationFetcher` parse les pages de publication et compare la liste des fichiers à `output/state/deliverables.json`. `DeliverableTextExtractor` télécharge les PDF sans cache et extrait leur texte avec pdfplumber. `DeliverableSummarizer` produit le résumé (même chat generator que l'extraction). `DeliverableWriter` met à jour l'état et la section des fichiers projet. Le writer d'extraction réinsère la section quand il réécrit un fichier. Le chunker vectorise la section (`ai_generated`).

**Tech stack :** Python 3.11+, haystack-ai 3.2, pdfplumber (déjà installé), httpx, pytest.

**Spec :** [docs/superpowers/specs/2026-09-30-tsi-deliverables-design.md](../specs/2026-09-30-tsi-deliverables-design.md)

**Conventions :**
- Racine : `C:\Users\MGE\OneDrive - Agilos\Documents\Claude Code Projects\DG REFORM\Haystack Pipeline`, branche `feature/deliverables`.
- Tests : `.venv/Scripts/python -m pytest -q` (239 passent, 2 désélectionnés).
- Chaque commit se termine par la ligne `Co-Authored-By: Claude Opus 5.5 (1M context) <noreply@anthropic.com>`, sans autre attribution.
- Ne rien modifier sous `output/`. Suivre le style du fichier modifié.

**Fixtures réelles (déjà dans `tests/fixtures/`) :**

| Fichier | Contenu |
|---|---|
| `detail_with_publication.html` | Page détail « Developing eHealth… Slovenia ». Un lien `/publications-0/developing-ehealth-healthier-society-slovenia_en` |
| `detail_hems_croatia.html` | Page détail HEMS. Un lien `/publications-0/establishment-helicopter-emergency-medical-services-croatia_en` |
| `pub_one_file.html` | Publication Slovénie : titre « Developing eHealth for a healthier society in Slovenia », date « 17 October 2023 », 1 document « Final - deliverable », URL `/document/download/6ab9d519-2de1-499d-870b-77ae9a1458ca_en?filename=Final%20-%20deliverable.pdf`, langue en, taille « (569.01 KB - PDF) » |
| `pub_two_languages.html` | 1 document « Final Assessment Report » : version principale `_en` + traduction `_sk` (`li.ecl-file__translation-item`) |
| `pub_three_files.html` | 3 documents en anglais : « Final Report », « Final Presentation », « Project Factsheet » |
| `deliverable_small.pdf` | Le vrai PDF de la Slovénie : 2 pages, ~597 mots, commence par « PROJECT OBJECTIVES » |

Structure HTML d'une publication :
- `main dt` / `dd` : « Publication date », « Author », « Country », « Funding Programme », « Year », « Final deliverable » ;
- `div.ecl-file` : un document, avec `.ecl-file__title`, le lien principal `a.ecl-file__download` et sa `.ecl-file__meta`, puis les traductions `li.ecl-file__translation-item`, chacune avec `a.ecl-file__translation-download` et `.ecl-file__translation-meta`.

La langue d'un fichier se lit dans son URL : `/document/download/<uuid>_<lang>?filename=...`.

## Structure des fichiers

| Fichier | Rôle |
|---|---|
| `src/tsi_agent/http.py` | + `PoliteFetcher.cached(url)` ; `get(url, cache=True)` |
| `src/tsi_agent/models.py` | + `PublicationFile`, `Publication`, `DeliverableFileText`, `DeliverableText`, `DeliverableContent`, `DeliverableRecord` |
| `src/tsi_agent/parsers/publication.py` (nouveau) | `publication_links`, `parse_publication`, `select_files`, `files_key` |
| `src/tsi_agent/deliverables_state.py` (nouveau) | `DeliverableEntry`, `DeliverableStore` (`output/state/deliverables.json`) |
| `src/tsi_agent/render.py` | + `DELIVERABLES_HEADING`, `deliverables_section`, `replace_deliverables_section`, `with_deliverables_count` |
| `src/tsi_agent/components/writer.py` | Réinsertion de la section et du compteur (`_write_record`, `_refresh`) |
| `src/tsi_agent/chunking.py` | `AI_SECTIONS`, `CHUNKER_VERSION = 2` |
| `src/tsi_agent/components/deliverables.py` (nouveau) | Les 5 composants |
| `src/tsi_agent/deliverables_pipeline.py` (nouveau) | `build_deliverables_pipeline`, `default_deliverables_generator` |
| `src/tsi_agent/runner.py`, `src/tsi_agent/__main__.py` | `run_deliverables`, commande `deliverables`, `export-yaml --deliverables` |
| `src/tsi_agent/config.py`, `config/settings.yaml`, `README.md` | Réglages, documentation |
| `tests/conftest.py` | `FakeFetcher` : `get(url, cache=True)`, `cached(url)` |
| `tests/test_publication.py`, `tests/test_deliverables.py` (nouveaux), `tests/test_http.py`, `tests/test_render.py`, `tests/test_detail_fetcher_writer.py`, `tests/test_chunking.py`, `tests/test_cli.py` | Tests |

---

### Task 1 : fetcher sans cache, modèles, parser de publication

**Files :** Modify `src/tsi_agent/http.py`, `src/tsi_agent/models.py`, `tests/conftest.py`, `tests/test_http.py`. Create `src/tsi_agent/parsers/publication.py`, `tests/test_publication.py`.

- [ ] **Step 1 : tests du fetcher (à ajouter à `tests/test_http.py`)**

Lire d'abord `tests/test_http.py` pour réutiliser sa façon de construire un `PoliteFetcher` avec `httpx.MockTransport` et un `sleep` factice. Ajouter :

```python
def test_cached_and_uncached_get(tmp_path):
    calls = []

    def handler(request):
        calls.append(str(request.url))
        return httpx.Response(200, content=b"%PDF-1.4 data")
    f = PoliteFetcher(cache_dir=tmp_path, min_interval_s=0, transport=httpx.MockTransport(handler),
                      respect_robots=False, sleep=lambda s: None)
    assert f.cached("https://x/a") is None
    f.get("https://x/a")
    assert f.cached("https://x/a") == b"%PDF-1.4 data"
    before = len(list(tmp_path.iterdir()))
    assert f.get("https://x/pdf", cache=False).content == b"%PDF-1.4 data"
    assert len(list(tmp_path.iterdir())) == before          # rien ecrit dans le cache
    f.get("https://x/pdf", cache=False)
    assert calls.count("https://x/pdf") == 2                 # ni memo ni cache
    assert f.cached("https://x/pdf") is None
```

- [ ] **Step 2 : implémentation dans `src/tsi_agent/http.py`**

Dans `PoliteFetcher`, section « API publique » :

1. Ajouter avant `get` :

```python
    def cached(self, url: str) -> bytes | None:
        """Contenu deja recupere (memo ou cache disque), sans requete reseau ; None si absent."""
        if url in self._memo:
            return self._memo[url].content
        _, body = self._cache_read(url)
        return body
```

2. Changer la signature en `def get(self, url: str, cache: bool = True) -> FetchResult:`, avec une docstring d'une ligne : « `cache=False` : ni memo, ni lecture ou ecriture du cache disque (gros fichiers, PDF). » Puis :
   - `if url in self._memo:` devient `if cache and url in self._memo:` ;
   - `meta, body = self._cache_read(url)` devient `meta, body = self._cache_read(url) if cache else (None, None)` ;
   - dans la branche `resp.status_code < 400`, n'appeler `self._cache_write(...)` et n'affecter `self._memo[url] = result` que si `cache` est vrai. La branche 304 ne peut pas se produire sans cache, puisque aucun en-tête conditionnel n'est envoyé.

- [ ] **Step 3 : `FakeFetcher` dans `tests/conftest.py`**

Modifier `FakeFetcher` :
- `__init__` accepte `cached_pages: dict[str, bytes] | None = None` (stocké dans `self.cached_pages`) et initialise `self.uncached_calls: list[str] = []` ;
- `get(self, url: str, cache: bool = True)` ajoute l'URL à `self.uncached_calls` quand `cache` est faux, le reste est inchangé ;
- ajouter la méthode :

```python
    def cached(self, url: str) -> bytes | None:
        return self.cached_pages.get(url)
```

- [ ] **Step 4 : modèles (fin de `src/tsi_agent/models.py`)**

```python
class PublicationFile(BaseModel):
    document: str  # titre du document sur la page de publication (ex. "Final Report")
    url: str
    language: str | None = None  # code ISO 639-1 lu dans l'URL (/document/download/<uuid>_<lang>)
    filename: str = ""
    size_label: str = ""  # ex. "(569.01 KB - PDF)"


class PublicationParse(BaseModel):
    title: str
    date: str | None = None
    meta: dict[str, str] = Field(default_factory=dict)
    documents: list[list[PublicationFile]] = Field(default_factory=list)  # par document : version principale d'abord


class Publication(BaseModel):
    publication_id: str
    url: str
    project_ids: list[str] = Field(default_factory=list)
    title: str = ""
    date: str | None = None
    meta: dict[str, str] = Field(default_factory=dict)
    files: list[PublicationFile] = Field(default_factory=list)  # fichiers retenus (anglais en priorite)
    files_key: str | None = None


class DeliverableFileText(BaseModel):
    file: PublicationFile
    pages: int
    words: int
    text: str


class DeliverableText(BaseModel):
    publication: Publication
    files: list[DeliverableFileText]
    full_text_path: str  # relatif a output_dir, ex. "deliverables/<id>.md"


class DeliverableContent(BaseModel):
    # Docstring en ASCII : le schema JSON est passe en argument de ligne de commande.
    """Content generated from the deliverables (final reports, presentations, factsheets) of a project."""

    summary: str
    key_outputs: list[str] = Field(default_factory=list)
    results: list[str] = Field(default_factory=list)
    figures: list[str] = Field(default_factory=list)


class DeliverableRecord(BaseModel):
    text: DeliverableText
    content: DeliverableContent
    model: str
```

- [ ] **Step 5 : tests du parser**

```python
# tests/test_publication.py
from conftest import fixture_bytes
from tsi_agent.models import PublicationFile, PublicationParse
from tsi_agent.parsers.publication import files_key, parse_publication, publication_links, select_files

SITE = "https://reforms-investments.ec.europa.eu"


def test_publication_links_from_detail_pages():
    assert publication_links(fixture_bytes("detail_with_publication.html")) == [
        f"{SITE}/publications-0/developing-ehealth-healthier-society-slovenia_en"]
    assert publication_links(fixture_bytes("detail_hems_croatia.html")) == [
        f"{SITE}/publications-0/establishment-helicopter-emergency-medical-services-croatia_en"]


def test_parse_one_file():
    p = parse_publication(fixture_bytes("pub_one_file.html"))
    assert p.title == "Developing eHealth for a healthier society in Slovenia"
    assert p.date == "17 October 2023" and p.meta["Country"] == "Slovenia"
    assert len(p.documents) == 1 and len(p.documents[0]) == 1
    f = p.documents[0][0]
    assert f.document == "Final - deliverable" and f.language == "en"
    assert f.url == f"{SITE}/document/download/6ab9d519-2de1-499d-870b-77ae9a1458ca_en?filename=Final%20-%20deliverable.pdf"
    assert f.filename == "Final - deliverable.pdf" and f.size_label == "(569.01 KB - PDF)"


def test_parse_translations_and_select_english():
    p = parse_publication(fixture_bytes("pub_two_languages.html"))
    assert [v.language for v in p.documents[0]] == ["en", "sk"]
    assert [f.language for f in select_files(p)] == ["en"]


def test_parse_three_documents():
    p = parse_publication(fixture_bytes("pub_three_files.html"))
    assert [d[0].document for d in p.documents] == ["Final Report", "Final Presentation", "Project Factsheet"]
    assert len(select_files(p)) == 3


def test_select_main_version_when_no_english():
    sk = PublicationFile(document="R", url="https://x/d/1_sk", language="sk")
    cs = PublicationFile(document="R", url="https://x/d/1_cs", language="cs")
    assert select_files(PublicationParse(title="t", documents=[[sk, cs]])) == [sk]


def test_files_key_is_order_independent():
    a = PublicationFile(document="A", url="https://x/1_en")
    b = PublicationFile(document="B", url="https://x/2_en")
    assert files_key([a, b]) == files_key([b, a]) != files_key([a])
```

- [ ] **Step 6 : lancer, échec attendu**

Run : `.venv/Scripts/python -m pytest -q tests/test_publication.py tests/test_http.py`
Attendu : `ModuleNotFoundError: No module named 'tsi_agent.parsers.publication'`, et échec du test du fetcher.

- [ ] **Step 7 : `src/tsi_agent/parsers/publication.py`**

```python
"""Pages de publication (livrables des projets) et liens de publication des pages detail."""

import re
from urllib.parse import parse_qs, unquote, urljoin, urlsplit

from tsi_agent.ids import clean_text, sha256_text
from tsi_agent.models import PublicationFile, PublicationParse
from tsi_agent.parsers import main_of, soup_of

SITE = "https://reforms-investments.ec.europa.eu"
_PUBLICATION_PATH = re.compile(r"^/publications-0/[^/?#]+$")
_LANG = re.compile(r"_([a-z]{2})$")


def publication_links(detail_html: bytes | str) -> list[str]:
    """Liens /publications-0/... du contenu principal d'une page detail (section « More about the project »)."""
    links: list[str] = []
    for a in main_of(soup_of(detail_html)).select("a[href]"):
        url = urljoin(SITE, a["href"].split("#")[0])
        parts = urlsplit(url)
        if parts.netloc == urlsplit(SITE).netloc and _PUBLICATION_PATH.match(parts.path) and url not in links:
            links.append(url)
    return links


def _file(document: str, a, size_label: str) -> PublicationFile:
    url = urljoin(SITE, a["href"])
    parts = urlsplit(url)
    lang = _LANG.search(parts.path)
    filename = parse_qs(parts.query).get("filename", [parts.path.rsplit("/", 1)[-1]])[0]
    return PublicationFile(document=document, url=url, language=lang.group(1) if lang else None,
                           filename=unquote(filename), size_label=size_label)


def parse_publication(html: bytes | str) -> PublicationParse:
    soup = soup_of(html)
    main = main_of(soup)
    h1 = soup.find("h1")
    meta: dict[str, str] = {}
    for dt in main.select("dt"):
        dd = dt.find_next("dd")
        if dd is not None:
            meta[clean_text(dt.get_text(" "))] = clean_text(dd.get_text(" "))
    documents: list[list[PublicationFile]] = []
    for block in main.select("div.ecl-file"):
        title_el = block.select_one(".ecl-file__title")
        document = clean_text(title_el.get_text(" ")) if title_el else ""
        variants: list[PublicationFile] = []
        primary = block.select_one("a.ecl-file__download")
        if primary is not None and primary.get("href"):
            size = block.select_one(".ecl-file__meta")
            variants.append(_file(document, primary, clean_text(size.get_text(" ")) if size else ""))
        for item in block.select("li.ecl-file__translation-item"):
            a = item.select_one("a.ecl-file__translation-download")
            if a is not None and a.get("href"):
                size = item.select_one(".ecl-file__translation-meta")
                variants.append(_file(document, a, clean_text(size.get_text(" ")) if size else ""))
        if variants:
            documents.append(variants)
    return PublicationParse(title=clean_text(h1.get_text(" ")) if h1 else "", date=meta.get("Publication date"),
                            meta=meta, documents=documents)


def select_files(parse: PublicationParse) -> list[PublicationFile]:
    """Par document : la version anglaise si elle existe, sinon la version principale."""
    return [next((v for v in variants if v.language == "en"), variants[0]) for variants in parse.documents]


def files_key(files: list[PublicationFile]) -> str:
    """Empreinte de la liste des fichiers : chaque URL porte un uuid, un fichier remplace change la cle."""
    return sha256_text("\n".join(sorted(f.url for f in files)))
```

Si `main_of` ne contient pas le bloc « Files » pour une page de publication, `test_parse_one_file` le révèle. Dans ce cas, chercher les `div.ecl-file` dans toute la page (`soup`) plutôt que dans `main`.

- [ ] **Step 8 : lancer, succès attendu, suite complète**

Run : `.venv/Scripts/python -m pytest -q tests/test_publication.py tests/test_http.py` puis `.venv/Scripts/python -m pytest -q`.

- [ ] **Step 9 : commit**

```bash
git add src/tsi_agent/http.py src/tsi_agent/models.py src/tsi_agent/parsers/publication.py tests/conftest.py tests/test_http.py tests/test_publication.py
git commit -m "feat: parser des pages de publication, fetcher sans cache pour les PDF

Co-Authored-By: Claude Opus 5.5 (1M context) <noreply@anthropic.com>"
```

---

### Task 2 : état des livrables, section du fichier projet, préservation par l'extraction, chunking

**Files :** Create `src/tsi_agent/deliverables_state.py`. Modify `src/tsi_agent/render.py`, `src/tsi_agent/components/writer.py`, `src/tsi_agent/chunking.py`. Tests : `tests/test_deliverables.py` (nouveau), `tests/test_detail_fetcher_writer.py`, `tests/test_chunking.py`.

- [ ] **Step 1 : tests**

```python
# tests/test_deliverables.py
from tsi_agent.deliverables_state import DeliverableEntry, DeliverableStore
from tsi_agent.models import DeliverableContent
from tsi_agent.render import (DELIVERABLES_HEADING, deliverables_section, replace_deliverables_section,
                              with_deliverables_count)


def entry(pid="pub-a", date="17 October 2023", projects=("hems",), summary="Delivered a strategy."):
    return DeliverableEntry(
        publication_id=pid, url=f"https://x/publications-0/{pid}_en", title=f"Title {pid}", date=date,
        project_ids=list(projects), files_key="sha256:k",
        files=[{"document": "Final Report", "url": "https://x/d/1_en", "language": "en",
                "filename": "r.pdf", "pages": 12, "words": 4000}],
        full_text_path=f"deliverables/{pid}.md",
        content=DeliverableContent(summary=summary, key_outputs=["Strategy"], results=["Adopted"],
                                   figures=["40+ stakeholders"]),
        model="claude-cli/x", processed="2026-09-30")


def test_store_round_trip_and_for_project(tmp_path):
    s = DeliverableStore(tmp_path)
    s.put(entry("old", date="1 March 2022"))
    s.put(entry("new", date="5 May 2025"))
    s.put(entry("other", projects=("x",)))
    s.save()
    again = DeliverableStore(tmp_path)
    assert again.get("old").title == "Title old"
    assert [e.publication_id for e in again.for_project("hems")] == ["new", "old"]  # plus recente d'abord
    assert again.for_project("nope") == []


def test_section_text():
    text = deliverables_section([entry()])
    assert text.startswith(DELIVERABLES_HEADING + "\n\n### Title pub-a (17 October 2023)")
    assert "Files: [Final Report (EN, 12 p.)](https://x/d/1_en)" in text
    assert "Full text: deliverables/pub-a.md" in text
    assert "Key outputs:\n- Strategy" in text and "Results:\n- Adopted" in text
    assert "Key figures:\n- 40+ stakeholders" in text
    assert deliverables_section([]) is None


BODY = "# HEMS\n\nIntro.\n\n## Summary (AI-generated)\n\nS.\n\n## Context\n\nC.\n\n## Sources\n\n- https://x"


def test_replace_section_inserts_before_tail_then_replaces_then_removes():
    once = replace_deliverables_section(BODY, "## Deliverables (AI-generated summary)\n\nA")
    assert once.index("## Context") < once.index(DELIVERABLES_HEADING) < once.index("## Sources")
    twice = replace_deliverables_section(once, "## Deliverables (AI-generated summary)\n\nB")
    assert twice.count(DELIVERABLES_HEADING) == 1 and "\n\nB\n\n## Sources" in twice and "\n\nA\n" not in twice
    assert replace_deliverables_section(twice, None) == BODY
    assert replace_deliverables_section("# T\n\nx", "## Deliverables (AI-generated summary)\n\nA").endswith("\n\nA")


def test_deliverables_count_follows_results():
    fm = {"project_id": "p", "results": "achieved", "spotlight": False}
    assert list(with_deliverables_count(fm, 2)) == ["project_id", "results", "deliverables", "spotlight"]
    assert "deliverables" not in with_deliverables_count({**fm, "deliverables": 3}, 0)
```

À ajouter à `tests/test_detail_fetcher_writer.py` (les helpers `writer`, `record` et `ref` y existent déjà) :

```python
def test_extraction_writer_keeps_deliverables_section(tmp_path):
    from test_deliverables import entry
    from tsi_agent.deliverables_state import DeliverableStore
    ds = DeliverableStore(tmp_path)
    ds.put(entry(projects=("hems",)))
    ds.save()
    store = LocalProjectStore(tmp_path)
    w = writer(store)
    w.run(run_id="r1", records=[record()])
    fm, body = parse_front_matter(store.read_project_file("hems"))
    assert fm["deliverables"] == 1 and "## Deliverables (AI-generated summary)" in body
    assert body.index("## Deliverables") < body.index("## Sources")
    w.run(run_id="r2", unchanged=[ref(spotlight=True)])            # rafraichissement du listing
    fm, body = parse_front_matter(store.read_project_file("hems"))
    assert fm["deliverables"] == 1 and body.count("## Deliverables (AI-generated summary)") == 1
```

À ajouter à `tests/test_chunking.py` :

```python
def test_deliverables_section_is_ai_generated():
    text = read("vec_detail_sections.md").replace(
        "## Sources", "## Deliverables (AI-generated summary)\n\n### Report (1 May 2025)\n\nDelivered X.\n\n## Sources")
    docs = chunk_file("projects/x.md", text)
    deliv = [d for d in docs if d.meta["section"] == "Deliverables (AI-generated summary)"]
    assert len(deliv) == 1 and deliv[0].meta["ai_generated"] is True and "Delivered X." in deliv[0].content
    assert CHUNKER_VERSION == 2
```

- [ ] **Step 2 : lancer, échec attendu**

Run : `.venv/Scripts/python -m pytest -q tests/test_deliverables.py tests/test_detail_fetcher_writer.py tests/test_chunking.py`

- [ ] **Step 3 : `src/tsi_agent/deliverables_state.py`**

```python
"""Etat de l'etage livrables : output/state/deliverables.json (une entree par publication traitee)."""

import json
import os
from datetime import datetime
from pathlib import Path
from typing import Any

from pydantic import BaseModel, Field

from tsi_agent.models import DeliverableContent
from tsi_agent.store import _retry_on_lock


class DeliverableEntry(BaseModel):
    publication_id: str
    url: str
    title: str
    date: str | None = None
    project_ids: list[str] = Field(default_factory=list)
    files_key: str
    files: list[dict[str, Any]] = Field(default_factory=list)  # document, url, language, filename, pages, words
    full_text_path: str
    content: DeliverableContent
    model: str
    processed: str


def _date_key(entry: DeliverableEntry) -> datetime:
    try:
        return datetime.strptime(entry.date or "", "%d %B %Y")
    except ValueError:
        return datetime.min


class DeliverableStore:
    def __init__(self, output_dir: str | Path):
        self.path = Path(output_dir) / "state" / "deliverables.json"
        data = json.loads(self.path.read_text(encoding="utf-8")) if self.path.exists() else {}
        self._entries = {k: DeliverableEntry.model_validate(v) for k, v in data.get("publications", {}).items()}

    def get(self, publication_id: str) -> DeliverableEntry | None:
        return self._entries.get(publication_id)

    def put(self, entry: DeliverableEntry) -> None:
        self._entries[entry.publication_id] = entry

    def for_project(self, project_id: str) -> list[DeliverableEntry]:
        """Publications d'un projet, la plus recente d'abord."""
        found = [e for e in self._entries.values() if project_id in e.project_ids]
        return sorted(found, key=lambda e: (_date_key(e), e.publication_id), reverse=True)

    def save(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        payload = json.dumps({"publications": {k: v.model_dump() for k, v in sorted(self._entries.items())}},
                             indent=1, ensure_ascii=False)
        tmp = self.path.with_suffix(".json.tmp")

        def write_and_replace() -> None:
            tmp.write_text(payload, encoding="utf-8")
            os.replace(tmp, self.path)

        _retry_on_lock(write_and_replace)
```

- [ ] **Step 4 : `src/tsi_agent/render.py`**

Après `SUMMARY_HEADING`, ajouter `DELIVERABLES_HEADING = "## Deliverables (AI-generated summary)"`. Après `replace_listing_tail`, ajouter :

```python
def deliverables_section(entries: list[Any]) -> str | None:
    """Section des livrables d'un projet (entrees DeliverableEntry, la plus recente d'abord) ; None si aucune."""
    if not entries:
        return None
    parts = [DELIVERABLES_HEADING]
    for e in entries:
        files = " · ".join(f"[{f.get('document') or f.get('filename')} ({(f.get('language') or '?').upper()}, "
                           f"{f.get('pages')} p.)]({f.get('url')})" for f in e.files)
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
```

Vérifier que `test_replace_section_inserts_before_tail_then_replaces_then_removes` redonne exactement `BODY` après retrait (jointure `\n\n`). Si des lignes vides en trop apparaissent, ajuster le `strip`, pas le test.

- [ ] **Step 5 : writer d'extraction (`src/tsi_agent/components/writer.py`)**

- Imports : `from tsi_agent.deliverables_state import DeliverableStore` ; ajouter `deliverables_section`, `replace_deliverables_section` et `with_deliverables_count` à l'import depuis `tsi_agent.render`.
- `__init__` : `self._deliverables: DeliverableStore | None = None`.
- Au début de `run(...)` : `self._deliverables = None`, pour relire l'état à chaque run, puisque l'étage livrables a pu l'écrire entre deux runs. Méthode d'accès (voir plus bas pour le dossier) :

```python
    def _deliverable_entries(self, project_id: str) -> list:
        if self._deliverables is None:
            # Dossier du store de projets (LocalProjectStore.root) : c'est aussi celui qu'injectent les tests.
            self._deliverables = DeliverableStore(getattr(self._get_store(), "root", self.output_dir))
        return self._deliverables.for_project(project_id)
```

- `_write_record` : remplacer la ligne `file = store.write_project_file(ref.project_id, render(fm, detail_body(ref, rec)))` par :

```python
        entries = self._deliverable_entries(ref.project_id)
        body = replace_deliverables_section(detail_body(ref, rec), deliverables_section(entries))
        file = store.write_project_file(ref.project_id, render(with_deliverables_count(fm, len(entries)), body))
```

- `_refresh`, branche `if ref is not None:` : après la ligne `fm["title"], fm["programme"], fm["year"] = ...`, ajouter `fm = with_deliverables_count(fm, int(old.get("deliverables") or 0))`. `replace_listing_tail` garde déjà la section, puisqu'elle précède la partie listing.

- Vérifier que `self.output_dir` existe sur `MarkdownProjectWriter` (paramètre d'init). Les tests créent le writer avec `MarkdownProjectWriter()` et remplacent `_store` : `DeliverableStore(self.output_dir)` pointerait alors sur `output/` par défaut. Pour que `test_extraction_writer_keeps_deliverables_section` lise bien `tmp_path`, le writer doit prendre le dossier du store quand il est injecté : `DeliverableStore(getattr(self._get_store(), "root", self.output_dir))`. `LocalProjectStore.root` est le dossier de sortie.

- [ ] **Step 6 : `src/tsi_agent/chunking.py`**

- `CHUNKER_VERSION = 2`, avec en commentaire : `# 2 : section Deliverables (resume IA des livrables)`.
- Après `SUMMARY`, ajouter `DELIVERABLES = "Deliverables (AI-generated summary)"` et `AI_SECTIONS = {SUMMARY, DELIVERABLES}`.
- Dans `chunk_file`, `"ai_generated": section == SUMMARY` devient `"ai_generated": section in AI_SECTIONS`.

- [ ] **Step 7 : lancer, succès attendu, suite complète**

Run : `.venv/Scripts/python -m pytest -q`. Attendu : tout vert. `tests/test_vector_layer.py` utilise `CHUNKER_VERSION - 1` et reste valide.

- [ ] **Step 8 : commit**

```bash
git add src/tsi_agent/deliverables_state.py src/tsi_agent/render.py src/tsi_agent/components/writer.py src/tsi_agent/chunking.py tests/test_deliverables.py tests/test_detail_fetcher_writer.py tests/test_chunking.py
git commit -m "feat: etat des livrables, section Deliverables des fichiers projet (preservee par l'extraction), chunking

Co-Authored-By: Claude Opus 5.5 (1M context) <noreply@anthropic.com>"
```

---

### Task 3 : découverte, pages de publication, texte des PDF

**Files :** Create `src/tsi_agent/components/deliverables.py`. Test : `tests/test_deliverables.py`.

- [ ] **Step 1 : tests (à ajouter à `tests/test_deliverables.py`)**

```python
import pytest

from conftest import FakeFetcher, fixture_bytes
from tsi_agent import runlog
from tsi_agent.components.deliverables import DeliverableTextExtractor, PublicationDiscoverer, PublicationFetcher
from tsi_agent.models import Publication, PublicationFile
from tsi_agent.render import parse_front_matter
from tsi_agent.store import LocalProjectStore, StoreEntry

SITE = "https://reforms-investments.ec.europa.eu"
SLO_DETAIL = f"{SITE}/technical-support-instrument-0/health-and-long-term-care/developing-ehealth-healthier-society-slovenia_en"
HEMS_DETAIL = f"{SITE}/technical-support-instrument-0/health-and-long-term-care/hems_en"
SLO_PUB = f"{SITE}/publications-0/developing-ehealth-healthier-society-slovenia_en"
PDF_URL = f"{SITE}/document/download/6ab9d519-2de1-499d-870b-77ae9a1458ca_en?filename=Final%20-%20deliverable.pdf"


def project_store(tmp_path):
    store = LocalProjectStore(tmp_path)
    for pid, url, detail in [("slo", SLO_DETAIL, True), ("hems", HEMS_DETAIL, True), ("gone", f"{SITE}/x_en", True),
                             ("thin", None, False)]:
        store.put(StoreEntry(project_id=pid, file=f"projects/{pid}.md", detail_page=detail,
                             source_urls=[url] if url else [], first_seen="d", last_updated="d"), save=False)
    store.flush()
    return store


def test_discoverer_reads_detail_pages_from_cache(tmp_path):
    d = PublicationDiscoverer(output_dir=str(tmp_path))
    d._store = project_store(tmp_path)
    d._fetcher = FakeFetcher(cached_pages={SLO_DETAIL: fixture_bytes("detail_with_publication.html"),
                                           HEMS_DETAIL: fixture_bytes("detail_hems_croatia.html")})
    with runlog.open_run(tmp_path / "logs", "r1") as run:
        pubs = d.run()["publications"]
    assert {p.url: p.project_ids for p in pubs} == {
        SLO_PUB: ["slo"],
        f"{SITE}/publications-0/establishment-helicopter-emergency-medical-services-croatia_en": ["hems"]}
    assert pubs[0].publication_id in {"developing-ehealth-healthier-society-slovenia",
                                      "establishment-helicopter-emergency-medical-services-croatia"}
    assert d._fetcher.calls == []                       # aucune requete reseau
    assert any("not in the HTTP cache" in a for a in run.final_summary["alerts"])


def fetcher_component(tmp_path, pages):
    f = PublicationFetcher(output_dir=str(tmp_path))
    f._fetcher = FakeFetcher(pages=pages)
    return f


def slo_pub():
    return Publication(publication_id="developing-ehealth-healthier-society-slovenia", url=SLO_PUB,
                       project_ids=["slo"])


def test_publication_fetcher_new_unchanged_full(tmp_path):
    pages = {SLO_PUB: fixture_bytes("pub_one_file.html")}
    out = fetcher_component(tmp_path, pages).run(publications=[slo_pub()])
    assert len(out["to_process"]) == 1 and out["unchanged"] == [] and out["remaining"] == 0
    pub = out["to_process"][0]
    assert pub.title.startswith("Developing eHealth") and pub.files[0].url == PDF_URL and pub.files_key
    ds = DeliverableStore(tmp_path)
    ds.put(entry(pid=pub.publication_id).model_copy(update={"files_key": pub.files_key}))
    ds.save()
    out = fetcher_component(tmp_path, pages).run(publications=[slo_pub()])
    assert out["to_process"] == [] and len(out["unchanged"]) == 1
    assert len(fetcher_component(tmp_path, pages).run(publications=[slo_pub()], full=True)["to_process"]) == 1


def test_publication_fetcher_limit_offered_and_failures(tmp_path):
    other = Publication(publication_id="three", url=f"{SITE}/publications-0/three_en", project_ids=["p"])
    missing = Publication(publication_id="missing", url=f"{SITE}/publications-0/missing_en", project_ids=["p"])
    f = fetcher_component(tmp_path, {SLO_PUB: fixture_bytes("pub_one_file.html"),
                                     other.url: fixture_bytes("pub_three_files.html")})
    pubs = [slo_pub(), other, missing]
    first = f.run(publications=pubs, limit=1)
    assert [p.publication_id for p in first["to_process"]] == ["developing-ehealth-healthier-society-slovenia"]
    assert first["remaining"] == 2 and first["failed"] == []   # au-dela de la limite : ni recuperees ni comptees
    second = f.run(publications=pubs, limit=1)                  # meme instance : deja proposees exclues
    assert [p.publication_id for p in second["to_process"]] == ["three"] and second["remaining"] == 1
    third = f.run(publications=pubs, limit=1)
    assert third["to_process"] == [] and third["failed"] == ["missing"] and third["remaining"] == 0


def extractor(tmp_path, pages, **kw):
    x = DeliverableTextExtractor(output_dir=str(tmp_path), deliverables_dir=str(tmp_path / "deliverables"), **kw)
    x._fetcher = FakeFetcher(pages=pages)
    return x


def pub_with_file(url=PDF_URL):
    return slo_pub().model_copy(update={"title": "Developing eHealth", "date": "17 October 2023", "files": [
        PublicationFile(document="Final - deliverable", url=url, language="en", filename="Final - deliverable.pdf")]})


def test_extractor_writes_full_text_without_http_cache(tmp_path):
    x = extractor(tmp_path, {PDF_URL: fixture_bytes("deliverable_small.pdf")})
    out = x.run(publications=[pub_with_file()])
    assert out["failed"] == [] and len(out["texts"]) == 1
    t = out["texts"][0]
    assert t.files[0].pages == 2 and t.files[0].words > 500 and "PROJECT OBJECTIVES" in t.files[0].text
    assert t.full_text_path == "deliverables/developing-ehealth-healthier-society-slovenia.md"
    fm, body = parse_front_matter((tmp_path / t.full_text_path).read_text(encoding="utf-8"))
    assert fm["publication_id"] == "developing-ehealth-healthier-society-slovenia" and fm["projects"] == ["slo"]
    assert fm["files"][0]["pages"] == 2 and "--- page 1 ---" in body and "PROJECT OBJECTIVES" in body
    assert x._fetcher.uncached_calls == [PDF_URL]


@pytest.mark.parametrize("content,kw", [(b"<html>error</html>", {}), (b"%PDF-1.4 junk", {}),
                                        (None, {"max_pdf_mb": 0.0001})])
def test_extractor_skips_bad_files(tmp_path, content, kw):
    pdf = fixture_bytes("deliverable_small.pdf") if content is None else content
    with runlog.open_run(tmp_path / "logs", "r1") as run:
        out = extractor(tmp_path, {PDF_URL: pdf}, **kw).run(publications=[pub_with_file()])
    assert out["texts"] == [] and out["failed"] == ["developing-ehealth-healthier-society-slovenia"]
    assert run.final_summary["alerts"]
```

- [ ] **Step 2 : lancer, échec attendu**

Run : `.venv/Scripts/python -m pytest -q tests/test_deliverables.py`
Attendu : `ModuleNotFoundError: No module named 'tsi_agent.components.deliverables'`.

- [ ] **Step 3 : `src/tsi_agent/components/deliverables.py` (première partie)**

```python
"""Etage livrables : publications liees aux projets, PDF, texte integral, resume IA, section des fichiers projet."""

import io
from pathlib import Path
from typing import Any

import pdfplumber
from haystack import component

from tsi_agent import runlog
from tsi_agent.deliverables_state import DeliverableStore
from tsi_agent.http import DEFAULT_USER_AGENT, FetchError, PoliteFetcher, shared_fetcher
from tsi_agent.ids import slug_from_url
from tsi_agent.models import DeliverableFileText, DeliverableText, Publication
from tsi_agent.parsers.publication import files_key, parse_publication, publication_links, select_files
from tsi_agent.render import render
from tsi_agent.store import ProjectStore, _retry_on_lock, file_stem, shared_store


class _Http:
    """Acces partage au PoliteFetcher (un par processus) ; les tests injectent `_fetcher`."""

    cache_dir: str
    min_interval_s: float
    user_agent: str
    _fetcher: PoliteFetcher | None

    def _get_fetcher(self) -> PoliteFetcher:
        if self._fetcher is None:
            self._fetcher = shared_fetcher(self.cache_dir, self.min_interval_s, self.user_agent)
        return self._fetcher


@component
class PublicationDiscoverer(_Http):
    """Liens de publication des pages detail, lues dans le cache HTTP (le run d'extraction vient de le rafraichir)."""

    def __init__(self, output_dir: str = "output", cache_dir: str = ".cache/http", min_interval_s: float = 2.0,
                 user_agent: str = DEFAULT_USER_AGENT):
        self.output_dir = output_dir
        self.cache_dir = cache_dir
        self.min_interval_s = min_interval_s
        self.user_agent = user_agent
        self._fetcher = None
        self._store: ProjectStore | None = None

    @component.output_types(publications=list[Publication])
    def run(self) -> dict[str, Any]:
        store = self._store if self._store is not None else shared_store(self.output_dir)
        fetcher = self._get_fetcher()
        by_url: dict[str, Publication] = {}
        missing: list[str] = []
        projects = 0
        for project_id, entry in sorted(store.all().items()):
            if not entry.detail_page or not entry.listed or not entry.source_urls:
                continue
            html = fetcher.cached(entry.source_urls[0])  # source_urls[0] = page detail (render.source_urls)
            if html is None:
                missing.append(project_id)
                continue
            links = publication_links(html)
            projects += bool(links)
            for url in links:
                pub = by_url.setdefault(url, Publication(publication_id=slug_from_url(url), url=url))
                if project_id not in pub.project_ids:
                    pub.project_ids.append(project_id)
        if missing:
            runlog.event("PublicationDiscoverer", "alert", None, "warning", missing=missing[:20],
                         message=f"{len(missing)} detail page(s) not in the HTTP cache: run `tsi_agent run` first")
        publications = list(by_url.values())
        runlog.event("PublicationDiscoverer", "discover", None, publications=len(publications),
                     projects_with_publications=projects, missing_pages=len(missing))
        return {"publications": publications}


@component
class PublicationFetcher(_Http):
    """Pages de publication ; seules les publications nouvelles ou dont les fichiers ont change sont retraitees."""

    def __init__(self, output_dir: str = "output", cache_dir: str = ".cache/http", min_interval_s: float = 2.0,
                 user_agent: str = DEFAULT_USER_AGENT):
        self.output_dir = output_dir
        self.cache_dir = cache_dir
        self.min_interval_s = min_interval_s
        self.user_agent = user_agent
        self._fetcher = None
        self._offered: set[str] = set()  # deja proposees pendant ce run (boucle par lots du runner)

    @component.output_types(to_process=list[Publication], unchanged=list[Publication], failed=list[str],
                            remaining=int)
    def run(self, publications: list[Publication], full: bool = False, limit: int | None = None) -> dict[str, Any]:
        known = DeliverableStore(self.output_dir)
        to_process: list[Publication] = []
        unchanged: list[Publication] = []
        failed: list[str] = []
        remaining = 0
        for pub in publications:
            if pub.url in self._offered:
                continue
            if limit is not None and len(to_process) >= limit:
                remaining += 1
                continue
            self._offered.add(pub.url)
            try:
                parse = parse_publication(self._get_fetcher().get(pub.url).content)
            except FetchError as exc:
                runlog.event("PublicationFetcher", "alert", pub.publication_id, "warning",
                             message=f"publication page unavailable: {exc}")
                failed.append(pub.publication_id)
                continue
            files = select_files(parse)
            pub = pub.model_copy(update={"title": parse.title or pub.publication_id, "date": parse.date,
                                         "meta": parse.meta, "files": files, "files_key": files_key(files)})
            if not files:
                runlog.event("PublicationFetcher", "classify", pub.publication_id, "no_files")
                continue
            entry = known.get(pub.publication_id)
            if entry is not None and entry.files_key == pub.files_key and not full:
                unchanged.append(pub)
                continue
            runlog.event("PublicationFetcher", "classify", pub.publication_id, "changed" if entry else "new",
                         files=len(files))
            to_process.append(pub)
        return {"to_process": to_process, "unchanged": unchanged, "failed": failed, "remaining": remaining}


def pdf_text(data: bytes) -> tuple[int, str]:
    """(nombre de pages, texte) ; pages separees par un marqueur."""
    with pdfplumber.open(io.BytesIO(data)) as pdf:
        pages = [f"--- page {i} ---\n{page.extract_text() or ''}" for i, page in enumerate(pdf.pages, start=1)]
    return len(pages), "\n\n".join(pages)


@component
class DeliverableTextExtractor(_Http):
    """Telecharge les PDF retenus (sans cache HTTP), extrait le texte, ecrit output/deliverables/<id>.md."""

    def __init__(self, output_dir: str = "output", deliverables_dir: str = "output/deliverables",
                 cache_dir: str = ".cache/http", min_interval_s: float = 2.0, user_agent: str = DEFAULT_USER_AGENT,
                 max_pdf_mb: float = 50):
        self.output_dir = output_dir
        self.deliverables_dir = deliverables_dir
        self.cache_dir = cache_dir
        self.min_interval_s = min_interval_s
        self.user_agent = user_agent
        self.max_pdf_mb = max_pdf_mb
        self._fetcher = None

    def _alert(self, pub: Publication, url: str, reason: str) -> None:
        runlog.event("DeliverableTextExtractor", "alert", url, "warning",
                     message=f"{pub.publication_id}: file skipped ({reason})")

    def _read(self, pub: Publication, url: str) -> tuple[int, str] | None:
        try:
            data = self._get_fetcher().get(url, cache=False).content
        except FetchError as exc:
            self._alert(pub, url, str(exc))
            return None
        if len(data) > self.max_pdf_mb * 1024 * 1024:
            self._alert(pub, url, f"too large: {len(data) // 1024} KB")
            return None
        if not data.startswith(b"%PDF"):
            self._alert(pub, url, "not a PDF")
            return None
        try:
            pages, text = pdf_text(data)
        except Exception as exc:  # pdfplumber/pdfminer levent des types varies sur un PDF abime
            self._alert(pub, url, f"unreadable PDF: {type(exc).__name__}")
            return None
        runlog.event("DeliverableTextExtractor", "download", url, bytes=len(data), pages=pages,
                     words=len(text.split()))
        return pages, text

    def _write(self, pub: Publication, files: list[DeliverableFileText]) -> str:
        folder = Path(self.deliverables_dir)
        folder.mkdir(parents=True, exist_ok=True)
        path = folder / f"{file_stem(pub.publication_id)}.md"
        front = {"publication_id": pub.publication_id, "title": pub.title, "url": pub.url, "date": pub.date,
                 "projects": list(pub.project_ids),
                 "files": [{"document": f.file.document, "filename": f.file.filename, "language": f.file.language,
                            "url": f.file.url, "pages": f.pages, "words": f.words} for f in files]}
        body = f"# {pub.title}\n\n" + "\n\n".join(
            f"## {f.file.document or f.file.filename}\n\nSource: {f.file.url}\n\n{f.text}" for f in files)
        _retry_on_lock(lambda: path.write_text(render(front, body), encoding="utf-8"))
        try:
            return path.relative_to(Path(self.output_dir)).as_posix()
        except ValueError:
            return path.as_posix()

    @component.output_types(texts=list[DeliverableText], failed=list[str])
    def run(self, publications: list[Publication]) -> dict[str, Any]:
        texts: list[DeliverableText] = []
        failed: list[str] = []
        for pub in publications:
            files = []
            for f in pub.files:
                read = self._read(pub, f.url)
                if read is not None:
                    files.append(DeliverableFileText(file=f, pages=read[0], words=len(read[1].split()), text=read[1]))
            if not files:
                failed.append(pub.publication_id)  # retentee au prochain run (pas d'entree dans l'etat)
                continue
            texts.append(DeliverableText(publication=pub, files=files, full_text_path=self._write(pub, files)))
        return {"texts": texts, "failed": failed}
```

Points d'attention :
- `test_extractor_writes_full_text_without_http_cache` attend `full_text_path == "deliverables/<id>.md"` : `deliverables_dir` doit être sous `output_dir` (c'est le cas du réglage par défaut).
- Avec le dispositif `@component`, un mixin `_Http` sans `__init__` convient. Si Haystack refuse l'héritage (erreur au décorateur), dupliquer `_get_fetcher` dans chaque classe.
- Le test `test_discoverer_reads_detail_pages_from_cache` utilise `HEMS_DETAIL` avec le contenu de `detail_hems_croatia.html` : seule la présence dans le cache compte.

- [ ] **Step 4 : lancer, succès attendu, suite complète**

Run : `.venv/Scripts/python -m pytest -q tests/test_deliverables.py` puis `.venv/Scripts/python -m pytest -q`.

- [ ] **Step 5 : commit**

```bash
git add src/tsi_agent/components/deliverables.py tests/test_deliverables.py
git commit -m "feat: decouverte des publications, pages de publication (incremental), texte des PDF

Co-Authored-By: Claude Opus 5.5 (1M context) <noreply@anthropic.com>"
```

---

### Task 4 : résumé IA et writer des livrables

**Files :** Modify `src/tsi_agent/components/deliverables.py`. Test : `tests/test_deliverables.py`.

- [ ] **Step 1 : tests (à ajouter)**

```python
import json

from conftest import FakeChatGenerator
from tsi_agent.components.claude_cli import LLMQuotaExceeded
from tsi_agent.components.deliverables import DeliverableSummarizer, DeliverableWriter
from tsi_agent.models import DeliverableFileText, DeliverableRecord, DeliverableText
from tsi_agent.render import render

CONTENT = json.dumps({"summary": "The project delivered an eHealth strategy.", "key_outputs": ["Strategy"],
                      "results": ["Adopted by the ministry"], "figures": ["40+ stakeholders"]})


def text_of(words=600):
    pub = pub_with_file()
    ft = DeliverableFileText(file=pub.files[0], pages=2, words=words, text=" ".join(["word"] * words))
    return DeliverableText(publication=pub.model_copy(update={"files_key": "sha256:k"}), files=[ft],
                           full_text_path="deliverables/slo.md")


def test_summarizer_parses_json_and_truncates(tmp_path):
    gen = FakeChatGenerator(replies=[CONTENT])
    with runlog.open_run(tmp_path / "logs", "r1") as run:
        out = DeliverableSummarizer(chat_generator=gen, max_words=50).run(texts=[text_of()])
    assert out["failed"] == [] and out["quota_exceeded"] is False
    rec = out["records"][0]
    assert rec.content.figures == ["40+ stakeholders"] and rec.model == "fake/model"
    prompt = gen.calls[0][-1].text
    assert "Developing eHealth" in prompt and "[... truncated" in prompt and prompt.count("word") <= 60
    assert run.final_summary["llm_calls"] == 1


def test_summarizer_retries_invalid_json_then_quota(tmp_path):
    gen = FakeChatGenerator(replies=["not json", CONTENT])
    assert len(DeliverableSummarizer(chat_generator=gen).run(texts=[text_of()])["records"]) == 1
    quota = FakeChatGenerator(error=LLMQuotaExceeded("usage limit reached"))
    out = DeliverableSummarizer(chat_generator=quota).run(texts=[text_of(), text_of()])
    assert out["quota_exceeded"] is True and out["records"] == []


def project_file(store, pid):
    fm = {"project_id": pid, "title": "T", "results": "achieved", "spotlight": False}
    store.write_project_file(pid, render(fm, f"# T\n\nIntro.\n\n## Context\n\nC.\n\n## Sources\n\n- https://x/{pid}"))


def record_for(pid="slo", summary="The project delivered an eHealth strategy."):
    return DeliverableRecord(text=text_of(), model="fake/model",
                             content=DeliverableContent(summary=summary, key_outputs=["Strategy"]))


def test_writer_updates_state_and_project_files(tmp_path):
    store = LocalProjectStore(tmp_path)
    project_file(store, "slo")
    w = DeliverableWriter(output_dir=str(tmp_path))
    w._store = store
    stats = w.run(records=[record_for()])["stats"]
    assert stats == {"publications": 1, "projects_updated": 1}
    fm, body = parse_front_matter(store.read_project_file("slo"))
    assert fm["deliverables"] == 1 and list(fm).index("deliverables") == list(fm).index("results") + 1
    assert body.index("## Context") < body.index("## Deliverables (AI-generated summary)") < body.index("## Sources")
    assert "The project delivered an eHealth strategy." in body
    assert DeliverableStore(tmp_path).get("developing-ehealth-healthier-society-slovenia").project_ids == ["slo"]
    w.run(records=[record_for(summary="Updated summary.")])
    body = parse_front_matter(store.read_project_file("slo"))[1]
    assert body.count("## Deliverables (AI-generated summary)") == 1 and "Updated summary." in body
    assert w.run(records=[record_for(summary="Updated summary.")])["stats"]["projects_updated"] == 0


def test_writer_moves_section_when_project_list_changes(tmp_path):
    store = LocalProjectStore(tmp_path)
    project_file(store, "slo")
    project_file(store, "other")
    w = DeliverableWriter(output_dir=str(tmp_path))
    w._store = store
    w.run(records=[record_for()])
    moved = text_of().publication.model_copy(update={"project_ids": ["other"]})
    assert w.run(records=[], unchanged=[moved])["stats"]["projects_updated"] == 2
    assert "## Deliverables" not in store.read_project_file("slo")
    assert "## Deliverables (AI-generated summary)" in store.read_project_file("other")
```

- [ ] **Step 2 : lancer, échec attendu**

Run : `.venv/Scripts/python -m pytest -q tests/test_deliverables.py`

- [ ] **Step 3 : implémentation (suite de `src/tsi_agent/components/deliverables.py`)**

Imports à ajouter en tête : `contextvars`, `json`, `re`, `threading`, `time`, `from concurrent.futures import ThreadPoolExecutor`, `from datetime import date`, `from haystack import default_from_dict, default_to_dict`, `from haystack.core.serialization import component_to_dict`, `from haystack.dataclasses import ChatMessage`, `from haystack.utils import deserialize_chatgenerator_inplace`, `from pydantic import ValidationError`, `from tsi_agent.components.claude_cli import LLMQuotaExceeded`, `from tsi_agent.deliverables_state import DeliverableEntry`, `from tsi_agent.models import DeliverableContent, DeliverableRecord`, ainsi que `deliverables_section`, `parse_front_matter`, `replace_deliverables_section` et `with_deliverables_count` depuis `tsi_agent.render`.

```python
SUMMARY_SYSTEM = ("You summarise the deliverables of European Commission technical support projects into JSON. "
                  "You never add facts that are not in the documents.")
SUMMARY_TEMPLATE = """You are given the text of the deliverables (final report, presentation, factsheet...) of a \
European Commission technical support project (Technical Support Instrument or Structural Reform Support Programme).

Return a JSON object with exactly these keys:
- "summary": 4 to 8 sentences on what the project delivered and achieved, using only the documents.
- "key_outputs": up to 6 short items, the main outputs produced (strategies, analyses, tools, trainings...).
- "results": up to 6 short items, results achieved or main recommendations.
- "figures": up to 6 short items, key figures with their context (for example "40+ stakeholders involved").

Rules:
- Use only information present in the documents; never invent facts or figures.
- Do not name individual persons; organisations may be named.
- Write in English. Output only the JSON object, without code fences.

Publication: {title}
Source URL: {url}

--- DOCUMENTS START ---
{text}
--- DOCUMENTS END ---"""
SUMMARY_RETRY = ("Your previous answer could not be parsed ({error}). Return only the JSON object with the keys "
                 "summary, key_outputs, results, figures.")
_FENCE = re.compile(r"^```(?:json)?\s*|\s*```$")


def parse_deliverable_content(text: str) -> DeliverableContent:
    cleaned = _FENCE.sub("", text.strip())
    start, end = cleaned.find("{"), cleaned.rfind("}")
    if start < 0 or end < start:
        raise ValueError("no JSON object in reply")
    try:
        return DeliverableContent.model_validate(json.loads(cleaned[start:end + 1]))
    except (json.JSONDecodeError, ValidationError) as exc:
        raise ValueError(str(exc).splitlines()[0]) from exc


def documents_text(text: DeliverableText, max_words: int) -> tuple[str, bool]:
    """Texte des fichiers, dans l'ordre de la page, limite a `max_words` mots (debut des documents)."""
    parts: list[str] = []
    budget = max_words
    truncated = False
    for f in text.files:
        words = f.text.split()
        if budget <= 0:
            truncated = True
            break
        kept = words[:budget]
        truncated = truncated or len(kept) < len(words)
        budget -= len(kept)
        parts.append(f"### {f.file.document or f.file.filename}\n" + " ".join(kept))
    body = "\n\n".join(parts)
    if truncated:
        body += f"\n\n[... truncated after {max_words} words ...]"
    return body, truncated


@component
class DeliverableSummarizer:
    def __init__(self, chat_generator: Any, max_concurrency: int = 2, max_retries: int = 1,
                 pricing: dict[str, dict[str, float]] | None = None, max_words: int = 15000):
        self.chat_generator = chat_generator
        self.max_concurrency = max_concurrency
        self.max_retries = max_retries
        self.pricing = pricing or {}
        self.max_words = max_words
        self._warmed = False

    def warm_up(self) -> None:
        if not self._warmed and hasattr(self.chat_generator, "warm_up"):
            self.chat_generator.warm_up()
        self._warmed = True

    def to_dict(self) -> dict[str, Any]:
        return default_to_dict(self, chat_generator=component_to_dict(obj=self.chat_generator, name="chat_generator"),
                               max_concurrency=self.max_concurrency, max_retries=self.max_retries,
                               pricing=self.pricing, max_words=self.max_words)

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "DeliverableSummarizer":
        deserialize_chatgenerator_inplace(data["init_parameters"], key="chat_generator")
        return default_from_dict(cls, data)

    def _cost(self, meta: dict[str, Any]) -> float | None:
        if meta.get("cost_usd") is not None:
            return float(meta["cost_usd"])
        usage = meta.get("usage") or {}
        model = str(meta.get("model", ""))
        for prefix, price in sorted(self.pricing.items(), key=lambda kv: len(kv[0]), reverse=True):
            if model.startswith(prefix):
                return (usage.get("prompt_tokens", 0) * price.get("input", 0)
                        + usage.get("completion_tokens", 0) * price.get("output", 0)) / 1_000_000
        return None

    def _summarize_one(self, text: DeliverableText) -> DeliverableRecord:
        pub = text.publication
        body, truncated = documents_text(text, self.max_words)
        messages = [ChatMessage.from_system(SUMMARY_SYSTEM),
                    ChatMessage.from_user(SUMMARY_TEMPLATE.format(title=pub.title, url=pub.url, text=body))]
        error = "no attempt"
        for attempt in range(1, self.max_retries + 2):
            t0 = time.monotonic()
            reply = self.chat_generator.run(messages=messages)["replies"][0]
            meta = reply.meta or {}
            usage = meta.get("usage") or {}
            try:
                content = parse_deliverable_content(reply.text or "")
                result = "ok"
            except ValueError as exc:
                content, result, error = None, "invalid_json", str(exc)
            runlog.event("DeliverableSummarizer", "llm_call", pub.publication_id, result,
                         duration_ms=int((time.monotonic() - t0) * 1000), attempt=attempt,
                         model=meta.get("model"), input_tokens=usage.get("prompt_tokens", 0),
                         output_tokens=usage.get("completion_tokens", 0),
                         cache_read_tokens=usage.get("cache_read_input_tokens", 0),
                         cache_creation_tokens=usage.get("cache_creation_input_tokens", 0),
                         cost_usd=self._cost(meta), truncated=truncated)
            if content is not None:
                return DeliverableRecord(text=text, content=content, model=str(meta.get("model", "unknown")))
            messages = [*messages, reply, ChatMessage.from_user(SUMMARY_RETRY.format(error=error))]
        raise ValueError(error)

    @component.output_types(records=list[DeliverableRecord], failed=list[str], quota_exceeded=bool)
    def run(self, texts: list[DeliverableText]) -> dict[str, Any]:
        self.warm_up()
        stop = threading.Event()
        results: dict[int, DeliverableRecord] = {}
        failed: list[str] = []

        def work(i: int, text: DeliverableText) -> None:
            pid = text.publication.publication_id
            if stop.is_set():
                runlog.event("DeliverableSummarizer", "error", pid, "skipped_quota")
                failed.append(pid)
                return
            try:
                results[i] = self._summarize_one(text)
            except Exception as exc:
                quota = isinstance(exc, LLMQuotaExceeded) or "RateLimit" in type(exc).__name__
                if quota:
                    stop.set()
                runlog.event("DeliverableSummarizer", "error", pid, "quota_exceeded" if quota else "summary_failed",
                             error=str(exc)[:300])
                failed.append(pid)

        with ThreadPoolExecutor(max_workers=max(1, self.max_concurrency)) as pool:
            for i, text in enumerate(texts):
                pool.submit(contextvars.copy_context().run, work, i, text)
        return {"records": [results[i] for i in sorted(results)], "failed": failed,
                "quota_exceeded": stop.is_set()}


@component
class DeliverableWriter:
    """Met a jour output/state/deliverables.json et la section Deliverables des fichiers projet concernes."""

    def __init__(self, output_dir: str = "output"):
        self.output_dir = output_dir
        self._store: ProjectStore | None = None

    @component.output_types(stats=dict[str, int])
    def run(self, records: list[DeliverableRecord], unchanged: list[Publication] | None = None) -> dict[str, Any]:
        state = DeliverableStore(self.output_dir)
        today = date.today().isoformat()
        affected: set[str] = set()
        for rec in records:
            pub = rec.text.publication
            old = state.get(pub.publication_id)
            affected |= set(pub.project_ids) | set(old.project_ids if old else [])
            state.put(DeliverableEntry(
                publication_id=pub.publication_id, url=pub.url, title=pub.title, date=pub.date,
                project_ids=list(pub.project_ids), files_key=pub.files_key or "",
                files=[{"document": f.file.document, "url": f.file.url, "language": f.file.language,
                        "filename": f.file.filename, "pages": f.pages, "words": f.words} for f in rec.text.files],
                full_text_path=rec.text.full_text_path, content=rec.content, model=rec.model, processed=today))
            runlog.event("DeliverableWriter", "write", pub.publication_id, "publication", projects=pub.project_ids)
        for pub in unchanged or []:
            old = state.get(pub.publication_id)
            if old is not None and sorted(old.project_ids) != sorted(pub.project_ids):
                affected |= set(old.project_ids) | set(pub.project_ids)
                state.put(old.model_copy(update={"project_ids": list(pub.project_ids)}))
        state.save()
        store = self._store if self._store is not None else shared_store(self.output_dir)
        updated = 0
        for project_id in sorted(affected):
            text = store.read_project_file(project_id)
            if text is None:
                continue
            fm, body = parse_front_matter(text)
            entries = state.for_project(project_id)
            new = render(with_deliverables_count(fm, len(entries)),
                         replace_deliverables_section(body, deliverables_section(entries)))
            if new != text:
                store.write_project_file(project_id, new)
                updated += 1
                runlog.event("DeliverableWriter", "write", project_id, "project_section", deliverables=len(entries))
        return {"stats": {"publications": len(records), "projects_updated": updated}}
```

Point d'attention : `DeliverableWriter` ne modifie pas l'entrée `StoreEntry` du projet (`content_hash`, `listing_hash`). L'extraction ne voit donc pas de changement, et la couche vectorisée détecte le fichier modifié par son hash.

- [ ] **Step 4 : lancer, succès attendu, suite complète**

Run : `.venv/Scripts/python -m pytest -q`.

- [ ] **Step 5 : commit**

```bash
git add src/tsi_agent/components/deliverables.py tests/test_deliverables.py
git commit -m "feat: resume IA des livrables et writer (etat + section des fichiers projet)

Co-Authored-By: Claude Opus 5.5 (1M context) <noreply@anthropic.com>"
```

---

### Task 5 : pipeline, runner, CLI, réglages, README

**Files :** Create `src/tsi_agent/deliverables_pipeline.py`. Modify `src/tsi_agent/runner.py`, `src/tsi_agent/__main__.py`, `src/tsi_agent/config.py`, `config/settings.yaml`, `README.md`. Tests : `tests/test_deliverables.py`, `tests/test_cli.py`.

- [ ] **Step 1 : réglages**

`Settings` (après `chat_port`) :

```python
    # Etage livrables (pipeline `deliverables`).
    deliverables_dir: str = "output/deliverables"
    deliverables_max_words: int = 15000
    deliverables_max_pdf_mb: float = 50
```

`config/settings.yaml` :

```yaml
# Livrables des projets (python -m tsi_agent deliverables)
deliverables_dir: output/deliverables
deliverables_max_words: 15000   # debut des documents envoye au LLM pour le resume
deliverables_max_pdf_mb: 50
```

- [ ] **Step 2 : tests**

À ajouter à `tests/test_deliverables.py` :

```python
from tsi_agent.config import Settings
from tsi_agent.deliverables_pipeline import build_deliverables_pipeline
from tsi_agent.pipeline import load_pipeline
from tsi_agent.runner import run_deliverables


def pipeline_settings(tmp_path):
    return Settings(output_dir=str(tmp_path), deliverables_dir=str(tmp_path / "deliverables"),
                    logs_dir=str(tmp_path / "logs"), chunk_size=1)


def wired_pipeline(settings, gen, tmp_path):
    p = build_deliverables_pipeline(settings, chat_generator=gen)
    store = project_store(tmp_path)
    project_file(store, "slo")
    fake = FakeFetcher(pages={SLO_PUB: fixture_bytes("pub_one_file.html"),
                              PDF_URL: fixture_bytes("deliverable_small.pdf")},
                       cached_pages={SLO_DETAIL: fixture_bytes("detail_with_publication.html")})
    for name in ("discoverer", "publications", "texts"):
        p.get_component(name)._fetcher = fake
    p.get_component("discoverer")._store = store
    p.get_component("writer")._store = store
    return p, store


def test_run_deliverables_end_to_end_then_incremental(tmp_path):
    s = pipeline_settings(tmp_path)
    gen = FakeChatGenerator(replies=[CONTENT])
    built = {}

    def factory(settings, **kw):
        built["p"], built["store"] = wired_pipeline(settings, gen, tmp_path)
        return built["p"]
    summary = run_deliverables(s, run_id="d1", pipeline_factory=factory)
    assert summary["status"] == "ok" and summary["quota_exceeded"] is False
    assert summary["written"]["publications"] == 1 and summary["remaining_publications"] == 0
    assert "## Deliverables (AI-generated summary)" in built["store"].read_project_file("slo")
    again = run_deliverables(s, run_id="d2", pipeline_factory=factory)
    assert again["written"]["publications"] == 0 and len(gen.calls) == 1


def test_run_deliverables_stops_on_quota(tmp_path):
    s = pipeline_settings(tmp_path)
    gen = FakeChatGenerator(error=LLMQuotaExceeded("usage limit reached"))
    summary = run_deliverables(s, run_id="q1", pipeline_factory=lambda settings, **kw:
                               wired_pipeline(settings, gen, tmp_path)[0])
    assert summary["quota_exceeded"] is True


def test_deliverables_pipeline_yaml_round_trip(tmp_path):
    text = build_deliverables_pipeline(pipeline_settings(tmp_path)).dumps()
    for name in ("PublicationDiscoverer", "PublicationFetcher", "DeliverableTextExtractor",
                 "DeliverableSummarizer", "DeliverableWriter", "ClaudeCliChatGenerator"):
        assert name in text
    assert load_pipeline(text).get_component("summarizer").max_words == 15000
```

À ajouter à `tests/test_cli.py` :

```python
def test_deliverables_command_exit_codes(monkeypatch, capsys):
    seen = {}

    def fake(settings, **kw):
        seen.update(kw)
        return {"quota_exceeded": False}
    monkeypatch.setattr(cli, "run_deliverables", fake)
    assert cli.main(["deliverables", "--full", "--limit", "3"]) == 0 and seen["full"] and seen["limit"] == 3
    monkeypatch.setattr(cli, "run_deliverables", lambda *a, **k: {"quota_exceeded": True})
    assert cli.main(["deliverables"]) == 1

    def boom(*a, **k):
        raise RuntimeError("kaput")
    monkeypatch.setattr(cli, "run_deliverables", boom)
    assert cli.main(["deliverables"]) == 2 and "kaput" in capsys.readouterr().err


def test_export_deliverables_yaml(tmp_path):
    out = tmp_path / "d.yaml"
    assert cli.main(["export-yaml", "--deliverables", str(out)]) == 0
    assert "DeliverableSummarizer" in out.read_text(encoding="utf-8")
```

- [ ] **Step 3 : lancer, échec attendu**

Run : `.venv/Scripts/python -m pytest -q tests/test_deliverables.py tests/test_cli.py`

- [ ] **Step 4 : `src/tsi_agent/deliverables_pipeline.py`**

```python
"""Pipeline Haystack de l'etage livrables (export YAML pour deepset)."""

from typing import Any

from haystack import Pipeline

from tsi_agent.components.claude_cli import ClaudeCliChatGenerator
from tsi_agent.components.deliverables import (DeliverableSummarizer, DeliverableTextExtractor, DeliverableWriter,
                                               PublicationDiscoverer, PublicationFetcher)
from tsi_agent.config import Settings
from tsi_agent.models import DeliverableContent


def default_deliverables_generator(settings: Settings, model: str | None = None) -> Any:
    return ClaudeCliChatGenerator(model=model or settings.model, timeout_s=settings.llm_timeout_s,
                                  json_schema=DeliverableContent.model_json_schema())


def build_deliverables_pipeline(settings: Settings, *, chat_generator: Any = None, model: str | None = None) -> Pipeline:
    http = {"cache_dir": settings.cache_dir, "min_interval_s": settings.min_interval_s,
            "user_agent": settings.user_agent}
    p = Pipeline()
    p.add_component("discoverer", PublicationDiscoverer(output_dir=settings.output_dir, **http))
    p.add_component("publications", PublicationFetcher(output_dir=settings.output_dir, **http))
    p.add_component("texts", DeliverableTextExtractor(output_dir=settings.output_dir,
                                                      deliverables_dir=settings.deliverables_dir,
                                                      max_pdf_mb=settings.deliverables_max_pdf_mb, **http))
    p.add_component("summarizer", DeliverableSummarizer(
        chat_generator=chat_generator or default_deliverables_generator(settings, model),
        max_concurrency=settings.max_concurrency, pricing=settings.pricing,
        max_words=settings.deliverables_max_words))
    p.add_component("writer", DeliverableWriter(output_dir=settings.output_dir))
    p.connect("discoverer.publications", "publications.publications")
    p.connect("publications.to_process", "texts.publications")
    p.connect("texts.texts", "summarizer.texts")
    p.connect("summarizer.records", "writer.records")
    p.connect("publications.unchanged", "writer.unchanged")
    return p
```

- [ ] **Step 5 : `run_deliverables` (fin de `src/tsi_agent/runner.py`)**

Import : `from tsi_agent.deliverables_pipeline import build_deliverables_pipeline`.

```python
def run_deliverables(settings: Settings, *, full: bool = False, limit: int | None = None,
                     chunk_size: int | None = None, model: str | None = None, run_id: str | None = None,
                     pipeline_factory: PipelineFactory = build_deliverables_pipeline) -> dict[str, Any]:
    """Livrables des projets, par lots : chaque lot est ecrit avant le suivant (reprise sur limite de souscription)."""
    chunk = chunk_size if chunk_size is not None else settings.chunk_size
    with runlog.open_run(settings.logs_dir, run_id) as run:
        pipeline = pipeline_factory(settings, model=model)
        budget = limit
        chunks = 0
        quota = False
        remaining = 0
        written: dict[str, int] = {}
        failed: list[str] = []
        while True:
            n = chunk if budget is None else min(chunk, budget)
            out = pipeline.run({"publications": {"full": full, "limit": n}},
                               include_outputs_from={"publications", "texts", "summarizer", "writer"})
            chunks += 1
            sent = len(out["publications"]["to_process"])
            remaining = out["publications"]["remaining"]
            for key, value in out["writer"]["stats"].items():
                written[key] = written.get(key, 0) + value
            failed += out["publications"]["failed"] + out["texts"]["failed"] + out["summarizer"]["failed"]
            quota = bool(out["summarizer"]["quota_exceeded"])
            if budget is not None:
                budget -= sent
            if quota or remaining == 0 or sent == 0 or (budget is not None and budget <= 0):
                break
        run.extra.update({"mode": "deliverables", "chunks": chunks, "quota_exceeded": quota, "written": written,
                          "remaining_publications": remaining, "failed": sorted(set(failed))})
    return run.final_summary
```

- [ ] **Step 6 : CLI (`src/tsi_agent/__main__.py`)**

- Import : `run_deliverables` depuis `tsi_agent.runner`, et `build_deliverables_pipeline` depuis `tsi_agent.deliverables_pipeline`.
- Sous-parseur, après `run` :

```python
    deliv = sub.add_parser("deliverables", help="livrables des projets (publications PDF) : texte et résumé IA")
    deliv.add_argument("--full", action="store_true", help="retraiter toutes les publications")
    deliv.add_argument("--limit", type=int, help="nombre maximal de publications envoyées au LLM")
    deliv.add_argument("--chunk-size", type=int)
    deliv.add_argument("--model")
```

- `export-yaml` : `export.add_argument("--deliverables", action="store_true", help="exporter le pipeline des livrables")`. Dans le bloc de construction, avant les autres cas : `if args.deliverables: pipe = build_deliverables_pipeline(settings)`, puis enchaîner sur les `elif` existants.
- Commande, avant `index`, sur le modèle de `run` (même try/except, code 2) :

```python
    if args.command == "deliverables":
        try:
            summary = run_deliverables(settings, full=args.full, limit=args.limit, chunk_size=args.chunk_size,
                                       model=args.model)
        except Exception as exc:
            print(f"ERREUR : {exc!r} — voir le journal du run dans {settings.logs_dir}/runs/", file=sys.stderr)
            traceback.print_exc(file=sys.stderr)
            return 2
        print(json.dumps(summary, indent=2, ensure_ascii=False))
        return 1 if summary.get("quota_exceeded") else 0
```

- [ ] **Step 7 : README**

- Dans « Utilisation », ajouter `.venv\Scripts\python -m tsi_agent deliverables` avec un commentaire, et préciser l'ordre recommandé : `run`, puis `deliverables`, puis `index`.
- Nouvelle section « Livrables des projets » après « Sorties », qui explique :
  - les publications et les PDF ;
  - `output/deliverables/<id>.md` (texte intégral tel que publié, non vectorisé) ;
  - la section « Deliverables (AI-generated summary) » et `deliverables: N` dans le front matter ;
  - `output/state/deliverables.json` ;
  - le choix de la langue ;
  - la limite de 15 000 mots pour le résumé ;
  - l'absence de cache pour les PDF ;
  - les coûts et la durée du premier run (~45 min de réseau et ~500 appels LLM, reprise sur limite de souscription) ;
  - le code de sortie 1.
- Planification Windows : enchaîner les trois commandes.
- deepset : remplacer le `chat_generator` du composant `summarizer`, comme pour `extractor`.

- [ ] **Step 8 : lancer, succès attendu, suite complète, commit**

Run : `.venv/Scripts/python -m pytest -q`.

```bash
git add src/tsi_agent/deliverables_pipeline.py src/tsi_agent/runner.py src/tsi_agent/__main__.py src/tsi_agent/config.py config/settings.yaml README.md tests/test_deliverables.py tests/test_cli.py
git commit -m "feat: pipeline et commande deliverables (par lots, reprise sur quota), export YAML, README

Co-Authored-By: Claude Opus 5.5 (1M context) <noreply@anthropic.com>"
```

- [ ] **Step 9 : essai réel (contrôleur)**

`python -m tsi_agent deliverables --limit 3 --model sonnet`, puis relire les trois résumés et les fichiers de `output/deliverables/`, et vérifier la section du fichier `developing-ehealth-healthier-society-slovenia.md`. Relancer une seconde fois : aucune publication ne doit être renvoyée au LLM.
