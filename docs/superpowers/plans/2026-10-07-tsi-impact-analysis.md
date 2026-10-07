# Analyse d'impact d'un projet — Plan d'implémentation

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** pour un projet choisi dans la page web locale, un agent Haystack lit le projet, cherche sur le web des
indices d'impact positif ou négatif au regard des objectifs, et produit un rapport structuré en anglais, sources
citées avec lien, enregistré dans `output/impact/<project_id>.md`.

**Architecture:** nouveau paquet `src/tsi_agent/impact/` sur le modèle de `chat/` : un `Agent` Haystack avec 4
outils (`get_project`, `search_deliverables` réutilisés, `web_search`, `fetch_page` nouveaux), état `sources`
partagé (numérotation continue), réponse finale JSON assemblée en Markdown par le code (citations inventées
retirées). La page web existante gagne un onglet « Impact analysis » qui lance une tâche en arrière-plan et en
affiche la progression (via un écouteur sur le journal de run).

**Tech Stack:** Python 3.13, haystack-ai 3.2 (Agent, ComponentTool, LinkContentFetcher, HTMLToDocument), CLI
`claude` (souscription ; outil natif WebSearch), trafilatura, pdfplumber, Flask, pytest.

**Spec :** `docs/superpowers/specs/2026-10-07-tsi-impact-analysis-design.md`

**Conventions du dépôt :** commentaires et docstrings internes en français sans accents dans le code récent
(`# erreur generique ...`), docstrings des méthodes `run` d'outils **en anglais** (le LLM les lit). Tests :
`.venv/Scripts/python.exe -m pytest -q` depuis la racine. Ne jamais écrire dans `output/` pendant les tests
(toujours `tmp_path`). Les tests qui appellent `from_dict` hors `Pipeline.loads` posent
`monkeypatch.setenv("HAYSTACK_DESERIALIZATION_ALLOWLIST", "tsi_agent")`. Fin de message de commit :
`Co-Authored-By: Claude Opus 5.5 (1M context) <noreply@anthropic.com>`.

---

## Structure des fichiers

| Fichier | Action | Rôle |
|---|---|---|
| `pyproject.toml` | modifier | dépendance `trafilatura>=1.12` |
| `config/settings.yaml`, `src/tsi_agent/config.py` | modifier | réglages `impact_*`, `web_search_*` |
| `src/tsi_agent/runlog.py` | modifier | `RunLog.listeners` (progression en direct) |
| `src/tsi_agent/components/claude_cli.py` | modifier | paramètre `allowed_tools` (outils natifs du CLI) |
| `src/tsi_agent/chat/tools.py` | modifier | `ProjectReader(max_chars=...)` |
| `src/tsi_agent/impact/__init__.py` | créer | vide |
| `src/tsi_agent/impact/web_search.py` | créer | `ClaudeWebSearch` |
| `src/tsi_agent/impact/tools.py` | créer | `WebSearchTool`, `PageReader`, `page_text` |
| `src/tsi_agent/impact/report.py` | créer | parsing JSON, citations, rendu Markdown, lecture des rapports |
| `src/tsi_agent/impact/prompt.py` | créer | prompt système et message utilisateur |
| `src/tsi_agent/impact/pipeline.py` | créer | `build_impact_pipeline` |
| `src/tsi_agent/impact/catalog.py` | créer | index des projets (front matter) et recherche par titre/pays/année |
| `src/tsi_agent/impact/service.py` | créer | `analyse`, `progress_text`, `ImpactJobs` |
| `src/tsi_agent/chat/web.py` | modifier | routes `/api/projects`, `/api/impact/...` |
| `src/tsi_agent/chat/static/index.html` | modifier | onglets Chat / Impact analysis |
| `src/tsi_agent/__main__.py` | modifier | `export-yaml --impact` |
| `tests/test_impact_*.py`, `tests/fixtures/web_article.html` | créer | tests |

---

### Task 1 : socle — réglages, dépendance, écouteurs du journal, outils natifs du CLI, `ProjectReader(max_chars)`

**Files:**
- Modify: `pyproject.toml`, `config/settings.yaml`, `src/tsi_agent/config.py`, `src/tsi_agent/runlog.py`,
  `src/tsi_agent/components/claude_cli.py`, `src/tsi_agent/chat/tools.py`
- Test: `tests/test_runlog.py`, `tests/test_claude_cli.py`, `tests/test_chat_store_tools.py`

- [ ] **Step 1 : tests qui échouent**

Ajouter à `tests/test_runlog.py` :

```python
def test_listeners_receive_each_event_and_errors_are_ignored(tmp_path):
    seen = []

    def boom(record):
        raise RuntimeError("listener failure must not break the run")

    with runlog.open_run(tmp_path, "l1") as run:
        run.listeners.append(seen.append)
        run.listeners.append(boom)
        runlog.event("X", "tool_call", "web_search", query="q")
    assert seen[0]["target"] == "web_search" and seen[0]["query"] == "q"
    assert run.final_summary["counts"] == {"tool_call:ok": 1}
```

(adapter l'import en tête de fichier si `runlog` n'y est pas déjà importé : `from tsi_agent import runlog`).

Ajouter à `tests/test_claude_cli.py` :

```python
def test_allowed_tools_enable_native_cli_tools(monkeypatch):
    gen = ClaudeCliChatGenerator(model="haiku", allowed_tools=["WebSearch"])
    monkeypatch.setattr(gen, "_resolve_executable", lambda: "claude")
    cmd = gen._command("sys")
    assert cmd[cmd.index("--tools") + 1] == "WebSearch"
    assert cmd[cmd.index("--allowedTools") + 1] == "WebSearch"
    plain = ClaudeCliChatGenerator(model="haiku")
    monkeypatch.setattr(plain, "_resolve_executable", lambda: "claude")
    assert plain._command("sys")[plain._command("sys").index("--tools") + 1] == ""
    assert "--allowedTools" not in plain._command("sys")
```

Ajouter à `tests/test_chat_store_tools.py` (le fichier construit déjà un store de test ; réutiliser sa fixture ou
`make_vector_store` de `conftest`) :

```python
def test_project_reader_max_chars(tmp_path):
    from conftest import make_vector_store
    from tsi_agent.chat.tools import ProjectReader
    from tsi_agent.qdrant_local import open_store
    store = open_store(str(make_vector_store(tmp_path)), 8)
    short = ProjectReader(document_store=store, max_chars=300).run(doc_id="energy-regulation-academy")
    assert len(short["result"]) == 300
    assert ProjectReader(document_store=store).to_dict()["init_parameters"]["max_chars"] == 8000
```

- [ ] **Step 2 : vérifier l'échec**

Run: `.venv/Scripts/python.exe -m pytest -q tests/test_runlog.py tests/test_claude_cli.py tests/test_chat_store_tools.py`
Expected: FAIL (`listeners`, `allowed_tools`, `max_chars` inconnus).

- [ ] **Step 3 : implémentation**

`src/tsi_agent/runlog.py` — dans `RunLog.__init__`, après `self.final_summary = None` :

```python
        # Ecouteurs appeles a chaque evenement (progression en direct, ex. page d'analyse d'impact).
        self.listeners: list[Any] = []
```

et à la fin de `RunLog.event`, **après** le bloc `with self._lock:` :

```python
        for listener in list(self.listeners):
            try:
                listener(record)
            except Exception:  # un ecouteur defaillant ne doit jamais casser le run
                _logger.exception("runlog listener failed")
```

`src/tsi_agent/components/claude_cli.py` — `__init__` gagne `allowed_tools: list[str] | None = None` (attribut
`self.allowed_tools = allowed_tools`, même nom pour la sérialisation par défaut). Dans `_command` et
`_command_for_tools`, remplacer `"--tools", ""` par `*self._tools_args()` avec :

```python
    def _tools_args(self) -> list[str]:
        """Outils natifs du CLI : aucun par defaut ; `allowed_tools` (ex. ["WebSearch"]) les active sans demande."""
        if not self.allowed_tools:
            return ["--tools", ""]
        names = ",".join(self.allowed_tools)
        return ["--tools", names, "--allowedTools", names]
```

`src/tsi_agent/chat/tools.py` — `ProjectReader.__init__(self, document_store, max_chars: int = READER_MAX_CHARS)`
(`self.max_chars = max_chars`), `to_dict` : `default_to_dict(self, document_store=..., max_chars=self.max_chars)`,
et dans `run` : `[:self.max_chars]` au lieu de `[:READER_MAX_CHARS]`.

`src/tsi_agent/config.py` — après le bloc livrables :

```python
    # Analyse d'impact (onglet de la page web).
    impact_dir: str = "output/impact"
    impact_model: str = "sonnet"
    impact_max_steps: int = 20  # appels LLM maximum par analyse
    impact_max_searches: int = 10  # recherches web maximum par analyse (impose par l'outil)
    impact_page_words: int = 3000  # texte d'une page web transmis a l'agent
    impact_project_chars: int = 20000  # texte du projet transmis a l'agent
    impact_llm_timeout_s: int = 600  # le rapport final est long
    web_search_model: str = "haiku"
    web_search_timeout_s: int = 180
```

`config/settings.yaml` — à la fin :

```yaml

# Analyse d'impact (onglet "Impact analysis" de la page web)
impact_dir: output/impact
impact_model: sonnet
impact_max_steps: 20        # appels LLM maximum par analyse
impact_max_searches: 10     # recherches web maximum par analyse
impact_page_words: 3000     # texte d'une page web transmis a l'agent
impact_project_chars: 20000
impact_llm_timeout_s: 600
web_search_model: haiku     # la recherche web elle-meme (CLI claude, outil WebSearch)
web_search_timeout_s: 180
```

`pyproject.toml` — ajouter `"trafilatura>=1.12",` aux `dependencies` (requis par `HTMLToDocument`), puis
installer : `.venv/Scripts/python.exe -m pip install -e ".[local,dev]"`.

- [ ] **Step 4 : vérifier**

Run: `.venv/Scripts/python.exe -m pytest -q`
Expected: tout passe.

- [ ] **Step 5 : commit**

```bash
git add pyproject.toml config/settings.yaml src/tsi_agent/config.py src/tsi_agent/runlog.py src/tsi_agent/components/claude_cli.py src/tsi_agent/chat/tools.py tests/
git commit -m "feat(impact): socle - reglages, ecouteurs du journal, outils natifs du CLI, ProjectReader(max_chars)"
```

---

### Task 2 : `ClaudeWebSearch` (recherche web via le CLI, interface SerperDevWebSearch)

**Files:**
- Create: `src/tsi_agent/impact/__init__.py` (vide), `src/tsi_agent/impact/web_search.py`
- Test: `tests/test_impact_web_search.py`

- [ ] **Step 1 : tests qui échouent**

```python
import json

import pytest
from haystack.dataclasses import ChatMessage

from tsi_agent import runlog
from tsi_agent.impact.web_search import SEARCH_SCHEMA, ClaudeWebSearch


class FakeCli:
    def __init__(self, payload):
        self.payload = payload
        self.messages = None

    def run(self, messages, tools=None):
        self.messages = messages
        if isinstance(self.payload, Exception):
            raise self.payload
        meta = {"model": "claude-cli/haiku", "usage": {"prompt_tokens": 50, "completion_tokens": 20},
                "cost_usd": 0.002}
        return {"replies": [ChatMessage.from_assistant(json.dumps(self.payload), meta=meta)]}


def test_returns_documents_and_links_and_logs_cost(tmp_path, monkeypatch):
    search = ClaudeWebSearch(top_k=2)
    fake = FakeCli({"results": [
        {"title": "A", "url": "https://a.eu/x", "snippet": "alpha"},
        {"title": "Bad", "url": "javascript:alert(1)", "snippet": "no"},
        {"title": "B", "url": "https://b.org/y", "snippet": "beta"},
        {"title": "C", "url": "https://c.org/z", "snippet": "gamma"}]})
    monkeypatch.setattr(search, "_cli", lambda: fake)
    with runlog.open_run(tmp_path, "w1") as run:
        out = search.run(query="Croatia health reform impact")
    assert [d.meta["link"] for d in out["documents"]] == ["https://a.eu/x", "https://b.org/y"]
    assert out["documents"][0].meta["title"] == "A" and out["documents"][0].content == "alpha"
    assert out["links"] == ["https://a.eu/x", "https://b.org/y"]
    assert "Croatia health reform impact" in fake.messages[-1].text
    assert run.final_summary["llm_calls"] == 1 and run.final_summary["cost_usd"] == 0.002


def test_cli_is_configured_for_websearch_only():
    cli = ClaudeWebSearch(model="haiku", timeout_s=99)._cli()
    assert cli.allowed_tools == ["WebSearch"] and cli.json_schema == SEARCH_SCHEMA
    assert cli.model == "haiku" and cli.timeout_s == 99


def test_round_trip(monkeypatch):
    monkeypatch.setenv("HAYSTACK_DESERIALIZATION_ALLOWLIST", "tsi_agent")
    again = ClaudeWebSearch.from_dict(ClaudeWebSearch(model="haiku", top_k=5).to_dict())
    assert again.model == "haiku" and again.top_k == 5


def test_unparseable_reply_gives_no_documents(tmp_path, monkeypatch):
    search = ClaudeWebSearch()
    monkeypatch.setattr(search, "_cli", lambda: FakeCli("not a dict"))
    assert search.run(query="q") == {"documents": [], "links": []}
```

- [ ] **Step 2 : vérifier l'échec** — `pytest -q tests/test_impact_web_search.py` → FAIL (module absent).

- [ ] **Step 3 : implémentation** — `src/tsi_agent/impact/web_search.py` :

```python
"""Recherche web locale par le CLI `claude` (outil natif WebSearch, souscription) ; meme interface que
SerperDevWebSearch (serperdev-haystack), qui la remplace dans deepset : run(query) -> documents + links."""

import json
import time
from typing import Any

from haystack import Document, component, default_from_dict, default_to_dict
from haystack.dataclasses import ChatMessage

from tsi_agent import runlog
from tsi_agent.components.claude_cli import ClaudeCliChatGenerator

SEARCH_SCHEMA = {
    "type": "object",
    "properties": {"results": {"type": "array", "items": {
        "type": "object",
        "properties": {"title": {"type": "string"}, "url": {"type": "string"}, "snippet": {"type": "string"}},
        "required": ["title", "url", "snippet"]}}},
    "required": ["results"],
}
SEARCH_SYSTEM = ("You are a web search engine. Run the WebSearch tool for the user's query (you may refine it once "
                 "if the first results are poor), then return the results. Only return URLs that appeared in the "
                 "WebSearch results; never invent or guess a URL. For each result give its title and a 1-3 "
                 "sentence snippet saying what the page contains.")


@component
class ClaudeWebSearch:
    def __init__(self, model: str = "haiku", top_k: int = 8, timeout_s: int = 180, executable: str = "claude"):
        self.model = model
        self.top_k = top_k
        self.timeout_s = timeout_s
        self.executable = executable

    def to_dict(self) -> dict[str, Any]:
        return default_to_dict(self, model=self.model, top_k=self.top_k, timeout_s=self.timeout_s,
                               executable=self.executable)

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "ClaudeWebSearch":
        return default_from_dict(cls, data)

    def _cli(self) -> Any:
        return ClaudeCliChatGenerator(model=self.model, timeout_s=self.timeout_s, json_schema=SEARCH_SCHEMA,
                                      executable=self.executable, allowed_tools=["WebSearch"])

    @component.output_types(documents=list[Document], links=list[str])
    def run(self, query: str) -> dict[str, Any]:
        t0 = time.monotonic()
        messages = [ChatMessage.from_system(SEARCH_SYSTEM),
                    ChatMessage.from_user(f"Query: {query}\nReturn up to {self.top_k} results.")]
        reply = self._cli().run(messages=messages)["replies"][0]
        meta = reply.meta or {}
        usage = meta.get("usage") or {}
        try:
            payload = json.loads(reply.text or "")
        except json.JSONDecodeError:
            payload = None
        results = payload.get("results") if isinstance(payload, dict) else None
        documents: list[Document] = []
        for item in results or []:
            url = str((item or {}).get("url") or "")
            if not url.startswith(("http://", "https://")):
                continue
            documents.append(Document(content=str(item.get("snippet") or ""),
                                      meta={"title": str(item.get("title") or url), "link": url}))
            if len(documents) == self.top_k:
                break
        runlog.event("ClaudeWebSearch", "llm_call", str(meta.get("model")), model=meta.get("model"),
                     input_tokens=usage.get("prompt_tokens", 0), output_tokens=usage.get("completion_tokens", 0),
                     cache_read_tokens=usage.get("cache_read_input_tokens", 0),
                     cache_creation_tokens=usage.get("cache_creation_input_tokens", 0),
                     cost_usd=float(meta.get("cost_usd") or 0.0), query=query, results=len(documents),
                     duration_ms=round((time.monotonic() - t0) * 1000))
        return {"documents": documents, "links": [d.meta["link"] for d in documents]}
```

- [ ] **Step 4 : vérifier** — `pytest -q tests/test_impact_web_search.py` → PASS.

- [ ] **Step 5 : commit** — `git commit -m "feat(impact): ClaudeWebSearch - recherche web via le CLI claude (interface SerperDev)"`

---

### Task 3 : outils `web_search` et `fetch_page`

**Files:**
- Create: `src/tsi_agent/impact/tools.py`, `tests/fixtures/web_article.html`
- Test: `tests/test_impact_tools.py`

- [ ] **Step 1 : fixture** — `tests/fixtures/web_article.html` : une page HTML d'article (balises `<html>`,
  `<head><title>Croatia cuts hospital arrears</title></head>`, `<body>` avec un `<nav>` de menu, un `<article>` de
  5 paragraphes d'au moins 60 mots chacun sur la réduction des arriérés hospitaliers en Croatie après une réforme
  appuyée par la Commission, dont la phrase exacte « Hospital arrears fell by 40 percent between 2021 and 2023. »,
  et un `<footer>`). Texte en anglais, inventé pour le test.

- [ ] **Step 2 : tests qui échouent** — `tests/test_impact_tools.py` :

```python
from pathlib import Path

import pytest
from haystack import Document, component
from haystack.dataclasses import ByteStream

from tsi_agent import runlog
from tsi_agent.components.claude_cli import LLMQuotaExceeded
from tsi_agent.impact.tools import PageReader, WebSearchTool, page_text

FIX = Path(__file__).parent / "fixtures"


@component
class FakeEngine:
    def __init__(self, results=None, error=None):
        self.results = results or []
        self.error = error
        self.queries = []

    @component.output_types(documents=list[Document], links=list[str])
    def run(self, query: str):
        self.queries.append(query)
        if self.error:
            raise self.error
        docs = [Document(content=s, meta={"title": t, "link": u}) for t, u, s in self.results]
        return {"documents": docs, "links": [d.meta["link"] for d in docs]}


@component
class FakeFetcher:
    def __init__(self, pages=None):
        self.pages = pages or {}

    @component.output_types(streams=list[ByteStream])
    def run(self, urls: list[str]):
        url = urls[0]
        if url not in self.pages:
            raise RuntimeError(f"HTTP 404 for {url}")
        data, ctype = self.pages[url]
        return {"streams": [ByteStream(data=data, meta={"url": url, "content_type": ctype})]}


def test_web_search_numbers_after_known_sources_and_reuses_known_urls():
    tool = WebSearchTool(search_engine=FakeEngine([("A", "https://a.eu", "alpha"), ("Old", "https://old.eu", "o")]))
    known = [{"ref": 1, "url": "https://project"}, {"ref": 2, "url": "https://old.eu", "title": "Old"}]
    out = tool.run(query="impact", known_sources=known, past_searches=[])
    assert out["result"].startswith("[3] A\nURL: https://a.eu\nalpha")
    assert "[2] Old" in out["result"]
    assert [s["ref"] for s in out["sources"]] == [3]
    assert out["sources"][0]["kind"] == "web" and out["searches"] == ["impact"]


def test_web_search_limit_and_errors():
    engine = FakeEngine([("A", "https://a.eu", "alpha")])
    tool = WebSearchTool(search_engine=engine, max_searches=2)
    out = tool.run(query="third", known_sources=[], past_searches=["one", "two"])
    assert "limit" in out["result"].lower() and out["sources"] == [] and engine.queries == []
    broken = WebSearchTool(search_engine=FakeEngine(error=RuntimeError("network down")))
    assert "failed" in broken.run(query="q")["result"]
    with pytest.raises(LLMQuotaExceeded):
        WebSearchTool(search_engine=FakeEngine(error=LLMQuotaExceeded("limit"))).run(query="q")


def test_page_text_html_pdf_and_plain():
    html = (FIX / "web_article.html").read_bytes()
    text = page_text(html, "text/html; charset=utf-8", "https://x.eu/a")
    assert "Hospital arrears fell by 40 percent" in text
    pdf = (FIX / "deliverable_small.pdf").read_bytes()
    assert "--- page 1 ---" in page_text(pdf, "application/pdf", "https://x.eu/r.pdf")
    assert page_text(b"plain words", "text/plain", "https://x.eu/t") == "plain words"
    with pytest.raises(ValueError):
        page_text(b"\x00\x01", "image/png", "https://x.eu/i.png")


def test_fetch_page_numbers_truncates_and_logs(tmp_path):
    html = (FIX / "web_article.html").read_bytes()
    reader = PageReader(max_words=50, fetcher=FakeFetcher({"https://x.eu/a": (html, "text/html")}))
    known = [{"ref": 1, "url": "https://p"}, {"ref": 2, "url": "https://x.eu/a", "title": "Croatia cuts"}]
    with runlog.open_run(tmp_path, "p1") as run:
        out = reader.run(url="https://x.eu/a", known_sources=known)
        miss = reader.run(url="https://missing.eu", known_sources=known)
    assert out["result"].startswith("[2] Croatia cuts\nURL: https://x.eu/a")
    assert out["sources"] == []  # URL deja numerotee : meme numero, pas de nouvelle source
    assert "truncated" in out["result"] and len(out["result"].split()) < 90
    assert miss["result"].startswith("Could not read https://missing.eu") and miss["sources"] == []
    assert run.final_summary["counts"]["tool_call:ok"] == 1 and run.final_summary["counts"]["tool_call:error"] == 1


def test_fetch_page_new_url_gets_next_number():
    reader = PageReader(fetcher=FakeFetcher({"https://n.eu": (b"some text", "text/plain")}))
    out = reader.run(url="https://n.eu", known_sources=[{"ref": 1, "url": "https://p"}])
    assert out["sources"][0]["ref"] == 2 and out["sources"][0]["url"] == "https://n.eu"


def test_tools_round_trip(monkeypatch):
    monkeypatch.setenv("HAYSTACK_DESERIALIZATION_ALLOWLIST", "tsi_agent")
    from tsi_agent.impact.web_search import ClaudeWebSearch
    tool = WebSearchTool.from_dict(WebSearchTool(search_engine=ClaudeWebSearch(), max_searches=4).to_dict())
    assert isinstance(tool.search_engine, ClaudeWebSearch) and tool.max_searches == 4
    reader = PageReader.from_dict(PageReader(max_words=10).to_dict())
    assert reader.max_words == 10 and type(reader.fetcher).__name__ == "LinkContentFetcher"
```

- [ ] **Step 3 : vérifier l'échec** — `pytest -q tests/test_impact_tools.py` → FAIL.

- [ ] **Step 4 : implémentation** — `src/tsi_agent/impact/tools.py` :

```python
"""Outils web de l'agent d'analyse d'impact : recherche web et lecture d'une page (HTML, PDF, Word, texte).

Les sources sont numerotees a la suite de l'etat `sources` de l'Agent (comme le chatbot) ; une URL deja connue
garde son numero.
"""

import time
from typing import Any
from urllib.parse import urlparse

from bs4 import BeautifulSoup
from haystack import component, default_from_dict, default_to_dict
from haystack.components.converters import HTMLToDocument
from haystack.components.fetchers import LinkContentFetcher
from haystack.core.serialization import component_to_dict
from haystack.dataclasses import ByteStream
from haystack.utils import deserialize_component_inplace

from tsi_agent import runlog
from tsi_agent.components.claude_cli import LLMQuotaExceeded
from tsi_agent.components.deliverables import docx_text, pdf_text

USER_AGENT = "TSI-Impact-Agent/0.1 (DG REFORM project)"


def _known_ref(known: list[dict], url: str) -> dict | None:
    return next((s for s in known if s.get("url") == url), None)


@component
class WebSearchTool:
    def __init__(self, search_engine: Any, max_searches: int = 10):
        """:param search_engine: composant run(query) -> {"documents": [...]} (ClaudeWebSearch, SerperDevWebSearch)."""
        self.search_engine = search_engine
        self.max_searches = max_searches

    def to_dict(self) -> dict[str, Any]:
        return default_to_dict(self, max_searches=self.max_searches,
                               search_engine=component_to_dict(obj=self.search_engine, name="search_engine"))

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "WebSearchTool":
        deserialize_component_inplace(data["init_parameters"], key="search_engine")
        return default_from_dict(cls, data)

    @component.output_types(result=str, sources=list[dict], searches=list[str])
    def run(self, query: str, known_sources: list[dict] | None = None,
            past_searches: list[str] | None = None) -> dict[str, Any]:
        """Search the web (news, official sites, OECD/Commission reports, studies). Returns numbered results [n]
        with title, URL and snippet. Read a result with fetch_page before relying on it.

        :param query: Search query, preferably in English or in the country's language (e.g. "Croatia hospital
            arrears 2023 reform results").
        """
        known = known_sources or []
        if len(past_searches or []) >= self.max_searches:
            runlog.event("WebSearchTool", "tool_call", "web_search", "limit", query=query)
            return {"result": f"Search limit reached ({self.max_searches} searches). Use fetch_page on the results "
                              "you already have, then write the report.", "sources": [], "searches": []}
        t0 = time.monotonic()
        try:
            docs = self.search_engine.run(query=query)["documents"]
        except LLMQuotaExceeded:
            raise
        except Exception as exc:  # erreur renvoyee a l'agent, qui continue
            runlog.event("WebSearchTool", "tool_call", "web_search", "error", query=query, error=repr(exc)[:300])
            return {"result": f"Web search failed: {exc}", "sources": [], "searches": [query]}
        next_ref = len(known) + 1
        sources: list[dict[str, Any]] = []
        blocks: list[str] = []
        for doc in docs:
            url = doc.meta.get("link") or doc.meta.get("url")
            title = doc.meta.get("title") or url
            if not url:
                continue
            existing = _known_ref(known + sources, url)
            if existing:
                ref = existing["ref"]
            else:
                ref = next_ref
                next_ref += 1
                sources.append({"ref": ref, "kind": "web", "title": title, "url": url, "section": None,
                                "ai_generated": False})
            blocks.append(f"[{ref}] {title}\nURL: {url}\n{doc.content or ''}")
        runlog.event("WebSearchTool", "tool_call", "web_search", query=query, results=len(blocks),
                     duration_ms=round((time.monotonic() - t0) * 1000))
        return {"result": "\n\n".join(blocks) if blocks else "No results.", "sources": sources,
                "searches": [query]}


def page_text(data: bytes, content_type: str, url: str) -> str:
    """Texte d'une page web ou d'un document ; ValueError si le type n'est pas pris en charge."""
    ctype = (content_type or "").lower()
    if "pdf" in ctype or data[:5] == b"%PDF-":
        return pdf_text(data)[1]
    if "wordprocessingml" in ctype:
        return docx_text(data)[1]
    if "html" in ctype or data.lstrip()[:15].lower().startswith((b"<!doctype", b"<html")):
        stream = ByteStream(data=data, meta={"url": url, "content_type": "text/html"})
        docs = HTMLToDocument().run(sources=[stream])["documents"]
        text = docs[0].content if docs and docs[0].content else ""
        if len(text.split()) < 50:  # extraction trafilatura trop maigre : texte brut de la page
            text = BeautifulSoup(data, "html.parser").get_text(" ", strip=True)
        return text
    if ctype.startswith("text/"):
        return data.decode("utf-8", errors="replace")
    raise ValueError(f"unsupported content type: {content_type or 'unknown'}")


@component
class PageReader:
    def __init__(self, max_words: int = 3000, timeout_s: int = 20, fetcher: Any = None):
        self.max_words = max_words
        self.timeout_s = timeout_s
        self.fetcher = fetcher if fetcher is not None else LinkContentFetcher(
            raise_on_failure=True, timeout=timeout_s, user_agents=[USER_AGENT], retry_attempts=1)

    def to_dict(self) -> dict[str, Any]:
        return default_to_dict(self, max_words=self.max_words, timeout_s=self.timeout_s,
                               fetcher=component_to_dict(obj=self.fetcher, name="fetcher"))

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "PageReader":
        deserialize_component_inplace(data["init_parameters"], key="fetcher")
        return default_from_dict(cls, data)

    @component.output_types(result=str, sources=list[dict])
    def run(self, url: str, known_sources: list[dict] | None = None) -> dict[str, Any]:
        """Read a web page or online document (HTML, PDF, Word) and return its text, to check what it says
        before citing it. Long pages are truncated.

        :param url: Full URL (http or https), e.g. one returned by web_search.
        """
        known = known_sources or []
        t0 = time.monotonic()
        try:
            if not url.startswith(("http://", "https://")):
                raise ValueError("not an http(s) URL")
            stream = self.fetcher.run(urls=[url])["streams"][0]
            text = page_text(stream.data, str(stream.meta.get("content_type") or ""), url)
        except Exception as exc:  # erreur renvoyee a l'agent, qui continue
            runlog.event("PageReader", "tool_call", "fetch_page", "error", url=url, error=repr(exc)[:300])
            return {"result": f"Could not read {url}: {exc}", "sources": []}
        words = text.split()
        body = " ".join(words[: self.max_words])
        if len(words) > self.max_words:
            body += f"\n(truncated: first {self.max_words} of {len(words)} words)"
        existing = _known_ref(known, url)
        sources: list[dict[str, Any]] = []
        if existing:
            ref, title = existing["ref"], existing.get("title") or url
        else:
            ref, title = len(known) + 1, urlparse(url).netloc + urlparse(url).path
            sources.append({"ref": ref, "kind": "web", "title": title, "url": url, "section": None,
                            "ai_generated": False})
        runlog.event("PageReader", "tool_call", "fetch_page", url=url, words=len(words),
                     duration_ms=round((time.monotonic() - t0) * 1000))
        return {"result": f"[{ref}] {title}\nURL: {url}\n{body}", "sources": sources}
```

- [ ] **Step 5 : vérifier** — `pytest -q tests/test_impact_tools.py` → PASS. Si `test_page_text_html_pdf_and_plain`
  échoue sur le HTML (trafilatura absent), refaire `pip install -e ".[local,dev]"`.

- [ ] **Step 6 : commit** — `git commit -m "feat(impact): outils web_search et fetch_page (HTML, PDF, Word), numerotation des sources"`

---

### Task 4 : rapport — parsing, citations, rendu, lecture

**Files:**
- Create: `src/tsi_agent/impact/report.py`
- Test: `tests/test_impact_report.py`

- [ ] **Step 1 : tests qui échouent**

```python
import json

import pytest

from tsi_agent.impact.report import (ReportFormatError, clean_citations, list_reports, parse_report, read_report,
                                     render_report, valid_project_id, write_report)

REPORT = {
    "project_overview": "Supports hospital reform [1].",
    "positive_evidence": "- Arrears fell 40% [3][9].",
    "negative_evidence": "Insufficient data on quality [2, 7].",
    "impact_by_level": {"country": "Lower arrears [3].", "europe": "Cited in the Semester [4].", "world": "None found."},
    "objectives": [{"objective": "Reduce arrears", "verdict": "achieved", "rationale": "See [3] | table"},
                   {"objective": "Improve quality", "verdict": "bogus", "rationale": "No data."}],
    "overall_verdict": "Positive but partly attributable.",
    "confidence": "medium",
}
SOURCES = [{"ref": 1, "title": "Project page", "url": "https://p.eu", "section": "Objectives"},
           {"ref": 2, "title": "Report", "url": "https://r.eu", "section": "Final report, p. 4"},
           {"ref": 3, "title": "News", "url": "https://n.eu", "kind": "web"},
           {"ref": 4, "title": "Semester", "url": "https://s.eu", "kind": "web"},
           {"ref": 5, "title": "Unused", "url": "https://u.eu", "kind": "web"}]
META = {"project_id": "hosp-hr", "title": "Hospital reform", "countries": ["HR"], "year": 2022, "programme": "TSI",
        "policy_area": "health-and-long-term-care"}
RUN = {"run_id": "r1", "model": "sonnet", "cost_usd": 0.5, "duration_s": 200.0, "generated": "2026-10-07"}


def test_parse_report_accepts_fences_and_rejects_garbage():
    assert parse_report("```json\n" + json.dumps(REPORT) + "\n```")["confidence"] == "medium"
    assert parse_report("Here it is: " + json.dumps(REPORT))["objectives"][0]["verdict"] == "achieved"
    with pytest.raises(ReportFormatError):
        parse_report("no json at all")


def test_clean_citations_removes_unknown_refs():
    text, removed = clean_citations("A [3][9] and [2, 7] and [12].", {2, 3})
    assert text == "A [3] and [2] and ." and removed == [7, 9, 12]


def test_render_report_structure_sources_and_front_matter():
    md = render_report(META, REPORT, SOURCES, RUN)
    assert md.startswith("---\n") and "overall_verdict: Positive but partly attributable." in md
    body = md.split("\n---\n", 1)[1]
    for heading in ("# Impact analysis — Hospital reform", "## 1. Project overview",
                    "## 2. Evidence of positive impact", "## 3. Evidence of negative impact or limitations",
                    "## 4. Impact by level", "### Country", "## 5. Assessment against objectives",
                    "## 6. Sources"):
        assert heading in body
    assert "[9]" not in body and "[7]" not in body
    assert "| Reduce arrears | achieved | See [3] \\| table |" in body
    assert "| Improve quality | insufficient evidence |" in body
    sources = body.split("## 6. Sources", 1)[1]
    assert "- [1] Project page — Objectives — https://p.eu" in sources
    assert "https://u.eu" not in sources  # source non citee
    from tsi_agent.render import parse_front_matter
    front = parse_front_matter(md)[0]
    assert front["sources"] == 4 and front["removed_citations"] == [7, 9]


def test_write_list_read(tmp_path):
    write_report(tmp_path, "hosp-hr", render_report(META, REPORT, SOURCES, RUN))
    assert [r["project_id"] for r in list_reports(tmp_path)] == ["hosp-hr"]
    front, body = read_report(tmp_path, "hosp-hr")
    assert front["confidence"] == "medium" and body.startswith("# Impact analysis")
    assert read_report(tmp_path, "unknown") is None
    assert not valid_project_id("../etc") and valid_project_id("hosp-hr-2")
```

- [ ] **Step 2 : vérifier l'échec** — FAIL (module absent).

- [ ] **Step 3 : implémentation** — `src/tsi_agent/impact/report.py` :

```python
"""Rapport d'analyse d'impact : reponse JSON de l'agent -> Markdown (front matter + sections fixes).

La section Sources est construite par le code a partir des sources reellement renvoyees par les outils ; toute
citation [n] inconnue est retiree (garde-fou contre les liens inventes).
"""

import json
import re
from pathlib import Path
from typing import Any

from tsi_agent.render import parse_front_matter, render

VERDICTS = ("achieved", "partly achieved", "not achieved", "insufficient evidence")
CONFIDENCES = ("high", "medium", "low")
_CITE = re.compile(r"\[(\d+(?:\s*,\s*\d+)*)\]")
_PROJECT_ID = re.compile(r"^[a-z0-9][a-z0-9-]{0,200}$")


class ReportFormatError(ValueError):
    pass


def valid_project_id(project_id: str) -> bool:
    return bool(_PROJECT_ID.match(project_id or ""))


def parse_report(text: str) -> dict[str, Any]:
    """Premier objet JSON de la reponse (avec ou sans bloc ```json) ; champs manquants completes."""
    start, end = (text or "").find("{"), (text or "").rfind("}")
    if start < 0 or end <= start:
        raise ReportFormatError("no JSON object in the final answer")
    try:
        data = json.loads(text[start:end + 1])
    except json.JSONDecodeError as exc:
        raise ReportFormatError(f"invalid JSON in the final answer: {exc}") from exc
    if not isinstance(data, dict):
        raise ReportFormatError("final answer is not a JSON object")
    levels = data.get("impact_by_level") if isinstance(data.get("impact_by_level"), dict) else {}
    objectives = []
    for item in data.get("objectives") or []:
        if not isinstance(item, dict):
            continue
        verdict = str(item.get("verdict") or "").strip().lower()
        objectives.append({"objective": str(item.get("objective") or "").strip(),
                           "verdict": verdict if verdict in VERDICTS else "insufficient evidence",
                           "rationale": str(item.get("rationale") or "").strip()})
    confidence = str(data.get("confidence") or "").strip().lower()
    return {
        "project_overview": str(data.get("project_overview") or "").strip(),
        "positive_evidence": str(data.get("positive_evidence") or "").strip(),
        "negative_evidence": str(data.get("negative_evidence") or "").strip(),
        "impact_by_level": {k: str(levels.get(k) or "").strip() for k in ("country", "europe", "world")},
        "objectives": objectives,
        "overall_verdict": str(data.get("overall_verdict") or "").strip(),
        "confidence": confidence if confidence in CONFIDENCES else "low",
    }


def clean_citations(text: str, valid: set[int]) -> tuple[str, list[int]]:
    """Retire les numeros [n] inconnus ; "[2, 7]" devient "[2]" (ou rien). Renvoie (texte, numeros retires)."""
    removed: set[int] = set()

    def repl(m: re.Match) -> str:
        refs = [int(n) for n in re.findall(r"\d+", m.group(1))]
        removed.update(n for n in refs if n not in valid)
        return "".join(f"[{n}]" for n in refs if n in valid)

    return _CITE.sub(repl, text or ""), sorted(removed)


def _cell(text: str) -> str:
    return " ".join((text or "").split()).replace("|", "\\|")


def _cited(texts: list[str]) -> set[int]:
    return {int(n) for t in texts for group in _CITE.findall(t) for n in re.findall(r"\d+", group)}


def render_report(meta: dict[str, Any], report: dict[str, Any], sources: list[dict[str, Any]],
                  run: dict[str, Any]) -> str:
    valid = {int(s["ref"]) for s in sources if s.get("ref") is not None}
    removed: set[int] = set()

    def clean(text: str) -> str:
        cleaned, gone = clean_citations(text, valid)
        removed.update(gone)
        return cleaned

    overview = clean(report["project_overview"])
    positive = clean(report["positive_evidence"])
    negative = clean(report["negative_evidence"])
    levels = {k: clean(v) for k, v in report["impact_by_level"].items()}
    objectives = [{**o, "rationale": clean(o["rationale"])} for o in report["objectives"]]
    verdict = clean(report["overall_verdict"])
    cited = _cited([overview, positive, negative, verdict, *levels.values(), *(o["rationale"] for o in objectives)])
    used = sorted((s for s in sources if s.get("ref") in cited), key=lambda s: s["ref"])

    scope = " — ".join(p for p in (" ".join(str(v) for v in (meta.get("programme"), meta.get("year")) if v),
                                   ", ".join(meta.get("countries") or []), meta.get("policy_area")) if p)
    table = ["| Objective | Verdict | Rationale |", "|---|---|---|"]
    table += [f"| {_cell(o['objective'])} | {o['verdict']} | {_cell(o['rationale'])} |" for o in objectives]
    lines = []
    for s in used:
        parts = [s.get("title") or s.get("url"), s.get("section"), s.get("url")]
        lines.append(f"- [{s['ref']}] " + " — ".join(str(p) for p in parts if p))
    body = "\n\n".join([
        f"# Impact analysis — {meta.get('title') or meta.get('project_id')}",
        f"{scope}  \nGenerated {run['generated']} · {run['model']} · run {run['run_id']}",
        f"**Overall verdict:** {verdict} (confidence: {report['confidence']})",
        f"## 1. Project overview\n\n{overview}",
        f"## 2. Evidence of positive impact\n\n{positive}",
        f"## 3. Evidence of negative impact or limitations\n\n{negative}",
        "## 4. Impact by level\n\n" + "\n\n".join(f"### {label}\n\n{levels[key] or 'No evidence found.'}"
                                                  for key, label in (("country", "Country"), ("europe", "Europe"),
                                                                     ("world", "World"))),
        "## 5. Assessment against objectives\n\n" + "\n".join(table),
        "## 6. Sources\n\n" + ("\n".join(lines) if lines else "No sources cited."),
    ])
    front = {"project_id": meta.get("project_id"), "title": meta.get("title"), "countries": meta.get("countries"),
             "year": meta.get("year"), "generated": run["generated"], "run_id": run["run_id"],
             "model": run["model"], "cost_usd": run.get("cost_usd"), "duration_s": run.get("duration_s"),
             "overall_verdict": verdict, "confidence": report["confidence"], "sources": len(used),
             "removed_citations": sorted(removed)}
    return render(front, body)


def write_report(impact_dir: str | Path, project_id: str, markdown: str) -> Path:
    if not valid_project_id(project_id):
        raise ValueError(f"invalid project id: {project_id!r}")
    path = Path(impact_dir) / f"{project_id}.md"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(markdown, encoding="utf-8")
    return path


def read_report(impact_dir: str | Path, project_id: str) -> tuple[dict[str, Any], str] | None:
    if not valid_project_id(project_id):
        return None
    path = Path(impact_dir) / f"{project_id}.md"
    if not path.exists():
        return None
    return parse_front_matter(path.read_text(encoding="utf-8"))


def list_reports(impact_dir: str | Path) -> list[dict[str, Any]]:
    """Front matter des rapports existants, le plus recent d'abord."""
    out = []
    for path in Path(impact_dir).glob("*.md"):
        try:
            out.append(parse_front_matter(path.read_text(encoding="utf-8"))[0])
        except (ValueError, OSError):
            continue
    return sorted(out, key=lambda f: (str(f.get("generated") or ""), str(f.get("run_id") or "")), reverse=True)
```

Note : `clean_citations("A [3][9] and [2, 7] and [12].", {2, 3})` doit donner exactement
`"A [3] and [2] and ."` — `[3][9]` sont deux correspondances distinctes du motif.

- [ ] **Step 4 : vérifier** — `pytest -q tests/test_impact_report.py` → PASS.

- [ ] **Step 5 : commit** — `git commit -m "feat(impact): rapport - parsing JSON, citations verifiees, rendu Markdown"`

---

### Task 5 : prompt, pipeline, export YAML

**Files:**
- Create: `src/tsi_agent/impact/prompt.py`, `src/tsi_agent/impact/pipeline.py`
- Modify: `src/tsi_agent/__main__.py`
- Test: `tests/test_impact_pipeline.py`

- [ ] **Step 1 : tests qui échouent**

```python
import pytest
import yaml
from haystack.components.embedders import MockTextEmbedder
from haystack.dataclasses import ChatMessage

from test_chat_agent import ScriptedGenerator, chat_settings
from test_impact_tools import FakeEngine, FakeFetcher
from tsi_agent.components.claude_cli import ClaudeCliChatGenerator
from tsi_agent.impact.pipeline import build_impact_pipeline
from tsi_agent.impact.prompt import system_prompt, user_prompt
from tsi_agent.pipeline import load_pipeline


def impact_pipeline(tmp_path, replies, settings=None, **kw):
    s = settings if settings is not None else chat_settings(tmp_path)  # chat_settings cree le store : une fois
    return build_impact_pipeline(s, chat_generator=ScriptedGenerator(replies),
                                 text_embedder=MockTextEmbedder(model="mock", dimension=8), **kw)


def test_prompt():
    text = system_prompt(max_searches=7)
    for word in ("get_project", "web_search", "fetch_page", "search_deliverables", "7", "JSON",
                 "insufficient evidence", "causal"):
        assert word in text
    assert "{{" not in text and "energy-regulation-academy" in user_prompt("energy-regulation-academy")


def test_agent_runs_tools_with_continuous_numbering(tmp_path):
    engine = FakeEngine([("News", "https://n.eu", "arrears fell")])
    fetcher = FakeFetcher({"https://n.eu": (b"Arrears fell by 40 percent.", "text/plain")})
    pipe = impact_pipeline(tmp_path, [[("get_project", {"doc_id": "energy-regulation-academy"})],
                                      [("web_search", {"query": "energy regulation academy results"})],
                                      [("fetch_page", {"url": "https://n.eu"})], '{"confidence": "low"}'],
                           search_engine=engine, page_fetcher=fetcher)
    out = pipe.run({"agent": {"messages": [ChatMessage.from_user("go")]}})["agent"]
    assert [s["ref"] for s in out["sources"]] == [1, 2]  # page lue = meme numero que le resultat de recherche
    assert out["searches"] == ["energy regulation academy results"]
    assert out["last_message"].text == '{"confidence": "low"}'


def test_yaml_round_trip(tmp_path):
    s = chat_settings(tmp_path)
    pipe = build_impact_pipeline(s, chat_generator=ClaudeCliChatGenerator(model="sonnet"),
                                 text_embedder=MockTextEmbedder(model="mock", dimension=8))
    text = pipe.dumps()
    for name in ("ClaudeWebSearch", "WebSearchTool", "PageReader", "LinkContentFetcher", "ProjectReader",
                 "DeliverableSearch"):
        assert name in text
    agent = load_pipeline(text).get_component("agent")
    assert [t.name for t in agent.tools] == ["get_project", "search_deliverables", "web_search", "fetch_page"]
    web = next(t for t in yaml.safe_load(text)["components"]["agent"]["init_parameters"]["tools"]
               if t["data"]["name"] == "web_search")
    assert web["data"]["component"]["init_parameters"]["max_searches"] == s.impact_max_searches
```

- [ ] **Step 2 : vérifier l'échec** — FAIL.

- [ ] **Step 3 : implémentation**

`src/tsi_agent/impact/prompt.py` :

```python
"""Prompt de l'agent d'analyse d'impact (texte brut, sans Jinja)."""

REPORT_FORMAT = """{
  "project_overview": "Markdown: objectives and actions carried out, with citations [n]",
  "positive_evidence": "Markdown: evidence of positive impact, with citations [n]",
  "negative_evidence": "Markdown: evidence of negative impact, limitations, criticism or missing evidence, with citations [n]",
  "impact_by_level": {"country": "Markdown", "europe": "Markdown", "world": "Markdown"},
  "objectives": [{"objective": "one objective of the project", "verdict": "achieved | partly achieved | not achieved | insufficient evidence", "rationale": "short, with citations [n]"}],
  "overall_verdict": "one or two sentences, with citations [n]",
  "confidence": "high | medium | low"
}"""


def system_prompt(max_searches: int) -> str:
    return f"""You are an impact analyst for the European Commission's DG REFORM. You assess the impact of one \
Technical Support Instrument (TSI) or SRSP project on its country, on Europe and on the world, against the \
project's own objectives.

Method:
1. Call get_project with the project id to read the project: identify its objectives and the actions carried out. \
Use search_deliverables (with project_id) for details from its reports (recommendations, figures, outputs).
2. Search the web with web_search for evidence of impact: adoption and implementation of the reform or strategy \
(laws, decrees, action plans), indicators before and after, evaluations, OECD / World Bank / European Commission \
documents (European Semester country reports, recovery and resilience plans), government sites, press articles, \
criticism. Use precise queries naming the country, the institution and the reform; search in English and, when \
useful, in the country's language. At most {max_searches} searches.
3. Read the most relevant results with fetch_page before relying on them.

Rules:
- Cite every factual statement with the numbers [n] given by the tools, e.g. [3] or [3][5]. Only cite numbers \
returned by the tools; never invent a source, a URL, a figure or an impact.
- Distinguish correlation from causation: a reform supported by the project is rarely its only cause. Say how \
directly the evidence can be attributed to the project (causal link established, plausible, or unclear).
- When evidence is missing or too recent, say so and use the verdict "insufficient evidence". A short, honest \
report is better than a speculative one.
- Write in English, concise and factual.

When you have enough evidence (or the search limit is reached), give your final answer as a single JSON object, \
with no text before or after it, in this format:
{REPORT_FORMAT}"""


def user_prompt(project_id: str) -> str:
    return f"Run the impact analysis of project id `{project_id}`."
```

`src/tsi_agent/impact/pipeline.py` :

```python
"""Pipeline Haystack de l'analyse d'impact : un Agent avec 4 outils (projet, livrables, recherche web, lecture de
page) ; export YAML deepset (ClaudeWebSearch -> SerperDevWebSearch, generator -> OpenAIChatGenerator)."""

from typing import Any

from haystack import Pipeline
from haystack.components.agents import Agent
from haystack.tools import ComponentTool

from tsi_agent.chat.generator import LoggedChatGenerator
from tsi_agent.chat.pipeline import default_text_embedder, open_vector_store
from tsi_agent.chat.tools import DeliverableSearch, ProjectReader
from tsi_agent.components.claude_cli import ClaudeCliChatGenerator
from tsi_agent.config import Settings
from tsi_agent.impact.prompt import system_prompt
from tsi_agent.impact.tools import PageReader, WebSearchTool
from tsi_agent.impact.web_search import ClaudeWebSearch
from tsi_agent.qdrant_local import LocalQdrantEmbeddingRetriever

IMPACT_STATE = {"sources": {"type": list[dict]}, "searches": {"type": list[str]}}
_SOURCES_IO = {"outputs_to_state": {"sources": {"source": "sources"}},
               "inputs_from_state": {"sources": "known_sources"}}


def build_impact_pipeline(settings: Settings, *, chat_generator: Any = None, text_embedder: Any = None,
                          document_store: Any = None, search_engine: Any = None,
                          page_fetcher: Any = None) -> Pipeline:
    store = document_store if document_store is not None else open_vector_store(settings)
    generator = chat_generator if chat_generator is not None else ClaudeCliChatGenerator(
        model=settings.impact_model, timeout_s=settings.impact_llm_timeout_s)
    embedder = text_embedder if text_embedder is not None else default_text_embedder(settings)
    engine = search_engine if search_engine is not None else ClaudeWebSearch(
        model=settings.web_search_model, timeout_s=settings.web_search_timeout_s)
    tools = [
        ComponentTool(ProjectReader(document_store=store, max_chars=settings.impact_project_chars),
                      name="get_project", description="Full text of the project to analyse, by its id.",
                      outputs_to_string={"source": "result"}, **_SOURCES_IO),
        ComponentTool(DeliverableSearch(text_embedder=embedder,
                                        retriever=LocalQdrantEmbeddingRetriever(document_store=store)),
                      name="search_deliverables",
                      description="Search the full text of the project's deliverables (final reports, studies): "
                                  "recommendations, figures, outputs. Returns numbered passages [n].",
                      outputs_to_string={"source": "result"}, **_SOURCES_IO),
        ComponentTool(WebSearchTool(search_engine=engine, max_searches=settings.impact_max_searches),
                      name="web_search",
                      description="Search the web for evidence of the project's impact. Returns numbered results "
                                  "[n] (title, URL, snippet).",
                      outputs_to_string={"source": "result"},
                      outputs_to_state={"sources": {"source": "sources"}, "searches": {"source": "searches"}},
                      inputs_from_state={"sources": "known_sources", "searches": "past_searches"}),
        ComponentTool(PageReader(max_words=settings.impact_page_words, fetcher=page_fetcher),
                      name="fetch_page", description="Read a web page or online document (HTML, PDF) by URL.",
                      outputs_to_string={"source": "result"}, **_SOURCES_IO),
    ]
    agent = Agent(chat_generator=LoggedChatGenerator(chat_generator=generator, pricing=settings.pricing),
                  tools=tools, system_prompt=system_prompt(settings.impact_max_searches), state_schema=IMPACT_STATE,
                  max_agent_steps=settings.impact_max_steps)
    p = Pipeline()
    p.add_component("agent", agent)
    return p
```

`src/tsi_agent/__main__.py` : ajouter
`export.add_argument("--impact", action="store_true", help="exporter le pipeline d'analyse d'impact")` et, dans
la branche `export-yaml`, avant `elif args.chat:` :

```python
            elif args.impact:
                from tsi_agent.impact.pipeline import build_impact_pipeline
                pipe = build_impact_pipeline(settings)
```

- [ ] **Step 4 : vérifier** — `pytest -q tests/test_impact_pipeline.py` puis `pytest -q` → PASS.

- [ ] **Step 5 : commit** — `git commit -m "feat(impact): prompt et pipeline Agent (4 outils), export-yaml --impact"`

---

### Task 6 : catalogue des projets, service d'analyse, tâches en arrière-plan

**Files:**
- Create: `src/tsi_agent/impact/catalog.py`, `src/tsi_agent/impact/service.py`
- Test: `tests/test_impact_service.py`

- [ ] **Step 1 : tests qui échouent**

```python
import json
import shutil
import threading
from pathlib import Path

import pytest

from test_impact_pipeline import impact_pipeline
from test_impact_report import REPORT
from test_impact_tools import FakeEngine, FakeFetcher
from tsi_agent.components.claude_cli import LLMQuotaExceeded
from tsi_agent.impact.catalog import load_catalog, search_catalog
from tsi_agent.impact.service import AnalysisFailed, ImpactJobs, JobRunning, analyse, progress_text

FIX = Path(__file__).parent / "fixtures"
META = {"project_id": "energy-regulation-academy", "title": "Energy Regulation Academy", "countries": [],
        "year": 2023, "programme": "TSI", "policy_area": "green-transition"}


def projects_dir(tmp_path):
    d = tmp_path / "out" / "projects"
    d.mkdir(parents=True)
    shutil.copy(FIX / "vec_detail_sections.md", d / "energy-regulation-academy.md")
    shutil.copy(FIX / "vec_detail_page_text.md", d / "building-capacity-competition-authority-spain.md")
    return d


def test_catalog_search_by_title_country_and_year(tmp_path):
    catalog = load_catalog(projects_dir(tmp_path))
    assert len(catalog) == 2
    assert search_catalog(catalog, "energy academy")[0]["project_id"] == "energy-regulation-academy"
    assert [p["project_id"] for p in search_catalog(catalog, "spain")] == [
        "building-capacity-competition-authority-spain"]
    assert search_catalog(catalog, "")[:1] and search_catalog(catalog, "zzz") == []


def test_progress_text():
    assert progress_text({"action": "tool_call", "target": "web_search", "query": "q1"}) == "Searching the web: q1"
    assert progress_text({"action": "tool_call", "target": "fetch_page", "url": "https://www.oecd.org/x"}) == \
        "Reading: www.oecd.org"
    assert progress_text({"action": "tool_call", "target": "get_project", "doc_id": "p"}) == "Reading project p"
    assert progress_text({"action": "llm_call", "component": "ChatAgent"}) == "Analysing…"
    assert progress_text({"action": "llm_call", "component": "ClaudeWebSearch"}) is None


def run_analysis(tmp_path, final):
    from test_chat_agent import chat_settings
    s = chat_settings(tmp_path).model_copy(update={"impact_dir": str(tmp_path / "impact")})
    pipe = impact_pipeline(tmp_path, [[("get_project", {"doc_id": "energy-regulation-academy"})],
                                      [("web_search", {"query": "q"})], final], settings=s,
                           search_engine=FakeEngine([("News", "https://n.eu", "x")]), page_fetcher=FakeFetcher())
    steps = []
    return analyse(pipe, "energy-regulation-academy", settings=s, project_meta=META, on_step=steps.append), steps, s


def test_analyse_writes_report_and_reports_progress(tmp_path):
    report = dict(REPORT, positive_evidence="Good [2] and invented [8].")
    result, steps, s = run_analysis(tmp_path, json.dumps(report))
    assert "Reading project energy-regulation-academy" in steps and "Searching the web: q" in steps
    text = (tmp_path / "impact" / "energy-regulation-academy.md").read_text(encoding="utf-8")
    assert "Good [2] and invented ." in text and "- [2] News — https://n.eu" in text
    assert 8 in result["front"]["removed_citations"] and result["run_id"]  # sources reelles : [1] et [2]
    assert result["body"].startswith("# Impact analysis — Energy Regulation Academy")


def test_analyse_fails_without_json_or_on_step_limit(tmp_path):
    with pytest.raises(AnalysisFailed):
        run_analysis(tmp_path, "I could not finish.")
    assert not (tmp_path / "impact" / "energy-regulation-academy.md").exists()


def test_jobs_single_run_status_and_errors():
    gate = threading.Event()

    def runner(project_id, on_step):
        on_step("step one")
        gate.wait(5)
        if project_id == "quota":
            raise LLMQuotaExceeded("limit")
        return {"project_id": project_id, "body": "# R", "front": {}}

    jobs = ImpactJobs(runner)
    jobs.start("p1")
    with pytest.raises(JobRunning):
        jobs.start("p2")
    assert jobs.status()["state"] == "running" and jobs.status()["steps"] == ["step one"]
    gate.set()
    jobs.wait(5)
    assert jobs.status()["state"] == "done" and jobs.status()["result"]["body"] == "# R"
    jobs.start("quota")
    jobs.wait(5)
    st = jobs.status()
    assert st["state"] == "error" and "limit" in st["error"].lower()
```

- [ ] **Step 2 : vérifier l'échec** — FAIL.

- [ ] **Step 3 : implémentation**

`src/tsi_agent/impact/catalog.py` :

```python
"""Catalogue des projets pour la page d'analyse d'impact : front matter de output/projects/*.md, recherche simple
par mots (titre, id, pays, annee, programme)."""

import unicodedata
from pathlib import Path
from typing import Any

import yaml

from tsi_agent.countries import COUNTRIES, name_from_iso

FIELDS = ("project_id", "title", "year", "programme", "countries", "policy_area", "detail_page")


def _front_matter(path: Path) -> dict[str, Any] | None:
    """Front matter seul (lecture ligne a ligne jusqu'au second ---) : les fichiers projet peuvent etre longs."""
    lines: list[str] = []
    with path.open(encoding="utf-8") as fh:
        if fh.readline().rstrip("\n") != "---":
            return None
        for line in fh:
            if line.rstrip("\n") == "---":
                return yaml.safe_load("".join(lines)) or {}
            lines.append(line)
    return None


def _fold(text: str) -> str:
    return unicodedata.normalize("NFKD", text).encode("ascii", "ignore").decode().lower()


def load_catalog(projects_dir: str | Path) -> list[dict[str, Any]]:
    catalog = []
    for path in sorted(Path(projects_dir).glob("*.md")):
        try:
            fm = _front_matter(path)
        except (OSError, yaml.YAMLError):
            continue
        if not fm or not fm.get("project_id"):
            continue
        entry = {k: fm.get(k) for k in FIELDS}
        names = [name_from_iso(c) for c in entry.get("countries") or [] if c in COUNTRIES]
        entry["_search"] = _fold(" ".join(str(v) for v in (entry["title"], entry["project_id"], entry["year"],
                                                            entry["programme"], *(entry.get("countries") or []),
                                                            *names) if v))
        catalog.append(entry)
    return catalog


def search_catalog(catalog: list[dict[str, Any]], query: str, limit: int = 20) -> list[dict[str, Any]]:
    """Projets dont le texte de recherche contient tous les mots ; pages detaillees puis plus recents d'abord."""
    words = _fold(query or "").split()
    hits = [p for p in catalog if all(w in p["_search"] for w in words)]
    hits.sort(key=lambda p: (not p.get("detail_page"), -(p.get("year") or 0), str(p.get("title"))))
    return [{k: p[k] for k in FIELDS} for p in hits[:limit]]
```

(vérifier que `name_from_iso` et `COUNTRIES` existent dans `tsi_agent.countries` — ils sont déjà utilisés par
`render.py`.)

`src/tsi_agent/impact/service.py` :

```python
"""Une analyse d'impact = un run journalise : Agent -> JSON -> rapport Markdown enregistre. Les tâches tournent en
arriere-plan, une a la fois ; la progression vient des evenements du journal (ecouteur)."""

import threading
from datetime import date
from typing import Any, Callable
from urllib.parse import urlparse

from haystack import Pipeline
from haystack.dataclasses import ChatMessage, ChatRole

from tsi_agent import runlog
from tsi_agent.chat.service import find_cause, root_cause
from tsi_agent.components.claude_cli import LLMQuotaExceeded
from tsi_agent.config import Settings
from tsi_agent.impact.prompt import user_prompt
from tsi_agent.impact.report import ReportFormatError, parse_report, read_report, render_report, write_report

QUOTA_MESSAGE = "The Claude subscription limit has been reached. Please try again later."


class AnalysisFailed(RuntimeError):
    pass


class JobRunning(RuntimeError):
    pass


def progress_text(record: dict[str, Any]) -> str | None:
    """Libelle de progression d'un evenement du journal ; None si l'evenement n'est pas affiche."""
    action, target = record.get("action"), record.get("target")
    if action == "llm_call":
        return "Analysing…" if record.get("component") == "ChatAgent" else None
    if action != "tool_call":
        return None
    if target == "get_project":
        return f"Reading project {record.get('doc_id')}"
    if target == "search_deliverables":
        return f"Searching deliverables: {record.get('query')}"
    if target == "web_search":
        return f"Searching the web: {record.get('query')}"
    if target == "fetch_page":
        return f"Reading: {urlparse(str(record.get('url') or '')).netloc or record.get('url')}"
    return None


def analyse(pipeline: Pipeline, project_id: str, *, settings: Settings, project_meta: dict[str, Any],
            on_step: Callable[[str], None] | None = None) -> dict[str, Any]:
    with runlog.open_run(settings.logs_dir) as run:
        if on_step is not None:
            def listener(record: dict[str, Any]) -> None:
                text = progress_text(record)
                if text:
                    on_step(text)
            run.listeners.append(listener)
        runlog.event("ImpactService", "impact_start", project_id)
        out = pipeline.run({"agent": {"messages": [ChatMessage.from_user(user_prompt(project_id))]}})["agent"]
        last = out.get("last_message")
        if last is None or not last.is_from(ChatRole.ASSISTANT) or last.tool_calls or not last.text:
            runlog.event("ImpactService", "impact_end", project_id, "step_limit", exit_reason=out.get("exit_reason"))
            raise AnalysisFailed("The analysis did not complete (step limit reached). Details in logs/runs/.")
        try:
            report = parse_report(last.text)
        except ReportFormatError as exc:
            runlog.event("ImpactService", "impact_end", project_id, "bad_format", error=str(exc),
                         answer=last.text[:2000])
            raise AnalysisFailed(f"The agent's final answer could not be read ({exc}).") from exc
        sources = out.get("sources") or []
        runlog.event("ImpactService", "impact_end", project_id, sources=len(sources),
                     searches=len(out.get("searches") or []))
        run.extra.update({"mode": "impact", "project_id": project_id})
    summary = run.final_summary
    markdown = render_report(project_meta, report, sources, {
        "run_id": run.run_id, "model": settings.impact_model, "cost_usd": summary["cost_usd"],
        "duration_s": summary["duration_s"], "generated": date.today().isoformat()})
    write_report(settings.impact_dir, project_id, markdown)
    front, body = read_report(settings.impact_dir, project_id)
    if front.get("removed_citations"):
        runlog.event("ImpactService", "alert", project_id,
                     message=f"citations inconnues retirees : {front['removed_citations']}")
    return {"project_id": project_id, "front": front, "body": body, "run_id": run.run_id,
            "cost_usd": summary["cost_usd"], "duration_s": summary["duration_s"]}


class ImpactJobs:
    """Une analyse a la fois, en arriere-plan ; `status()` donne l'etat courant (pour la page)."""

    def __init__(self, runner: Callable[[str, Callable[[str], None]], dict[str, Any]]):
        self.runner = runner
        self._lock = threading.Lock()
        self._thread: threading.Thread | None = None
        self._state: dict[str, Any] = {"state": "idle", "project_id": None, "steps": [], "result": None,
                                       "error": None}

    def start(self, project_id: str) -> None:
        with self._lock:
            if self._state["state"] == "running":
                raise JobRunning(self._state["project_id"])
            self._state = {"state": "running", "project_id": project_id, "steps": [], "result": None, "error": None}
            self._thread = threading.Thread(target=self._run, args=(project_id,), daemon=True)
            self._thread.start()

    def _step(self, text: str) -> None:
        with self._lock:
            self._state["steps"].append(text)

    def _run(self, project_id: str) -> None:
        try:
            result = self.runner(project_id, self._step)
            update = {"state": "done", "result": result}
        except Exception as exc:  # message lisible pour la page ; detail complet dans les logs
            if find_cause(exc, LLMQuotaExceeded):
                message = QUOTA_MESSAGE
            elif find_cause(exc, AnalysisFailed):
                message = str(find_cause(exc, AnalysisFailed))
            else:
                import logging
                logging.getLogger("tsi_agent").exception("Erreur lors d'une analyse d'impact")
                message = f"Internal error ({type(root_cause(exc)).__name__}). Details in logs/runs/."
            update = {"state": "error", "error": message}
        with self._lock:
            self._state.update(update)

    def status(self) -> dict[str, Any]:
        with self._lock:
            return {**self._state, "steps": list(self._state["steps"])}

    def wait(self, timeout: float | None = None) -> None:
        if self._thread is not None:
            self._thread.join(timeout)
```

- [ ] **Step 4 : vérifier** — `pytest -q tests/test_impact_service.py` → PASS ; puis `pytest -q`.

- [ ] **Step 5 : commit** — `git commit -m "feat(impact): catalogue des projets, service d'analyse journalise, taches en arriere-plan"`

---

### Task 7 : API web et onglet « Impact analysis »

**Files:**
- Modify: `src/tsi_agent/chat/web.py`, `src/tsi_agent/chat/static/index.html`
- Test: `tests/test_impact_web.py`

- [ ] **Step 1 : tests qui échouent**

```python
import json

from haystack.components.embedders import MockTextEmbedder

from test_chat_agent import POLICY_AREAS, ScriptedGenerator, chat_settings
from test_impact_report import REPORT
from test_impact_service import projects_dir
from test_impact_tools import FakeEngine, FakeFetcher
from tsi_agent.chat.pipeline import build_chat_pipeline
from tsi_agent.chat.web import create_app
from tsi_agent.impact.catalog import load_catalog
from tsi_agent.impact.pipeline import build_impact_pipeline


def client(tmp_path, replies):
    s = chat_settings(tmp_path).model_copy(update={"impact_dir": str(tmp_path / "impact")})
    emb = MockTextEmbedder(model="mock", dimension=8)
    chat = build_chat_pipeline(s, POLICY_AREAS, chat_generator=ScriptedGenerator([]), text_embedder=emb)
    impact = build_impact_pipeline(s, chat_generator=ScriptedGenerator(replies), text_embedder=emb,
                                   search_engine=FakeEngine([("News", "https://n.eu", "x")]),
                                   page_fetcher=FakeFetcher())
    app = create_app(s, pipeline=chat, impact_pipeline=impact, catalog=load_catalog(projects_dir(tmp_path)))
    return app, app.test_client()


def test_projects_search_and_page_tabs(tmp_path):
    app, c = client(tmp_path, [])
    res = c.get("/api/projects?q=energy")
    assert res.status_code == 200 and res.get_json()["projects"][0]["project_id"] == "energy-regulation-academy"
    page = c.get("/").data
    assert b"Impact analysis" in page and b"/api/impact" in page


def test_impact_job_lifecycle(tmp_path):
    app, c = client(tmp_path, [[("web_search", {"query": "q"})], json.dumps(REPORT)])
    assert c.post("/api/impact", json={"project_id": "unknown"}).status_code == 404
    assert c.post("/api/impact", json={"project_id": "energy-regulation-academy"}).status_code == 202
    app.config["IMPACT_JOBS"].wait(10)
    st = c.get("/api/impact/job").get_json()
    assert st["state"] == "done" and st["result"]["body"].startswith("# Impact analysis")
    assert "Searching the web: q" in st["steps"]
    reports = c.get("/api/impact/reports").get_json()["reports"]
    assert reports[0]["project_id"] == "energy-regulation-academy"
    one = c.get("/api/impact/reports/energy-regulation-academy").get_json()
    assert one["front"]["confidence"] == "medium" and one["body"].startswith("# Impact analysis")
    assert c.get("/api/impact/reports/nope").status_code == 404
    assert c.get("/api/impact/reports/..%2Fsecret").status_code == 404


def test_impact_conflict_when_running(tmp_path):
    app, c = client(tmp_path, [])
    jobs = app.config["IMPACT_JOBS"]
    jobs._state["state"] = "running"
    assert c.post("/api/impact", json={"project_id": "energy-regulation-academy"}).status_code == 409
```

- [ ] **Step 2 : vérifier l'échec** — FAIL.

- [ ] **Step 3 : implémentation de l'API** — `src/tsi_agent/chat/web.py` :

`create_app(settings, pipeline=None, policy_areas=None, impact_pipeline=None, catalog=None)` :

- **production** (`pipeline is None`) : construire **un seul** embedder (`default_text_embedder(settings)`) et le
  passer à `build_chat_pipeline(..., text_embedder=emb)` et, si `impact_pipeline is None`, à
  `build_impact_pipeline(settings, text_embedder=emb)` (le modèle e5 n'est chargé qu'une fois) ; `catalog` par
  défaut : `load_catalog(Path(settings.output_dir) / "projects")` ;
- **tests** (`pipeline` fourni) : rien n'est construit ; l'analyse d'impact n'est active que si `impact_pipeline`
  est fourni, `catalog` vaut `catalog or []` (les tests de chat existants ne doivent ni charger e5 ni lire
  `output/`) ;
- `impact_pipeline.warm_up()` s'il existe ; index `by_id = {p["project_id"]: p for p in catalog}` ;
- si `impact_pipeline is None`, les routes `/api/impact*` (POST et GET job) répondent 503
  `{"error": "impact analysis not available"}` ; `/api/projects` et les rapports restent servis.

```python
    def run_impact(project_id: str, on_step: Any) -> dict[str, Any]:
        return analyse(impact_pipeline, project_id, settings=settings, project_meta=by_id[project_id],
                       on_step=on_step)

    jobs = ImpactJobs(run_impact)
    app.config["IMPACT_JOBS"] = jobs

    @app.get("/api/projects")
    def projects() -> Any:
        return jsonify({"projects": search_catalog(catalog, request.args.get("q", ""))})

    @app.post("/api/impact")
    def impact_start() -> Any:
        project_id = str((request.get_json(silent=True) or {}).get("project_id") or "")
        if project_id not in by_id:
            return jsonify({"error": "unknown project"}), 404
        try:
            jobs.start(project_id)
        except JobRunning:
            return jsonify({"error": "an analysis is already running"}), 409
        return jsonify(jobs.status()), 202

    @app.get("/api/impact/job")
    def impact_job() -> Any:
        return jsonify(jobs.status())

    @app.get("/api/impact/reports")
    def impact_reports() -> Any:
        return jsonify({"reports": list_reports(settings.impact_dir)})

    @app.get("/api/impact/reports/<project_id>")
    def impact_report(project_id: str) -> Any:
        found = read_report(settings.impact_dir, project_id)
        if found is None:
            return jsonify({"error": "no report"}), 404
        return jsonify({"front": found[0], "body": found[1]})
```

Les dates du front matter (`generated`) peuvent être relues par YAML comme `datetime.date` : `jsonify` les
sérialise ; vérifier dans le test et, au besoin, convertir en `str` dans `list_reports` / `read_report`
(`generated: str(...)`).

- [ ] **Step 4 : implémentation de la page** — `src/tsi_agent/chat/static/index.html` :

1. En-tête : deux boutons-onglets `Chat` et `Impact analysis` (classe `tab`, `aria-selected`), le bouton
   « New conversation » reste visible uniquement sur l'onglet Chat. Le formulaire fixe du chat (`#form`) est
   masqué (`hidden`) sur l'onglet Impact.
2. Nouveau `<section id="impact" hidden>` dans `main` :
   - champ `<input id="pq" placeholder="Search a project by title, country or year…">` (appel
     `/api/projects?q=` avec un délai de 250 ms après la frappe) et liste `#presults` de boutons
     `titre — programme année — pays` ;
   - carte du projet choisi `#pselected` avec un bouton **Run impact analysis** (désactivé si une analyse tourne) ;
   - zone `#prun` : animation `.loading` (mêmes points que le chat) + compteur + liste des étapes reçues ;
   - zone `#preport` : rapport rendu avec la fonction `render()` existante ;
   - liste `#ppast` « Previous analyses » (`/api/impact/reports`) : titre, date, verdict, confiance ; un clic
     charge `/api/impact/reports/<id>`.
3. Liens des citations : avant `render(body, sources)`, extraire les sources de la section `## 6. Sources` avec
   `/^- \[(\d+)\] .*?(https?:\/\/\S+)\s*$/gm` → `[{ref, url}]`, pour que `[n]` devienne un lien.
4. Lancement : `POST /api/impact` ; puis interrogation de `/api/impact/job` toutes les 2 s jusqu'à `done` ou
   `error` (afficher l'erreur dans une carte `.msg.error`), puis rafraîchir « Previous analyses ». Au chargement de
   l'onglet, si `state === "running"`, reprendre l'affichage de la progression.
5. Tout le texte venant de l'API passe par `esc()` (titres de projets, étapes, erreurs) ; le rapport passe par
   `render()` (qui échappe d'abord).
6. Styles : réutiliser les variables existantes ; `.tab[aria-selected=true]` souligné de `--accent` ; liste de
   résultats en cartes compactes, 100 % de largeur sur mobile.

- [ ] **Step 5 : vérifier** — `pytest -q` → PASS. Vérification manuelle rapide : lancer
  `.venv/Scripts/python.exe -m tsi_agent chat --no-browser --port 8766` (le serveur du port 8765 garde la base
  Qdrant : l'arrêter d'abord, ou faire ce test après la tâche 8), ouvrir la page, chercher un projet, vérifier
  l'affichage sans lancer d'analyse.

- [ ] **Step 6 : commit** — `git commit -m "feat(impact): API et onglet Impact analysis de la page web"`

---

### Task 8 : test d'intégration et essais réels

**Files:**
- Test: `tests/test_impact_web_search.py` (ajout)

- [ ] **Step 1 : test d'intégration (marqué)**

```python
@pytest.mark.integration
def test_real_web_search_via_cli():
    out = ClaudeWebSearch(model="haiku", top_k=5).run(query="Technical Support Instrument Croatia health reform")
    assert out["documents"], "no results"
    assert all(d.meta["link"].startswith("http") for d in out["documents"])
```

Run: `.venv/Scripts/python.exe -m pytest -q -m integration tests/test_impact_web_search.py`
Expected: PASS (environ 15-30 s). Si le CLI refuse l'outil WebSearch avec `--json-schema`, analyser la sortie
brute (`claude -p --output-format json --tools WebSearch --allowedTools WebSearch --json-schema ...`) et ajuster
`_tools_args` avant de continuer.

- [ ] **Step 2 : commit** — `git commit -m "test(impact): recherche web reelle via le CLI (integration)"`

- [ ] **Step 3 : essais réels (contrôleur, pas de sous-agent)** — arrêter le serveur du port 8765, relancer
  `logs\chat_qdrant.ps1`, puis lancer par l'API 3 analyses, l'une après l'autre :
  1. un projet riche en résultats publiés (ex. un projet e-santé ou santé en Croatie / Slovénie, TSI 2021) ;
  2. un projet récent (TSI 2024) — attendu : beaucoup de « insufficient evidence » ;
  3. un projet multi-pays.
  Pour chacun : durée, coût, nombre de recherches, citations retirées, ouverture de 3 liens au hasard pour vérifier
  qu'ils disent bien ce que le rapport leur attribue. Consigner le bilan dans le message final à l'utilisateur.
```
