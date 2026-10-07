import json

import pytest
from haystack.dataclasses import ChatMessage

from tsi_agent import runlog
from tsi_agent.impact.web_search import SEARCH_SCHEMA, ClaudeWebSearch


class FakeCli:
    def __init__(self, payload):
        self.payload = payload
        self.messages = None

    def run(self, messages, tools=None):
        self.messages = messages
        if isinstance(self.payload, Exception):
            raise self.payload
        meta = {"model": "claude-cli/haiku", "usage": {"prompt_tokens": 50, "completion_tokens": 20},
                "cost_usd": 0.002}
        return {"replies": [ChatMessage.from_assistant(json.dumps(self.payload), meta=meta)]}


def test_returns_documents_and_links_and_logs_cost(tmp_path, monkeypatch):
    search = ClaudeWebSearch(top_k=2)
    fake = FakeCli({"results": [
        {"title": "A", "url": "https://a.eu/x", "snippet": "alpha"},
        {"title": "Bad", "url": "javascript:alert(1)", "snippet": "no"},
        {"title": "B", "url": "https://b.org/y", "snippet": "beta"},
        {"title": "C", "url": "https://c.org/z", "snippet": "gamma"}]})
    monkeypatch.setattr(search, "_cli", lambda: fake)
    with runlog.open_run(tmp_path, "w1") as run:
        out = search.run(query="Croatia health reform impact")
    assert [d.meta["link"] for d in out["documents"]] == ["https://a.eu/x", "https://b.org/y"]
    assert out["documents"][0].meta["title"] == "A" and out["documents"][0].content == "alpha"
    assert out["links"] == ["https://a.eu/x", "https://b.org/y"]
    assert "Croatia health reform impact" in fake.messages[-1].text
    assert run.final_summary["llm_calls"] == 1 and run.final_summary["cost_usd"] == 0.002


def test_cli_is_configured_for_websearch_only():
    cli = ClaudeWebSearch(model="haiku", timeout_s=99)._cli()
    assert cli.allowed_tools == ["WebSearch"] and cli.json_schema == SEARCH_SCHEMA
    assert cli.model == "haiku" and cli.timeout_s == 99


def test_round_trip(monkeypatch):
    monkeypatch.setenv("HAYSTACK_DESERIALIZATION_ALLOWLIST", "tsi_agent")
    again = ClaudeWebSearch.from_dict(ClaudeWebSearch(model="haiku", top_k=5).to_dict())
    assert again.model == "haiku" and again.top_k == 5


def test_unparseable_reply_gives_no_documents(tmp_path, monkeypatch):
    search = ClaudeWebSearch()
    monkeypatch.setattr(search, "_cli", lambda: FakeCli("not a dict"))
    assert search.run(query="q") == {"documents": [], "links": []}


@pytest.mark.integration
def test_real_web_search_via_cli():
    out = ClaudeWebSearch(model="haiku", top_k=5).run(query="Technical Support Instrument Croatia health reform")
    assert out["documents"], "no results"
    assert all(d.meta["link"].startswith("http") for d in out["documents"])


def test_unparseable_reply_is_logged_as_bad_format(tmp_path, monkeypatch):
    search = ClaudeWebSearch()
    monkeypatch.setattr(search, "_cli", lambda: FakeCli({"no_results": []}))
    with runlog.open_run(tmp_path, "w2") as run:
        assert search.run(query="q")["documents"] == []
    counts = run.final_summary["counts"]
    assert counts.get("web_search:bad_format") == 1 and run.final_summary["llm_calls"] == 1
