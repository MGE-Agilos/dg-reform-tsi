import shutil

from haystack.components.embedders import MockDocumentEmbedder, MockTextEmbedder

from conftest import FIXTURES, make_vector_store
from tsi_agent.chunking import chunk_file
from tsi_agent.eval.bench import VARIANTS, build_bm25_store, run_variants
from tsi_agent.eval.chunk_sizes import chunk_corpus, evaluate_chunk_sizes, split_params
from tsi_agent.eval.questions import _record
from tsi_agent.eval.sampling import load_all_documents
from tsi_agent.qdrant_local import open_store


def mirror_question(index, doc):
    """Question identique au texte vectorise du morceau : l'embedder factice la place au rang 1."""
    q = _record(index, doc, f"{doc.meta['context_header']}\n{doc.content}")
    return q


def test_run_variants_on_small_store(tmp_path):
    store = open_store(str(make_vector_store(tmp_path)), 8)
    docs = load_all_documents(store)
    project = next(d for d in docs if d.meta["doc_type"] == "project" and d.meta.get("countries"))
    programme = next(d for d in docs if d.meta["doc_type"] == "programme")
    questions = [mirror_question(0, project), mirror_question(1, programme),
                 {**_record(2, project, "zzz unrelated words"), "id": "q-miss"}]
    lines: list[str] = []
    rows = run_variants(questions, store, MockTextEmbedder(dimension=8), top_k=5, candidates=10,
                        progress=lines.append)
    assert VARIANTS == ("A", "B", "C", "D")
    assert len(rows) == 3 and lines
    first, prog = rows[0], rows[1]
    for v in VARIANTS:
        assert first[f"{v}_chunk"] == 1 and first[f"{v}_file"] == 1 and first[f"{v}_project"] == 1
        assert f"{v}_chunk" in rows[2] and f"{v}_file" in rows[2]
    assert first["B_filtered"] is True and prog["B_filtered"] is False
    assert prog["A_chunk"] == 1 and prog["A_project"] is None  # programme : pas de projet attendu
    assert len(first["A_top3"]) == 3 and first["A_top3"][0]["title"] == project.meta["title"]
    assert all(r[f"{v}_chunk"] is None or r[f"{v}_chunk"] <= 5 for r in rows for v in VARIANTS)


def test_bm25_store_indexes_header_and_content(tmp_path):
    store = open_store(str(make_vector_store(tmp_path)), 8)
    docs = load_all_documents(store)
    bm25 = build_bm25_store(docs)
    assert bm25.count_documents() == len(docs)
    target = docs[0]
    found = bm25.bm25_retrieval(query=target.content, top_k=3)
    assert found[0].id == target.id and target.meta["context_header"] in found[0].content
    # meme cle de morceau que dans Qdrant
    rows = run_variants([mirror_question(0, target)], store, MockTextEmbedder(dimension=8), bm25_store=bm25)
    assert rows[0]["C_chunk"] == 1


def make_corpus(tmp_path):
    root = tmp_path / "output"
    (root / "projects").mkdir(parents=True)
    (root / "programme").mkdir()
    (root / "deliverables").mkdir()
    for name, rel in [("vec_detail_sections.md", "projects/energy-regulation-academy.md"),
                      ("vec_detail_long_section.md", "projects/governance-digital-skills.md"),
                      ("vec_programme.md", "programme/country-croatia.md"),
                      ("vec_deliverable.md", "deliverables/greece.md")]:
        shutil.copy(FIXTURES / name, root / rel)
    (root / "projects" / "broken.md").write_text("no front matter", encoding="utf-8")
    return root


def test_split_params():
    assert split_params(250) == {"threshold": 300, "window": 250, "overlap": 40}
    assert split_params(150) == {"threshold": 200, "window": 150, "overlap": 24}
    assert split_params(400) == {"threshold": 450, "window": 400, "overlap": 64}


def test_chunk_corpus_only_projects_and_programme(tmp_path):
    root = make_corpus(tmp_path)
    small, large = chunk_corpus(root, 150), chunk_corpus(root, 400)
    assert {d.meta["doc_type"] for d in small} == {"project", "programme"}
    assert len(small) > len(large)


def test_evaluate_chunk_sizes(tmp_path):
    root = make_corpus(tmp_path)
    rel = "projects/governance-digital-skills.md"
    chunks = chunk_file(rel, (root / rel).read_text(encoding="utf-8"))
    target = chunks[2]
    deliv = chunk_file("deliverables/greece.md", (root / "deliverables/greece.md").read_text(encoding="utf-8"))[0]
    questions = [mirror_question(0, target), _record(1, chunks[0], "unrelated"), _record(2, deliv, "deliv?")]
    lines: list[str] = []
    results = evaluate_chunk_sizes(
        questions, root, MockDocumentEmbedder(dimension=8, meta_fields_to_embed=["context_header"]),
        MockTextEmbedder(dimension=8), windows=(150, 250), batch_size=4, progress=lines.append)
    assert [r["window"] for r in results] == [150, 250]
    current = results[1]
    assert current["questions"] == 2 and current["overlap"] == 40 and current["chunks"] > 0
    # le morceau d'origine existe tel quel a 250 mots : fichier et passage trouves pour la 1re question
    assert current["file@8"] >= 0.5 and current["passage@8"] >= 0.5
    assert set(results[0]) >= {"window", "overlap", "threshold", "chunks", "questions",
                                "file@8", "file@24", "passage@8", "passage@24"}
    assert any("150" in line for line in lines)
