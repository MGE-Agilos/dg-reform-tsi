import json

import pytest
from haystack import Document

from conftest import FakeChatGenerator, make_vector_store
from tsi_agent.eval import metrics
from tsi_agent.eval.questions import (QUESTION_SCHEMA, generate_questions, load_questions, question_lang,
                                      save_questions)
from tsi_agent.eval.sampling import load_all_documents, sample_chunks, stratum
from tsi_agent.qdrant_local import open_store


def words(n: int, tag: str = "w") -> str:
    return " ".join(f"{tag}{i}" for i in range(n))


def doc(i: int, doc_type: str = "project", ai: bool = False, n_words: int = 80, **meta) -> Document:
    base = {"doc_type": doc_type, "ai_generated": ai, "doc_id": f"{doc_type}-{i // 3}", "chunk_index": i % 3,
            "source_file": f"{doc_type}s/{doc_type}-{i // 3}.md", "title": f"Title {i}", "section": "Context",
            "countries": ["HR"], "projects": [], "context_header": f"Title {i} — TSI 2022 — Croatia"}
    return Document(id=f"{doc_type}s/{doc_type}-{i // 3}#{i % 3}", content=words(n_words, f"t{i}x"),
                    meta={**base, **meta})


def corpus() -> list[Document]:
    docs = [doc(i) for i in range(100)]
    docs += [doc(i, ai=True) for i in range(100, 120)]
    docs += [doc(i, "deliverable", projects=[f"project-{i % 7}"]) for i in range(80)]
    docs += [doc(i, "programme") for i in range(10)]
    docs += [doc(i, n_words=20) for i in range(200, 260)]  # trop courts
    return docs


# --- tirage ---

def test_stratum():
    assert stratum(doc(1)) == "project"
    assert stratum(doc(1, ai=True)) == "other"
    assert stratum(doc(1, "programme")) == "other"
    assert stratum(doc(1, "deliverable")) == "deliverable"


def test_sample_is_stratified_and_skips_short_chunks():
    sample = sample_chunks(corpus(), n_project=30, n_deliverable=20, n_other=10)
    assert [stratum(d) for d in sample] == ["project"] * 30 + ["deliverable"] * 20 + ["other"] * 10
    assert all(len(d.content.split()) >= 60 for d in sample)
    assert len({d.id for d in sample}) == 60


def test_sample_is_reproducible_and_independent_of_input_order():
    docs = corpus()
    a = [d.id for d in sample_chunks(docs, seed=7)]
    assert a == [d.id for d in sample_chunks(list(reversed(docs)), seed=7)]
    assert a != [d.id for d in sample_chunks(docs, seed=8)]


def test_short_stratum_takes_what_exists():
    sample = sample_chunks(corpus(), n_project=5, n_deliverable=5, n_other=500)
    assert sum(stratum(d) == "other" for d in sample) == 30


def test_load_all_documents_from_qdrant_keeps_ids_without_embeddings(tmp_path):
    store = open_store(str(make_vector_store(tmp_path)), 8)
    docs = load_all_documents(store)
    assert len(docs) == store.count_documents() > 5
    # qdrant-haystack convertit l'id en UUID pour le point, mais garde l'id d'origine dans le payload
    assert all(d.id == f"{d.meta['source_file'].removesuffix('.md')}#{d.meta['chunk_index']}" for d in docs)
    assert all(d.embedding is None for d in docs)


# --- questions ---

def batch_reply(refs_questions: list[tuple[int, str]]) -> str:
    return json.dumps({"questions": [{"chunk_ref": r, "question": q} for r, q in refs_questions]})


def test_question_lang_alternates():
    assert [question_lang(i) for i in range(4)] == ["fr", "en", "fr", "en"]


def test_generate_questions_maps_refs_and_languages():
    chunks = [doc(i) for i in range(12)]
    gen = FakeChatGenerator(replies=[
        batch_reply([(i + 1, f"q{i}?") for i in range(10)]),
        batch_reply([(2, "q11?"), (1, "q10?"), (9, "hors lot")]),
    ])
    out = generate_questions(chunks, gen, batch_size=10)
    assert len(gen.calls) == 2
    assert [q["question"] for q in out] == [f"q{i}?" for i in range(12)]
    assert [q["lang"] for q in out[:4]] == ["fr", "en", "fr", "en"]
    q = out[11]
    assert q["chunk_id"] == chunks[11].id and q["doc_id"] == chunks[11].meta["doc_id"]
    assert q["doc_type"] == "project" and q["stratum"] == "project" and q["countries"] == ["HR"]
    assert q["key_text"] == " ".join(chunks[11].content.split()[:30]) and q["words"] == 80
    assert q["id"] == "q012"
    prompt = gen.calls[0][-1].text
    assert "Passage 1 — write the question in French" in prompt
    assert "Passage 2 — write the question in English" in prompt
    assert chunks[0].content in prompt and "Title 0 — TSI 2022 — Croatia" in prompt


def test_generate_questions_retries_then_skips_a_failing_batch():
    chunks = [doc(i) for i in range(3)]
    gen = FakeChatGenerator(replies=["not json", "still not json"])
    assert generate_questions(chunks, gen, batch_size=10) == []
    assert len(gen.calls) == 2


def test_question_schema_and_jsonl_roundtrip(tmp_path):
    assert QUESTION_SCHEMA["properties"]["questions"]["items"]["required"] == ["chunk_ref", "question"]
    qs = [{"id": "q001", "question": "Où ?", "countries": ["HR"]}]
    path = tmp_path / "eval" / "q.jsonl"
    save_questions(qs, path)
    assert load_questions(path) == qs


# --- metriques ---

def d(key: str, doc_id: str, projects=None) -> Document:
    return Document(id=key, content=key, meta={"doc_id": doc_id, "projects": projects or []})


def test_ranks():
    found = [d("a#0", "a"), d("b#1", "b"), d("a#2", "a"), d("c#0", "c")]
    assert metrics.chunk_rank(found, "a#2") == 3
    assert metrics.chunk_rank(found, "z#0") is None
    assert metrics.file_rank(found, "b") == 2
    assert metrics.file_rank(found, "z") is None
    deliv = [d("x#0", "pub", ["p1"]), d("p2#0", "p2")]
    assert metrics.project_rank(deliv, ["p2"]) == 2
    assert metrics.project_rank(deliv, ["p1"]) == 1
    assert metrics.project_rank(deliv, []) is None


def test_chunk_key_falls_back_on_source_file():
    doc_ = Document(id="0f1e", content="x", meta={"source_file": "projects/a.md", "chunk_index": 4})
    assert metrics.chunk_key(doc_) == "projects/a#4"
    assert metrics.chunk_key(Document(id="k", content="x")) == "k"


def test_recall_and_mrr():
    ranks = [1, 3, None, 10]
    assert metrics.recall_at(ranks, 1) == 0.25
    assert metrics.recall_at(ranks, 3) == 0.5
    assert metrics.recall_at(ranks, 24) == 0.75
    assert metrics.mrr(ranks) == pytest.approx((1 + 1 / 3 + 0 + 0.1) / 4)
    assert metrics.recall_at([], 3) is None and metrics.mrr([]) is None
    s = metrics.summarize(ranks)
    assert set(s) == {"n", "mrr", *(f"r@{k}" for k in metrics.KS)} and s["n"] == 4


def test_group_by():
    rows = [{"lang": "fr"}, {"lang": "en"}, {"lang": "fr"}]
    assert {k: len(v) for k, v in metrics.group_by(rows, "lang").items()} == {"fr": 2, "en": 1}


def test_rrf_fuse():
    a = [d("x", "1"), d("y", "1"), d("z", "1")]
    b = [d("z", "1"), d("w", "1")]
    fused = metrics.rrf_fuse([a, b], k=60)
    # z : 1/63 + 1/61 > x : 1/61 > y : 1/62 = w : 1/62 (egalite : ordre de premiere apparition)
    assert [x.id for x in fused] == ["z", "x", "y", "w"]


def test_diversify():
    docs = [d("a#0", "a"), d("a#1", "a"), d("a#2", "a"), d("b#0", "b"), d("a#3", "a"), d("c#0", "c")]
    assert [x.id for x in metrics.diversify(docs, max_per_file=2, top_k=4)] == ["a#0", "a#1", "b#0", "c#0"]
    assert [x.id for x in metrics.diversify(docs, max_per_file=1, top_k=2)] == ["a#0", "b#0"]


def test_text_overlap():
    key = "The reform, of the Croatian tax-system: (2022)"
    assert metrics.text_overlap_hit("... THE reform of the croatian tax system 2022 and more", key)
    assert not metrics.text_overlap_hit("nothing related here at all", key)
    assert metrics.overlap_share("reform croatian", "reform croatian tax system") == 0.5
    assert metrics.text_overlap_hit("reform croatian", "reform croatian tax system", threshold=0.5)
    assert not metrics.text_overlap_hit("anything", "")
