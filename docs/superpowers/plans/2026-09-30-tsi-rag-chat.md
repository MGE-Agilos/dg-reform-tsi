# Agent RAG / chatbot — Plan d'implémentation

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal :** un chatbot local (page web) qui répond aux questions sur les projets TSI/SRSP avec un `Agent` Haystack et trois outils sur la couche vectorisée, en passant par le CLI `claude` en local et par OpenAI dans deepset.

**Architecture :** pipeline `chat` = un composant `haystack.components.agents.Agent`. Son `chat_generator` est un `LoggedChatGenerator`, qui journalise les appels, autour de `ClaudeCliChatGenerator`, étendu pour émuler l'appel d'outils. Ses outils sont trois `ComponentTool` (`ProjectSearch`, `ProjectLister`, `ProjectReader`) branchés sur un `PersistedInMemoryDocumentStore` qui charge `output/vectors/store.json`. Les sources sont numérotées de façon continue grâce à l'état de l'Agent (`state_schema` + `inputs_from_state`/`outputs_to_state`). Un service `ask()` ouvre un run `runlog` par question. Un serveur Flask sert une page unique.

**Tech stack :** Python 3.11+, haystack-ai 3.2, sentence-transformers-haystack (`[local]`), Flask (`[local]`), pytest.

**Spec :** [docs/superpowers/specs/2026-09-30-tsi-rag-chat-design.md](../specs/2026-09-30-tsi-rag-chat-design.md)

**Conventions du dépôt :**
- Racine : `C:\Users\MGE\OneDrive - Agilos\Documents\Claude Code Projects\DG REFORM\Haystack Pipeline`, branche `feature/rag-chat`.
- Tests : `.venv/Scripts/python -m pytest -q` (189 passent, 1 test d'intégration désélectionné).
- Chaque commit se termine par la ligne `Co-Authored-By: Claude Opus 5.5 (1M context) <noreply@anthropic.com>`, sans autre attribution.
- Commentaires en français sans accents dans le code (style des fichiers voisins).
- Ne rien modifier sous `output/`.

**Faits vérifiés sur Haystack 3.2 (prototype) :**
- `Agent.run(messages=...)` appelle `chat_generator.run(messages=..., tools=...)`. Le `system_prompt` de l'Agent est rendu comme un template Jinja : il ne faut **ni `{{` ni `{%`** dans le prompt.
- Les outils s'exécutent dans un `ThreadPoolExecutor`, avec le contexte (contextvars) propagé : `runlog.event` fonctionne dans les outils.
- `ComponentTool(component, name, description, outputs_to_string={"source": "result"}, outputs_to_state={"sources": {"source": "sources"}}, inputs_from_state={"sources": "known_sources"})` et `Agent(..., state_schema={"sources": {"type": list[dict]}})` : le paramètre `known_sources` est caché au LLM et reçoit les sources déjà accumulées. Les listes renvoyées sont concaténées dans `out["sources"]`. Les paramètres de l'outil viennent de la signature de `run` et de la docstring (`:param x:`).
- La sortie de `Agent.run` contient `messages`, `last_message`, `step_count`, `token_usage`, `tool_call_counts`, `exit_reason` et `sources`.
- `Pipeline.dumps()` sérialise un Agent avec ses `ComponentTool`, et `Pipeline.loads(..., allowed_modules=["tsi_agent"])` le recharge. Les modules `haystack` et `haystack_integrations` sont toujours autorisés.
- `ChatMessage.from_assistant(text=None, meta=..., tool_calls=[ToolCall(tool_name, arguments, id)])`, `ChatMessage.from_tool(tool_result, origin)`. Pour lire un message : `m.tool_calls`, `m.tool_call_results` (`.result`, `.origin`, `.error`) et `m.text`.
- `haystack.tools.flatten_tools_or_toolsets` existe.

## Structure des fichiers

| Fichier | Rôle |
|---|---|
| `src/tsi_agent/vector_state.py` | + `read_documents(path)` (lecture de `store.json`, réutilisée par `load_store`) |
| `src/tsi_agent/chat/__init__.py` (nouveau) | Paquet du chatbot |
| `src/tsi_agent/chat/store.py` (nouveau) | `PersistedInMemoryDocumentStore` |
| `src/tsi_agent/chat/tools.py` (nouveau) | Filtres, `ProjectSearch`, `ProjectLister`, `ProjectReader` |
| `src/tsi_agent/components/claude_cli.py` | Émulation des outils (`run(messages, tools=None)`) |
| `src/tsi_agent/chat/generator.py` (nouveau) | `LoggedChatGenerator` (journal `llm_call`, coût) |
| `src/tsi_agent/chat/prompt.py` (nouveau) | `system_prompt(policy_areas)` |
| `src/tsi_agent/chat/pipeline.py` (nouveau) | `build_chat_pipeline`, `default_text_embedder` |
| `src/tsi_agent/chat/service.py` (nouveau) | `ask()`, `cited_sources`, `extract_steps`, `find_cause` |
| `src/tsi_agent/chat/web.py` + `src/tsi_agent/chat/static/index.html` (nouveaux) | Serveur Flask et page |
| `src/tsi_agent/config.py`, `config/settings.yaml`, `pyproject.toml` | Réglages `chat_*`, Flask, package-data |
| `src/tsi_agent/__main__.py` | Commandes `chat`, `export-yaml --chat` |
| `tests/conftest.py` | + `make_vector_store(tmp_path)` |
| `tests/test_chat_store_tools.py`, `tests/test_chat_agent.py`, `tests/test_chat_web.py` (nouveaux), `tests/test_claude_cli.py`, `tests/test_cli.py` | Tests |

---

### Task 1 : réglages, dépendances, `read_documents`, `PersistedInMemoryDocumentStore`

**Files :**
- Modify : `pyproject.toml`, `src/tsi_agent/config.py`, `config/settings.yaml`, `src/tsi_agent/vector_state.py`, `tests/conftest.py`
- Create : `src/tsi_agent/chat/__init__.py`, `src/tsi_agent/chat/store.py`
- Test : `tests/test_chat_store_tools.py`

- [ ] **Step 1 : dépendances et réglages**

`pyproject.toml` : remplacer la ligne `local = [...]` par

```toml
local = ["sentence-transformers-haystack>=0.1.2", "flask>=3"]
```

et ajouter après le bloc `[tool.setuptools.packages.find]` :

```toml
[tool.setuptools.package-data]
tsi_agent = ["chat/static/*.html"]
```

`src/tsi_agent/config.py`, dans `Settings`, après `embedding_batch_size` :

```python
    # Chatbot (pipeline `chat`).
    chat_model: str = "sonnet"
    chat_max_steps: int = 6
    chat_port: int = 8765
```

`config/settings.yaml`, à la fin :

```yaml
# Chatbot (python -m tsi_agent chat)
chat_model: sonnet
chat_max_steps: 6     # appels LLM maximum par question
chat_port: 8765
```

Installer Flask : `.venv/Scripts/python -m pip install -q -e ".[local]"`.

- [ ] **Step 2 : helper de test dans `tests/conftest.py`**

Ajouter à la fin de `tests/conftest.py` :

```python
def make_vector_store(tmp_path: Path) -> Path:
    """Couche vectorisee de test : 4 projets (fixtures vec_*) + 1 document programme, embedder factice (dim 8)."""
    import shutil

    from haystack.components.embedders import MockDocumentEmbedder

    from tsi_agent.config import Settings
    from tsi_agent.runner import run_index

    out = tmp_path / "output"
    (out / "projects").mkdir(parents=True)
    (out / "programme").mkdir()
    for name, rel in [("vec_detail_sections.md", "projects/energy-regulation-academy.md"),
                      ("vec_detail_page_text.md", "projects/building-capacity-competition-authority-spain.md"),
                      ("vec_detail_long_section.md", "projects/governance-digital-skills.md"),
                      ("vec_thin.md", "projects/srsp-2016-property-tax.md"),
                      ("vec_programme.md", "programme/country-croatia.md")]:
        shutil.copy(FIXTURES / name, out / rel)
    settings = Settings(output_dir=str(out), vectors_dir=str(out / "vectors"), logs_dir=str(tmp_path / "logs"))
    run_index(settings, embedder=MockDocumentEmbedder(model="mock", dimension=8,
                                                      meta_fields_to_embed=["context_header"]))
    return out / "vectors" / "store.json"
```

Projets de ce store, pour les assertions des tâches suivantes :

| doc_id | programme | année | pays | domaine | détail |
|---|---|---|---|---|---|
| `energy-regulation-academy` | TSI | 2026 | les 27 (dont ES, GR) | green-transition | oui |
| `governance-and-monitoring-system-national-plan-digital-skills` | TSI | 2022 | ES | labour-market-and-social-protection | oui |
| `building-capacity-competition-authority-spain` | SRSP | 2019 | ES | competitiveness | oui |
| `srsp-2016-technical-support-for-the-property-tax-reform` | SRSP | 2016 | GR | — | non (thin) |
| `country-croatia` (programme) | — | — | — | — | — |

- [ ] **Step 3 : tests**

```python
# tests/test_chat_store_tools.py
import pytest

from conftest import make_vector_store
from tsi_agent import vector_state
from tsi_agent.chat.store import PersistedInMemoryDocumentStore
from tsi_agent.vector_state import VectorLayerError


def test_persisted_store_loads_and_serializes_as_path(tmp_path):
    path = make_vector_store(tmp_path)
    store = PersistedInMemoryDocumentStore(path=str(path))
    assert store.count_documents() > 5
    assert store.to_dict() == {"type": "tsi_agent.chat.store.PersistedInMemoryDocumentStore",
                               "init_parameters": {"path": str(path)}}
    again = PersistedInMemoryDocumentStore.from_dict(store.to_dict())
    assert again.count_documents() == store.count_documents()


def test_second_instance_shares_storage_without_reloading(tmp_path, monkeypatch):
    path = make_vector_store(tmp_path)
    first = PersistedInMemoryDocumentStore(path=str(path))
    calls = []
    monkeypatch.setattr("tsi_agent.chat.store.read_documents", lambda p: calls.append(p) or [])
    second = PersistedInMemoryDocumentStore(path=str(path))
    assert calls == [] and second.count_documents() == first.count_documents()


def test_missing_store_is_a_clear_error(tmp_path):
    with pytest.raises(VectorLayerError, match="tsi_agent index"):
        PersistedInMemoryDocumentStore(path=str(tmp_path / "nope" / "store.json"))


def test_read_documents_round_trip(tmp_path):
    path = make_vector_store(tmp_path)
    docs = vector_state.read_documents(path)
    assert docs and all(d.embedding for d in docs)
```

- [ ] **Step 4 : lancer, échec attendu**

Run : `.venv/Scripts/python -m pytest -q tests/test_chat_store_tools.py`
Attendu : `ModuleNotFoundError: No module named 'tsi_agent.chat'`.

- [ ] **Step 5 : `read_documents` dans `src/tsi_agent/vector_state.py`**

Remplacer la fonction `load_store` par :

```python
def read_documents(path: Path) -> list[Document]:
    """Morceaux de `store.json` (format InMemoryDocumentStore.save_to_disk)."""
    try:
        data = json.loads(Path(path).read_text(encoding="utf-8"))
        return [Document.from_dict(d) for d in data["documents"]]
    except FileNotFoundError as exc:
        raise VectorLayerError(f"couche vectorisee absente ({path}) : lancez d'abord python -m tsi_agent index") \
            from exc
    except (OSError, ValueError, KeyError, TypeError) as exc:
        raise VectorLayerError(f"couche vectorisee illisible ({path}) : relancer avec --full") from exc


def load_store(path: Path) -> InMemoryDocumentStore:
    store = new_store()
    store.write_documents(read_documents(path), policy=DuplicatePolicy.OVERWRITE)
    return store
```

Vérifier que `test_unreadable_store_is_a_clear_error` (tests/test_vector_layer.py) passe toujours : le message contient `--full`.

- [ ] **Step 6 : `src/tsi_agent/chat/__init__.py` et `src/tsi_agent/chat/store.py`**

```python
# src/tsi_agent/chat/__init__.py
"""Agent RAG / chatbot sur la couche vectorisee (pipeline `chat`)."""
```

```python
# src/tsi_agent/chat/store.py
"""Document store local du chatbot : InMemoryDocumentStore charge depuis output/vectors/store.json.

Serialise sous la forme de son seul chemin (le YAML reste petit). Dans deepset, remplace par le document store
de l'espace de travail.
"""

from pathlib import Path
from typing import Any

from haystack import default_from_dict, default_to_dict
from haystack.document_stores.in_memory import InMemoryDocumentStore
from haystack.document_stores.types import DuplicatePolicy

from tsi_agent.vector_state import read_documents


class PersistedInMemoryDocumentStore(InMemoryDocumentStore):
    def __init__(self, path: str = "output/vectors/store.json"):
        self.path = path
        # Stockage partage par chemin : les outils (et un rechargement YAML) ne chargent le fichier qu'une fois.
        super().__init__(embedding_similarity_function="cosine", index=f"tsi:{Path(path).resolve()}", shared=True)
        if self.count_documents() == 0:
            self.write_documents(read_documents(Path(path)), policy=DuplicatePolicy.OVERWRITE)

    def to_dict(self) -> dict[str, Any]:
        return default_to_dict(self, path=self.path)

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "PersistedInMemoryDocumentStore":
        return default_from_dict(cls, data)
```

Point d'attention : si `shared=True` avec un `index` donné ne partage pas le stockage entre instances dans Haystack 3.2 (le test `test_second_instance_shares_storage_without_reloading` le révèle), lire le code d'`InMemoryDocumentStore.__init__` dans `.venv` et adapter : par exemple un cache de module `_LOADED: dict[str, list[Document]]` rempli au premier chargement. Le test doit passer tel quel.

- [ ] **Step 7 : lancer, succès attendu, puis suite complète**

Run : `.venv/Scripts/python -m pytest -q tests/test_chat_store_tools.py` puis `.venv/Scripts/python -m pytest -q`
Attendu : tout vert.

- [ ] **Step 8 : commit**

```bash
git add pyproject.toml src/tsi_agent/config.py config/settings.yaml src/tsi_agent/vector_state.py src/tsi_agent/chat tests/conftest.py tests/test_chat_store_tools.py
git commit -m "feat: document store du chatbot charge depuis la couche vectorisee, reglages chat

Co-Authored-By: Claude Opus 5.5 (1M context) <noreply@anthropic.com>"
```

---

### Task 2 : outils `ProjectSearch`, `ProjectLister`, `ProjectReader`

**Files :**
- Create : `src/tsi_agent/chat/tools.py`
- Test : `tests/test_chat_store_tools.py`

- [ ] **Step 1 : tests (à ajouter à `tests/test_chat_store_tools.py`)**

```python
from haystack.components.embedders import MockTextEmbedder
from haystack.components.retrievers.in_memory import InMemoryEmbeddingRetriever

from tsi_agent import runlog
from tsi_agent.chat.tools import ProjectLister, ProjectReader, ProjectSearch, build_filters

ENERGY = "energy-regulation-academy"
THIN = "srsp-2016-technical-support-for-the-property-tax-reform"


@pytest.fixture
def store(tmp_path):
    return PersistedInMemoryDocumentStore(path=str(make_vector_store(tmp_path)))


def search_tool(store, top_k=50):
    return ProjectSearch(text_embedder=MockTextEmbedder(model="mock", dimension=8),
                         retriever=InMemoryEmbeddingRetriever(document_store=store), top_k=top_k)


def test_build_filters():
    assert build_filters() == {"operator": "AND", "conditions": [
        {"field": "meta.doc_type", "operator": "==", "value": "project"}]}
    f = build_filters(year_from=2020, year_to=2023, programme="tsi", policy_area="green-transition",
                      detail_only=True, doc_type=None)
    assert f["conditions"] == [
        {"field": "meta.year", "operator": ">=", "value": 2020},
        {"field": "meta.year", "operator": "<=", "value": 2023},
        {"field": "meta.programme", "operator": "==", "value": "TSI"},
        {"field": "meta.policy_area", "operator": "==", "value": "green-transition"},
        {"field": "meta.detail_page", "operator": "==", "value": True}]
    assert build_filters(doc_type=None) is None


def test_search_filters_countries_in_python_and_numbers_sources(store):
    out = search_tool(store).run(query="energy regulation", countries=["gr"], known_sources=[{"ref": 1}] * 3)
    ids = {s["doc_id"] for s in out["sources"]}
    assert ids and ids <= {ENERGY, THIN}
    assert [s["ref"] for s in out["sources"]] == list(range(4, 4 + len(out["sources"])))
    assert out["result"].startswith("[4] ")
    first = out["sources"][0]
    assert set(first) >= {"ref", "doc_id", "title", "url", "section", "ai_generated"}


def test_search_portable_filters_and_programme_docs(store):
    out = search_tool(store).run(query="x", programme="SRSP")
    assert {s["doc_id"] for s in out["sources"]} <= {THIN, "building-capacity-competition-authority-spain"}
    assert "country-croatia" not in {s["doc_id"] for s in search_tool(store).run(query="x")["sources"]}
    assert "country-croatia" in {s["doc_id"] for s in
                                 search_tool(store).run(query="x", include_programme=True)["sources"]}


def test_search_top_k_and_empty(store):
    assert len(search_tool(store, top_k=2).run(query="x")["sources"]) == 2
    out = search_tool(store).run(query="x", countries=["FR"], programme="SRSP")
    assert out["sources"] == [] and "No matching" in out["result"]


def test_search_logs_tool_call(store, tmp_path):
    with runlog.open_run(tmp_path / "l", "r1") as run:
        search_tool(store).run(query="x")
    assert run.final_summary["counts"]["tool_call:ok"] == 1


def test_lister_counts_distinct_projects_with_filters(store):
    lister = ProjectLister(document_store=store)
    out = lister.run()
    assert out["result"].startswith("Total matching projects: 4 (SRSP: 2, TSI: 2)")
    assert "Total matching projects: 3" in lister.run(countries=["ES"])["result"]
    assert "Total matching projects: 2" in lister.run(countries=["GR"])["result"]
    assert "Total matching projects: 2" in lister.run(year_from=2020)["result"]
    assert "Total matching projects: 3" in lister.run(detail_only=True)["result"]
    assert "Total matching projects: 1" in lister.run(policy_area="green-transition")["result"]
    assert "Total matching projects: 1" in lister.run(title_contains="TAX")["result"]
    assert "Total matching projects: 2" in lister.run(countries=["ES"], programme="TSI")["result"]
    assert "Total matching projects: 0" in lister.run(countries=["FR"], programme="SRSP")["result"]


def test_lister_limit_and_lines(store):
    out = ProjectLister(document_store=store).run(limit=2)["result"]
    assert "Showing 2 of 4" in out
    assert f"({ENERGY})" in out          # annee la plus recente d'abord
    assert "By year: 2026: 1, 2022: 1, 2019: 1, 2016: 1" in out


def test_reader_rebuilds_project_text(store):
    out = ProjectReader(document_store=store).run(doc_id=ENERGY, known_sources=[{"ref": 1}])
    text = out["result"]
    assert text.startswith("[2] The Energy Regulation Academy")
    assert "## Summary (AI-generated) (AI-generated summary)" in text and "## Context" in text
    assert out["sources"] == [{"ref": 2, "doc_id": ENERGY, "title": "The Energy Regulation Academy",
                               "url": out["sources"][0]["url"], "section": None, "ai_generated": False}]
    assert out["sources"][0]["url"].endswith("energy-regulation-academy_en")


def test_reader_unknown_id(store):
    out = ProjectReader(document_store=store).run(doc_id="nope")
    assert out["sources"] == [] and "No project" in out["result"]
```

- [ ] **Step 2 : lancer, échec attendu**

Run : `.venv/Scripts/python -m pytest -q tests/test_chat_store_tools.py`
Attendu : `ModuleNotFoundError: No module named 'tsi_agent.chat.tools'`.

- [ ] **Step 3 : implémentation**

```python
# src/tsi_agent/chat/tools.py
"""Outils de l'agent chatbot : recherche semantique, liste/comptage de projets, lecture d'un projet.

Composants Haystack exposes via ComponentTool. Les filtres annee/programme/domaine/detail passent par les
filtres Haystack standards (meme comportement InMemory et OpenSearch) ; le filtre pays est applique ici en
Python, un filtre sur un champ liste n'ayant pas la meme semantique selon le store.
"""

import time
from collections import Counter
from typing import Any

from haystack import Document, component, default_from_dict, default_to_dict
from haystack.core.serialization import component_to_dict, import_class_by_name
from haystack.utils import deserialize_component_inplace

from tsi_agent import runlog

SEARCH_CANDIDATES_WITH_COUNTRY = 100
SNIPPET_CHARS = 1200
READER_MAX_CHARS = 8000


def build_filters(year_from: int | None = None, year_to: int | None = None, programme: str | None = None,
                  policy_area: str | None = None, detail_only: bool = False,
                  doc_type: str | None = "project") -> dict[str, Any] | None:
    conditions: list[dict[str, Any]] = []
    if doc_type:
        conditions.append({"field": "meta.doc_type", "operator": "==", "value": doc_type})
    if year_from is not None:
        conditions.append({"field": "meta.year", "operator": ">=", "value": int(year_from)})
    if year_to is not None:
        conditions.append({"field": "meta.year", "operator": "<=", "value": int(year_to)})
    if programme:
        conditions.append({"field": "meta.programme", "operator": "==", "value": programme.upper()})
    if policy_area:
        conditions.append({"field": "meta.policy_area", "operator": "==", "value": policy_area})
    if detail_only:
        conditions.append({"field": "meta.detail_page", "operator": "==", "value": True})
    return {"operator": "AND", "conditions": conditions} if conditions else None


def _in_countries(doc: Document, countries: list[str] | None) -> bool:
    if not countries:
        return True
    wanted = {c.strip().upper() for c in countries}
    return bool(wanted & set(doc.meta.get("countries") or []))


def _label(meta: dict[str, Any]) -> str:
    programme = " ".join(str(v) for v in (meta.get("programme"), meta.get("year")) if v)
    countries = ", ".join(meta.get("countries") or [])
    return " — ".join(p for p in (meta.get("title") or meta.get("doc_id"), programme, countries) if p)


def _source(ref: int, doc: Document, section: str | None = None, ai_generated: bool = False) -> dict[str, Any]:
    return {"ref": ref, "doc_id": doc.meta.get("doc_id"), "title": doc.meta.get("title") or doc.meta.get("doc_id"),
            "url": doc.meta.get("source_url"), "section": section, "ai_generated": ai_generated}


def _deserialize_object(data: dict[str, Any]) -> Any:
    return import_class_by_name(data["type"]).from_dict(data)


@component
class ProjectSearch:
    def __init__(self, text_embedder: Any, retriever: Any, top_k: int = 8):
        """:param text_embedder: embedder de requete ; :param retriever: retriever par embedding du document store."""
        self.text_embedder = text_embedder
        self.retriever = retriever
        self.top_k = top_k

    def warm_up(self) -> None:
        if hasattr(self.text_embedder, "warm_up"):
            self.text_embedder.warm_up()

    def to_dict(self) -> dict[str, Any]:
        return default_to_dict(self, top_k=self.top_k,
                               text_embedder=component_to_dict(obj=self.text_embedder, name="text_embedder"),
                               retriever=component_to_dict(obj=self.retriever, name="retriever"))

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "ProjectSearch":
        deserialize_component_inplace(data["init_parameters"], key="text_embedder")
        deserialize_component_inplace(data["init_parameters"], key="retriever")
        return default_from_dict(cls, data)

    @component.output_types(result=str, sources=list[dict])
    def run(self, query: str, countries: list[str] | None = None, year_from: int | None = None,
            year_to: int | None = None, programme: str | None = None, policy_area: str | None = None,
            detail_only: bool = False, include_programme: bool = False,
            known_sources: list[dict] | None = None) -> dict[str, Any]:
        """Semantic search in the TSI/SRSP project texts. Returns numbered passages [n] to cite.

        :param query: What to look for, in any language (e.g. "reform of the healthcare system").
        :param countries: ISO country codes to keep (e.g. ["HR", "SI"]; Greece is "GR").
        :param year_from: Keep projects from this year on (e.g. 2022).
        :param year_to: Keep projects up to this year.
        :param programme: "TSI" (2021 onwards) or "SRSP" (2017-2020).
        :param policy_area: Policy area slug (e.g. "green-transition").
        :param detail_only: Keep only projects that have a detail page (richer descriptions).
        :param include_programme: Also search country factsheets and policy area pages (programme figures).
        """
        t0 = time.monotonic()
        embedding = self.text_embedder.run(text=query)["embedding"]
        filters = build_filters(year_from, year_to, programme, policy_area, detail_only,
                                doc_type=None if include_programme else "project")
        candidates = SEARCH_CANDIDATES_WITH_COUNTRY if countries else self.top_k * 3
        docs = self.retriever.run(query_embedding=embedding, filters=filters, top_k=candidates)["documents"]
        docs = [d for d in docs if _in_countries(d, countries)][: self.top_k]
        start = len(known_sources or [])
        sources: list[dict[str, Any]] = []
        blocks: list[str] = []
        for i, doc in enumerate(docs, start=start + 1):
            ai = bool(doc.meta.get("ai_generated"))
            section = doc.meta.get("section")
            sources.append(_source(i, doc, section, ai))
            flag = " [AI-generated summary]" if ai else ""
            blocks.append(f"[{i}] {_label(doc.meta)} — section: {section}{flag}\nURL: {doc.meta.get('source_url')}\n"
                          f"{(doc.content or '')[:SNIPPET_CHARS]}")
        result = "\n\n".join(blocks) if blocks else "No matching passages found."
        runlog.event("ProjectSearch", "tool_call", "search_projects", query=query, countries=countries,
                     filters=filters, results=len(docs), duration_ms=round((time.monotonic() - t0) * 1000))
        return {"result": result, "sources": sources}


@component
class ProjectLister:
    def __init__(self, document_store: Any):
        self.document_store = document_store

    def to_dict(self) -> dict[str, Any]:
        return default_to_dict(self, document_store=self.document_store.to_dict())

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "ProjectLister":
        data["init_parameters"]["document_store"] = _deserialize_object(data["init_parameters"]["document_store"])
        return default_from_dict(cls, data)

    @component.output_types(result=str)
    def run(self, countries: list[str] | None = None, year_from: int | None = None, year_to: int | None = None,
            programme: str | None = None, policy_area: str | None = None, detail_only: bool = False,
            title_contains: str | None = None, limit: int = 50) -> dict[str, Any]:
        """Count and list distinct projects matching filters, over the whole corpus. Use it for any number or list.

        :param countries: ISO country codes (e.g. ["GR"]); a project matches if it involves any of them.
        :param year_from: From this year on.
        :param year_to: Up to this year.
        :param programme: "TSI" or "SRSP".
        :param policy_area: Policy area slug (e.g. "health-and-long-term-care").
        :param detail_only: Only projects with a detail page.
        :param title_contains: Case-insensitive word or phrase that must appear in the project title.
        :param limit: Maximum number of projects to list (the total is always given).
        """
        t0 = time.monotonic()
        filters = build_filters(year_from, year_to, programme, policy_area, detail_only)
        projects: dict[str, dict[str, Any]] = {}
        for doc in self.document_store.filter_documents(filters=filters):
            doc_id = doc.meta.get("doc_id")
            if doc_id in projects or not _in_countries(doc, countries):
                continue
            if title_contains and title_contains.lower() not in str(doc.meta.get("title") or "").lower():
                continue
            projects[doc_id] = doc.meta
        ordered = sorted(projects.values(), key=lambda m: (-(m.get("year") or 0), str(m.get("title"))))
        by_programme = Counter(m.get("programme") or "?" for m in ordered)
        by_year = Counter(m.get("year") for m in ordered)
        lines = [f"Total matching projects: {len(ordered)} ("
                 + ", ".join(f"{k}: {v}" for k, v in sorted(by_programme.items())) + ")",
                 "By year: " + ", ".join(f"{y}: {n}" for y, n in sorted(by_year.items(), key=lambda kv: -(kv[0] or 0))),
                 f"Showing {min(limit, len(ordered))} of {len(ordered)}:"]
        for m in ordered[:limit]:
            lines.append(f"- {m.get('title')} ({m.get('doc_id')}) — {m.get('programme')} {m.get('year')} — "
                         f"{', '.join(m.get('countries') or [])} — detail page: "
                         f"{'yes' if m.get('detail_page') else 'no'} — {m.get('source_url')}")
        runlog.event("ProjectLister", "tool_call", "list_projects", countries=countries, filters=filters,
                     title_contains=title_contains, results=len(ordered),
                     duration_ms=round((time.monotonic() - t0) * 1000))
        return {"result": "\n".join(lines)}


@component
class ProjectReader:
    def __init__(self, document_store: Any):
        self.document_store = document_store

    def to_dict(self) -> dict[str, Any]:
        return default_to_dict(self, document_store=self.document_store.to_dict())

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "ProjectReader":
        data["init_parameters"]["document_store"] = _deserialize_object(data["init_parameters"]["document_store"])
        return default_from_dict(cls, data)

    @component.output_types(result=str, sources=list[dict])
    def run(self, doc_id: str, known_sources: list[dict] | None = None) -> dict[str, Any]:
        """Full text of one project (all its sections), by its id as returned by search_projects or list_projects.

        :param doc_id: Project id, e.g. "energy-regulation-academy".
        """
        t0 = time.monotonic()
        docs = self.document_store.filter_documents(
            filters={"field": "meta.doc_id", "operator": "==", "value": doc_id})
        docs.sort(key=lambda d: d.meta.get("chunk_index") or 0)
        if not docs:
            runlog.event("ProjectReader", "tool_call", "get_project", "not_found", doc_id=doc_id)
            return {"result": f"No project with id '{doc_id}'.", "sources": []}
        ref = len(known_sources or []) + 1
        meta = docs[0].meta
        parts = [f"[{ref}] {_label(meta)}\nURL: {meta.get('source_url')}\n"
                 f"Policy area: {meta.get('policy_area')} — detail page: {'yes' if meta.get('detail_page') else 'no'}"]
        current = None
        for doc in docs:
            if doc.meta.get("section") != current:
                current = doc.meta.get("section")
                flag = " (AI-generated summary)" if doc.meta.get("ai_generated") else ""
                parts.append(f"## {current}{flag}")
            parts.append(doc.content or "")
        runlog.event("ProjectReader", "tool_call", "get_project", doc_id=doc_id, chunks=len(docs),
                     duration_ms=round((time.monotonic() - t0) * 1000))
        return {"result": "\n\n".join(parts)[:READER_MAX_CHARS], "sources": [_source(ref, docs[0])]}
```

Points d'attention :
- `test_lister_counts_distinct_projects_with_filters` attend `(SRSP: 2, TSI: 2)`, trié par clé.
- Pour `ProjectLister`, un morceau exclu par le filtre pays ne doit pas « réserver » son `doc_id` : c'est le cas, puisque le `continue` intervient avant l'ajout. Tous les morceaux d'un même projet ont de toute façon les mêmes pays.
- Si `default_to_dict` n'accepte pas un dict imbriqué pour `document_store`, ou si le store se désérialise déjà tout seul, adapter `to_dict`/`from_dict`. Les tests d'aller-retour YAML de la tâche 4 le vérifient.

- [ ] **Step 4 : lancer, succès attendu**

Run : `.venv/Scripts/python -m pytest -q tests/test_chat_store_tools.py`
Attendu : tout vert. Avec l'embedder factice, l'ordre des résultats de recherche est arbitraire : les tests ne vérifient que des ensembles et la numérotation.

- [ ] **Step 5 : commit**

```bash
git add src/tsi_agent/chat/tools.py tests/test_chat_store_tools.py
git commit -m "feat: outils du chatbot (recherche, liste et comptage, lecture d'un projet)

Co-Authored-By: Claude Opus 5.5 (1M context) <noreply@anthropic.com>"
```

---

### Task 3 : appel d'outils émulé dans `ClaudeCliChatGenerator`

**Files :**
- Modify : `src/tsi_agent/components/claude_cli.py`
- Test : `tests/test_claude_cli.py`

- [ ] **Step 1 : tests (à ajouter à `tests/test_claude_cli.py`)**

```python
from haystack.dataclasses import ToolCall
from haystack.tools import Tool

SEARCH = Tool(name="search_projects", description="Semantic search.",
              parameters={"type": "object", "properties": {"query": {"type": "string"}}, "required": ["query"]},
              function=lambda query: query)
LISTER = Tool(name="list_projects", description="Count projects.",
              parameters={"type": "object", "properties": {}}, function=lambda: "")


@pytest.fixture
def scripted_run(monkeypatch):
    """subprocess.run renvoie successivement les structured_output donnes."""
    calls = []

    def install(*outputs):
        queue = list(outputs)

        def run(cmd, **kw):
            calls.append({"cmd": cmd, "input": kw["input"]})
            return subprocess.CompletedProcess(cmd, 0, stdout=cli_output(structured_output=queue.pop(0)), stderr="")
        monkeypatch.setattr(claude_cli.subprocess, "run", run)
        monkeypatch.setattr(claude_cli.shutil, "which", lambda exe: "C:/npm/claude.CMD")
        return calls
    return install


def test_tool_call_reply(scripted_run):
    calls = scripted_run({"tool_calls": [{"tool_name": "search_projects", "arguments": {"query": "santé"}}],
                          "answer": ""})
    reply = ClaudeCliChatGenerator().run(messages=MESSAGES, tools=[SEARCH, LISTER])["replies"][0]
    assert reply.text is None and reply.tool_calls[0].tool_name == "search_projects"
    assert reply.tool_calls[0].arguments == {"query": "santé"} and reply.tool_calls[0].id.startswith("call_")
    schema = json.loads(calls[0]["cmd"][calls[0]["cmd"].index("--json-schema") + 1])
    assert schema["properties"]["tool_calls"]["items"]["properties"]["tool_name"]["enum"] == \
        ["search_projects", "list_projects"]
    prompt = calls[0]["input"]
    assert "### search_projects" in prompt and "Semantic search." in prompt and '"query"' in prompt
    assert "## User\nHello\nworld" in prompt
    assert reply.meta["usage"]["prompt_tokens"] == 4272


def test_answer_reply(scripted_run):
    scripted_run({"tool_calls": [], "answer": "Bonjour [1]"})
    reply = ClaudeCliChatGenerator().run(messages=MESSAGES, tools=[SEARCH])["replies"][0]
    assert reply.text == "Bonjour [1]" and not reply.tool_calls


def test_transcript_includes_tool_calls_and_results(scripted_run):
    call = ToolCall(tool_name="search_projects", arguments={"query": "x"}, id="c1")
    messages = [ChatMessage.from_system("Sys."), ChatMessage.from_user("Q"),
                ChatMessage.from_assistant(tool_calls=[call]), ChatMessage.from_tool("R1 text", origin=call),
                ChatMessage.from_assistant("Previous answer")]
    calls = scripted_run({"tool_calls": [], "answer": "ok"})
    ClaudeCliChatGenerator().run(messages=messages, tools=[SEARCH])
    prompt = calls[0]["input"]
    assert '## Assistant (tool calls)\n- search_projects {"query": "x"}' in prompt
    assert "## Tool result (search_projects)\nR1 text" in prompt
    assert "## Assistant\nPrevious answer" in prompt
    assert prompt.index("## User\nQ") < prompt.index("## Tool result")


def test_invalid_reply_is_retried_once_then_fails(scripted_run):
    calls = scripted_run({"tool_calls": [], "answer": ""}, {"tool_calls": [], "answer": "fine"})
    assert ClaudeCliChatGenerator().run(messages=MESSAGES, tools=[SEARCH])["replies"][0].text == "fine"
    assert len(calls) == 2
    scripted_run({"tool_calls": [{"tool_name": "unknown", "arguments": {}}], "answer": ""},
                 {"tool_calls": [], "answer": "  "})
    with pytest.raises(LLMCallError, match="neither"):
        ClaudeCliChatGenerator().run(messages=MESSAGES, tools=[SEARCH])


def test_without_tools_behaviour_is_unchanged(scripted_run):
    calls = scripted_run({"summary": "S"})
    ClaudeCliChatGenerator(json_schema={"type": "object"}).run(messages=MESSAGES)
    assert calls[0]["input"] == "Hello\nworld"
    assert calls[0]["cmd"][calls[0]["cmd"].index("--json-schema") + 1] == '{"type":"object"}'
```

- [ ] **Step 2 : lancer, échec attendu**

Run : `.venv/Scripts/python -m pytest -q tests/test_claude_cli.py`
Attendu : échecs sur les nouveaux tests (`run() got an unexpected keyword argument 'tools'`).

- [ ] **Step 3 : implémentation**

Dans `src/tsi_agent/components/claude_cli.py` :

1. Imports : ajouter `import uuid`, `from haystack.dataclasses import ChatMessage, ChatRole, ToolCall` (en remplacement de l'import actuel) et `from haystack.tools import Tool, flatten_tools_or_toolsets`.

2. Ajouter après `_QUOTA` :

```python
TOOLS_INTRO = ("You can call tools to gather information before answering. Available tools, with their "
               "parameters as JSON schema:")
TOOLS_INSTRUCTION = ("Reply with JSON only. Either put one or more tool calls in `tool_calls` (and leave "
                     "`answer` empty) to gather more information, or leave `tool_calls` empty and put your final "
                     "answer to the user in `answer`. Never invent tool results.")
```

3. Découper le corps actuel de `run` en une méthode privée qui renvoie les données JSON et les métadonnées. Remplacer toute la méthode `run` actuelle par :

```python
    def _invoke(self, system: str, prompt: str, json_schema: dict[str, Any] | None) -> tuple[dict[str, Any], dict]:
        cmd = self._command(system or DEFAULT_SYSTEM_PROMPT)
        if json_schema is not self.json_schema:  # schema propre a l'appel (outils) : remplace celui de l'init
            if "--json-schema" in cmd:
                del cmd[cmd.index("--json-schema"):cmd.index("--json-schema") + 2]
            cmd += ["--json-schema", json.dumps(json_schema, separators=(",", ":"))]
        try:
            proc = subprocess.run(cmd, input=prompt, capture_output=True, text=True, encoding="utf-8",
                                  errors="replace", timeout=self.timeout_s)
        except subprocess.TimeoutExpired as exc:
            raise LLMCallError(f"timeout after {self.timeout_s}s") from exc
        except OSError as exc:
            raise LLMCallError(f"cannot run claude CLI: {exc}") from exc
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
        usage = data.get("usage") or {}
        # Avec --json-schema, le CLI ajoute un petit appel Haiku, liste en premier : on retient
        # le modele qui a coute le plus, c'est-a-dire celui qui a fait l'extraction.
        model_usage = data.get("modelUsage") or {}
        model_used = max(model_usage, key=lambda m: (model_usage[m] or {}).get("costUSD") or 0, default=self.model)
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
        return data, meta

    @staticmethod
    def _system(messages: list[ChatMessage]) -> str:
        # Le prompt systeme passe en argument de ligne de commande : une seule ligne.
        return " ".join(" ".join(m.text.split()) for m in messages if m.is_from(ChatRole.SYSTEM) and m.text)

    @component.output_types(replies=list[ChatMessage])
    def run(self, messages: list[ChatMessage], tools: Any = None) -> dict[str, list[ChatMessage]]:
        if tools:
            return self._run_with_tools(messages, list(flatten_tools_or_toolsets(tools)))
        prompt = "\n\n".join(m.text for m in messages if not m.is_from(ChatRole.SYSTEM) and m.text)
        data, meta = self._invoke(self._system(messages), prompt, self.json_schema)
        structured = data.get("structured_output")
        text = json.dumps(structured, ensure_ascii=False) if structured is not None else data.get("result", "")
        return {"replies": [ChatMessage.from_assistant(text, meta=meta)]}

    @staticmethod
    def _tools_schema(tools: list[Tool]) -> dict[str, Any]:
        call = {"type": "object", "properties": {"tool_name": {"type": "string", "enum": [t.name for t in tools]},
                                                 "arguments": {"type": "object"}},
                "required": ["tool_name", "arguments"]}
        return {"type": "object", "properties": {"tool_calls": {"type": "array", "items": call},
                                                 "answer": {"type": "string"}},
                "required": ["tool_calls", "answer"]}

    @staticmethod
    def _transcript(messages: list[ChatMessage]) -> str:
        parts: list[str] = []
        for m in messages:
            if m.is_from(ChatRole.SYSTEM):
                continue
            if m.is_from(ChatRole.USER):
                parts.append(f"## User\n{m.text or ''}")
            elif m.is_from(ChatRole.TOOL):
                for r in m.tool_call_results:
                    flag = " — ERROR" if r.error else ""
                    parts.append(f"## Tool result ({r.origin.tool_name}){flag}\n{r.result}")
            elif m.tool_calls:
                calls = "\n".join(f"- {c.tool_name} {json.dumps(c.arguments, ensure_ascii=False)}"
                                  for c in m.tool_calls)
                parts.append(f"## Assistant (tool calls)\n{calls}")
            else:
                parts.append(f"## Assistant\n{m.text or ''}")
        return "\n\n".join(parts)

    def _run_with_tools(self, messages: list[ChatMessage], tools: list[Tool]) -> dict[str, list[ChatMessage]]:
        """Le CLI n'appelle pas d'outils Python : on lui fait choisir, en JSON, entre appels d'outils et reponse."""
        names = {t.name for t in tools}
        described = "\n\n".join(f"### {t.name}\n{t.description}\nParameters: "
                                f"{json.dumps(t.parameters, ensure_ascii=False)}" for t in tools)
        prompt = (f"{TOOLS_INTRO}\n\n{described}\n\n# Conversation\n\n{self._transcript(messages)}\n\n"
                  f"{TOOLS_INSTRUCTION}")
        schema = self._tools_schema(tools)
        for _ in range(2):  # une nouvelle tentative si la reponse n'est ni appel d'outil ni texte
            data, meta = self._invoke(self._system(messages), prompt, schema)
            out = data.get("structured_output") or {}
            calls = [c for c in out.get("tool_calls") or [] if isinstance(c, dict) and c.get("tool_name") in names]
            if calls:
                tool_calls = [ToolCall(tool_name=c["tool_name"], arguments=c.get("arguments") or {},
                                       id=f"call_{uuid.uuid4().hex[:12]}") for c in calls]
                return {"replies": [ChatMessage.from_assistant(tool_calls=tool_calls, meta=meta)]}
            answer = str(out.get("answer") or "").strip()
            if answer:
                return {"replies": [ChatMessage.from_assistant(answer, meta=meta)]}
        raise LLMCallError("CLI reply has neither tool_calls nor answer")
```

`_command(system_prompt)` reste inchangé : il ajoute `--json-schema` quand `self.json_schema` est défini. `_invoke` le remplace par le schéma des outils quand ils sont différents.

- [ ] **Step 4 : lancer, succès attendu**

Run : `.venv/Scripts/python -m pytest -q tests/test_claude_cli.py tests/test_extractor.py tests/test_pipeline.py`
Attendu : tout vert (l'extraction n'a pas changé de comportement).

- [ ] **Step 5 : suite complète puis commit**

Run : `.venv/Scripts/python -m pytest -q` → vert.

```bash
git add src/tsi_agent/components/claude_cli.py tests/test_claude_cli.py
git commit -m "feat: appel d'outils emule pour le CLI claude (JSON tool_calls ou answer)

Co-Authored-By: Claude Opus 5.5 (1M context) <noreply@anthropic.com>"
```

---

### Task 4 : `LoggedChatGenerator`, prompt système et pipeline `chat`

**Files :**
- Create : `src/tsi_agent/chat/generator.py`, `src/tsi_agent/chat/prompt.py`, `src/tsi_agent/chat/pipeline.py`
- Test : `tests/test_chat_agent.py`

- [ ] **Step 1 : tests**

```python
# tests/test_chat_agent.py
from typing import Any

import pytest
import yaml
from haystack import component
from haystack.components.embedders import MockTextEmbedder
from haystack.dataclasses import ChatMessage, ToolCall

from conftest import make_vector_store
from tsi_agent import runlog
from tsi_agent.chat.generator import LoggedChatGenerator
from tsi_agent.chat.pipeline import build_chat_pipeline
from tsi_agent.chat.prompt import system_prompt
from tsi_agent.components.claude_cli import ClaudeCliChatGenerator
from tsi_agent.config import Settings
from tsi_agent.pipeline import load_pipeline

POLICY_AREAS = ["green-transition", "health-and-long-term-care"]


@component
class ScriptedGenerator:
    """Renvoie les reponses donnees une par une ; garde les messages recus."""

    def __init__(self, replies: list[Any] | None = None):
        self.replies = list(replies or [])
        self.received: list[list[ChatMessage]] = []

    @component.output_types(replies=list[ChatMessage])
    def run(self, messages: list[ChatMessage], tools: Any = None) -> dict[str, Any]:
        self.received.append(list(messages))
        reply = self.replies.pop(0)
        if isinstance(reply, Exception):
            raise reply
        meta = {"model": "scripted", "usage": {"prompt_tokens": 10, "completion_tokens": 5}, "cost_usd": 0.001}
        if isinstance(reply, str):
            return {"replies": [ChatMessage.from_assistant(reply, meta=meta)]}
        return {"replies": [ChatMessage.from_assistant(tool_calls=[ToolCall(tool_name=n, arguments=a, id=f"c{i}")
                                                                   for i, (n, a) in enumerate(reply)], meta=meta)]}


def chat_settings(tmp_path) -> Settings:
    path = make_vector_store(tmp_path)
    return Settings(vectors_dir=str(path.parent), logs_dir=str(tmp_path / "logs"), chat_max_steps=6)


def test_system_prompt_has_rules_and_no_jinja():
    text = system_prompt(POLICY_AREAS)
    assert "list_projects" in text and "[n]" in text and "GR" in text and "green-transition" in text
    assert "{{" not in text and "{%" not in text


def test_logged_generator_logs_llm_call_and_forwards_tools(tmp_path):
    inner = ScriptedGenerator(["hello"])
    gen = LoggedChatGenerator(chat_generator=inner)
    with runlog.open_run(tmp_path, "g1") as run:
        reply = gen.run(messages=[ChatMessage.from_user("q")], tools=None)["replies"][0]
    assert reply.text == "hello"
    s = run.final_summary
    assert s["llm_calls"] == 1 and s["tokens"]["input_tokens"] == 10 and s["tokens"]["output_tokens"] == 5
    assert s["cost_usd"] == 0.001


def test_logged_generator_uses_pricing_when_no_cost(tmp_path):
    @component
    class NoCost:
        @component.output_types(replies=list[ChatMessage])
        def run(self, messages: list[ChatMessage], tools: Any = None) -> dict[str, Any]:
            return {"replies": [ChatMessage.from_assistant("x", meta={
                "model": "gpt-4o-mini-2024", "usage": {"prompt_tokens": 1_000_000, "completion_tokens": 0}})]}
    gen = LoggedChatGenerator(chat_generator=NoCost(), pricing={"gpt-4o-mini": {"input": 0.15, "output": 0.6}})
    with runlog.open_run(tmp_path, "g2") as run:
        gen.run(messages=[ChatMessage.from_user("q")])
    assert run.final_summary["cost_usd"] == 0.15


def test_logged_generator_round_trip():
    gen = LoggedChatGenerator(chat_generator=ClaudeCliChatGenerator(model="sonnet"), pricing={"a": {"input": 1}})
    again = LoggedChatGenerator.from_dict(gen.to_dict())
    assert isinstance(again.chat_generator, ClaudeCliChatGenerator) and again.chat_generator.model == "sonnet"
    assert again.pricing == {"a": {"input": 1}}


def test_pipeline_runs_agent_with_real_tools(tmp_path):
    s = chat_settings(tmp_path)
    gen = ScriptedGenerator([[("search_projects", {"query": "energy", "countries": ["GR"]})],
                             [("list_projects", {"programme": "SRSP"})], "Done [1]."])
    pipe = build_chat_pipeline(s, POLICY_AREAS, chat_generator=gen,
                               text_embedder=MockTextEmbedder(model="mock", dimension=8))
    out = pipe.run({"agent": {"messages": [ChatMessage.from_user("q")]}})["agent"]
    assert out["last_message"].text == "Done [1]."
    assert out["sources"] and out["sources"][0]["ref"] == 1
    tool_texts = [m.tool_call_results[0].result for m in out["messages"] if m.tool_call_results]
    assert tool_texts[0].startswith("[1] ") and tool_texts[1].startswith("Total matching projects: 2")
    assert gen.received[0][0].text == system_prompt(POLICY_AREAS)


def test_yaml_round_trip_and_openai_swap(tmp_path, monkeypatch):
    monkeypatch.setenv("OPENAI_API_KEY", "sk-test")
    s = chat_settings(tmp_path)
    pipe = build_chat_pipeline(s, POLICY_AREAS, chat_generator=ClaudeCliChatGenerator(model="sonnet"),
                               text_embedder=MockTextEmbedder(model="mock", dimension=8))
    text = pipe.dumps()
    for name in ("Agent", "LoggedChatGenerator", "ClaudeCliChatGenerator", "ProjectSearch", "ProjectLister",
                 "ProjectReader", "PersistedInMemoryDocumentStore"):
        assert name in text
    reloaded = load_pipeline(text)
    tools = reloaded.get_component("agent").tools
    assert [t.name for t in tools] == ["search_projects", "list_projects", "get_project"]

    data = yaml.safe_load(text)
    agent = data["components"]["agent"]["init_parameters"]
    agent["chat_generator"]["init_parameters"]["chat_generator"] = {
        "type": "haystack.components.generators.chat.openai.OpenAIChatGenerator",
        "init_parameters": {"model": "gpt-4o-mini",
                            "api_key": {"type": "env_var", "env_vars": ["OPENAI_API_KEY"], "strict": True}}}
    search = next(t for t in agent["tools"] if t["data"]["name"] == "search_projects")
    search["data"]["component"]["init_parameters"]["text_embedder"] = {
        "type": "haystack.components.embedders.openai_text_embedder.OpenAITextEmbedder",
        "init_parameters": {"model": "text-embedding-3-small",
                            "api_key": {"type": "env_var", "env_vars": ["OPENAI_API_KEY"], "strict": True}}}
    swapped = load_pipeline(yaml.safe_dump(data)).get_component("agent")
    assert type(swapped.chat_generator.chat_generator).__name__ == "OpenAIChatGenerator"
```

- [ ] **Step 2 : lancer, échec attendu**

Run : `.venv/Scripts/python -m pytest -q tests/test_chat_agent.py`
Attendu : `ModuleNotFoundError: No module named 'tsi_agent.chat.generator'`.

- [ ] **Step 3 : `src/tsi_agent/chat/generator.py`**

```python
# src/tsi_agent/chat/generator.py
"""Chat generator qui journalise chaque appel LLM (tokens, cout, duree) autour d'un generator Haystack."""

import inspect
import time
from typing import Any

from haystack import component, default_from_dict, default_to_dict
from haystack.core.serialization import component_to_dict
from haystack.dataclasses import ChatMessage
from haystack.utils import deserialize_chatgenerator_inplace

from tsi_agent import runlog


@component
class LoggedChatGenerator:
    def __init__(self, chat_generator: Any, pricing: dict[str, dict[str, float]] | None = None):
        """:param pricing: $ par million de tokens par prefixe de modele, quand le generator ne donne pas de cout."""
        self.chat_generator = chat_generator
        self.pricing = pricing or {}

    def warm_up(self) -> None:
        if hasattr(self.chat_generator, "warm_up"):
            self.chat_generator.warm_up()

    def to_dict(self) -> dict[str, Any]:
        return default_to_dict(self, pricing=self.pricing,
                               chat_generator=component_to_dict(obj=self.chat_generator, name="chat_generator"))

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "LoggedChatGenerator":
        deserialize_chatgenerator_inplace(data["init_parameters"], key="chat_generator")
        return default_from_dict(cls, data)

    def _cost(self, meta: dict[str, Any]) -> float:
        if meta.get("cost_usd") is not None:
            return float(meta["cost_usd"])
        usage = meta.get("usage") or {}
        model = str(meta.get("model") or "")
        for prefix, price in sorted(self.pricing.items(), key=lambda kv: len(kv[0]), reverse=True):
            if model.startswith(prefix):
                return (usage.get("prompt_tokens", 0) * price.get("input", 0)
                        + usage.get("completion_tokens", 0) * price.get("output", 0)) / 1_000_000
        return 0.0

    @component.output_types(replies=list[ChatMessage])
    def run(self, messages: list[ChatMessage], tools: Any = None, **kwargs: Any) -> dict[str, Any]:
        accepted = inspect.signature(self.chat_generator.run).parameters
        extra = {k: v for k, v in kwargs.items() if k in accepted and v is not None}
        if tools is not None and "tools" in accepted:
            extra["tools"] = tools
        t0 = time.monotonic()
        result = self.chat_generator.run(messages=messages, **extra)
        reply = result["replies"][0]
        meta = reply.meta or {}
        usage = meta.get("usage") or {}
        runlog.event("ChatAgent", "llm_call", str(meta.get("model")), model=meta.get("model"),
                     input_tokens=usage.get("prompt_tokens", 0), output_tokens=usage.get("completion_tokens", 0),
                     cache_read_tokens=usage.get("cache_read_input_tokens", 0),
                     cache_creation_tokens=usage.get("cache_creation_input_tokens", 0),
                     cost_usd=self._cost(meta), duration_ms=round((time.monotonic() - t0) * 1000),
                     tool_calls=[c.tool_name for c in reply.tool_calls])
        return result
```

Point d'attention : l'Agent peut vérifier que son `chat_generator` accepte `tools` en inspectant la signature de `run`. Elle déclare `tools` explicitement, ce qui convient. S'il passe d'autres arguments nommés (`generation_kwargs`, `streaming_callback`), ils arrivent dans `**kwargs` et ne sont transmis au generator interne que s'il les accepte.

- [ ] **Step 4 : `src/tsi_agent/chat/prompt.py`**

```python
# src/tsi_agent/chat/prompt.py
"""Prompt systeme de l'agent chatbot (sans syntaxe Jinja : l'Agent Haystack le rend comme un template)."""

from tsi_agent.countries import COUNTRIES, name_from_iso


def system_prompt(policy_areas: list[str]) -> str:
    countries = ", ".join(f"{iso} ({name_from_iso(iso)})" for iso in COUNTRIES)
    areas = ", ".join(policy_areas)
    return f"""You answer questions about technical support projects of the European Commission (DG REFORM): the
Technical Support Instrument (TSI, 2021 onwards) and its predecessor, the Structural Reform Support Programme
(SRSP, 2017-2020). You only know what the tools return.

Tools:
- search_projects: semantic search in project texts. Returns numbered passages [n].
- list_projects: exact counts and lists of projects over the whole corpus, with filters.
- get_project: full text of one project, by its id.

Rules:
1. Answer in the language of the user's question.
2. Use only information returned by the tools in this conversation. If it is not there, say so plainly.
3. Cite every factual statement with the passage number, like [n], using the numbers given by the tools. End
   with a "Sources" list: one line per cited number with the project title and its URL.
4. When a passage is marked as an AI-generated summary, say that the information comes from an AI-generated
   summary rather than from the official text.
5. For any number, count or list of projects, call list_projects and report its figures. Never estimate.
6. For a comparison, search each compared item separately (for example one search per country).
7. Projects without a detail page ("detail page: no") are only known by their title, country and year: say so.
8. For follow-up questions, reuse the context of the conversation (countries, topics, projects already cited).
9. You have a limited number of steps: after a few tool calls, answer with what you found.

Filter values: countries are ISO codes: {countries}. Programmes: TSI, SRSP. Policy areas: {areas}."""
```

- [ ] **Step 5 : `src/tsi_agent/chat/pipeline.py`**

```python
# src/tsi_agent/chat/pipeline.py
"""Pipeline Haystack du chatbot : un Agent avec trois outils sur la couche vectorisee (export YAML deepset)."""

from pathlib import Path
from typing import Any

from haystack import Pipeline
from haystack.components.agents import Agent
from haystack.components.retrievers.in_memory import InMemoryEmbeddingRetriever
from haystack.tools import ComponentTool

from tsi_agent.chat.generator import LoggedChatGenerator
from tsi_agent.chat.prompt import system_prompt
from tsi_agent.chat.store import PersistedInMemoryDocumentStore
from tsi_agent.chat.tools import ProjectLister, ProjectReader, ProjectSearch
from tsi_agent.components.claude_cli import ClaudeCliChatGenerator
from tsi_agent.config import Settings

# Numerotation continue des sources entre appels d'outils : l'outil lit les sources deja connues (etat de
# l'Agent) et y ajoute les siennes.
SOURCES_STATE = {"sources": {"type": list[dict]}}
_SOURCES_IO = {"outputs_to_state": {"sources": {"source": "sources"}},
               "inputs_from_state": {"sources": "known_sources"}}


def default_text_embedder(settings: Settings) -> Any:
    try:
        from haystack_integrations.components.embedders.sentence_transformers import \
            SentenceTransformersTextEmbedder
    except ImportError as exc:
        raise RuntimeError('embedder local absent : installer avec pip install -e ".[local]"') from exc
    # Meme modele que la couche vectorisee ; prefixe `query: ` exige par e5 cote requete.
    return SentenceTransformersTextEmbedder(model=settings.embedding_model, prefix="query: ",
                                            normalize_embeddings=True, progress_bar=False)


def build_chat_pipeline(settings: Settings, policy_areas: list[str], *, chat_generator: Any = None,
                        text_embedder: Any = None, document_store: Any = None) -> Pipeline:
    store = document_store if document_store is not None else PersistedInMemoryDocumentStore(
        path=str(Path(settings.vectors_dir) / "store.json"))
    generator = chat_generator if chat_generator is not None else ClaudeCliChatGenerator(
        model=settings.chat_model, timeout_s=settings.llm_timeout_s)
    embedder = text_embedder if text_embedder is not None else default_text_embedder(settings)
    tools = [
        ComponentTool(ProjectSearch(text_embedder=embedder, retriever=InMemoryEmbeddingRetriever(document_store=store)),
                      name="search_projects",
                      description="Semantic search in TSI/SRSP project texts; returns numbered passages to cite.",
                      outputs_to_string={"source": "result"}, **_SOURCES_IO),
        ComponentTool(ProjectLister(document_store=store), name="list_projects",
                      description="Exact count and list of distinct projects matching filters (whole corpus).",
                      outputs_to_string={"source": "result"}),
        ComponentTool(ProjectReader(document_store=store), name="get_project",
                      description="Full text of one project by id.", outputs_to_string={"source": "result"},
                      **_SOURCES_IO),
    ]
    agent = Agent(chat_generator=LoggedChatGenerator(chat_generator=generator, pricing=settings.pricing),
                  tools=tools, system_prompt=system_prompt(policy_areas), state_schema=SOURCES_STATE,
                  max_agent_steps=settings.chat_max_steps)
    p = Pipeline()
    p.add_component("agent", agent)
    return p
```

Points d'attention :
- Si la sérialisation d'un `ComponentTool` avec `outputs_to_string={"source": "result"}` diffère en 3.2 (clé `source` ou forme attendue), s'aligner sur la docstring de `ComponentTool` dans `.venv`. Le prototype a validé `outputs_to_string={"source": "result"}` à l'exécution.
- `test_pipeline_runs_agent_with_real_tools` vérifie que le premier message reçu par le generator est bien le prompt système. Si l'Agent le transmet autrement, adapter le test (pas le prompt).

- [ ] **Step 6 : lancer, succès attendu, suite complète**

Run : `.venv/Scripts/python -m pytest -q tests/test_chat_agent.py` puis `.venv/Scripts/python -m pytest -q`
Attendu : tout vert.

- [ ] **Step 7 : commit**

```bash
git add src/tsi_agent/chat/generator.py src/tsi_agent/chat/prompt.py src/tsi_agent/chat/pipeline.py tests/test_chat_agent.py
git commit -m "feat: pipeline chat (Agent Haystack, trois outils, generator journalise), export YAML

Co-Authored-By: Claude Opus 5.5 (1M context) <noreply@anthropic.com>"
```

---

### Task 5 : service `ask()` (une question = un run journalisé)

**Files :**
- Create : `src/tsi_agent/chat/service.py`
- Test : `tests/test_chat_agent.py`

- [ ] **Step 1 : tests (à ajouter à `tests/test_chat_agent.py`)**

```python
from tsi_agent.chat.service import ask, cited_sources, find_cause
from tsi_agent.components.claude_cli import LLMQuotaExceeded


def test_cited_sources_keeps_cited_refs_in_order_else_all_unique():
    srcs = [{"ref": 1, "doc_id": "a", "url": "u1"}, {"ref": 2, "doc_id": "b", "url": "u2"},
            {"ref": 3, "doc_id": "a", "url": "u1"}]
    assert [s["ref"] for s in cited_sources("See [2] and [1][2].", srcs)] == [1, 2]
    assert [s["ref"] for s in cited_sources("No refs.", srcs)] == [1, 2]


def test_find_cause():
    try:
        try:
            raise LLMQuotaExceeded("limit")
        except LLMQuotaExceeded as inner:
            raise RuntimeError("wrapped") from inner
    except RuntimeError as exc:
        assert isinstance(find_cause(exc, LLMQuotaExceeded), LLMQuotaExceeded)
        assert find_cause(exc, KeyError) is None


def pipeline_with(tmp_path, replies, max_steps=6):
    s = chat_settings(tmp_path)
    s.chat_max_steps = max_steps
    gen = ScriptedGenerator(replies)
    return s, gen, build_chat_pipeline(s, POLICY_AREAS, chat_generator=gen,
                                       text_embedder=MockTextEmbedder(model="mock", dimension=8))


def test_ask_returns_answer_sources_steps_and_logs(tmp_path):
    s, gen, pipe = pipeline_with(tmp_path, [[("search_projects", {"query": "energy", "countries": ["GR"]})],
                                            [("list_projects", {"programme": "SRSP"})], "Answer [1]."])
    res = ask(pipe, "Quels projets ?", [], logs_dir=s.logs_dir)
    assert res["answer"] == "Answer [1]."
    assert [x["ref"] for x in res["sources"]] == [1]
    assert [st["tool"] for st in res["steps"]] == ["search_projects", "list_projects"]
    assert res["steps"][1]["summary"].startswith("Total matching projects: 2")
    assert res["cost_usd"] == pytest.approx(0.003) and res["run_id"]
    import json
    summary = json.loads((tmp_path / "logs" / "runs" / f"{res['run_id']}.summary.json").read_text(encoding="utf-8"))
    assert summary["mode"] == "chat" and summary["counts"]["tool_call:ok"] == 2
    assert summary["counts"]["llm_call:ok"] == 3 and summary["counts"]["question:ok"] == 1


def test_ask_sends_history_as_messages(tmp_path):
    s, gen, pipe = pipeline_with(tmp_path, ["A1"])
    ask(pipe, "Et en Slovénie ?", [{"role": "user", "text": "Projets santé en Croatie ?"},
                                   {"role": "assistant", "text": "Deux projets [1]."}], logs_dir=s.logs_dir)
    roles = [m.role.value for m in gen.received[0]]
    assert roles == ["system", "user", "assistant", "user"]
    assert gen.received[0][-1].text == "Et en Slovénie ?"


def test_ask_when_step_limit_is_reached(tmp_path):
    s, gen, pipe = pipeline_with(tmp_path, [[("list_projects", {})]] * 5, max_steps=2)
    res = ask(pipe, "q", [], logs_dir=s.logs_dir)
    assert "limite" in res["answer"]


def test_ask_propagates_quota(tmp_path):
    s, gen, pipe = pipeline_with(tmp_path, [LLMQuotaExceeded("usage limit reached")])
    with pytest.raises(Exception) as err:
        ask(pipe, "q", [], logs_dir=s.logs_dir)
    assert find_cause(err.value, LLMQuotaExceeded) is not None
```

- [ ] **Step 2 : lancer, échec attendu**

Run : `.venv/Scripts/python -m pytest -q tests/test_chat_agent.py`
Attendu : `ModuleNotFoundError: No module named 'tsi_agent.chat.service'`.

- [ ] **Step 3 : implémentation**

```python
# src/tsi_agent/chat/service.py
"""Une question au chatbot = un run journalise : historique -> Agent -> reponse, sources citees, etapes."""

import re
from typing import Any

from haystack import Pipeline
from haystack.dataclasses import ChatMessage, ChatRole

from tsi_agent import runlog

STEP_LIMIT_ANSWER = ("L'agent a atteint la limite d'etapes sans conclure. Reformulez ou precisez la question "
                     "(pays, annee, programme).")
_REF = re.compile(r"\[(\d+)\]")


def find_cause(exc: BaseException | None, cls: type) -> BaseException | None:
    """Premiere exception de type `cls` dans la chaine (__cause__ / __context__) : l'Agent et le Pipeline enveloppent."""
    seen: set[int] = set()
    while exc is not None and id(exc) not in seen:
        if isinstance(exc, cls):
            return exc
        seen.add(id(exc))
        exc = exc.__cause__ or exc.__context__
    return None


def cited_sources(answer: str, sources: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Sources citees [n] dans la reponse, dans l'ordre des numeros ; a defaut, toutes (une par projet)."""
    cited = {int(n) for n in _REF.findall(answer)}
    picked = [s for s in sources if s.get("ref") in cited]
    if picked:
        return sorted(picked, key=lambda s: s["ref"])
    unique: dict[Any, dict[str, Any]] = {}
    for s in sources:
        unique.setdefault(s.get("doc_id"), s)
    return list(unique.values())


def extract_steps(messages: list[ChatMessage]) -> list[dict[str, Any]]:
    results = {r.origin.id: r for m in messages if m.is_from(ChatRole.TOOL) for r in m.tool_call_results}
    steps = []
    for m in messages:
        for call in m.tool_calls if m.is_from(ChatRole.ASSISTANT) else []:
            r = results.get(call.id)
            text = str(r.result) if r else ""
            steps.append({"tool": call.tool_name, "arguments": call.arguments,
                          "summary": text.splitlines()[0][:200] if text else "", "error": bool(r and r.error)})
    return steps


def _history_messages(history: list[dict[str, str]]) -> list[ChatMessage]:
    messages = []
    for turn in history:
        text = str(turn.get("text") or "")
        if turn.get("role") == "user":
            messages.append(ChatMessage.from_user(text))
        elif turn.get("role") == "assistant":
            messages.append(ChatMessage.from_assistant(text))
    return messages


def ask(pipeline: Pipeline, question: str, history: list[dict[str, str]], *, logs_dir: str) -> dict[str, Any]:
    messages = _history_messages(history) + [ChatMessage.from_user(question)]
    with runlog.open_run(logs_dir) as run:
        runlog.event("ChatService", "question", None, text=question, history_turns=len(history))
        out = pipeline.run({"agent": {"messages": messages}})["agent"]
        last = out.get("last_message")
        ok = last is not None and last.is_from(ChatRole.ASSISTANT) and bool(last.text) and not last.tool_calls
        answer = last.text if ok else STEP_LIMIT_ANSWER
        sources = cited_sources(answer, out.get("sources") or []) if ok else []
        steps = extract_steps(out.get("messages") or [])
        runlog.event("ChatService", "answer", None, "ok" if ok else "step_limit", chars=len(answer),
                     sources=len(sources), steps=len(steps), exit_reason=out.get("exit_reason"))
        run.extra.update({"mode": "chat", "question": question, "steps": len(steps)})
    summary = run.final_summary
    return {"answer": answer, "sources": sources, "steps": steps, "duration_s": summary["duration_s"],
            "cost_usd": summary["cost_usd"], "run_id": run.run_id}
```

Point d'attention pour `test_ask_when_step_limit_is_reached` : vérifier dans `.venv` ce que l'Agent renvoie quand `max_agent_steps` est atteint (`last_message` peut être un message d'outil, ou un message assistant avec des `tool_calls`). La condition `ok` couvre les deux cas. Si l'Agent lève une exception à la place, l'attraper dans `ask` (type exact vu dans le code de l'Agent) et renvoyer `STEP_LIMIT_ANSWER`.

- [ ] **Step 4 : lancer, succès attendu, suite complète**

Run : `.venv/Scripts/python -m pytest -q tests/test_chat_agent.py` puis `.venv/Scripts/python -m pytest -q`
Attendu : tout vert.

- [ ] **Step 5 : commit**

```bash
git add src/tsi_agent/chat/service.py tests/test_chat_agent.py
git commit -m "feat: service ask (historique, sources citees, etapes, un run journalise par question)

Co-Authored-By: Claude Opus 5.5 (1M context) <noreply@anthropic.com>"
```

---

### Task 6 : serveur Flask et page web

**Files :**
- Create : `src/tsi_agent/chat/web.py`, `src/tsi_agent/chat/static/index.html`
- Test : `tests/test_chat_web.py`

- [ ] **Step 1 : tests**

```python
# tests/test_chat_web.py
from haystack.components.embedders import MockTextEmbedder

from test_chat_agent import POLICY_AREAS, ScriptedGenerator, chat_settings
from tsi_agent.chat.pipeline import build_chat_pipeline
from tsi_agent.chat.web import create_app
from tsi_agent.components.claude_cli import LLMCallError, LLMQuotaExceeded


def client_with(tmp_path, replies):
    s = chat_settings(tmp_path)
    pipe = build_chat_pipeline(s, POLICY_AREAS, chat_generator=ScriptedGenerator(replies),
                               text_embedder=MockTextEmbedder(model="mock", dimension=8))
    return create_app(s, pipeline=pipe).test_client()


def test_index_page(tmp_path):
    res = client_with(tmp_path, []).get("/")
    assert res.status_code == 200 and b"<html" in res.data and b"/api/chat" in res.data


def test_chat_api(tmp_path):
    client = client_with(tmp_path, [[("search_projects", {"query": "energy"})], "Voici [1]."])
    res = client.post("/api/chat", json={"question": "Projets énergie ?", "history": []})
    assert res.status_code == 200
    body = res.get_json()
    assert body["answer"] == "Voici [1]." and body["sources"][0]["ref"] == 1
    assert body["steps"][0]["tool"] == "search_projects" and body["run_id"]


def test_chat_api_validation(tmp_path):
    client = client_with(tmp_path, [])
    assert client.post("/api/chat", json={"question": "  "}).status_code == 400
    assert client.post("/api/chat", json={"question": "q", "history": "x"}).status_code == 400


def test_chat_api_quota_and_errors(tmp_path):
    client = client_with(tmp_path, [LLMQuotaExceeded("usage limit reached"), LLMCallError("boom")])
    res = client.post("/api/chat", json={"question": "q", "history": []})
    assert res.status_code == 429 and "limite" in res.get_json()["error"]
    res = client.post("/api/chat", json={"question": "q", "history": []})
    assert res.status_code == 500 and "boom" in res.get_json()["error"]
```

- [ ] **Step 2 : lancer, échec attendu**

Run : `.venv/Scripts/python -m pytest -q tests/test_chat_web.py`
Attendu : `ModuleNotFoundError: No module named 'tsi_agent.chat.web'`.

- [ ] **Step 3 : `src/tsi_agent/chat/web.py`**

```python
# src/tsi_agent/chat/web.py
"""Serveur web local du chatbot (Flask, 127.0.0.1 uniquement). L'historique vit dans la page."""

import threading
from pathlib import Path
from typing import Any

from flask import Flask, Response, jsonify, request

from tsi_agent.chat.pipeline import build_chat_pipeline
from tsi_agent.chat.service import ask, find_cause
from tsi_agent.components.claude_cli import LLMQuotaExceeded
from tsi_agent.config import Settings

INDEX_HTML = Path(__file__).parent / "static" / "index.html"
QUOTA_MESSAGE = "La limite de la souscription Claude est atteinte. Réessayez plus tard."


def create_app(settings: Settings, pipeline: Any = None, policy_areas: list[str] | None = None) -> Flask:
    """Construit l'application ; le pipeline (store + modele) est charge une seule fois, ici."""
    if pipeline is None:
        pipeline = build_chat_pipeline(settings, policy_areas or [])
    pipeline.warm_up()
    lock = threading.Lock()  # une question a la fois : un seul CLI claude en parallele
    app = Flask(__name__)

    @app.get("/")
    def index() -> Response:
        return Response(INDEX_HTML.read_text(encoding="utf-8"), mimetype="text/html")

    @app.post("/api/chat")
    def chat() -> Any:
        data = request.get_json(silent=True) or {}
        question = str(data.get("question") or "").strip()
        history = data.get("history") or []
        if not question or not isinstance(history, list):
            return jsonify({"error": "question vide ou historique invalide"}), 400
        with lock:
            try:
                return jsonify(ask(pipeline, question, history, logs_dir=settings.logs_dir))
            except Exception as exc:  # erreur renvoyee a la page ; le detail est dans le journal du run
                if find_cause(exc, LLMQuotaExceeded):
                    return jsonify({"error": QUOTA_MESSAGE}), 429
                return jsonify({"error": f"{type(exc).__name__}: {exc}"}), 500

    return app
```

- [ ] **Step 4 : `src/tsi_agent/chat/static/index.html`**

```html
<!doctype html>
<html lang="fr">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>TSI Projects Chat</title>
<style>
  :root { --bg:#f6f7f9; --card:#fff; --text:#1d2433; --muted:#667085; --accent:#1f4e9e; --border:#dde2ea; --ai:#8a5a00; }
  @media (prefers-color-scheme: dark) {
    :root { --bg:#12151b; --card:#1b2029; --text:#e6e9ef; --muted:#98a2b3; --accent:#7aa7ff; --border:#2c3441; --ai:#e0b458; }
  }
  * { box-sizing: border-box; }
  body { margin:0; font:15px/1.55 system-ui, -apple-system, "Segoe UI", sans-serif; background:var(--bg); color:var(--text); }
  header { padding:14px 20px; border-bottom:1px solid var(--border); display:flex; justify-content:space-between; align-items:center; background:var(--card); }
  header h1 { font-size:16px; margin:0; }
  header small { color:var(--muted); }
  button { font:inherit; cursor:pointer; border:1px solid var(--border); background:var(--card); color:var(--text); border-radius:8px; padding:6px 12px; }
  button.primary { background:var(--accent); color:#fff; border-color:var(--accent); }
  button:disabled { opacity:.5; cursor:default; }
  main { max-width:900px; margin:0 auto; padding:20px 16px 140px; }
  .msg { background:var(--card); border:1px solid var(--border); border-radius:12px; padding:12px 16px; margin:12px 0; }
  .msg.user { border-left:4px solid var(--accent); }
  .msg .who { font-size:12px; color:var(--muted); margin-bottom:4px; }
  .msg .body p { margin:.4em 0; }
  .msg .body ul { margin:.4em 0; padding-left:1.3em; }
  .msg sup a { text-decoration:none; }
  .sources, details { margin-top:10px; font-size:13px; color:var(--muted); }
  .sources ol { margin:4px 0; padding-left:1.4em; }
  .sources a, .msg a { color:var(--accent); }
  .ai { color:var(--ai); font-size:12px; }
  details code { font-size:12px; }
  .meta { font-size:12px; color:var(--muted); margin-top:6px; }
  .error { border-color:#d92d20; color:#d92d20; }
  form { position:fixed; bottom:0; left:0; right:0; background:var(--card); border-top:1px solid var(--border); padding:12px 16px; }
  form .row { max-width:900px; margin:0 auto; display:flex; gap:8px; }
  textarea { flex:1; resize:vertical; min-height:48px; max-height:200px; font:inherit; padding:10px; border-radius:8px; border:1px solid var(--border); background:var(--bg); color:var(--text); }
  .hint { max-width:900px; margin:6px auto 0; font-size:12px; color:var(--muted); }
</style>
</head>
<body>
<header>
  <div><h1>Projets TSI / SRSP — assistant</h1><small>Réponses fondées sur les pages publiques de la Commission (DG REFORM)</small></div>
  <button id="reset" type="button">Nouvelle conversation</button>
</header>
<main id="thread"></main>
<form id="form">
  <div class="row">
    <textarea id="q" placeholder="Ex. : Quels projets aident la Croatie à réformer son système de santé ?" required></textarea>
    <button class="primary" id="send" type="submit">Envoyer</button>
  </div>
  <div class="hint">Entrée pour envoyer, Maj+Entrée pour aller à la ligne. Une réponse prend 20 à 60 s en local.</div>
</form>
<script>
const thread = document.getElementById("thread");
const form = document.getElementById("form");
const input = document.getElementById("q");
const send = document.getElementById("send");
let history = [];

function esc(s) {
  return String(s).replace(/[&<>"']/g, c => ({"&":"&amp;","<":"&lt;",">":"&gt;",'"':"&quot;","'":"&#39;"}[c]));
}

function render(text, sources) {
  const urls = {};
  (sources || []).forEach(s => { urls[s.ref] = s.url; });
  const blocks = esc(text).split(/\n{2,}/).map(block => {
    const lines = block.split("\n");
    if (lines.every(l => /^\s*[-*] /.test(l))) {
      return "<ul>" + lines.map(l => "<li>" + l.replace(/^\s*[-*] /, "") + "</li>").join("") + "</ul>";
    }
    return "<p>" + lines.join("<br>") + "</p>";
  }).join("");
  return blocks
    .replace(/\*\*(.+?)\*\*/g, "<strong>$1</strong>")
    .replace(/(https?:\/\/[^\s<)]+)/g, '<a href="$1" target="_blank" rel="noopener">$1</a>')
    .replace(/\[(\d+)\]/g, (m, n) => urls[n] ? `<sup><a href="${esc(urls[n])}" target="_blank" rel="noopener">[${n}]</a></sup>` : m);
}

function addMessage(role, html, extra) {
  const div = document.createElement("div");
  div.className = "msg " + role + (extra && extra.error ? " error" : "");
  div.innerHTML = `<div class="who">${role === "user" ? "Vous" : "Assistant"}</div><div class="body">${html}</div>`;
  if (extra && extra.after) div.insertAdjacentHTML("beforeend", extra.after);
  thread.appendChild(div);
  div.scrollIntoView({behavior: "smooth", block: "end"});
  return div;
}

function details(res) {
  let html = "";
  if (res.sources && res.sources.length) {
    html += '<div class="sources"><strong>Sources</strong><ol>' + res.sources.map(s =>
      `<li value="${s.ref}"><a href="${esc(s.url || "#")}" target="_blank" rel="noopener">${esc(s.title || s.doc_id)}</a>` +
      (s.section ? ` — ${esc(s.section)}` : "") + (s.ai_generated ? ' <span class="ai">(résumé IA)</span>' : "") + "</li>").join("") +
      "</ol></div>";
  }
  if (res.steps && res.steps.length) {
    html += `<details><summary>${res.steps.length} étape(s) de l'agent</summary><ol>` + res.steps.map(s =>
      `<li><code>${esc(s.tool)}</code> ${esc(JSON.stringify(s.arguments))}<br><small>${esc(s.summary)}</small></li>`).join("") +
      "</ol></details>";
  }
  html += `<div class="meta">${res.duration_s} s · coût équivalent API ${Number(res.cost_usd).toFixed(4)} $ · run ${esc(res.run_id)}</div>`;
  return html;
}

async function submit(question) {
  addMessage("user", render(question));
  const pending = addMessage("assistant", "<p><em>Recherche en cours…</em></p>");
  send.disabled = true;
  try {
    const r = await fetch("/api/chat", {method: "POST", headers: {"Content-Type": "application/json"},
                                        body: JSON.stringify({question, history})});
    const res = await r.json();
    pending.remove();
    if (!r.ok) { addMessage("assistant", `<p>${esc(res.error || "Erreur")}</p>`, {error: true}); return; }
    addMessage("assistant", render(res.answer, res.sources), {after: details(res)});
    history.push({role: "user", text: question}, {role: "assistant", text: res.answer});
  } catch (e) {
    pending.remove();
    addMessage("assistant", `<p>${esc(e)}</p>`, {error: true});
  } finally {
    send.disabled = false;
    input.focus();
  }
}

form.addEventListener("submit", e => {
  e.preventDefault();
  const q = input.value.trim();
  if (!q || send.disabled) return;
  input.value = "";
  submit(q);
});
input.addEventListener("keydown", e => {
  if (e.key === "Enter" && !e.shiftKey) { e.preventDefault(); form.requestSubmit(); }
});
document.getElementById("reset").addEventListener("click", () => { history = []; thread.innerHTML = ""; input.focus(); });
input.focus();
</script>
</body>
</html>
```

- [ ] **Step 5 : lancer, succès attendu, suite complète**

Run : `.venv/Scripts/python -m pytest -q tests/test_chat_web.py` puis `.venv/Scripts/python -m pytest -q`
Attendu : tout vert. Note : `tests/test_chat_web.py` importe depuis `test_chat_agent`. `pythonpath` inclut `src`, et pytest ajoute `tests/` à `sys.path` (rootdir, mode d'import par défaut) : c'est déjà ce qui permet `from conftest import ...`. Si l'import échoue, déplacer `ScriptedGenerator`, `chat_settings` et `POLICY_AREAS` dans `tests/conftest.py`.

- [ ] **Step 6 : commit**

```bash
git add src/tsi_agent/chat/web.py src/tsi_agent/chat/static/index.html tests/test_chat_web.py
git commit -m "feat: page web locale du chatbot (Flask, sources, etapes, cout)

Co-Authored-By: Claude Opus 5.5 (1M context) <noreply@anthropic.com>"
```

---

### Task 7 : commandes `chat` et `export-yaml --chat`, README

**Files :**
- Modify : `src/tsi_agent/__main__.py`, `README.md`
- Test : `tests/test_cli.py`

- [ ] **Step 1 : tests (à ajouter à `tests/test_cli.py`)**

```python
def test_chat_command_starts_server(monkeypatch):
    started = {}

    class FakeApp:
        def run(self, host, port, threaded):
            started.update(host=host, port=port)
    monkeypatch.setattr("tsi_agent.chat.web.create_app", lambda settings, **kw: FakeApp())
    monkeypatch.setattr("webbrowser.open", lambda url: started.setdefault("url", url))
    assert cli.main(["chat", "--port", "9999", "--no-browser"]) == 0
    assert started == {"host": "127.0.0.1", "port": 9999}


def test_chat_command_without_vector_layer(monkeypatch, capsys):
    from tsi_agent.vector_state import VectorLayerError

    def boom(settings, **kw):
        raise VectorLayerError("couche vectorisee absente : lancez d'abord python -m tsi_agent index")
    monkeypatch.setattr("tsi_agent.chat.web.create_app", boom)
    assert cli.main(["chat", "--no-browser"]) == 2
    assert "tsi_agent index" in capsys.readouterr().err


def test_export_chat_yaml(tmp_path, monkeypatch):
    from haystack import Pipeline
    monkeypatch.setattr("tsi_agent.chat.pipeline.build_chat_pipeline", lambda settings, areas: Pipeline())
    out = tmp_path / "chat.yaml"
    assert cli.main(["export-yaml", "--chat", str(out)]) == 0 and out.exists()
```

- [ ] **Step 2 : lancer, échec attendu**

Run : `.venv/Scripts/python -m pytest -q tests/test_cli.py`
Attendu : erreur argparse sur `chat` et `--chat`.

- [ ] **Step 3 : CLI**

Dans `src/tsi_agent/__main__.py` :

- Après le sous-parseur `index`, ajouter :

```python
    chat = sub.add_parser("chat", help="chatbot local sur la couche vectorisée (page web)")
    chat.add_argument("--port", type=int, help="port local (défaut : chat_port des réglages)")
    chat.add_argument("--no-browser", action="store_true", help="ne pas ouvrir le navigateur")
```

- Après `export.add_argument("--index", ...)`, ajouter :

```python
    export.add_argument("--chat", action="store_true", help="exporter le pipeline du chatbot")
```

- Dans le bloc `export-yaml`, la construction du pipeline devient (à l'intérieur du `try` existant) :

```python
            if args.chat:
                from tsi_agent.chat import pipeline as chat_pipeline
                pipe = chat_pipeline.build_chat_pipeline(settings, sources.get("policy_areas") or [])
            elif args.index:
                pipe = build_index_pipeline(settings)
            else:
                pipe = build_pipeline(settings, sources)
```

- Ajouter, avant le bloc `index` :

```python
    if args.command == "chat":
        import webbrowser

        from tsi_agent.chat import web
        port = args.port or settings.chat_port
        try:
            app = web.create_app(settings, policy_areas=sources.get("policy_areas") or [])
        except Exception as exc:
            print(f"ERREUR : {exc}", file=sys.stderr)
            return 2
        url = f"http://127.0.0.1:{port}/"
        print(f"Chatbot : {url}  (Ctrl+C pour arrêter)")
        if not args.no_browser:
            webbrowser.open(url)
        app.run(host="127.0.0.1", port=port, threaded=True)
        return 0
```

Les imports sont faits dans la fonction, pour que `run` et `index` ne chargent pas Flask. Les tests remplacent `tsi_agent.chat.web.create_app` et `tsi_agent.chat.pipeline.build_chat_pipeline` au niveau du module, d'où l'appel par attribut de module (`web.create_app`, `chat_pipeline.build_chat_pipeline`).

- [ ] **Step 4 : README**

Ajouter après la section « Couche vectorisée (préparation du RAG) » :

````markdown
## Chatbot (agent RAG)

Un troisième pipeline, `chat`, répond aux questions sur les projets à partir de la couche vectorisée. C'est un
`Agent` Haystack qui dispose de trois outils :

| Outil | Rôle |
|---|---|
| `search_projects` | Recherche sémantique dans les textes des projets, filtres pays, années, programme, domaine ; renvoie des passages numérotés [n] |
| `list_projects` | Comptage et liste exacts des projets correspondant à des filtres, sur tout le corpus |
| `get_project` | Texte complet d'un projet |

```powershell
# Prérequis : la couche vectorisée (python -m tsi_agent index) et l'extra local
.venv\Scripts\python -m tsi_agent chat            # ouvre http://127.0.0.1:8765
.venv\Scripts\python -m tsi_agent export-yaml --chat chat.yaml
```

Exemples de questions :
- « Que fait le projet Energy Regulation Academy ? »
- « Quels projets aident la Croatie à réformer son système de santé ? »
- « Combien de projets TSI pour la Grèce en 2023 ? »
- « Compare les projets santé de la Croatie et de la Slovénie. », puis « Et en Slovaquie ? »

Chaque réponse cite ses sources [n] vers les pages de la Commission et signale ce qui vient d'un résumé IA. La page
affiche les étapes de l'agent, la durée et le coût équivalent API. Chaque question est un run du journal
(`logs/runs/`, `mode: chat`). En local, une réponse demande 2 à 4 appels au CLI `claude`, soit 20 à 60 s.

**Passage à deepset :** dans le YAML exporté, remplacer :
- `agent.init_parameters.chat_generator.init_parameters.chat_generator` (le `ClaudeCliChatGenerator`) par un
  `OpenAIChatGenerator`, qui appelle les outils nativement ;
- le `text_embedder` de l'outil `search_projects` par un `OpenAITextEmbedder` (`text-embedding-3-small`, sans
  préfixe), cohérent avec l'embedder de l'index ;
- le `PersistedInMemoryDocumentStore`, qui apparaît dans les trois outils, par le document store de l'espace de
  travail (et `InMemoryEmbeddingRetriever` par le retriever correspondant, par exemple `OpenSearchEmbeddingRetriever`).

Les trois outils, le prompt et l'Agent ne changent pas.
````

- [ ] **Step 5 : lancer, succès attendu, suite complète**

Run : `.venv/Scripts/python -m pytest -q`
Attendu : tout vert.

- [ ] **Step 6 : commit**

```bash
git add src/tsi_agent/__main__.py tests/test_cli.py README.md
git commit -m "feat: commandes chat et export-yaml --chat ; README du chatbot

Co-Authored-By: Claude Opus 5.5 (1M context) <noreply@anthropic.com>"
```

- [ ] **Step 7 : essai réel (contrôleur, pas sous-agent)**

Lancer `python -m tsi_agent chat --no-browser` en arrière-plan, puis envoyer à `http://127.0.0.1:8765/api/chat` les cinq questions types de la spec (§10), la dernière comme question de suivi avec son historique. Noter pour chacune la durée, le coût, les étapes et la justesse de la réponse (comptages vérifiés contre `list_projects`). Corriger le prompt si besoin.
