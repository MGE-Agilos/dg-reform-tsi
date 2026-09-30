# Agent de vectorisation (couche RAG) — Plan d'implémentation

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal :** un pipeline Haystack `index` qui découpe et vectorise les fichiers de `output/projects` et `output/programme` dans une couche séparée `output/vectors/`, en full puis en incrémental.

**Architecture :** `ProjectFileLoader` (détection des fichiers nouveaux, modifiés et supprimés via `state.json`) → `ProjectChunker` (découpage déterministe par section, métadonnées) → embedder Haystack standard (sentence-transformers en local, OpenAI dans deepset) → `VectorLayerWriter` (`InMemoryDocumentStore` sauvegardé dans `store.json`, puis `state.json`). Journal et coûts via `tsi_agent.runlog`.

**Tech stack :** Python 3.11+, haystack-ai 3.x, sentence-transformers-haystack (extra `[local]`), pytest.

**Spec :** [docs/superpowers/specs/2026-09-30-tsi-vector-index-design.md](../specs/2026-09-30-tsi-vector-index-design.md)

**Conventions du dépôt :**
- Racine : `C:\Users\MGE\OneDrive - Agilos\Documents\Claude Code Projects\DG REFORM\Haystack Pipeline`, branche `feature/vector-index`.
- Tests : `.venv/Scripts/python -m pytest -q`. 144 tests passent au départ.
- Commentaires et messages en français, sans accents dans le code si le fichier voisin n'en a pas (les deux styles coexistent ; suivre le fichier modifié).
- Chaque commit se termine par la ligne `Co-Authored-By: Claude Opus 5.5 (1M context) <noreply@anthropic.com>`.
- Les fichiers écrits sur disque passent par `tsi_agent.store._retry_on_lock` (verrous OneDrive).

## Structure des fichiers

| Fichier | Rôle |
|---|---|
| `src/tsi_agent/chunking.py` (nouveau) | Fonctions pures : sections, fenêtres de mots, en-tête de contexte, `chunk_file` |
| `src/tsi_agent/vector_state.py` (nouveau) | `VectorState`, lecture/écriture de `state.json` et `store.json` |
| `src/tsi_agent/components/chunker.py` (nouveau) | Composant `ProjectChunker` |
| `src/tsi_agent/components/file_loader.py` (nouveau) | Composant `ProjectFileLoader` |
| `src/tsi_agent/components/vector_writer.py` (nouveau) | Composant `VectorLayerWriter` |
| `src/tsi_agent/index_pipeline.py` (nouveau) | `build_index_pipeline`, `default_embedder`, `embedder_id` |
| `src/tsi_agent/runlog.py` | Agrège tokens et coûts des événements `embed` |
| `src/tsi_agent/config.py`, `config/settings.yaml` | `vectors_dir`, `embedding_model`, `embedding_batch_size` |
| `src/tsi_agent/runner.py` | `run_index` |
| `src/tsi_agent/__main__.py` | Commande `index`, option `export-yaml --index` |
| `pyproject.toml` | Extra `[local]` |
| `README.md` | Section « Couche vectorisée » |
| `tests/fixtures/vec_*.md` | 5 vrais fichiers exportés |
| `tests/test_chunking.py`, `tests/test_vector_layer.py`, `tests/test_index_pipeline.py` (nouveaux) | Tests |

---

### Task 1 : dépendances, réglages et fixtures

**Files :**
- Modify : `pyproject.toml`, `src/tsi_agent/config.py`, `config/settings.yaml`
- Create : `tests/fixtures/vec_detail_sections.md`, `tests/fixtures/vec_detail_page_text.md`, `tests/fixtures/vec_detail_long_section.md`, `tests/fixtures/vec_thin.md`, `tests/fixtures/vec_programme.md`

- [ ] **Step 1 : extra `[local]` dans `pyproject.toml`**

Remplacer le bloc `[project.optional-dependencies]` par :

```toml
[project.optional-dependencies]
dev = ["pytest>=8"]
# Embedder local (PyTorch CPU + modele). Pas installe dans deepset, qui passe par l'API OpenAI.
local = ["sentence-transformers-haystack>=0.1.2"]
```

- [ ] **Step 2 : réglages**

Dans `src/tsi_agent/config.py`, ajouter à `Settings` après `chunk_size: int = 25` :

```python
    # Couche vectorisee (pipeline `index`).
    vectors_dir: str = "output/vectors"
    embedding_model: str = "intfloat/multilingual-e5-small"
    embedding_batch_size: int = 32
```

Dans `config/settings.yaml`, ajouter à la fin :

```yaml
# Couche vectorisée (python -m tsi_agent index)
vectors_dir: output/vectors
embedding_model: intfloat/multilingual-e5-small   # local, multilingue ; OpenAI text-embedding-3-small dans deepset
embedding_batch_size: 32
```

- [ ] **Step 3 : fixtures (copies de vrais exports)**

```bash
cd "C:/Users/MGE/OneDrive - Agilos/Documents/Claude Code Projects/DG REFORM/Haystack Pipeline"
cp output/projects/energy-regulation-academy.md tests/fixtures/vec_detail_sections.md
cp output/projects/building-capacity-competition-authority-spain.md tests/fixtures/vec_detail_page_text.md
cp output/projects/governance-and-monitoring-system-national-plan-digital-skills.md tests/fixtures/vec_detail_long_section.md
cp output/projects/srsp-2016-technical-support-for-the-property-tax-reform.md tests/fixtures/vec_thin.md
cp output/programme/country-croatia.md tests/fixtures/vec_programme.md
grep -c "^## " tests/fixtures/vec_*.md
```

Attendu : chaque fichier contient au moins une ligne `## `. `vec_detail_long_section.md` a une section de plus de 300 mots (« Support Delivered »), à vérifier :

```bash
awk '/^## /{if(n>300)print s, n; s=$0; n=0; next}{n+=NF}' tests/fixtures/vec_detail_long_section.md
```

Attendu : une ligne `## Support Delivered 362` (ou proche).

- [ ] **Step 4 : tests existants**

Run : `.venv/Scripts/python -m pytest -q`
Attendu : 144 passed.

- [ ] **Step 5 : commit**

```bash
git add pyproject.toml src/tsi_agent/config.py config/settings.yaml tests/fixtures/vec_*.md
git commit -m "chore: reglages et fixtures de la couche vectorisee, extra [local]

Co-Authored-By: Claude Opus 5.5 (1M context) <noreply@anthropic.com>"
```

---

### Task 2 : découpage (`chunking.py`)

**Files :**
- Create : `src/tsi_agent/chunking.py`
- Test : `tests/test_chunking.py`

- [ ] **Step 1 : tests**

```python
# tests/test_chunking.py
import pytest

from conftest import FIXTURES
from tsi_agent.chunking import (CHUNKER_VERSION, INTRO, SUMMARY, chunk_file, context_header, split_sections,
                                split_words)


def read(name: str) -> str:
    return (FIXTURES / name).read_text(encoding="utf-8")


def test_split_sections_drops_title_sources_and_listed():
    body = ("# Title\n\nIntro text.\n\n## Summary (AI-generated)\n\nSum.\n\n## Context\n\nCtx.\n\n"
            "## Listed in country factsheets as\n\n- X\n\n## Sources\n\n- https://x\n")
    assert split_sections(body) == [(INTRO, "Intro text."), (SUMMARY, "Sum."), ("Context", "Ctx.")]


def test_split_sections_skips_empty_intro():
    assert split_sections("# T\n\n## Context\n\nCtx.\n") == [("Context", "Ctx.")]


def test_split_words_keeps_short_text_verbatim():
    text = "Line one.\nLine two."
    assert split_words(text) == [text]


def test_split_words_windows_with_overlap_and_sentence_cut():
    words = [f"w{i}" + ("." if i % 20 == 19 else "") for i in range(620)]
    chunks = split_words(" ".join(words))
    assert len(chunks) == 3
    first = chunks[0].split()
    assert len(first) <= 250 and first[-1].endswith(".")
    assert chunks[1].split()[0] == first[-40]          # recouvrement de 40 mots
    assert chunks[-1].split()[-1] == "w619."
    assert all(len(c.split()) <= 250 for c in chunks)


def test_context_header():
    fm = {"title": "HEMS", "programme": "TSI", "year": 2022, "countries": ["HR", "SI"]}
    assert context_header(fm) == "HEMS — TSI 2022 — Croatia, Slovenia"
    assert context_header({"title": "TSI country factsheet - Croatia"}) == "TSI country factsheet - Croatia"


def test_detail_file_with_sections():
    docs = chunk_file("projects/energy-regulation-academy.md", read("vec_detail_sections.md"))
    sections = [d.meta["section"] for d in docs]
    assert sections == [INTRO, SUMMARY, "Context", "Support delivered", "Expected results"]
    assert [d.id for d in docs] == [f"energy-regulation-academy#{i}" for i in range(5)]
    first = docs[0].meta
    assert first["doc_type"] == "project" and first["detail_page"] is True and first["programme"] == "TSI"
    assert first["year"] == 2026 and len(first["countries"]) == 27 and first["multi_country"] is True
    assert first["source_file"] == "projects/energy-regulation-academy.md"
    assert first["source_url"].endswith("/green-transition/energy-regulation-academy_en")
    assert first["context_header"].startswith("The Energy Regulation Academy — TSI 2026 — Austria")
    assert [d.meta["ai_generated"] for d in docs] == [False, True, False, False, False]
    assert all("https://" not in d.content for d in docs)
    assert docs[0].content.startswith("The Energy Regulation Academy is a capacity-building initiative")


def test_detail_file_with_page_text():
    sections = [d.meta["section"] for d in chunk_file("projects/x.md", read("vec_detail_page_text.md"))]
    assert "Page text" in sections and SUMMARY in sections


def test_long_section_is_split():
    docs = chunk_file("projects/x.md", read("vec_detail_long_section.md"))
    support = [d for d in docs if d.meta["section"].lower() == "support delivered"]
    assert len(support) == 2
    assert [d.meta["chunk_index"] for d in docs] == list(range(len(docs)))


def test_thin_file_is_one_chunk():
    docs = chunk_file("projects/thin.md", read("vec_thin.md"))
    assert len(docs) == 1
    meta = docs[0].meta
    assert meta["detail_page"] is False and meta["programme"] == "SRSP" and meta["countries"] == ["GR"]
    assert "No detail page is published." in docs[0].content
    assert meta["source_url"].endswith(".pdf")


def test_programme_file():
    docs = chunk_file("programme/country-croatia.md", read("vec_programme.md"))
    assert docs and all(d.meta["doc_type"] == "programme" for d in docs)
    assert docs[0].meta["doc_id"] == "country-croatia" and docs[0].meta["detail_page"] is None
    assert docs[0].meta["source_url"].endswith("/croatia_en")


def test_crlf_file_is_accepted():
    text = read("vec_thin.md").replace("\n", "\r\n")
    assert len(chunk_file("projects/thin.md", text)) == 1


@pytest.mark.parametrize("text", ["no front matter", "---\n- a list\n---\n\nbody", "---\ntitle: x\n---\n\nbody"])
def test_invalid_files_raise_value_error(text):
    with pytest.raises(ValueError):
        chunk_file("projects/bad.md", text)


def test_chunker_version_is_an_int():
    assert isinstance(CHUNKER_VERSION, int)
```

- [ ] **Step 2 : lancer, échec attendu**

Run : `.venv/Scripts/python -m pytest -q tests/test_chunking.py`
Attendu : `ModuleNotFoundError: No module named 'tsi_agent.chunking'`.

- [ ] **Step 3 : implémentation**

```python
# src/tsi_agent/chunking.py
"""Decoupage des fichiers extraits en morceaux pour la couche vectorisee (deterministe, sans LLM)."""

import re
from typing import Any

from haystack import Document

from tsi_agent.countries import COUNTRIES, name_from_iso
from tsi_agent.render import parse_front_matter

# A incrementer a chaque changement des regles ci-dessous : force une revectorisation complete.
CHUNKER_VERSION = 1
SPLIT_THRESHOLD_WORDS = 300
WINDOW_WORDS = 250
OVERLAP_WORDS = 40
SENTENCE_LOOKBACK_WORDS = 50

INTRO = "Introduction"
SUMMARY = "Summary (AI-generated)"
# Listes d'URL et titres de listing : deja en metadonnees, sans valeur pour la recherche.
SKIPPED_SECTIONS = {"Sources", "Listed in country factsheets as"}
PROJECT_FIELDS = ("title", "programme", "year", "countries", "multi_country", "policy_area", "detail_page",
                  "results", "spotlight", "highlighted", "listed")

_TITLE = re.compile(r"\A# [^\n]*(?:\n|\Z)")
_HEADING = re.compile(r"^## (.+?)[ \t]*$", re.MULTILINE)
_SENTENCE_END = re.compile(r"[.!?][\"')\]]*$")


def split_sections(body: str) -> list[tuple[str, str]]:
    """(nom de section, texte) dans l'ordre ; le texte avant la premiere section `##` est l'introduction."""
    body = _TITLE.sub("", body.lstrip("\n"), count=1)
    parts = _HEADING.split(body)
    sections = [(INTRO, parts[0].strip())]
    sections += [(parts[i].strip(), parts[i + 1].strip()) for i in range(1, len(parts), 2)]
    return [(name, text) for name, text in sections if text and name not in SKIPPED_SECTIONS]


def split_words(text: str, threshold: int = SPLIT_THRESHOLD_WORDS, window: int = WINDOW_WORDS,
                overlap: int = OVERLAP_WORDS) -> list[str]:
    """Texte court : tel quel. Texte long : fenetres de `window` mots, coupees en fin de phrase si possible."""
    words = text.split()
    if len(words) <= threshold:
        return [text]
    chunks: list[str] = []
    start = 0
    while True:
        end = min(start + window, len(words))
        if end < len(words):
            for cut in range(end, start + window - SENTENCE_LOOKBACK_WORDS, -1):
                if _SENTENCE_END.search(words[cut - 1]):
                    end = cut
                    break
        chunks.append(" ".join(words[start:end]))
        if end >= len(words):
            return chunks
        start = end - overlap


def context_header(fm: dict[str, Any]) -> str:
    """« Titre — Programme Annee — Pays » : ajoute au texte vectorise pour qu'un morceau garde son contexte."""
    parts = [str(fm.get("title") or "").strip()]
    parts.append(" ".join(str(v) for v in (fm.get("programme"), fm.get("year")) if v))
    countries = [name_from_iso(c) if c in COUNTRIES else str(c) for c in fm.get("countries") or []]
    parts.append(", ".join(countries))
    return " — ".join(p for p in parts if p)


def chunk_file(rel_path: str, text: str) -> list[Document]:
    """Morceaux d'un fichier projet ou programme. ValueError si le front matter est absent ou invalide."""
    fm, body = parse_front_matter(text.replace("\r\n", "\n"))
    if not isinstance(fm, dict):
        raise ValueError("front matter is not a mapping")
    doc_id = fm.get("project_id") or fm.get("doc_id")
    if not doc_id:
        raise ValueError("front matter has no project_id or doc_id")
    urls = fm.get("source_urls") or []
    base: dict[str, Any] = {
        "doc_id": doc_id,
        "doc_type": "project" if fm.get("project_id") else "programme",
        "source_file": rel_path,
        "source_url": fm.get("source_url") or (urls[0] if urls else None),
        "context_header": context_header(fm),
        **{field: fm.get(field) for field in PROJECT_FIELDS},
    }
    docs: list[Document] = []
    for section, section_text in split_sections(body):
        for piece in split_words(section_text):
            index = len(docs)
            docs.append(Document(id=f"{doc_id}#{index}", content=piece,
                                 meta={**base, "section": section, "chunk_index": index,
                                       "ai_generated": section == SUMMARY}))
    return docs
```

Note : le test `test_invalid_files_raise_value_error` couvre aussi `"---\ntitle: x\n---"` (pas d'id) et un front matter liste. Si `parse_front_matter` lève `yaml.YAMLError` sur un YAML invalide, le composant (Task 3) l'attrape séparément.

- [ ] **Step 4 : lancer, succès attendu**

Run : `.venv/Scripts/python -m pytest -q tests/test_chunking.py`
Attendu : tous verts. Si `test_detail_file_with_sections` échoue sur la liste des sections, afficher `grep "^## " tests/fixtures/vec_detail_sections.md` et vérifier que seules Sources est exclue ; ne pas modifier la fixture.

- [ ] **Step 5 : commit**

```bash
git add src/tsi_agent/chunking.py tests/test_chunking.py
git commit -m "feat: decoupage des fichiers extraits par section, avec metadonnees

Co-Authored-By: Claude Opus 5.5 (1M context) <noreply@anthropic.com>"
```

---

### Task 3 : composant `ProjectChunker`

**Files :**
- Create : `src/tsi_agent/components/chunker.py`
- Modify : `src/tsi_agent/components/__init__.py`
- Test : `tests/test_vector_layer.py` (créé ici, complété aux tâches 4 et 5)

- [ ] **Step 1 : tests**

```python
# tests/test_vector_layer.py
from haystack.dataclasses import ByteStream

from conftest import FIXTURES
from tsi_agent import runlog
from tsi_agent.components.chunker import ProjectChunker


def stream(name: str, rel: str) -> ByteStream:
    return ByteStream(data=(FIXTURES / name).read_bytes(), meta={"file_path": rel, "content_hash": "sha256:x"})


def test_chunker_chunks_valid_files_and_skips_invalid(tmp_path):
    bad = ByteStream(data=b"no front matter", meta={"file_path": "projects/bad.md"})
    with runlog.open_run(tmp_path, "r1") as run:
        out = ProjectChunker().run(sources=[stream("vec_thin.md", "projects/thin.md"), bad])
    assert [d.meta["source_file"] for d in out["documents"]] == ["projects/thin.md"]
    assert out["skipped"] == ["projects/bad.md"]
    assert run.final_summary["counts"]["chunk:ok"] == 1 and run.final_summary["alerts"]


def test_chunker_accepts_deepset_file_name_meta():
    src = ByteStream(data=(FIXTURES / "vec_thin.md").read_bytes(), meta={"file_name": "thin.md"})
    assert ProjectChunker().run(sources=[src])["documents"][0].meta["source_file"] == "thin.md"
```

- [ ] **Step 2 : lancer, échec attendu**

Run : `.venv/Scripts/python -m pytest -q tests/test_vector_layer.py`
Attendu : `ModuleNotFoundError: No module named 'tsi_agent.components.chunker'`.

- [ ] **Step 3 : implémentation**

```python
# src/tsi_agent/components/chunker.py
"""Composant Haystack : fichiers extraits (ByteStream) -> morceaux (Document). Portable dans deepset."""

import yaml
from haystack import Document, component
from haystack.dataclasses import ByteStream

from tsi_agent import runlog
from tsi_agent.chunking import chunk_file


@component
class ProjectChunker:
    @component.output_types(documents=list[Document], skipped=list[str])
    def run(self, sources: list[ByteStream]) -> dict[str, list]:
        documents: list[Document] = []
        skipped: list[str] = []
        for src in sources:
            # En local : chemin relatif pose par ProjectFileLoader ; dans deepset : nom du fichier de l'espace.
            rel = str(src.meta.get("file_path") or src.meta.get("file_name") or "")
            try:
                chunks = chunk_file(rel, src.data.decode("utf-8"))
            except (ValueError, UnicodeDecodeError, yaml.YAMLError) as exc:
                runlog.event("ProjectChunker", "alert", rel, "warning", message=f"cannot chunk {rel}: {exc}")
                skipped.append(rel)
                continue
            runlog.event("ProjectChunker", "chunk", rel, chunks=len(chunks),
                         words=sum(len((d.content or "").split()) for d in chunks))
            documents.extend(chunks)
        return {"documents": documents, "skipped": skipped}
```

Dans `src/tsi_agent/components/__init__.py`, ajouter l'import `from tsi_agent.components.chunker import ProjectChunker` (ordre alphabétique des modules) et `"ProjectChunker"` dans `__all__` (ordre alphabétique).

- [ ] **Step 4 : lancer, succès attendu**

Run : `.venv/Scripts/python -m pytest -q tests/test_vector_layer.py`
Attendu : 2 passed.

- [ ] **Step 5 : commit**

```bash
git add src/tsi_agent/components/chunker.py src/tsi_agent/components/__init__.py tests/test_vector_layer.py
git commit -m "feat: composant ProjectChunker

Co-Authored-By: Claude Opus 5.5 (1M context) <noreply@anthropic.com>"
```

---

### Task 4 : état de la couche et `ProjectFileLoader`

**Files :**
- Create : `src/tsi_agent/vector_state.py`, `src/tsi_agent/components/file_loader.py`
- Modify : `src/tsi_agent/components/__init__.py`
- Test : `tests/test_vector_layer.py`

- [ ] **Step 1 : tests (à ajouter à `tests/test_vector_layer.py`)**

```python
import shutil

import pytest
from haystack import Document

from tsi_agent.chunking import CHUNKER_VERSION
from tsi_agent.components.file_loader import ProjectFileLoader
from tsi_agent.vector_state import (FileState, VectorLayerError, VectorState, load_state, load_store, new_store,
                                    save_state, save_store, store_path)


def make_output(tmp_path):
    out = tmp_path / "output"
    (out / "projects").mkdir(parents=True)
    (out / "programme").mkdir()
    shutil.copy(FIXTURES / "vec_thin.md", out / "projects" / "thin.md")
    shutil.copy(FIXTURES / "vec_detail_sections.md", out / "projects" / "detail.md")
    shutil.copy(FIXTURES / "vec_programme.md", out / "programme" / "country-croatia.md")
    return out


def loader(out, embedder_id="mock:m"):
    return ProjectFileLoader(output_dir=str(out), vectors_dir=str(out / "vectors"), embedder_id=embedder_id)


def test_state_and_store_round_trip(tmp_path):
    state = VectorState(embedder_id="e", chunker_version=1, files={"a.md": FileState(hash="h", chunk_ids=["a#0"])})
    save_state(tmp_path, state)
    assert load_state(tmp_path) == state
    store = new_store()
    store.write_documents([Document(id="a#0", content="x", embedding=[0.1, 0.2], meta={"k": 1})])
    save_store(store, store_path(tmp_path))
    loaded = load_store(store_path(tmp_path))
    assert [d.id for d in loaded.filter_documents()] == ["a#0"]
    assert loaded.filter_documents()[0].embedding == [0.1, 0.2]


def test_unreadable_store_is_a_clear_error(tmp_path):
    store_path(tmp_path).write_text("{not json", encoding="utf-8")
    with pytest.raises(VectorLayerError, match="--full"):
        load_store(store_path(tmp_path))


def test_loader_first_run_is_full(tmp_path):
    out = make_output(tmp_path)
    res = loader(out).run()
    assert res["full"] is True and res["removed"] == []
    assert sorted(s.meta["file_path"] for s in res["sources"]) == [
        "programme/country-croatia.md", "projects/detail.md", "projects/thin.md"]
    assert all(s.meta["content_hash"].startswith("sha256:") for s in res["sources"])


def indexed_state(out, embedder_id="mock:m", version=CHUNKER_VERSION):
    """Etat tel qu'apres un run complet : tous les fichiers actuels indexes."""
    files = {}
    for s in loader(out).run(full=True)["sources"]:
        files[s.meta["file_path"]] = FileState(hash=s.meta["content_hash"], chunk_ids=[s.meta["file_path"] + "#0"])
    vdir = out / "vectors"
    save_state(vdir, VectorState(embedder_id=embedder_id, chunker_version=version, files=files))
    save_store(new_store(), store_path(vdir))


def test_loader_incremental_new_changed_removed(tmp_path):
    out = make_output(tmp_path)
    indexed_state(out)
    assert loader(out).run() == {"sources": [], "removed": [], "full": False}
    (out / "projects" / "detail.md").write_bytes((out / "projects" / "detail.md").read_bytes() + b"\nmore\n")
    shutil.copy(FIXTURES / "vec_detail_page_text.md", out / "projects" / "new.md")
    (out / "programme" / "country-croatia.md").unlink()
    with runlog.open_run(tmp_path / "logs", "r1") as run:
        res = loader(out).run()
    assert res["full"] is False
    assert sorted(s.meta["file_path"] for s in res["sources"]) == ["projects/detail.md", "projects/new.md"]
    assert res["removed"] == ["programme/country-croatia.md"]
    counts = run.final_summary["counts"]
    assert counts["load:new"] == 1 and counts["load:changed"] == 1 and counts["load:removed"] == 1


@pytest.mark.parametrize("change", ["embedder", "chunker", "store", "flag"])
def test_loader_forces_full(tmp_path, change):
    out = make_output(tmp_path)
    indexed_state(out, version=CHUNKER_VERSION - 1 if change == "chunker" else CHUNKER_VERSION)
    if change == "store":
        store_path(out / "vectors").unlink()
    res = loader(out, embedder_id="other:m" if change == "embedder" else "mock:m").run(full=change == "flag")
    assert res["full"] is True and len(res["sources"]) == 3 and res["removed"] == []
```

- [ ] **Step 2 : lancer, échec attendu**

Run : `.venv/Scripts/python -m pytest -q tests/test_vector_layer.py`
Attendu : `ModuleNotFoundError: No module named 'tsi_agent.components.file_loader'` (ou `tsi_agent.vector_state`).

- [ ] **Step 3 : `vector_state.py`**

```python
# src/tsi_agent/vector_state.py
"""Etat de la couche vectorisee : `<vectors_dir>/state.json` (fichiers indexes) et `store.json` (morceaux)."""

import json
import os
from pathlib import Path

from haystack import Document
from haystack.document_stores.in_memory import InMemoryDocumentStore
from haystack.document_stores.types import DuplicatePolicy
from pydantic import BaseModel, Field

from tsi_agent.store import _retry_on_lock

STATE_FILE = "state.json"
STORE_FILE = "store.json"


class VectorLayerError(RuntimeError):
    pass


class FileState(BaseModel):
    hash: str
    chunk_ids: list[str] = Field(default_factory=list)


class VectorState(BaseModel):
    embedder_id: str | None = None
    chunker_version: int | None = None
    files: dict[str, FileState] = Field(default_factory=dict)


def _write_atomic(path: Path, payload: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")

    def write_and_replace() -> None:
        tmp.write_text(payload, encoding="utf-8")
        os.replace(tmp, path)

    _retry_on_lock(write_and_replace)


def store_path(vectors_dir: str | Path) -> Path:
    return Path(vectors_dir) / STORE_FILE


def load_state(vectors_dir: str | Path) -> VectorState | None:
    path = Path(vectors_dir) / STATE_FILE
    return VectorState.model_validate_json(path.read_text(encoding="utf-8")) if path.exists() else None


def save_state(vectors_dir: str | Path, state: VectorState) -> None:
    data = state.model_dump()
    data["files"] = dict(sorted(data["files"].items()))
    _write_atomic(Path(vectors_dir) / STATE_FILE, json.dumps(data, indent=1, ensure_ascii=False))


def new_store() -> InMemoryDocumentStore:
    # shared=False : pas de stockage partage entre instances du processus (tests, runs successifs).
    return InMemoryDocumentStore(embedding_similarity_function="cosine", shared=False)


def load_store(path: Path) -> InMemoryDocumentStore:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        documents = [Document.from_dict(d) for d in data["documents"]]
    except (OSError, ValueError, KeyError, TypeError) as exc:
        raise VectorLayerError(f"couche vectorisee illisible ({path}) : relancer avec --full") from exc
    store = new_store()
    store.write_documents(documents, policy=DuplicatePolicy.OVERWRITE)
    return store


def save_store(store: InMemoryDocumentStore, path: Path) -> None:
    """Meme format que InMemoryDocumentStore.save_to_disk, relisible par load_from_disk (futur chatbot)."""
    data = store.to_dict()
    data["documents"] = [d.to_dict(flatten=False) for d in store.filter_documents()]
    _write_atomic(path, json.dumps(data, ensure_ascii=False))
```

- [ ] **Step 4 : `ProjectFileLoader`**

```python
# src/tsi_agent/components/file_loader.py
"""Composant Haystack local : fichiers extraits nouveaux, modifies ou supprimes depuis le dernier index.

Dans deepset, ce role est tenu par l'espace de travail (les fichiers ajoutes ou modifies sont indexes).
"""

import hashlib
from pathlib import Path
from typing import Any

from haystack import component
from haystack.dataclasses import ByteStream

from tsi_agent import runlog
from tsi_agent.chunking import CHUNKER_VERSION
from tsi_agent.vector_state import load_state, store_path

FOLDERS = ("projects", "programme")


@component
class ProjectFileLoader:
    def __init__(self, output_dir: str = "output", vectors_dir: str = "output/vectors", embedder_id: str = "",
                 chunker_version: int = CHUNKER_VERSION):
        self.output_dir = output_dir
        self.vectors_dir = vectors_dir
        self.embedder_id = embedder_id
        self.chunker_version = chunker_version

    def _full_reason(self, full: bool) -> str | None:
        state = load_state(self.vectors_dir)
        if full:
            return "requested"
        if state is None:
            return "no_state"
        if not store_path(self.vectors_dir).exists():
            return "no_store"
        if state.embedder_id != self.embedder_id:
            return "embedder_changed"
        if state.chunker_version != self.chunker_version:
            return "chunker_changed"
        return None

    @component.output_types(sources=list[ByteStream], removed=list[str], full=bool)
    def run(self, full: bool = False) -> dict[str, Any]:
        reason = self._full_reason(full)
        if reason:
            runlog.event("ProjectFileLoader", "full", None, reason)
        state = load_state(self.vectors_dir)
        known = {} if reason or state is None else state.files
        root = Path(self.output_dir)
        sources: list[ByteStream] = []
        seen: set[str] = set()
        unchanged = 0
        for folder in FOLDERS:
            for path in sorted((root / folder).glob("*.md")):
                rel = path.relative_to(root).as_posix()
                seen.add(rel)
                data = path.read_bytes()
                digest = "sha256:" + hashlib.sha256(data).hexdigest()
                old = known.get(rel)
                if old is not None and old.hash == digest:
                    unchanged += 1
                    continue
                runlog.event("ProjectFileLoader", "load", rel, "changed" if old else "new")
                sources.append(ByteStream(data=data, meta={"file_path": rel, "content_hash": digest}))
        removed = sorted(set(known) - seen)
        for rel in removed:
            runlog.event("ProjectFileLoader", "load", rel, "removed")
        runlog.event("ProjectFileLoader", "load", None, "unchanged", files=unchanged)
        return {"sources": sources, "removed": removed, "full": reason is not None}
```

Dans `src/tsi_agent/components/__init__.py`, ajouter `from tsi_agent.components.file_loader import ProjectFileLoader` et `"ProjectFileLoader"` dans `__all__`.

- [ ] **Step 5 : lancer, succès attendu**

Run : `.venv/Scripts/python -m pytest -q tests/test_vector_layer.py`
Attendu : tous verts.

- [ ] **Step 6 : commit**

```bash
git add src/tsi_agent/vector_state.py src/tsi_agent/components/file_loader.py src/tsi_agent/components/__init__.py tests/test_vector_layer.py
git commit -m "feat: etat de la couche vectorisee et ProjectFileLoader (incremental, full force)

Co-Authored-By: Claude Opus 5.5 (1M context) <noreply@anthropic.com>"
```

---

### Task 5 : `VectorLayerWriter` et coûts d'embedding dans le journal

**Files :**
- Create : `src/tsi_agent/components/vector_writer.py`
- Modify : `src/tsi_agent/runlog.py`, `src/tsi_agent/components/__init__.py`
- Test : `tests/test_vector_layer.py`, `tests/test_runlog.py`

- [ ] **Step 1 : test du journal (à ajouter à `tests/test_runlog.py`)**

```python
def test_embed_events_count_tokens_and_cost_but_not_llm_calls(tmp_path):
    from tsi_agent import runlog
    with runlog.open_run(tmp_path, "e1") as run:
        runlog.event("VectorLayerWriter", "embed", "m", input_tokens=1000, cost_usd=0.00002)
    s = run.final_summary
    assert s["llm_calls"] == 0 and s["tokens"]["input_tokens"] == 1000 and s["cost_usd"] == 0.0
    assert run.cost_usd == pytest.approx(0.00002)
```

(Ajouter `import pytest` en tête du fichier s'il n'y est pas. `cost_usd` est arrondi à 4 décimales dans le résumé, d'où la vérification sur `run.cost_usd`.)

- [ ] **Step 2 : tests du writer (à ajouter à `tests/test_vector_layer.py`)**

```python
from haystack.components.embedders import MockDocumentEmbedder

from tsi_agent.components.vector_writer import VectorLayerWriter


def index_once(out, *, full=False, embedder_id="MockDocumentEmbedder:mock", pricing=None):
    """Un passage complet loader -> chunker -> mock embedder -> writer, sans Pipeline."""
    loaded = ProjectFileLoader(output_dir=str(out), vectors_dir=str(out / "vectors"),
                               embedder_id=embedder_id).run(full=full)
    chunked = ProjectChunker().run(sources=loaded["sources"])
    embedded = MockDocumentEmbedder(model="mock", dimension=8).run(documents=chunked["documents"])
    writer = VectorLayerWriter(vectors_dir=str(out / "vectors"), embedder_id=embedder_id, pricing=pricing)
    return writer.run(documents=embedded["documents"], sources=loaded["sources"], removed=loaded["removed"],
                      full=loaded["full"], skipped=chunked["skipped"], embed_meta=embedded["meta"])["stats"]


def stored_ids(out):
    return sorted(d.id for d in load_store(store_path(out / "vectors")).filter_documents())


def test_writer_full_then_incremental(tmp_path):
    out = make_output(tmp_path)
    stats = index_once(out)
    assert stats["files_indexed"] == 3 and stats["chunks_deleted"] == 0
    total = stats["chunks_total"]
    assert total == len(stored_ids(out)) and "energy-regulation-academy#0" in stored_ids(out)
    state = load_state(out / "vectors")
    assert state.embedder_id == "MockDocumentEmbedder:mock" and state.chunker_version == CHUNKER_VERSION
    assert state.files["projects/thin.md"].chunk_ids == ["srsp-2016-technical-support-for-the-property-tax-reform#0"]

    assert index_once(out) == {"files_indexed": 0, "files_removed": 0, "chunks_written": 0, "chunks_deleted": 0,
                               "chunks_total": total}

    (out / "projects" / "thin.md").unlink()
    stats = index_once(out)
    assert stats["files_removed"] == 1 and stats["chunks_deleted"] == 1 and stats["chunks_total"] == total - 1
    assert "projects/thin.md" not in load_state(out / "vectors").files


def test_writer_changed_file_replaces_its_chunks(tmp_path):
    out = make_output(tmp_path)
    index_once(out)
    before = stored_ids(out)
    detail = out / "projects" / "detail.md"
    detail.write_text(detail.read_text(encoding="utf-8").replace("## Context", "## Context\n\nNew first line."),
                      encoding="utf-8")
    stats = index_once(out)
    assert stats["files_indexed"] == 1 and stats["chunks_deleted"] == stats["chunks_written"]
    assert stored_ids(out) == before
    ctx = [d for d in load_store(store_path(out / "vectors")).filter_documents()
           if d.meta["section"] == "Context"][0]
    assert ctx.content.startswith("New first line.")


def test_writer_skipped_file_is_retried_next_run(tmp_path):
    out = make_output(tmp_path)
    index_once(out)
    (out / "projects" / "thin.md").write_text("broken", encoding="utf-8")
    index_once(out)
    assert "projects/thin.md" not in load_state(out / "vectors").files
    assert not any(i.startswith("srsp-2016") for i in stored_ids(out))
    shutil.copy(FIXTURES / "vec_thin.md", out / "projects" / "thin.md")
    assert index_once(out)["files_indexed"] == 1


def test_writer_full_rebuilds_the_store(tmp_path):
    out = make_output(tmp_path)
    total = index_once(out)["chunks_total"]
    stats = index_once(out, embedder_id="MockDocumentEmbedder:other")
    assert stats["files_indexed"] == 3 and stats["chunks_total"] == total
    assert load_state(out / "vectors").embedder_id == "MockDocumentEmbedder:other"


def test_writer_logs_embedding_tokens_and_cost(tmp_path):
    out = make_output(tmp_path)
    with runlog.open_run(tmp_path / "logs", "r1") as run:
        index_once(out, pricing={"mock": {"input": 1_000_000.0}})
    s = run.final_summary
    assert s["counts"]["embed:ok"] == 1 and s["tokens"]["input_tokens"] > 0
    assert s["cost_usd"] == pytest.approx(s["tokens"]["input_tokens"], rel=1e-6)
    assert s["counts"]["write:ok"] == 1


def test_writer_estimates_tokens_without_usage(tmp_path):
    docs = [Document(id="a#0", content="one two three", meta={"source_file": "a.md", "context_header": "T"})]
    with runlog.open_run(tmp_path / "logs", "r1") as run:
        VectorLayerWriter(vectors_dir=str(tmp_path / "v"), embedder_id="x").run(
            documents=docs, sources=[ByteStream(data=b"", meta={"file_path": "a.md", "content_hash": "h"})],
            removed=[], full=True)
    assert run.final_summary["tokens"]["input_tokens"] == round(4 * 1.33)
```

- [ ] **Step 3 : lancer, échec attendu**

Run : `.venv/Scripts/python -m pytest -q tests/test_vector_layer.py tests/test_runlog.py`
Attendu : `ModuleNotFoundError: No module named 'tsi_agent.components.vector_writer'` et échec de `test_embed_events_count_tokens_and_cost_but_not_llm_calls`.

- [ ] **Step 4 : journal**

Dans `src/tsi_agent/runlog.py`, méthode `RunLog.event`, remplacer :

```python
            if action == "llm_call":
                self.llm_calls += 1
                for name in LLM_TOKEN_FIELDS:
```

par :

```python
            # Les embeddings (pipeline `index`) comptent dans les tokens et le cout, pas dans llm_calls.
            if action in ("llm_call", "embed"):
                if action == "llm_call":
                    self.llm_calls += 1
                for name in LLM_TOKEN_FIELDS:
```

(le reste du bloc, tokens et coût, est inchangé et reste indenté sous le nouveau `if`).

- [ ] **Step 5 : `VectorLayerWriter`**

```python
# src/tsi_agent/components/vector_writer.py
"""Composant Haystack local : ecrit les morceaux vectorises dans output/vectors (store.json puis state.json).

Dans deepset, remplace par un DocumentWriter vers le document store de l'espace de travail.
"""

from collections import defaultdict
from pathlib import Path
from typing import Any

from haystack import Document, component
from haystack.dataclasses import ByteStream
from haystack.document_stores.types import DuplicatePolicy

from tsi_agent import runlog
from tsi_agent.chunking import CHUNKER_VERSION
from tsi_agent.vector_state import FileState, VectorState, load_state, load_store, new_store, save_state, \
    save_store, store_path

TOKENS_PER_WORD = 1.33


@component
class VectorLayerWriter:
    def __init__(self, vectors_dir: str = "output/vectors", embedder_id: str = "",
                 chunker_version: int = CHUNKER_VERSION, pricing: dict[str, dict[str, float]] | None = None):
        """:param pricing: $ par million de tokens par prefixe de modele, ex. {"text-embedding-3-small": {"input": 0.02}}."""
        self.vectors_dir = vectors_dir
        self.embedder_id = embedder_id
        self.chunker_version = chunker_version
        self.pricing = pricing or {}

    def _cost(self, model: str, tokens: int) -> float:
        for prefix, price in sorted(self.pricing.items(), key=lambda kv: len(kv[0]), reverse=True):
            if model.startswith(prefix):
                return tokens * price.get("input", 0) / 1_000_000
        return 0.0

    def _log_embedding(self, documents: list[Document], meta: dict[str, Any] | None) -> None:
        if not documents:
            return
        meta = meta or {}
        usage = meta.get("usage") or {}
        tokens = int(usage.get("prompt_tokens") or usage.get("total_tokens") or 0)
        estimated = tokens == 0
        if estimated:
            words = sum(len(f"{d.meta.get('context_header') or ''} {d.content or ''}".split()) for d in documents)
            tokens = round(words * TOKENS_PER_WORD)
        model = str(meta.get("model") or self.embedder_id)
        runlog.event("VectorLayerWriter", "embed", model, chunks=len(documents), input_tokens=tokens,
                     tokens_estimated=estimated, model=model, cost_usd=self._cost(model, tokens))

    @component.output_types(stats=dict[str, int])
    def run(self, documents: list[Document], sources: list[ByteStream], removed: list[str], full: bool = False,
            skipped: list[str] | None = None, embed_meta: dict[str, Any] | None = None) -> dict[str, Any]:
        vdir = Path(self.vectors_dir)
        state = None if full else load_state(vdir)
        if state is None:
            state, store = VectorState(), new_store()
        else:
            store = load_store(store_path(vdir))
        state.embedder_id, state.chunker_version = self.embedder_id, self.chunker_version

        touched = [str(s.meta["file_path"]) for s in sources] + list(removed)
        stale = [cid for rel in touched if rel in state.files for cid in state.files[rel].chunk_ids]
        if stale:
            store.delete_documents(stale)
        if documents:
            store.write_documents(documents, policy=DuplicatePolicy.OVERWRITE)

        by_file: dict[str, list[str]] = defaultdict(list)
        for doc in documents:
            by_file[doc.meta["source_file"]].append(doc.id)
        skipped_set = set(skipped or [])
        for src in sources:
            rel = str(src.meta["file_path"])
            if rel in skipped_set:
                state.files.pop(rel, None)  # retente au prochain run
            else:
                state.files[rel] = FileState(hash=src.meta["content_hash"], chunk_ids=by_file.get(rel, []))
        for rel in removed:
            state.files.pop(rel, None)

        self._log_embedding(documents, embed_meta)
        if full or sources or removed:
            save_store(store, store_path(vdir))  # store d'abord : un arret ici laisse l'ancien etat, retente
            save_state(vdir, state)
        stats = {"files_indexed": len(sources) - len(skipped_set & {str(s.meta["file_path"]) for s in sources}),
                 "files_removed": len(removed), "chunks_written": len(documents), "chunks_deleted": len(stale),
                 "chunks_total": store.count_documents()}
        runlog.event("VectorLayerWriter", "write", None, **stats)
        return {"stats": stats}
```

Dans `src/tsi_agent/components/__init__.py`, ajouter `from tsi_agent.components.vector_writer import VectorLayerWriter` et `"VectorLayerWriter"` dans `__all__`.

Note sur `test_writer_skipped_file_is_retried_next_run` : au deuxième run, `thin.md` est « changed » puis ignoré par le chunker ; ses anciens morceaux sont supprimés (il est dans `touched`) et il sort de l'état. Au troisième run il redevient « new ».

- [ ] **Step 6 : lancer, succès attendu**

Run : `.venv/Scripts/python -m pytest -q tests/test_vector_layer.py tests/test_runlog.py`
Attendu : tous verts.

- [ ] **Step 7 : suite complète puis commit**

Run : `.venv/Scripts/python -m pytest -q` → tout vert.

```bash
git add src/tsi_agent/components/vector_writer.py src/tsi_agent/components/__init__.py src/tsi_agent/runlog.py tests/test_vector_layer.py tests/test_runlog.py
git commit -m "feat: VectorLayerWriter (store.json puis state.json) et cout des embeddings dans le journal

Co-Authored-By: Claude Opus 5.5 (1M context) <noreply@anthropic.com>"
```

---

### Task 6 : pipeline `index` et export YAML

**Files :**
- Create : `src/tsi_agent/index_pipeline.py`
- Test : `tests/test_index_pipeline.py`

- [ ] **Step 1 : tests**

```python
# tests/test_index_pipeline.py
import shutil

import yaml
from haystack.components.embedders import MockDocumentEmbedder

from conftest import FIXTURES
from tsi_agent.config import Settings
from tsi_agent.index_pipeline import build_index_pipeline, embedder_id
from tsi_agent.pipeline import load_pipeline
from tsi_agent.vector_state import load_state


def settings_for(tmp_path) -> Settings:
    out = tmp_path / "output"
    (out / "projects").mkdir(parents=True)
    (out / "programme").mkdir()
    shutil.copy(FIXTURES / "vec_thin.md", out / "projects" / "thin.md")
    shutil.copy(FIXTURES / "vec_detail_sections.md", out / "projects" / "detail.md")
    return Settings(output_dir=str(out), vectors_dir=str(out / "vectors"), logs_dir=str(tmp_path / "logs"))


def mock():
    return MockDocumentEmbedder(model="mock", dimension=8, meta_fields_to_embed=["context_header"])


def test_embedder_id():
    assert embedder_id(mock()) == "MockDocumentEmbedder:mock"


def test_pipeline_runs_end_to_end(tmp_path):
    s = settings_for(tmp_path)
    out = build_index_pipeline(s, embedder=mock()).run({"loader": {"full": False}},
                                                       include_outputs_from={"vector_writer"})
    assert out["vector_writer"]["stats"]["files_indexed"] == 2
    assert load_state(s.vectors_dir).embedder_id == "MockDocumentEmbedder:mock"


def test_yaml_round_trip(tmp_path):
    s = settings_for(tmp_path)
    text = build_index_pipeline(s, embedder=mock()).dumps()
    for name in ("ProjectFileLoader", "ProjectChunker", "MockDocumentEmbedder", "VectorLayerWriter"):
        assert name in text
    reloaded = load_pipeline(text)
    out = reloaded.run({"loader": {"full": False}}, include_outputs_from={"vector_writer"})
    assert out["vector_writer"]["stats"]["files_indexed"] == 2


def test_embedder_can_be_swapped_for_openai_in_yaml(tmp_path, monkeypatch):
    monkeypatch.setenv("OPENAI_API_KEY", "sk-test")
    data = yaml.safe_load(build_index_pipeline(settings_for(tmp_path), embedder=mock()).dumps())
    data["components"]["embedder"] = {
        "type": "haystack.components.embedders.openai_document_embedder.OpenAIDocumentEmbedder",
        "init_parameters": {"model": "text-embedding-3-small", "meta_fields_to_embed": ["context_header"],
                            "api_key": {"type": "env_var", "env_vars": ["OPENAI_API_KEY"], "strict": True}},
    }
    pipe = load_pipeline(yaml.safe_dump(data))
    assert type(pipe.get_component("embedder")).__name__ == "OpenAIDocumentEmbedder"
```

- [ ] **Step 2 : lancer, échec attendu**

Run : `.venv/Scripts/python -m pytest -q tests/test_index_pipeline.py`
Attendu : `ModuleNotFoundError: No module named 'tsi_agent.index_pipeline'`.

- [ ] **Step 3 : implémentation**

```python
# src/tsi_agent/index_pipeline.py
"""Pipeline Haystack d'indexation (couche vectorisee), exportable en YAML pour un index deepset."""

from typing import Any

from haystack import Pipeline

from tsi_agent.components import ProjectChunker, ProjectFileLoader, VectorLayerWriter
from tsi_agent.config import Settings


def default_embedder(settings: Settings) -> Any:
    """Embedder local gratuit. Le prefixe `passage: ` est exige par les modeles e5 (requetes : `query: `)."""
    try:
        from haystack_integrations.components.embedders.sentence_transformers import \
            SentenceTransformersDocumentEmbedder
    except ImportError as exc:
        raise RuntimeError('embedder local absent : installer avec  pip install -e ".[local]"') from exc
    return SentenceTransformersDocumentEmbedder(model=settings.embedding_model, prefix="passage: ",
                                                normalize_embeddings=True, meta_fields_to_embed=["context_header"],
                                                batch_size=settings.embedding_batch_size, progress_bar=False)


def embedder_id(embedder: Any) -> str:
    return f"{type(embedder).__name__}:{getattr(embedder, 'model', 'unknown')}"


def build_index_pipeline(settings: Settings, embedder: Any = None) -> Pipeline:
    embedder = embedder if embedder is not None else default_embedder(settings)
    eid = embedder_id(embedder)
    p = Pipeline()
    p.add_component("loader", ProjectFileLoader(output_dir=settings.output_dir, vectors_dir=settings.vectors_dir,
                                                embedder_id=eid))
    p.add_component("chunker", ProjectChunker())
    p.add_component("embedder", embedder)
    p.add_component("vector_writer", VectorLayerWriter(vectors_dir=settings.vectors_dir, embedder_id=eid,
                                                       pricing=settings.pricing))
    p.connect("loader.sources", "chunker.sources")
    p.connect("chunker.documents", "embedder.documents")
    p.connect("embedder.documents", "vector_writer.documents")
    p.connect("loader.sources", "vector_writer.sources")
    p.connect("loader.removed", "vector_writer.removed")
    p.connect("loader.full", "vector_writer.full")
    p.connect("chunker.skipped", "vector_writer.skipped")
    # OpenAI et Mock renvoient l'usage (tokens) ; sentence-transformers n'a pas de sortie `meta`.
    if hasattr(embedder.__haystack_output__, "meta"):
        p.connect("embedder.meta", "vector_writer.embed_meta")
    return p
```

Point d'attention : un `Pipeline` Haystack n'exécute un composant que lorsque toutes ses entrées obligatoires sont reçues. `vector_writer.documents` vient de l'embedder, qui reçoit `chunker.documents` même vide : le writer tourne donc aussi quand rien n'a changé. Si `test_pipeline_runs_end_to_end` ou un run sans changement montre que le writer ne s'exécute pas, vérifier que `ProjectChunker` renvoie bien `documents=[]` (et non une absence de clé).

- [ ] **Step 4 : lancer, succès attendu**

Run : `.venv/Scripts/python -m pytest -q tests/test_index_pipeline.py`
Attendu : 4 passed.

- [ ] **Step 5 : commit**

```bash
git add src/tsi_agent/index_pipeline.py tests/test_index_pipeline.py
git commit -m "feat: pipeline Haystack d'indexation, embedder remplacable par YAML

Co-Authored-By: Claude Opus 5.5 (1M context) <noreply@anthropic.com>"
```

---

### Task 7 : runner et CLI

**Files :**
- Modify : `src/tsi_agent/runner.py`, `src/tsi_agent/__main__.py`
- Test : `tests/test_index_pipeline.py`, `tests/test_cli.py`

- [ ] **Step 1 : tests**

À ajouter à `tests/test_index_pipeline.py` :

```python
from tsi_agent.runner import run_index


def test_run_index_summary(tmp_path):
    s = settings_for(tmp_path)
    summary = run_index(s, embedder=mock(), run_id="i1")
    assert summary["status"] == "ok" and summary["mode"] == "index" and summary["full"] is True
    assert summary["stats"]["files_indexed"] == 2 and summary["skipped"] == []
    assert summary["counts"]["embed:ok"] == 1
    again = run_index(s, embedder=mock(), run_id="i2")
    assert again["full"] is False and again["stats"]["files_indexed"] == 0
```

À ajouter à `tests/test_cli.py` :

```python
def test_index_command_and_exit_codes(monkeypatch, capsys):
    calls = {}

    def fake(settings, *, full=False, **kw):
        calls["full"] = full
        return {"status": "ok"}
    monkeypatch.setattr(cli, "run_index", fake)
    assert cli.main(["index", "--full"]) == 0 and calls["full"] is True

    def boom(*a, **k):
        raise RuntimeError("kaput")
    monkeypatch.setattr(cli, "run_index", boom)
    assert cli.main(["index"]) == 2
    assert "kaput" in capsys.readouterr().err


def test_export_index_yaml(tmp_path, monkeypatch):
    from haystack.components.embedders import MockDocumentEmbedder
    monkeypatch.setattr("tsi_agent.index_pipeline.default_embedder", lambda s: MockDocumentEmbedder(model="m"))
    out = tmp_path / "index.yaml"
    assert cli.main(["export-yaml", "--index", str(out)]) == 0
    assert "VectorLayerWriter" in out.read_text(encoding="utf-8")
```

- [ ] **Step 2 : lancer, échec attendu**

Run : `.venv/Scripts/python -m pytest -q tests/test_index_pipeline.py tests/test_cli.py`
Attendu : `ImportError: cannot import name 'run_index'` et erreur argparse sur `index`.

- [ ] **Step 3 : `run_index` dans `src/tsi_agent/runner.py`**

Ajouter l'import `from tsi_agent.index_pipeline import build_index_pipeline` et, à la fin du fichier :

```python
def run_index(settings: Settings, *, full: bool = False, embedder: Any = None, run_id: str | None = None,
              pipeline_factory: PipelineFactory = build_index_pipeline) -> dict[str, Any]:
    """Vectorise les fichiers extraits nouveaux ou modifies (tout, au premier run ou avec full)."""
    with runlog.open_run(settings.logs_dir, run_id) as run:
        pipeline = pipeline_factory(settings, embedder=embedder)
        out = pipeline.run({"loader": {"full": full}},
                           include_outputs_from={"loader", "chunker", "vector_writer"})
        run.extra.update({"mode": "index", "full": out["loader"]["full"],
                          "stats": out["vector_writer"]["stats"], "skipped": out["chunker"]["skipped"]})
    return run.final_summary
```

- [ ] **Step 4 : CLI dans `src/tsi_agent/__main__.py`**

Modifier l'import : `from tsi_agent.runner import run_extraction, run_index` et ajouter `from tsi_agent.index_pipeline import build_index_pipeline`.

Après la définition du sous-parseur `run` et avant `export = ...`, ajouter :

```python
    index = sub.add_parser("index", help="vectoriser les fichiers extraits nouveaux ou modifiés (couche RAG)")
    index.add_argument("--full", action="store_true", help="revectoriser tous les fichiers")
```

Après `export.add_argument("path")`, ajouter :

```python
    export.add_argument("--index", action="store_true", help="exporter le pipeline d'indexation (couche vectorisée)")
```

Remplacer le bloc `if args.command == "export-yaml": ...` par :

```python
    if args.command == "export-yaml":
        pipe = build_index_pipeline(settings) if args.index else build_pipeline(settings, sources)
        Path(args.path).write_text(pipe.dumps(), encoding="utf-8")
        print(f"Pipeline exporté vers {args.path}")
        return 0
    if args.command == "index":
        try:
            summary = run_index(settings, full=args.full)
        except Exception as exc:
            print(f"ERREUR : {exc!r} — voir le journal du run dans {settings.logs_dir}/runs/", file=sys.stderr)
            traceback.print_exc(file=sys.stderr)
            return 2
        print(json.dumps(summary, indent=2, ensure_ascii=False))
        return 0
```

Pour que `test_export_index_yaml` puisse remplacer `default_embedder`, `build_index_pipeline` doit l'appeler via le module : c'est le cas (`default_embedder(settings)` résolu dans `tsi_agent.index_pipeline` au moment de l'appel).

- [ ] **Step 5 : lancer, succès attendu**

Run : `.venv/Scripts/python -m pytest -q`
Attendu : tout vert.

- [ ] **Step 6 : commit**

```bash
git add src/tsi_agent/runner.py src/tsi_agent/__main__.py tests/test_index_pipeline.py tests/test_cli.py
git commit -m "feat: commande index et export-yaml --index

Co-Authored-By: Claude Opus 5.5 (1M context) <noreply@anthropic.com>"
```

---

### Task 8 : README, installation locale et premier run réel

**Files :**
- Modify : `README.md`, `.gitignore` (vérifier seulement)
- Test : `tests/test_index_pipeline.py` (test d'intégration marqué)

- [ ] **Step 1 : section README**

Ajouter après la section « Sorties » :

````markdown
## Couche vectorisée (préparation du RAG)

Un second pipeline, `index`, découpe les fichiers de `output/projects` et `output/programme` en morceaux (un par
section, redécoupé au-delà de 300 mots), les vectorise et les range dans `output/vectors/`. Les fichiers extraits
ne sont pas modifiés : la couche vectorisée peut être supprimée et reconstruite à tout moment.

```powershell
# Une fois : embedder local (PyTorch CPU + modèle multilingual-e5-small, ~1 à 2 Go)
.venv\Scripts\python -m pip install -e ".[local]"

# Premier run : tout ; ensuite : seulement les fichiers nouveaux, modifiés ou supprimés
.venv\Scripts\python -m tsi_agent index
.venv\Scripts\python -m tsi_agent index --full

# Export pour un index deepset
.venv\Scripts\python -m tsi_agent export-yaml --index index.yaml
```

| Chemin | Contenu |
|---|---|
| `output/vectors/store.json` | Morceaux avec texte, métadonnées et vecteur (format `InMemoryDocumentStore.save_to_disk`) |
| `output/vectors/state.json` | Hash de chaque fichier indexé et identifiants de ses morceaux. Ne pas éditer à la main. |

Chaque morceau porte en métadonnées le projet (`doc_id`, titre, programme, année, pays, domaine, `detail_page`…),
la section et `ai_generated` (vrai pour le résumé IA). Aucun LLM n'intervient : seul l'embedder travaille.
Changer de modèle d'embedding ou de règles de découpage déclenche automatiquement un full.

Dans deepset, l'embedder devient `OpenAIDocumentEmbedder` (même `meta_fields_to_embed`, sans préfixe), et on ajoute
son tarif pour journaliser le coût :

```yaml
embedder:
  type: haystack.components.embedders.openai_document_embedder.OpenAIDocumentEmbedder
  init_parameters:
    model: text-embedding-3-small
    meta_fields_to_embed: [context_header]
    api_key: {type: env_var, env_vars: [OPENAI_API_KEY], strict: true}
```

```yaml
# config/settings.yaml
pricing:
  text-embedding-3-small: {input: 0.02}
```

Le chargeur local (`ProjectFileLoader`) et le writer local (`VectorLayerWriter`) sont remplacés dans deepset par
l'entrée fichiers de l'index et un `DocumentWriter` vers le document store de l'espace de travail ; `ProjectChunker`
reste identique. Le futur pipeline de requête devra utiliser le même embedder (en local : préfixe `query: `).
````

Vérifier que `output/` est dans `.gitignore` (c'est le cas : `output/vectors/` n'est pas versionné).

- [ ] **Step 2 : test d'intégration marqué**

Dans `pyproject.toml`, sous `[tool.pytest.ini_options]`, ajouter :

```toml
markers = ["integration: vrai modele d'embedding, lance a la main (pytest -m integration)"]
addopts = "-m 'not integration'"
```

À ajouter à `tests/test_index_pipeline.py` :

```python
import pytest


@pytest.mark.integration
def test_real_model_finds_project_from_french_question(tmp_path):
    pytest.importorskip("haystack_integrations.components.embedders.sentence_transformers")
    from haystack_integrations.components.embedders.sentence_transformers import SentenceTransformersTextEmbedder

    from tsi_agent.index_pipeline import default_embedder
    from tsi_agent.vector_state import load_store, store_path

    s = settings_for(tmp_path)
    shutil.copy(FIXTURES / "vec_detail_long_section.md", tmp_path / "output" / "projects" / "skills.md")
    run_index(s, embedder=default_embedder(s), run_id="it1")
    store = load_store(store_path(s.vectors_dir))
    query = SentenceTransformersTextEmbedder(model=s.embedding_model, prefix="query: ", normalize_embeddings=True)
    vec = query.run(text="formation des régulateurs de l'électricité")["embedding"]
    best = store.embedding_retrieval(query_embedding=vec, top_k=1)[0]
    assert best.meta["doc_id"] == "energy-regulation-academy"
```

- [ ] **Step 3 : installer l'extra local et lancer l'intégration**

```bash
.venv/Scripts/python -m pip install -e ".[local]"
.venv/Scripts/python -m pytest -q -m integration
```

Attendu : 1 passed (le premier lancement télécharge le modèle dans le cache Hugging Face de l'utilisateur, hors OneDrive).

- [ ] **Step 4 : suite complète**

Run : `.venv/Scripts/python -m pytest -q`
Attendu : tout vert, le test d'intégration désélectionné.

- [ ] **Step 5 : commit**

```bash
git add README.md pyproject.toml tests/test_index_pipeline.py
git commit -m "docs: couche vectorisee (usage, sorties, passage a OpenAI) ; test d'integration avec le vrai modele

Co-Authored-By: Claude Opus 5.5 (1M context) <noreply@anthropic.com>"
```

- [ ] **Step 6 : premier run réel (contrôleur, pas sous-agent)**

```bash
PYTHONIOENCODING=utf-8 .venv/Scripts/python -m tsi_agent index > logs/index1_summary.json 2> logs/index1_stderr.txt; echo "exit=$?"
```

Attendu : `exit=0`, `full: true`, `files_indexed` = 2 137 (2 100 projets + 37 programme), `skipped: []`, puis un
second run immédiat avec `files_indexed: 0`. Noter la durée, `chunks_total` et la taille de `store.json`.
