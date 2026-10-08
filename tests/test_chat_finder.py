import json

import pytest
from haystack import Document, component
from haystack.components.embedders import MockTextEmbedder

from conftest import make_vector_store
from test_chat_store_tools import ENERGY, THIN, add_deliverables
from tsi_agent import runlog
from tsi_agent.chat.tools import ProjectFinder
from tsi_agent.qdrant_local import LocalQdrantEmbeddingRetriever, open_store


@component
class FixedEmbedder:
    """Embedder de requete de test : toujours le meme vecteur, scores cosinus previsibles."""

    def __init__(self):
        self.texts: list[str] = []

    @component.output_types(embedding=list[float])
    def run(self, text: str):
        self.texts.append(text)
        return {"embedding": [1.0, 0, 0, 0, 0, 0, 0, 0]}


@component
class SpyRetriever:
    """Retriever de test : delegue au vrai retriever et note les filtres et top_k de chaque appel."""

    def __init__(self, store):
        self.inner = LocalQdrantEmbeddingRetriever(document_store=store)
        self.calls: list[tuple] = []

    @component.output_types(documents=list[Document])
    def run(self, query_embedding: list[float], filters: dict | None = None, top_k: int | None = None):
        self.calls.append((filters, top_k))
        return self.inner.run(query_embedding=query_embedding, filters=filters, top_k=top_k)


def vec(*head: float) -> list[float]:
    return list(head) + [0.0] * (8 - len(head))


def project(doc_id, title, emb, n=0, countries=("HR",), year=2022, programme="TSI", listing_titles=(),
            policy_area="revenue-administration", content=None):
    return Document(id=f"projects/{doc_id}#{n}", embedding=emb,
                    content=content or f"{title} — passage {n} about the project.",
                    meta={"doc_id": doc_id, "doc_type": "project", "title": title, "countries": list(countries),
                          "year": year, "programme": programme, "listing_titles": list(listing_titles),
                          "policy_area": policy_area, "detail_page": True, "section": "Context",
                          "source_url": f"https://example.org/{doc_id}", "chunk_index": n})


def deliverable(pub, projects, emb, n=0, countries=("HR",), year=2022, content="Deliverable page on customs risk."):
    return Document(id=f"deliverables/{pub}#{n}", embedding=emb, content=content,
                    meta={"doc_id": pub, "doc_type": "deliverable", "title": f"Publication {pub}",
                          "projects": list(projects), "countries": list(countries), "year": year,
                          "programme": "TSI", "section": "Report.pdf", "page": 2, "chunk_index": n,
                          "source_url": f"https://example.org/{pub}.pdf"})


# Scores cosinus avec la requete [1, 0, ...] : p1 1.0 (et 0.707), p6 0.8, pub-x 0.994 (p3, p4), p2/p3/p4 0, p5 -1.
@pytest.fixture
def small_store(tmp_path):
    store = open_store(str(tmp_path / "v"), 8)
    store.write_documents([
        project("p1", "Anti-money laundering supervision", vec(1), 0,
                content="The financial intelligence unit\nimproved its risk-based supervision. " + "x" * 300),
        project("p1", "Anti-money laundering supervision", vec(0.5, 0.5), 1),
        project("p2", "Tax administration support", vec(0, 1), listing_titles=["Support to the FIU of Croatia"]),
        project("p3", "Customs modernisation", vec(0, 0, 1), countries=("SI",)),
        project("p4", "Pension reform", vec(0, 0, 0, 1), countries=("SI", "HR"), year=2019, programme="SRSP"),
        project("p5", "Unrelated taxation topic", vec(-1)),
        project("p6", "Digital courts", vec(0.8, 0.6)),
        deliverable("pub-x", ["p3", "p4"], vec(0.9, 0.1), countries=("SI", "HR")),
    ])
    return store


def finder(store, retriever=None, **kw):
    return ProjectFinder(text_embedder=FixedEmbedder(),
                         retriever=retriever or LocalQdrantEmbeddingRetriever(document_store=store),
                         document_store=store, **kw)


def lines_of(out):
    return out["result"].splitlines()


def ids_of(out):
    return [s["doc_id"] for s in out["sources"]]


def line_for(out, pid):
    return next(l for l in lines_of(out) if f"({pid})" in l)


def test_pool_is_top_projects_plus_top_deliverable_projects(small_store):
    topic = "anti-money laundering and counter-terrorist financing"
    out = finder(small_store, project_pool=2, deliverable_pool=1).run(topic=topic)
    # projets : p1 (1.0), p6 (0.8) ; livrables : pub-x -> p3 et p4 a egalite, p3 garde (doc_id)
    assert ids_of(out) == ["p1", "p3", "p6"]
    assert lines_of(out)[0] == (
        f"Candidate projects for '{topic}': 3 (0 title matches, 2 from project texts, "
        "1 from deliverables). These are candidates, not answers: keep only those whose title or snippet is "
        "really about the topic.")
    assert "via: projects —" in line_for(out, "p1") and "via: deliverables —" in line_for(out, "p3")


def test_separate_searches_restricted_by_doc_type(small_store):
    spy = SpyRetriever(small_store)
    tool = finder(small_store, retriever=spy, project_chunks=7, deliverable_chunks=5)
    tool.run(topic="x", year_from=2020)
    (pf, pk), (df, dk) = spy.calls
    assert pk == 7 and dk == 5
    assert {"field": "meta.doc_type", "operator": "==", "value": "project"} in pf["conditions"]
    assert {"field": "meta.doc_type", "operator": "==", "value": "deliverable"} in df["conditions"]
    assert {"field": "meta.year", "operator": ">=", "value": 2020} in df["conditions"]
    assert tool.text_embedder.texts == ["x"]  # un seul embedding pour les deux recherches


def test_big_reports_do_not_crowd_out_projects(small_store):
    small_store.write_documents([deliverable("pub-big", ["p4"], vec(0.99, 0.01), n=i) for i in range(1, 6)])
    out = finder(small_store, project_chunks=2, project_pool=2, deliverable_pool=1).run(topic="x")
    # les 5 morceaux du gros rapport (0.9999) ne prennent pas la place des morceaux projet de p1 et p6
    assert ids_of(out) == ["p1", "p4", "p6"]
    assert "via: deliverables —" in line_for(out, "p4")


def test_no_score_threshold(small_store):
    out = finder(small_store).run(topic="x")
    assert set(ids_of(out)) == {"p1", "p2", "p3", "p4", "p5", "p6"}  # p5 (score -1) reste candidat
    assert ids_of(out)[-1] == "p5"


def test_keywords_match_titles_and_listing_titles_whole_words(small_store):
    out = finder(small_store, project_pool=0, deliverable_pool=0).run(
        topic="anti-money laundering and terrorist financing", keywords=["FIU", "tax"])
    # p2 : "FIU" dans un titre de listing ; "tax" ne doit pas trouver "taxation" (p5)
    assert ids_of(out) == ["p2"]
    assert ": 1 (1 title matches, 0 from project texts, 0 from deliverables)" in lines_of(out)[0]
    assert "via: title —" in line_for(out, "p2")


def test_short_topic_is_also_a_title_keyword(small_store):
    short = finder(small_store, project_pool=0, deliverable_pool=0).run(topic="pension reform")
    assert ids_of(short) == ["p4"]
    long = finder(small_store, project_pool=0, deliverable_pool=0).run(
        topic="reform of the pension system in member states")
    assert long["sources"] == [] and ": 0 (0 title matches" in lines_of(long)[0]


def test_ordering_title_matches_first_then_best_score(small_store):
    out = finder(small_store).run(topic="x", keywords=["customs", "pension"])
    # p3 et p4 par titre (0.994 tous deux, puis titre), puis p1 1.0, p6 0.8, p2 0, p5 -1
    assert ids_of(out) == ["p3", "p4", "p1", "p6", "p2", "p5"]
    assert "via: title, projects, deliverables —" in line_for(out, "p3")
    assert lines_of(out)[0].startswith("Candidate projects for 'x': 6 (2 title matches, 6 from project texts, "
                                       "2 from deliverables).")


def test_candidate_line_format_and_snippet(small_store):
    out = finder(small_store).run(topic="x")
    p1 = line_for(out, "p1")
    assert p1.startswith("[1] Anti-money laundering supervision (p1) — TSI 2022 — HR — via: projects — "
                         "The financial intelligence unit improved its risk-based supervision. xxx")
    assert p1.endswith(" — https://example.org/p1")
    snippet = p1.split(" — ")[4]
    assert len(snippet) == 140
    # snippet du meilleur morceau, livrable compris
    assert "Deliverable page on customs risk." in line_for(out, "p3")


def test_title_only_candidate_gets_first_chunk_snippet(small_store):
    out = finder(small_store, project_chunks=1, deliverable_chunks=1, project_pool=1, deliverable_pool=0).run(
        topic="x", keywords=["FIU"])
    assert ids_of(out) == ["p2", "p1"]
    assert "Tax administration support — passage 0 about the project." in line_for(out, "p2")
    assert out["sources"][0]["score"] is None


def test_no_breakdown_lines(small_store):
    result = finder(small_store).run(topic="x")["result"]
    assert "By country" not in result and "By year" not in result


def test_filters(small_store):
    assert set(ids_of(finder(small_store).run(topic="x", countries=["si"]))) == {"p3", "p4"}
    assert ids_of(finder(small_store).run(topic="x", programme="SRSP")) == ["p4"]
    assert ids_of(finder(small_store).run(topic="x", year_to=2020)) == ["p4"]
    assert set(ids_of(finder(small_store).run(topic="x", keywords=["FIU"], year_from=2020, countries=["HR"]))) == {
        "p1", "p2", "p5", "p6"}
    assert finder(small_store).run(topic="x", policy_area="other")["sources"] == []
    # un livrable lie a un projet hors filtres (p3, SI) ne l'ajoute pas
    hr = finder(small_store, project_pool=0).run(topic="x", countries=["HR"])
    assert ids_of(hr) == ["p4"]


def test_sources_continue_numbering_and_carry_urls(small_store):
    out = finder(small_store).run(topic="x", known_sources=[{"ref": 1}] * 3)
    assert [s["ref"] for s in out["sources"]] == [4, 5, 6, 7, 8, 9]
    first = out["sources"][0]
    assert set(first) >= {"ref", "doc_id", "title", "url", "section", "ai_generated", "score"}
    assert first["url"] == "https://example.org/p1" and first["ai_generated"] is False and first["score"] == 1.0
    assert lines_of(out)[1].startswith("[4] ")


def test_max_candidates_caps_listed_lines(small_store):
    out = finder(small_store, max_candidates=2).run(topic="x")
    lines = lines_of(out)
    assert ": 6 (" in lines[0] and lines[1] == "Showing the first 2 of 6 candidates."
    assert ids_of(out) == ["p1", "p3"] and len(lines) == 4


def test_logs_tool_call(small_store, tmp_path):
    with runlog.open_run(tmp_path / "l", "r1") as run:
        finder(small_store, project_pool=2, deliverable_pool=1).run(topic="x", keywords=["FIU"])
    assert run.final_summary["counts"]["tool_call:ok"] == 1
    events = [json.loads(l) for l in run.path.read_text(encoding="utf-8").splitlines()]
    ev = next(e for e in events if e.get("target") == "find_projects")
    assert (ev["topic"], ev["keywords"], ev["candidates"], ev["title_matches"], ev["from_projects"],
            ev["from_deliverables"]) == ("x", ["FIU"], 4, 1, 2, 1)
    assert "duration_ms" in ev


def test_on_the_indexed_test_layer(tmp_path):
    store = open_store(str(make_vector_store(tmp_path)), 8)
    add_deliverables(store)
    out = finder(store, project_pool=0, deliverable_pool=0).run(topic="x", keywords=["property tax"])
    assert ids_of(out) == [THIN]
    wide = finder(store).run(topic="x", keywords=["property tax"])
    assert ids_of(wide)[0] == THIN and ENERGY in ids_of(wide)
    assert "country-croatia" not in wide["result"] and "pub-energy" not in ids_of(wide)
    # titre de listing indexe en metadonnees
    listing = finder(store, project_pool=0, deliverable_pool=0).run(
        topic="x", keywords=["Monitoring System for National Plan"])
    assert ids_of(listing) == ["governance-and-monitoring-system-national-plan-digital-skills"]


def test_serialization_round_trip(small_store, monkeypatch):
    monkeypatch.setenv("HAYSTACK_DESERIALIZATION_ALLOWLIST", "tsi_agent")
    tool = ProjectFinder(text_embedder=MockTextEmbedder(model="mock", dimension=8),
                         retriever=LocalQdrantEmbeddingRetriever(document_store=small_store), document_store=small_store,
                         project_pool=5, project_chunks=50, deliverable_pool=3, deliverable_chunks=40, max_candidates=10)
    data = tool.to_dict()
    params = data["init_parameters"]
    assert (params["project_pool"], params["project_chunks"], params["deliverable_pool"],
            params["deliverable_chunks"], params["max_candidates"]) == (5, 50, 3, 40, 10)
    back = ProjectFinder.from_dict(data)
    assert (back.project_pool, back.deliverable_chunks, back.max_candidates) == (5, 40, 10)
    assert ids_of(back.run(topic="x", keywords=["FIU"]))[0] == "p2"
