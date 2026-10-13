from pathlib import Path

import pytest
from haystack import Document
from haystack.components.embedders import MockDocumentEmbedder, MockTextEmbedder

from conftest import make_vector_store
from tsi_agent import runlog
from tsi_agent.chat.pipeline import build_chat_pipeline, open_vector_store
from tsi_agent.chat.tools import CsrSearch, DeliverableSearch, ProjectLister, ProjectReader, ProjectSearch, build_filters
from tsi_agent.config import Settings
from tsi_agent.qdrant_local import LocalQdrantEmbeddingRetriever, open_store
from tsi_agent.vector_state import VectorLayerError


def test_open_vector_store_reads_the_indexed_layer(tmp_path):
    vdir = make_vector_store(tmp_path)
    store = open_vector_store(Settings(vectors_dir=str(vdir), embedding_dim=8))
    assert store.count_documents() > 5


def test_missing_layer_is_a_clear_error(tmp_path):
    with pytest.raises(VectorLayerError, match="tsi_agent index"):
        open_vector_store(Settings(vectors_dir=str(tmp_path / "nope"), embedding_dim=8))
    assert not (tmp_path / "nope").exists()  # rien n'est cree


def test_empty_layer_is_a_clear_error(tmp_path):
    open_store(str(tmp_path), 8).count_documents()  # cree la collection, vide
    with pytest.raises(VectorLayerError, match="tsi_agent index"):
        open_vector_store(Settings(vectors_dir=str(tmp_path), embedding_dim=8))


ENERGY = "energy-regulation-academy"
THIN = "srsp-2016-technical-support-for-the-property-tax-reform"

DELIVERABLES = [
    # (publication, projets lies, pays, annee, document, page, texte)
    ("pub-energy", ["energy-regulation-academy"], ["GR"], 2026, "Final report.pdf", 3,
     "The survey covered 120 regulators and the response rate reached 80 percent."),
    ("pub-energy", ["energy-regulation-academy"], ["GR"], 2026, "Final report.pdf", 4,
     "Recommendations: harmonise the training curriculum across national regulators."),
    ("pub-energy", ["energy-regulation-academy"], ["GR"], 2026, "Slides.pptx", 1, "Overview of the academy."),
    ("pub-tax", ["srsp-2016-technical-support-for-the-property-tax-reform"], ["HR"], 2016, "Tax study.pdf", 2,
     "The cadastre covers 2 million parcels."),
]


def add_deliverables(store):
    docs = []
    for n, (pub, projects, countries, year, section, page, text) in enumerate(DELIVERABLES):
        docs.append(Document(id=f"deliverables/{pub}#{n}", content=text, meta={
            "doc_id": pub, "doc_type": "deliverable", "title": f"Publication {pub}", "section": section,
            "page": page, "source_url": f"https://example.org/{pub}.pdf", "projects": projects,
            "countries": countries, "year": year, "programme": "TSI" if year >= 2021 else "SRSP",
            "policy_area": None, "ai_generated": False, "chunk_index": n}))
    embedded = MockDocumentEmbedder(model="mock", dimension=8).run(documents=docs)["documents"]
    store.write_documents(embedded)


@pytest.fixture
def store(tmp_path):
    s = open_store(str(make_vector_store(tmp_path)), 8)
    add_deliverables(s)
    return s


def search_tool(store, top_k=50):
    return ProjectSearch(text_embedder=MockTextEmbedder(model="mock", dimension=8),
                         retriever=LocalQdrantEmbeddingRetriever(document_store=store), top_k=top_k)


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
    assert set(first) >= {"ref", "doc_id", "title", "url", "section", "ai_generated", "score"}


def test_search_portable_filters_and_programme_docs(store):
    out = search_tool(store).run(query="x", programme="SRSP")
    assert {s["doc_id"] for s in out["sources"]} <= {THIN, "building-capacity-competition-authority-spain"}
    assert "country-croatia" not in {s["doc_id"] for s in search_tool(store).run(query="x")["sources"]}
    assert "country-croatia" in {s["doc_id"] for s in
                                 search_tool(store).run(query="x", include_programme=True)["sources"]}


def test_search_matches_country_factsheet_by_id(store):
    # country-croatia n'a pas de champ "countries" en meta : le pays vient de son doc_id.
    out = search_tool(store).run(query="x", countries=["HR"], include_programme=True)
    assert "country-croatia" in {s["doc_id"] for s in out["sources"]}
    out_fr = search_tool(store).run(query="x", countries=["FR"], include_programme=True)
    assert "country-croatia" not in {s["doc_id"] for s in out_fr["sources"]}


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


def test_lister_breakdown_by_country_and_policy_area(store):
    out = ProjectLister(document_store=store).run(limit=1)["result"]
    lines = out.splitlines()
    # tous les projets correspondants, pas seulement ceux affiches ; tri par nombre puis code
    others = ["AT", "BE", "BG", "CY", "CZ", "DE", "DK", "EE", "FI", "FR", "HR", "HU", "IE", "IT", "LT", "LU", "LV",
              "MT", "NL", "PL", "PT", "RO", "SE", "SI", "SK"]
    assert lines[2] == "By country: ES 3, GR 2, " + ", ".join(f"{c} 1" for c in others)
    assert lines[3] == ("By policy area: competitiveness 1, green-transition 1, "
                        "labour-market-and-social-protection 1, none 1")
    assert lines[1].startswith("By year:") and lines[4] == "Showing 1 of 4:"
    filtered = ProjectLister(document_store=store).run(policy_area="competitiveness")["result"]
    assert "By country: ES 1" in filtered and "By policy area" not in filtered
    assert "none" not in ProjectLister(document_store=store).run(countries=["ES"])["result"].split("By policy")[0]


def test_lister_counts_projects_without_countries_as_none():
    class Store:
        def filter_documents(self, filters=None):
            return [Document(content="x", meta={"doc_id": "a", "doc_type": "project", "countries": [], "year": 2022,
                                                "programme": "TSI", "policy_area": "green-transition"}),
                    Document(content="x", meta={"doc_id": "b", "doc_type": "project", "countries": ["HR"],
                                                "year": 2021, "programme": "TSI", "policy_area": None})]
    out = ProjectLister(document_store=Store()).run()["result"]
    assert "By country: HR 1, none 1" in out
    assert "By policy area: green-transition 1, none 1" in out


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


def deliverable_tool(store, top_k=50):
    return DeliverableSearch(text_embedder=MockTextEmbedder(model="mock", dimension=8),
                             retriever=LocalQdrantEmbeddingRetriever(document_store=store), top_k=top_k)


def test_project_search_never_returns_deliverable_chunks(store):
    ids = {s["doc_id"] for s in search_tool(store).run(query="x", include_programme=True)["sources"]}
    assert ids and not ids & {"pub-energy", "pub-tax"}
    assert {"country-croatia"} <= ids


def test_lister_ignores_deliverable_chunks(store):
    assert ProjectLister(document_store=store).run()["result"].startswith("Total matching projects: 4 ")


def test_deliverable_search_labels_pages_and_numbers_sources(store):
    out = deliverable_tool(store).run(query="survey", known_sources=[{"ref": 1}] * 2)
    assert {s["doc_id"] for s in out["sources"]} == {"pub-energy", "pub-tax"}
    assert [s["ref"] for s in out["sources"]] == [3, 4, 5, 6]
    assert out["result"].startswith("[3] Publication ")
    first = out["sources"][0]
    assert set(first) >= {"ref", "doc_id", "title", "url", "section", "ai_generated", "score"}
    assert first["ai_generated"] is False and ", p. " in first["section"]
    block = next(b for b in out["result"].split("\n\n") if "survey covered" in b)
    assert block.startswith("[") and block.endswith("response rate reached 80 percent.")
    assert ("Publication pub-energy — Final report.pdf, p. 3 — TSI 2026 — GR\n"
            "URL: https://example.org/pub-energy.pdf\n") in block


def test_deliverable_search_filters(store):
    tool = deliverable_tool(store)
    assert {s["doc_id"] for s in tool.run(query="x", publication_id="pub-tax")["sources"]} == {"pub-tax"}
    assert {s["doc_id"] for s in tool.run(query="x", project_id=ENERGY)["sources"]} == {"pub-energy"}
    assert {s["doc_id"] for s in tool.run(query="x", countries=["hr"])["sources"]} == {"pub-tax"}
    assert {s["doc_id"] for s in tool.run(query="x", year_from=2020)["sources"]} == {"pub-energy"}
    assert {s["doc_id"] for s in tool.run(query="x", programme="SRSP")["sources"]} == {"pub-tax"}
    empty = tool.run(query="x", countries=["FR"])
    assert empty["sources"] == [] and "No matching" in empty["result"]
    assert len(deliverable_tool(store, top_k=2).run(query="x")["sources"]) == 2


def test_deliverable_search_logs_tool_call(store, tmp_path):
    with runlog.open_run(tmp_path / "l", "r1") as run:
        deliverable_tool(store).run(query="x")
    assert run.final_summary["counts"]["tool_call:ok"] == 1


def test_pipeline_has_five_tools_and_deliverable_tool_runs(tmp_path):
    vdir = make_vector_store(tmp_path)
    add_deliverables(open_store(str(vdir), 8))
    s = Settings(vectors_dir=str(vdir), embedding_dim=8, logs_dir=str(tmp_path / "logs"))
    pipe = build_chat_pipeline(s, [], chat_generator=_gen(),
                               text_embedder=MockTextEmbedder(model="mock", dimension=8))
    tools = {t.name: t for t in pipe.get_component("agent").tools}
    assert list(tools) == ["search_projects", "find_projects", "search_deliverables", "search_csrs", "list_projects",
                           "get_project"]
    out = tools["search_deliverables"].invoke(query="survey", publication_id="pub-tax")
    assert out["sources"][0]["doc_id"] == "pub-tax"


def test_pipeline_wires_finder_settings(tmp_path):
    vdir = make_vector_store(tmp_path)
    s = Settings(vectors_dir=str(vdir), embedding_dim=8, logs_dir=str(tmp_path / "logs"), finder_project_pool=0,
                 finder_project_chunks=40, finder_deliverable_pool=0, finder_deliverable_chunks=30,
                 finder_max_candidates=3)
    pipe = build_chat_pipeline(s, [], chat_generator=_gen(),
                               text_embedder=MockTextEmbedder(model="mock", dimension=8))
    tool = next(t for t in pipe.get_component("agent").tools if t.name == "find_projects")
    finder = tool._component
    assert (finder.project_pool, finder.project_chunks, finder.deliverable_pool, finder.deliverable_chunks,
            finder.max_candidates) == (0, 40, 0, 30, 3)
    out = tool.invoke(topic="support to energy regulators across member states", keywords=["property tax"],
                      known_sources=[{"ref": 1}])
    assert [(s["ref"], s["doc_id"]) for s in out["sources"]] == [(2, THIN)]
    d = Settings()
    assert (d.finder_project_pool, d.finder_project_chunks, d.finder_deliverable_pool, d.finder_deliverable_chunks,
            d.finder_max_candidates) == (60, 400, 30, 300, 150)
    assert not hasattr(d, "finder_min_score")


def test_settings_yaml_carries_finder_settings():
    import yaml
    from tsi_agent.config import load_settings
    path = Path(__file__).resolve().parent.parent / "config" / "settings.yaml"
    raw = yaml.safe_load(path.read_text(encoding="utf-8"))
    assert not {"finder_min_score", "finder_candidates", "finder_max_projects"} & set(raw)
    s = load_settings(path)
    assert (s.finder_project_pool, s.finder_project_chunks, s.finder_deliverable_pool, s.finder_deliverable_chunks,
            s.finder_max_candidates) == (60, 400, 30, 300, 150)


def _gen():
    from test_chat_agent import ScriptedGenerator
    return ScriptedGenerator([])


def test_deliverable_search_on_chunks_built_from_a_real_full_text(tmp_path):
    from conftest import FIXTURES
    from tsi_agent.chunking import chunk_file

    pid = "financing-sustainability-greece-public-policy-instruments"
    chunks = chunk_file("deliverables/greece.md", (FIXTURES / "vec_deliverable.md").read_text(encoding="utf-8"))
    store = open_store(str(tmp_path), 8)
    store.write_documents(MockDocumentEmbedder(model="mock", dimension=8).run(documents=chunks)["documents"])
    tool = deliverable_tool(store)
    out = tool.run(query="factsheet", project_id=pid, countries=["gr"], year_from=2022, programme="TSI",
                   publication_id=pid)
    assert len(out["sources"]) == 6 and {s["doc_id"] for s in out["sources"]} == {pid}
    assert {s["section"] for s in out["sources"]} >= {"Factsheet, p. 3", "Project public brief, p. 1"}
    assert out["result"].startswith("[1] Financing sustainability in Greece")
    assert "TSI 2022 — GR\nURL: https://reforms-investments.ec.europa.eu/document/download/" in out["result"]
    assert tool.run(query="x", project_id="other")["sources"] == []
    assert tool.run(query="x", countries=["FR"])["sources"] == []


def test_build_filters_pushes_list_fields():
    f = build_filters(doc_type="deliverable", countries=["hr", " si"], project_id="p1")
    assert f["conditions"][1:] == [
        {"field": "meta.countries", "operator": "in", "value": ["HR", "SI"]},
        {"field": "meta.projects", "operator": "in", "value": ["p1"]}]


def test_country_and_project_filters_are_pushed_into_the_store(tmp_path):
    """Les documents recherches sont hors des 100 premiers candidats par similarite : seul le filtre du store les trouve."""
    store = open_store(str(tmp_path), 8)
    emb = MockTextEmbedder(model="mock", dimension=8).run(text="q")["embedding"]
    far = [-v for v in emb]
    noise = [Document(id=f"noise#{i}", content="n", embedding=[v * (1 - i * 1e-4) for v in emb], meta={
        "doc_id": f"n{i}", "doc_type": "deliverable", "countries": ["FR"], "projects": ["other"], "year": 2024,
        "section": "s", "page": 1}) for i in range(150)]
    target = Document(id="t#0", content="target", embedding=far, meta={
        "doc_id": "pub-t", "doc_type": "deliverable", "countries": ["HR"], "projects": ["wanted"], "year": 2024,
        "section": "s", "page": 1, "title": "T"})
    store.write_documents(noise + [target])
    tool = deliverable_tool(store, top_k=5)
    assert [s["doc_id"] for s in tool.run(query="q", countries=["HR"])["sources"]] == ["pub-t"]
    assert [s["doc_id"] for s in tool.run(query="q", project_id="wanted")["sources"]] == ["pub-t"]


def test_project_reader_max_chars(tmp_path):
    store = open_store(str(make_vector_store(tmp_path)), 8)
    short = ProjectReader(document_store=store, max_chars=300).run(doc_id="energy-regulation-academy")
    assert len(short["result"]) == 300
    assert ProjectReader(document_store=store).to_dict()["init_parameters"]["max_chars"] == 8000


def test_build_filters_accepts_the_srss_programme():
    filters = build_filters(programme="srss")
    assert {"field": "meta.programme", "operator": "==", "value": "SRSS"} in filters["conditions"]


CSRS = [
    ("csr-FR-2023-1", ["FR"], 2023, "Improve the sustainability of the pension system."),
    ("csr-HR-2019-2", ["HR"], 2019, "Reduce the administrative burden on businesses."),
    ("csr-HR-2024-1", ["HR"], 2024, "Strengthen the anti-money laundering framework."),
]


def csr_store(tmp_path):
    store = open_store(str(tmp_path), 8)
    docs = [Document(id=f"csr/{cid}#0", content=text, meta={
        "doc_id": cid, "doc_type": "csr", "title": cid, "countries": countries, "year": year, "section": "Introduction",
        "source_url": "https://ec.europa.eu/csr", "context_header": f"{cid} — {year}", "ai_generated": False,
        "programme": None, "policy_area": None}) for cid, countries, year, text in CSRS]
    docs.append(Document(id="projects/p#0", content="A project.", meta={
        "doc_id": "p", "doc_type": "project", "countries": ["HR"], "year": 2024, "section": "Introduction"}))
    store.write_documents(MockDocumentEmbedder(model="mock", dimension=8).run(documents=docs)["documents"])
    return store


def test_csr_search_filters_type_country_and_year(tmp_path):
    tool = CsrSearch(text_embedder=MockTextEmbedder(model="mock", dimension=8),
                     retriever=LocalQdrantEmbeddingRetriever(document_store=csr_store(tmp_path)), top_k=10)
    out = tool.run(query="x", countries=["HR"], year_from=2020, known_sources=[{"ref": 1}])
    assert [s["doc_id"] for s in out["sources"]] == ["csr-HR-2024-1"]
    assert out["sources"][0]["ref"] == 2
    assert out["result"].startswith("[2] csr-HR-2024-1 — 2024 — section: Introduction\nCSR id: csr-HR-2024-1\n")
    all_ids = {s["doc_id"] for s in tool.run(query="x")["sources"]}
    assert all_ids == {"csr-FR-2023-1", "csr-HR-2019-2", "csr-HR-2024-1"}


def test_csr_search_empty(tmp_path):
    tool = CsrSearch(text_embedder=MockTextEmbedder(model="mock", dimension=8),
                     retriever=LocalQdrantEmbeddingRetriever(document_store=csr_store(tmp_path)))
    assert tool.run(query="x", countries=["SE"])["result"] == "No matching CSR passages found."


def test_csr_search_keeps_best_passage_per_csr(tmp_path):
    store = csr_store(tmp_path)
    extra = Document(id="csr/csr-HR-2024-1#1", content="Sub-parts of AML.", meta={
        "doc_id": "csr-HR-2024-1", "doc_type": "csr", "title": "csr-HR-2024-1", "countries": ["HR"], "year": 2024,
        "section": "Sub-parts", "source_url": "https://ec.europa.eu/csr", "context_header": "csr-HR-2024-1 — 2024",
        "ai_generated": False, "programme": None, "policy_area": None})
    store.write_documents(MockDocumentEmbedder(model="mock", dimension=8).run(documents=[extra])["documents"])
    tool = CsrSearch(text_embedder=MockTextEmbedder(model="mock", dimension=8),
                     retriever=LocalQdrantEmbeddingRetriever(document_store=store), top_k=10)
    ids = [s["doc_id"] for s in tool.run(query="x", countries=["HR"])["sources"]]
    assert sorted(ids) == ["csr-HR-2019-2", "csr-HR-2024-1"]


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
