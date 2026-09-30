"""Pipeline Haystack du chatbot : un Agent avec trois outils sur la couche vectorisee (export YAML deepset)."""

from pathlib import Path
from typing import Any

from haystack import Pipeline
from haystack.components.agents import Agent
from haystack.components.retrievers.in_memory import InMemoryEmbeddingRetriever
from haystack.tools import ComponentTool

from tsi_agent.chat.generator import LoggedChatGenerator
from tsi_agent.chat.prompt import system_prompt
from tsi_agent.chat.store import PersistedInMemoryDocumentStore
from tsi_agent.chat.tools import ProjectLister, ProjectReader, ProjectSearch
from tsi_agent.components.claude_cli import ClaudeCliChatGenerator
from tsi_agent.config import Settings

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


def build_chat_pipeline(settings: Settings, policy_areas: list[str], *, chat_generator: Any = None,
                        text_embedder: Any = None, document_store: Any = None) -> Pipeline:
    store = document_store if document_store is not None else PersistedInMemoryDocumentStore(
        path=str(Path(settings.vectors_dir) / "store.json"))
    generator = chat_generator if chat_generator is not None else ClaudeCliChatGenerator(
        model=settings.chat_model, timeout_s=settings.llm_timeout_s)
    embedder = text_embedder if text_embedder is not None else default_text_embedder(settings)
    tools = [
        ComponentTool(ProjectSearch(text_embedder=embedder, retriever=InMemoryEmbeddingRetriever(document_store=store)),
                      name="search_projects",
                      description="Semantic search in TSI/SRSP project texts; returns numbered passages to cite.",
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
