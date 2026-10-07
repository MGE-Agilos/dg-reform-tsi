"""Pipeline Haystack de l'analyse d'impact : un Agent avec 4 outils (projet, livrables, recherche web, lecture de
page) ; export YAML deepset (ClaudeWebSearch -> SerperDevWebSearch, generator -> OpenAIChatGenerator)."""

from typing import Any

from haystack import Pipeline
from haystack.components.agents import Agent
from haystack.tools import ComponentTool

from tsi_agent.chat.generator import LoggedChatGenerator
from tsi_agent.chat.pipeline import default_text_embedder, open_vector_store
from tsi_agent.chat.tools import DeliverableSearch, ProjectReader
from tsi_agent.components.claude_cli import ClaudeCliChatGenerator
from tsi_agent.config import Settings
from tsi_agent.impact.prompt import system_prompt
from tsi_agent.impact.tools import PageReader, WebSearchTool
from tsi_agent.impact.web_search import ClaudeWebSearch
from tsi_agent.qdrant_local import LocalQdrantEmbeddingRetriever

IMPACT_STATE = {"sources": {"type": list[dict]}, "searches": {"type": list[str]}}
_SOURCES_IO = {"outputs_to_state": {"sources": {"source": "sources"}},
               "inputs_from_state": {"sources": "known_sources"}}


def build_impact_pipeline(settings: Settings, *, chat_generator: Any = None, text_embedder: Any = None,
                          document_store: Any = None, search_engine: Any = None,
                          page_fetcher: Any = None) -> Pipeline:
    store = document_store if document_store is not None else open_vector_store(settings)
    generator = chat_generator if chat_generator is not None else ClaudeCliChatGenerator(
        model=settings.impact_model, timeout_s=settings.impact_llm_timeout_s)
    embedder = text_embedder if text_embedder is not None else default_text_embedder(settings)
    engine = search_engine if search_engine is not None else ClaudeWebSearch(
        model=settings.web_search_model, timeout_s=settings.web_search_timeout_s)
    tools = [
        ComponentTool(ProjectReader(document_store=store, max_chars=settings.impact_project_chars),
                      name="get_project", description="Full text of the project to analyse, by its id.",
                      outputs_to_string={"source": "result"}, **_SOURCES_IO),
        ComponentTool(DeliverableSearch(text_embedder=embedder,
                                        retriever=LocalQdrantEmbeddingRetriever(document_store=store)),
                      name="search_deliverables",
                      description="Search the full text of the project's deliverables (final reports, studies): "
                                  "recommendations, figures, outputs. Returns numbered passages [n].",
                      outputs_to_string={"source": "result"}, **_SOURCES_IO),
        ComponentTool(WebSearchTool(search_engine=engine, max_searches=settings.impact_max_searches),
                      name="web_search",
                      description="Search the web for evidence of the project's impact. Returns numbered results "
                                  "[n] (title, URL, snippet).",
                      outputs_to_string={"source": "result"},
                      outputs_to_state={"sources": {"source": "sources"}, "searches": {"source": "searches"}},
                      inputs_from_state={"sources": "known_sources", "searches": "past_searches"}),
        ComponentTool(PageReader(max_words=settings.impact_page_words, fetcher=page_fetcher),
                      name="fetch_page", description="Read a web page or online document (HTML, PDF) by URL.",
                      outputs_to_string={"source": "result"}, **_SOURCES_IO),
    ]
    agent = Agent(chat_generator=LoggedChatGenerator(chat_generator=generator, pricing=settings.pricing),
                  tools=tools, system_prompt=system_prompt(settings.impact_max_searches), state_schema=IMPACT_STATE,
                  max_agent_steps=settings.impact_max_steps)
    p = Pipeline()
    p.add_component("agent", agent)
    return p
