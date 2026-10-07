import pytest
import yaml
from haystack.components.embedders import MockTextEmbedder
from haystack.dataclasses import ChatMessage

from test_chat_agent import ScriptedGenerator, chat_settings
from test_impact_tools import FakeEngine, FakeFetcher
from tsi_agent.components.claude_cli import ClaudeCliChatGenerator
from tsi_agent.impact.pipeline import build_impact_pipeline
from tsi_agent.impact.prompt import system_prompt, user_prompt
from tsi_agent.pipeline import load_pipeline


def impact_pipeline(tmp_path, replies, settings=None, **kw):
    s = settings if settings is not None else chat_settings(tmp_path)  # chat_settings cree le store : une fois
    return build_impact_pipeline(s, chat_generator=ScriptedGenerator(replies),
                                 text_embedder=MockTextEmbedder(model="mock", dimension=8), **kw)


def test_prompt():
    text = system_prompt(max_searches=7)
    for word in ("get_project", "web_search", "fetch_page", "search_deliverables", "7", "JSON",
                 "insufficient evidence", "causal"):
        assert word in text
    assert "{{" not in text and "energy-regulation-academy" in user_prompt("energy-regulation-academy")


def test_agent_runs_tools_with_continuous_numbering(tmp_path):
    engine = FakeEngine([("News", "https://n.eu", "arrears fell")])
    fetcher = FakeFetcher({"https://n.eu": (b"Arrears fell by 40 percent.", "text/plain")})
    pipe = impact_pipeline(tmp_path, [[("get_project", {"doc_id": "energy-regulation-academy"})],
                                      [("web_search", {"query": "energy regulation academy results"})],
                                      [("fetch_page", {"url": "https://n.eu"})], '{"confidence": "low"}'],
                           search_engine=engine, page_fetcher=fetcher)
    out = pipe.run({"agent": {"messages": [ChatMessage.from_user("go")]}})["agent"]
    assert [s["ref"] for s in out["sources"]] == [1, 2]  # page lue = meme numero que le resultat de recherche
    assert out["searches"] == ["energy regulation academy results"]
    assert out["last_message"].text == '{"confidence": "low"}'


def test_yaml_round_trip(tmp_path):
    s = chat_settings(tmp_path)
    pipe = build_impact_pipeline(s, chat_generator=ClaudeCliChatGenerator(model="sonnet"),
                                 text_embedder=MockTextEmbedder(model="mock", dimension=8))
    text = pipe.dumps()
    for name in ("ClaudeWebSearch", "WebSearchTool", "PageReader", "BoundedLinkFetcher", "ProjectReader",
                 "DeliverableSearch"):
        assert name in text
    agent = load_pipeline(text).get_component("agent")
    assert [t.name for t in agent.tools] == ["get_project", "search_deliverables", "web_search", "fetch_page"]
    web = next(t for t in yaml.safe_load(text)["components"]["agent"]["init_parameters"]["tools"]
               if t["data"]["name"] == "web_search")
    assert web["data"]["component"]["init_parameters"]["max_searches"] == s.impact_max_searches
    page = next(t for t in yaml.safe_load(text)["components"]["agent"]["init_parameters"]["tools"]
                if t["data"]["name"] == "fetch_page")
    assert page["data"]["component"]["init_parameters"]["fetcher"]["init_parameters"]["max_mb"] ==         s.impact_page_max_mb
    assert agent.raise_on_tool_invocation_failure is True
