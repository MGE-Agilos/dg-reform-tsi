from typing import Any

import pytest
import yaml
from haystack import component
from haystack.components.embedders import MockTextEmbedder
from haystack.dataclasses import ChatMessage, ToolCall

from conftest import make_vector_store
from tsi_agent import runlog
from tsi_agent.chat.generator import LoggedChatGenerator
from tsi_agent.chat.pipeline import build_chat_pipeline
from tsi_agent.chat.prompt import system_prompt
from tsi_agent.components.claude_cli import ClaudeCliChatGenerator
from tsi_agent.config import Settings
from tsi_agent.pipeline import load_pipeline

POLICY_AREAS = ["green-transition", "health-and-long-term-care"]


@component
class ScriptedGenerator:
    """Renvoie les reponses donnees une par une ; garde les messages recus."""

    def __init__(self, replies: list[Any] | None = None):
        self.replies = list(replies or [])
        self.received: list[list[ChatMessage]] = []

    @component.output_types(replies=list[ChatMessage])
    def run(self, messages: list[ChatMessage], tools: Any = None) -> dict[str, Any]:
        self.received.append(list(messages))
        reply = self.replies.pop(0)
        if isinstance(reply, Exception):
            raise reply
        meta = {"model": "scripted", "usage": {"prompt_tokens": 10, "completion_tokens": 5}, "cost_usd": 0.001}
        if isinstance(reply, str):
            return {"replies": [ChatMessage.from_assistant(reply, meta=meta)]}
        return {"replies": [ChatMessage.from_assistant(tool_calls=[ToolCall(tool_name=n, arguments=a, id=f"c{i}")
                                                                   for i, (n, a) in enumerate(reply)], meta=meta)]}


def chat_settings(tmp_path) -> Settings:
    path = make_vector_store(tmp_path)
    return Settings(vectors_dir=str(path.parent), logs_dir=str(tmp_path / "logs"), chat_max_steps=6)


def test_system_prompt_has_rules_and_no_jinja():
    text = system_prompt(POLICY_AREAS)
    assert "list_projects" in text and "[n]" in text and "GR" in text and "green-transition" in text
    assert "{{" not in text and "{%" not in text


def test_logged_generator_logs_llm_call_and_forwards_tools(tmp_path):
    inner = ScriptedGenerator(["hello"])
    gen = LoggedChatGenerator(chat_generator=inner)
    with runlog.open_run(tmp_path, "g1") as run:
        reply = gen.run(messages=[ChatMessage.from_user("q")], tools=None)["replies"][0]
    assert reply.text == "hello"
    s = run.final_summary
    assert s["llm_calls"] == 1 and s["tokens"]["input_tokens"] == 10 and s["tokens"]["output_tokens"] == 5
    assert s["cost_usd"] == 0.001


def test_logged_generator_uses_pricing_when_no_cost(tmp_path):
    @component
    class NoCost:
        @component.output_types(replies=list[ChatMessage])
        def run(self, messages: list[ChatMessage], tools: Any = None) -> dict[str, Any]:
            return {"replies": [ChatMessage.from_assistant("x", meta={
                "model": "gpt-4o-mini-2024", "usage": {"prompt_tokens": 1_000_000, "completion_tokens": 0}})]}
    gen = LoggedChatGenerator(chat_generator=NoCost(), pricing={"gpt-4o-mini": {"input": 0.15, "output": 0.6}})
    with runlog.open_run(tmp_path, "g2") as run:
        gen.run(messages=[ChatMessage.from_user("q")])
    assert run.final_summary["cost_usd"] == 0.15


def test_logged_generator_round_trip():
    gen = LoggedChatGenerator(chat_generator=ClaudeCliChatGenerator(model="sonnet"), pricing={"a": {"input": 1}})
    again = LoggedChatGenerator.from_dict(gen.to_dict())
    assert isinstance(again.chat_generator, ClaudeCliChatGenerator) and again.chat_generator.model == "sonnet"
    assert again.pricing == {"a": {"input": 1}}


def test_pipeline_runs_agent_with_real_tools(tmp_path):
    s = chat_settings(tmp_path)
    gen = ScriptedGenerator([[("search_projects", {"query": "energy", "countries": ["GR"]})],
                             [("list_projects", {"programme": "SRSP"})], "Done [1]."])
    pipe = build_chat_pipeline(s, POLICY_AREAS, chat_generator=gen,
                               text_embedder=MockTextEmbedder(model="mock", dimension=8))
    out = pipe.run({"agent": {"messages": [ChatMessage.from_user("q")]}})["agent"]
    assert out["last_message"].text == "Done [1]."
    assert out["sources"] and out["sources"][0]["ref"] == 1
    tool_texts = [m.tool_call_results[0].result for m in out["messages"] if m.tool_call_results]
    assert tool_texts[0].startswith("[1] ") and tool_texts[1].startswith("Total matching projects: 2")
    assert gen.received[0][0].text == system_prompt(POLICY_AREAS)


def test_yaml_round_trip_and_openai_swap(tmp_path, monkeypatch):
    monkeypatch.setenv("OPENAI_API_KEY", "sk-test")
    s = chat_settings(tmp_path)
    pipe = build_chat_pipeline(s, POLICY_AREAS, chat_generator=ClaudeCliChatGenerator(model="sonnet"),
                               text_embedder=MockTextEmbedder(model="mock", dimension=8))
    text = pipe.dumps()
    for name in ("Agent", "LoggedChatGenerator", "ClaudeCliChatGenerator", "ProjectSearch", "ProjectLister",
                 "ProjectReader", "PersistedInMemoryDocumentStore"):
        assert name in text
    reloaded = load_pipeline(text)
    tools = reloaded.get_component("agent").tools
    assert [t.name for t in tools] == ["search_projects", "list_projects", "get_project"]

    data = yaml.safe_load(text)
    agent = data["components"]["agent"]["init_parameters"]
    agent["chat_generator"]["init_parameters"]["chat_generator"] = {
        "type": "haystack.components.generators.chat.openai.OpenAIChatGenerator",
        "init_parameters": {"model": "gpt-4o-mini",
                            "api_key": {"type": "env_var", "env_vars": ["OPENAI_API_KEY"], "strict": True}}}
    search = next(t for t in agent["tools"] if t["data"]["name"] == "search_projects")
    search["data"]["component"]["init_parameters"]["text_embedder"] = {
        "type": "haystack.components.embedders.openai_text_embedder.OpenAITextEmbedder",
        "init_parameters": {"model": "text-embedding-3-small",
                            "api_key": {"type": "env_var", "env_vars": ["OPENAI_API_KEY"], "strict": True}}}
    swapped = load_pipeline(yaml.safe_dump(data)).get_component("agent")
    assert type(swapped.chat_generator.chat_generator).__name__ == "OpenAIChatGenerator"
