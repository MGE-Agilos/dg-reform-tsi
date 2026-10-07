"""Pipeline Haystack de l'analyse d'impact : un Agent avec 4 outils (projet, livrables, recherche web, lecture de
page).

L'export YAML (`export-yaml --impact`) contient les composants locaux ClaudeWebSearch (moteur de l'outil web_search)
et ClaudeCliChatGenerator (enveloppe par LoggedChatGenerator). Dans deepset, on les remplace a la main dans le YAML :
ClaudeWebSearch par SerperDevWebSearch (package serperdev-haystack, cle API Serper ; meme interface query ->
documents, links) et ClaudeCliChatGenerator par OpenAIChatGenerator. Rien n'est remplace automatiquement."""

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
from tsi_agent.impact.tools import BoundedLinkFetcher, PageReader, WebSearchTool
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
    fetcher = page_fetcher if page_fetcher is not None else BoundedLinkFetcher(max_mb=settings.impact_page_max_mb)
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
        ComponentTool(PageReader(max_words=settings.impact_page_words, fetcher=fetcher),
                      name="fetch_page", description="Read a web page or online document (HTML, PDF) by URL.",
                      outputs_to_string={"source": "result"}, **_SOURCES_IO),
    ]
    agent = Agent(chat_generator=LoggedChatGenerator(chat_generator=generator, pricing=settings.pricing),
                  tools=tools, system_prompt=system_prompt(settings.impact_max_searches), state_schema=IMPACT_STATE,
                  max_agent_steps=settings.impact_max_steps,
                  # Les outils renvoient deja leurs erreurs attendues en texte ; une exception qui remonte (quota
                  # de la souscription, bug) doit arreter l'analyse au lieu d'etre transmise au modele.
                  raise_on_tool_invocation_failure=True)
    p = Pipeline()
    p.add_component("agent", agent)
    return p
