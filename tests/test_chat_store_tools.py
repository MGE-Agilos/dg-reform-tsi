import pytest
from haystack.components.embedders import MockTextEmbedder
from haystack.components.retrievers.in_memory import InMemoryEmbeddingRetriever

from conftest import make_vector_store
from tsi_agent import runlog, vector_state
from tsi_agent.chat.store import PersistedInMemoryDocumentStore
from tsi_agent.chat.tools import ProjectLister, ProjectReader, ProjectSearch, build_filters
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
