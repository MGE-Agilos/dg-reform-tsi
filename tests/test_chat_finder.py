import pytest
from haystack import Document, component

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


def deliverable(pub, projects, emb, countries=("HR",), year=2022, content="Deliverable page on customs risk."):
    return Document(id=f"deliverables/{pub}#0", embedding=emb, content=content,
                    meta={"doc_id": pub, "doc_type": "deliverable", "title": f"Publication {pub}",
                          "projects": list(projects), "countries": list(countries), "year": year,
                          "programme": "TSI", "section": "Report.pdf", "page": 2,
                          "source_url": f"https://example.org/{pub}.pdf"})


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
        deliverable("pub-x", ["p3", "p4"], vec(0.9, 0.1), countries=("SI", "HR")),
    ])
    return store


def finder(store, **kw):
    kw.setdefault("min_score", 0.5)
    return ProjectFinder(text_embedder=FixedEmbedder(), retriever=LocalQdrantEmbeddingRetriever(document_store=store),
                         document_store=store, **kw)


def lines_of(out):
    return out["result"].splitlines()


def test_aggregates_chunks_by_project_and_maps_deliverables(small_store):
    out = finder(small_store).run(topic="anti-money laundering and counter-terrorist financing")
    lines = lines_of(out)
    assert lines[0] == "Found 3 distinct projects (0 by title keywords, 3 by semantic match >= 0.5)."
    ids = [s["doc_id"] for s in out["sources"]]
    assert ids[0] == "p1" and set(ids) == {"p1", "p3", "p4"}  # p1 une seule fois malgre deux morceaux
    p1 = next(l for l in lines if "(p1)" in l)
    assert p1.startswith("[1] Anti-money laundering supervision (p1) — TSI 2022 — HR — match: semantic 1.0")
    # meilleur passage, sans retour a la ligne, 160 caracteres
    assert "The financial intelligence unit improved its risk-based supervision." in p1
    snippet = p1.split(" — ")[-2] if p1.endswith("https://example.org/p1") else p1.split(" — ")[-1]
    assert len(snippet) <= 160
    p3 = next(l for l in lines if "(p3)" in l)
    assert "match: semantic 0.99" in p3 and "Deliverable page on customs risk." in p3


def test_min_score_threshold(small_store):
    out = finder(small_store, min_score=0.995).run(topic="aml")
    assert [s["doc_id"] for s in out["sources"]] == ["p1"]
    assert lines_of(out)[0] == "Found 1 distinct projects (0 by title keywords, 1 by semantic match >= 0.995)."
    everything = finder(small_store, min_score=-2).run(topic="aml")
    assert {s["doc_id"] for s in everything["sources"]} == {"p1", "p2", "p3", "p4", "p5"}


def test_keywords_match_titles_and_listing_titles_whole_words(small_store):
    out = finder(small_store).run(topic="anti-money laundering and terrorist financing", keywords=["FIU", "tax"])
    ids = [s["doc_id"] for s in out["sources"]]
    # p2 : "FIU" dans un titre de listing ; "tax" ne doit pas trouver "taxation" (p5)
    assert ids[0] == "p2" and "p5" not in ids
    assert lines_of(out)[0] == "Found 4 distinct projects (1 by title keywords, 3 by semantic match >= 0.5)."
    p2 = next(l for l in lines_of(out) if "(p2)" in l)
    assert "match: title" in p2


def test_short_topic_is_also_a_title_keyword(small_store):
    out = finder(small_store, min_score=2).run(topic="pension reform")
    assert [s["doc_id"] for s in out["sources"]] == ["p4"]
    long = finder(small_store, min_score=2).run(topic="reform of the pension system in member states")
    assert long["sources"] == [] and lines_of(long)[0].startswith("Found 0 distinct projects")


def test_title_matches_first_then_score(small_store):
    out = finder(small_store).run(topic="x", keywords=["customs", "pension"])
    ids = [s["doc_id"] for s in out["sources"]]
    assert ids == ["p3", "p4", "p1"]  # p3 et p4 par titre (scores egaux, puis titre), p1 semantique
    p3 = next(l for l in lines_of(out) if "(p3)" in l)
    assert "match: title, semantic 0.99" in p3


def test_filters(small_store):
    assert {s["doc_id"] for s in finder(small_store).run(topic="x", countries=["si"])["sources"]} == {"p3", "p4"}
    assert [s["doc_id"] for s in finder(small_store, min_score=-2).run(topic="x", programme="SRSP")["sources"]] == ["p4"]
    assert {s["doc_id"] for s in finder(small_store, min_score=-2).run(topic="x", year_to=2020)["sources"]} == {"p4"}
    assert {s["doc_id"] for s in finder(small_store).run(topic="x", keywords=["FIU"], year_from=2020,
                                                          countries=["HR"])["sources"]} == {"p1", "p2"}
    assert finder(small_store).run(topic="x", policy_area="other")["sources"] == []


def test_sources_continue_numbering_and_carry_urls(small_store):
    out = finder(small_store).run(topic="x", known_sources=[{"ref": 1}] * 3)
    assert [s["ref"] for s in out["sources"]] == [4, 5, 6]
    first = out["sources"][0]
    assert set(first) >= {"ref", "doc_id", "title", "url", "section", "ai_generated", "score"}
    assert first["url"] == "https://example.org/p1" and first["ai_generated"] is False
    assert any(l.startswith("[4] ") for l in lines_of(out))


def test_breakdowns_and_cap(small_store):
    out = finder(small_store, max_projects=1).run(topic="x", keywords=["FIU"])
    lines = lines_of(out)
    # semantique plafonne a 1 projet (p1) ; p2 toujours inclus par son titre
    assert lines[0] == "Found 2 distinct projects (1 by title keywords, 1 by semantic match >= 0.5)."
    assert lines[1] == "By country: HR 2"
    assert lines[2] == "By year: 2022: 2"
    assert lines[3] == "Showing 1 of 2:"
    assert [s["doc_id"] for s in out["sources"]] == ["p2"]
    wide = lines_of(finder(small_store).run(topic="x"))
    assert wide[1] == "By country: HR 2, SI 2" and wide[2] == "By year: 2022: 2, 2019: 1"


def test_logs_tool_call(small_store, tmp_path):
    with runlog.open_run(tmp_path / "l", "r1") as run:
        finder(small_store).run(topic="x", keywords=["FIU"])
    assert run.final_summary["counts"]["tool_call:ok"] == 1


def test_on_the_indexed_test_layer(tmp_path):
    store = open_store(str(make_vector_store(tmp_path)), 8)
    add_deliverables(store)
    out = finder(store, min_score=-2).run(topic="x", keywords=["property tax"])
    ids = [s["doc_id"] for s in out["sources"]]
    assert ids[0] == THIN and set(ids) == {ENERGY, THIN, "building-capacity-competition-authority-spain",
                                           "governance-and-monitoring-system-national-plan-digital-skills"}
    assert "country-croatia" not in out["result"] and "pub-energy" not in ids
    # titre de listing indexe en metadonnees
    listing = finder(store, min_score=2).run(topic="x", keywords=["Monitoring System for National Plan"])
    assert [s["doc_id"] for s in listing["sources"]] == [
        "governance-and-monitoring-system-national-plan-digital-skills"]


def test_serialization_round_trip(small_store):
    tool = finder(small_store, min_score=0.7, candidates=50, max_projects=10)
    data = tool.to_dict()
    assert data["init_parameters"]["min_score"] == 0.7 and data["init_parameters"]["candidates"] == 50
    assert data["init_parameters"]["max_projects"] == 10
