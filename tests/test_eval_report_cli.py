import csv
import json
import shutil

import pytest
from haystack.components.embedders import MockDocumentEmbedder, MockTextEmbedder

from conftest import FIXTURES, FakeChatGenerator, make_vector_store
from tsi_agent import __main__ as cli
from tsi_agent.config import Settings
from tsi_agent.eval import report, runner
from tsi_agent.eval.questions import load_questions
from tsi_agent.qdrant_local import open_store


def row(i, lang, stratum, a_chunk, a_file, c_chunk=None, **extra):
    r = {"id": f"q{i:03d}", "lang": lang, "stratum": stratum, "doc_type": "project", "chunk_id": f"projects/p{i}#0",
         "doc_id": f"p{i}", "title": f"Projet {i}", "section": "Context", "page": None, "question": f"Question {i} ?",
         "B_filtered": True, "A_top3": [{"doc_id": "x", "title": "Autre", "section": "Intro", "page": 3}]}
    for v in "ABCD":
        r[f"{v}_chunk"], r[f"{v}_file"], r[f"{v}_project"] = a_chunk, a_file, a_file
    r["C_chunk"] = c_chunk if c_chunk is not None else a_chunk
    return {**r, **extra}


ROWS = [row(1, "fr", "project", 1, 1), row(2, "en", "project", None, 2, c_chunk=3),
        row(3, "fr", "deliverable", 12, 5), row(4, "en", "other", 4, 1)]
SETUP = {"date": "2026-10-08", "index_size": 83900, "question_model": "haiku",
         "embedding_model": "intfloat/multilingual-e5-small", "top_k": 24, "candidates": 50}
SIZES = [{"window": 150, "overlap": 24, "threshold": 200, "chunks": 9000, "questions": 2, "file@8": 0.5,
          "file@24": 1.0, "passage@8": 0.5, "passage@24": 0.5},
         {"window": 250, "overlap": 40, "threshold": 300, "chunks": 6300, "questions": 2, "file@8": 1.0,
          "file@24": 1.0, "passage@8": 0.5, "passage@24": 1.0}]


def test_render_report():
    md = report.render_report(ROWS, SETUP, SIZES)
    assert md.startswith("# Banc de recherche")
    assert "2026-10-08" in md and "83900" in md and "haiku" in md
    assert "| A — Sémantique (actuel) | 4 | 25 % | 25 % | 50 % | 50 % | 75 % | 75 % |" in md
    assert "Niveau fichier" in md and "Taille des morceaux" in md and "| 150 | 24 | 9000 |" in md
    assert "Par type de document" in md and "Par langue" in md
    # questions ratees a k=8 (variante A) : q002 (absent) et q003 (rang 12)
    misses = md.split("## Questions ratées")[1].split("## Lecture")[0]
    assert "Question 2 ?" in misses and "Question 3 ?" in misses and "Question 1 ?" not in misses
    assert "Autre" in misses
    lecture = md.split("## Lecture")[1]
    assert "à k=8, le bon morceau est trouvé dans 50 % des cas et le bon fichier dans 100 %" in lecture
    assert "C" in lecture and "+25 points" in lecture
    assert "recommand" not in lecture.lower()


def test_render_report_without_chunk_sizes():
    md = report.render_report(ROWS, SETUP, None)
    assert "non mesurée" in md


def test_write_csv(tmp_path):
    path = tmp_path / "r.csv"
    report.write_csv(ROWS, path)
    with path.open(encoding="utf-8-sig", newline="") as fh:
        rows = list(csv.DictReader(fh))
    assert len(rows) == 4 and rows[1]["A_chunk"] == "" and rows[1]["C_chunk"] == "3"
    assert "Autre" in rows[0]["A_top3"]


# --- commande ---

def eval_setup(tmp_path):
    vdir = make_vector_store(tmp_path)
    root = tmp_path / "corpus"
    (root / "projects").mkdir(parents=True)
    (root / "programme").mkdir()
    shutil.copy(FIXTURES / "vec_detail_sections.md", root / "projects" / "energy-regulation-academy.md")
    shutil.copy(FIXTURES / "vec_detail_long_section.md", root / "projects" / "governance-digital-skills.md")
    settings = Settings(output_dir=str(root), vectors_dir=str(vdir), embedding_dim=8, logs_dir=str(tmp_path / "logs"))
    replies = [json.dumps({"questions": [{"chunk_ref": i, "question": f"Question {i} sur la réforme ?"}
                                         for i in range(1, 11)]})]
    return settings, FakeChatGenerator(replies=replies)


def test_run_eval_end_to_end(tmp_path):
    settings, gen = eval_setup(tmp_path)
    out_dir = tmp_path / "eval"
    kw = dict(out_dir=out_dir, n_project=4, n_deliverable=2, n_other=2, min_words=20,
              store=open_store(settings.vectors_dir, 8), text_embedder=MockTextEmbedder(dimension=8),
              document_embedder=MockDocumentEmbedder(dimension=8, meta_fields_to_embed=["context_header"]),
              generator=gen, windows=(150, 250), today="2026-10-08", progress=None)
    out = runner.run_eval(settings, **kw)
    assert out["report"] == str(out_dir / "retrieval-2026-10-08.md")
    assert out["csv"] == str(out_dir / "retrieval-2026-10-08.csv")
    questions = load_questions(out_dir / "questions.jsonl")
    assert len(questions) == out["questions"] > 0 and len(gen.calls) == 1
    md = (out_dir / "retrieval-2026-10-08.md").read_text(encoding="utf-8")
    assert "| 150 |" in md and "| 250 |" in md
    assert "cost_usd" in out and out["run_id"]
    # deuxieme run : questions reutilisees (aucun appel LLM), tailles de morceaux sautees
    out2 = runner.run_eval(settings, **{**kw, "skip_chunk_sizes": True})
    assert len(gen.calls) == 1 and out2["questions"] == out["questions"]
    assert "non mesurée" in (out_dir / "retrieval-2026-10-08.md").read_text(encoding="utf-8")
    # --regenerate : nouvel appel
    runner.run_eval(settings, **{**kw, "skip_chunk_sizes": True, "regenerate": True})
    assert len(gen.calls) == 2


def test_run_eval_locked_store_is_a_clear_error(tmp_path):
    settings, gen = eval_setup(tmp_path)

    class Locked:
        def check_lock(self):
            raise RuntimeError("couche vectorisee deja ouverte par un autre processus")
    with pytest.raises(RuntimeError, match="serveur du chatbot"):
        runner.run_eval(settings, out_dir=tmp_path / "eval", store=Locked(), generator=gen, progress=None)
    assert not (tmp_path / "eval").exists()


def test_run_eval_missing_layer(tmp_path):
    settings = Settings(vectors_dir=str(tmp_path / "nope"), embedding_dim=8, logs_dir=str(tmp_path / "logs"))
    with pytest.raises(RuntimeError, match="tsi_agent index"):
        runner.run_eval(settings, out_dir=tmp_path / "eval", progress=None)


def test_cli_eval_retrieval(monkeypatch, capsys, tmp_path):
    calls = {}

    def fake(settings, **kw):
        calls.update(kw)
        return {"report": "r.md", "csv": "r.csv", "questions_file": "q.jsonl", "questions": 3, "cost_usd": 0.12,
                "run_id": "x"}
    monkeypatch.setattr(runner, "run_eval", fake)
    assert cli.main(["eval-retrieval", "--regenerate", "--skip-chunk-sizes", "--n-project", "5",
                     "--out-dir", str(tmp_path / "e"), "--questions-file", str(tmp_path / "q.jsonl")]) == 0
    assert calls["regenerate"] and calls["skip_chunk_sizes"] and calls["n_project"] == 5
    assert calls["n_deliverable"] == 60 and calls["n_other"] == 30
    assert calls["out_dir"] == str(tmp_path / "e") and calls["questions_file"] == str(tmp_path / "q.jsonl")
    printed = capsys.readouterr().out
    assert "r.md" in printed and "r.csv" in printed and "0.12" in printed

    def boom(*a, **k):
        raise RuntimeError("kaput")
    monkeypatch.setattr(runner, "run_eval", boom)
    assert cli.main(["eval-retrieval"]) == 2
    assert "kaput" in capsys.readouterr().err
