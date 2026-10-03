import shutil

import pytest
import yaml
from haystack.components.embedders import MockDocumentEmbedder

from conftest import FIXTURES
from tsi_agent.config import Settings
from tsi_agent.index_pipeline import build_index_pipeline, embedder_id
from tsi_agent.pipeline import load_pipeline
from tsi_agent.runner import run_index
from tsi_agent.vector_state import load_state


def settings_for(tmp_path) -> Settings:
    out = tmp_path / "output"
    (out / "projects").mkdir(parents=True)
    (out / "programme").mkdir()
    shutil.copy(FIXTURES / "vec_thin.md", out / "projects" / "thin.md")
    shutil.copy(FIXTURES / "vec_detail_sections.md", out / "projects" / "detail.md")
    return Settings(output_dir=str(out), vectors_dir=str(out / "vectors"), embedding_dim=8, logs_dir=str(tmp_path / "logs"))


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


def test_run_index_summary(tmp_path):
    s = settings_for(tmp_path)
    summary = run_index(s, embedder=mock(), run_id="i1")
    assert summary["status"] == "ok" and summary["mode"] == "index" and summary["full"] is True
    assert summary["stats"]["files_indexed"] == 2 and summary["skipped"] == []
    assert summary["counts"]["embed:ok"] == 1
    again = run_index(s, embedder=mock(), run_id="i2")
    assert again["full"] is False and again["stats"]["files_indexed"] == 0


@pytest.mark.integration
def test_real_model_finds_project_from_french_question(tmp_path):
    pytest.importorskip("haystack_integrations.components.embedders.sentence_transformers")
    from haystack_integrations.components.embedders.sentence_transformers import SentenceTransformersTextEmbedder

    from tsi_agent.index_pipeline import default_embedder
    from tsi_agent.qdrant_local import LocalQdrantEmbeddingRetriever, open_store

    s = settings_for(tmp_path)
    s.embedding_dim = 384  # dimension du vrai modele e5-small
    shutil.copy(FIXTURES / "vec_detail_long_section.md", tmp_path / "output" / "projects" / "skills.md")
    run_index(s, embedder=default_embedder(s), run_id="it1")
    retriever = LocalQdrantEmbeddingRetriever(document_store=open_store(s.vectors_dir, s.embedding_dim), top_k=3)
    query = SentenceTransformersTextEmbedder(model=s.embedding_model, prefix="query: ", normalize_embeddings=True)
    vec = query.run(text="formation des régulateurs de l'électricité")["embedding"]
    results = retriever.run(query_embedding=vec)["documents"]
    if results[0].meta["doc_id"] != "energy-regulation-academy":
        lines = "\n".join(f"{i + 1}. {r.meta['doc_id']} (score={r.score:.4f})" for i, r in enumerate(results))
        pytest.fail(f"top-1 n'est pas energy-regulation-academy ; top-3 :\n{lines}")
    assert results[0].meta["doc_id"] == "energy-regulation-academy"
