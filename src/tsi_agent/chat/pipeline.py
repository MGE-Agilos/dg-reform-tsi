"""Pipeline Haystack du chatbot : un Agent avec cinq outils sur la couche vectorisee (export YAML deepset)."""

from typing import Any

from haystack import Pipeline
from haystack.components.agents import Agent
from haystack.tools import ComponentTool

from tsi_agent.chat.generator import LoggedChatGenerator
from tsi_agent.chat.prompt import system_prompt
from tsi_agent.chat.tools import DeliverableSearch, ProjectFinder, ProjectLister, ProjectReader, ProjectSearch
from tsi_agent.components.claude_cli import ClaudeCliChatGenerator
from tsi_agent.config import Settings
from tsi_agent.qdrant_local import LocalQdrantEmbeddingRetriever, collection_exists, open_store
from tsi_agent.vector_state import VectorLayerError

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


def open_vector_store(settings: Settings) -> Any:
    """Store Qdrant de la couche vectorisee ; erreur claire si l'index n'a pas encore ete lance."""
    missing = VectorLayerError(f"couche vectorisee absente ({settings.vectors_dir}) : "
                               "lancez d'abord python -m tsi_agent index")
    if not collection_exists(settings.vectors_dir):
        raise missing
    store = open_store(settings.vectors_dir, settings.embedding_dim)
    if store.count_documents() == 0:
        raise missing
    return store


def build_chat_pipeline(settings: Settings, policy_areas: list[str], *, chat_generator: Any = None,
                        text_embedder: Any = None, document_store: Any = None, retriever: Any = None) -> Pipeline:
    store = document_store if document_store is not None else open_vector_store(settings)
    generator = chat_generator if chat_generator is not None else ClaudeCliChatGenerator(
        model=settings.chat_model, timeout_s=settings.llm_timeout_s)
    embedder = text_embedder if text_embedder is not None else default_text_embedder(settings)
    retriever = retriever if retriever is not None else LocalQdrantEmbeddingRetriever(document_store=store)
    tools = [
        ComponentTool(ProjectSearch(text_embedder=embedder, retriever=retriever),
                      name="search_projects",
                      description="Semantic search in TSI/SRSP project texts; returns numbered passages to cite.",
                      outputs_to_string={"source": "result"}, **_SOURCES_IO),
        ComponentTool(ProjectFinder(text_embedder=embedder, retriever=retriever, document_store=store,
                                    min_score=settings.finder_min_score, candidates=settings.finder_candidates,
                                    max_projects=settings.finder_max_projects),
                      name="find_projects",
                      description="Find the distinct projects on a free topic (semantic relevance threshold plus "
                                  "keywords in titles), with counts by country and year; returns numbered "
                                  "projects [n] to cite.",
                      outputs_to_string={"source": "result"}, **_SOURCES_IO),
        ComponentTool(DeliverableSearch(text_embedder=embedder, retriever=retriever),
                      name="search_deliverables",
                      description="Search the full text of the projects' deliverables (final reports, "
                                  "presentations): methods, figures, recommendations, detailed results. Returns "
                                  "numbered passages [n] with document and page.",
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
