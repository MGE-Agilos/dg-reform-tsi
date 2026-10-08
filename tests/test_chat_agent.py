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
    return Settings(vectors_dir=str(path), embedding_dim=8, logs_dir=str(tmp_path / "logs"), chat_max_steps=6)


def test_system_prompt_has_rules_and_no_jinja():
    text = system_prompt(POLICY_AREAS)
    assert "list_projects" in text and "search_deliverables" in text and "[n]" in text and "GR" in text and "green-transition" in text
    assert "{{" not in text and "{%" not in text


def test_system_prompt_points_free_topics_to_find_projects_and_english_queries():
    text = system_prompt(POLICY_AREAS)
    assert "find_projects" in text and "keywords" in text and "AML" in text
    assert "not be exhaustive" in text and "official policy area" in text
    assert "in English" in text
    assert "EVERY candidate" in text and "get_project" in text and "kept projects" in text
    assert "comes from a relevance threshold" not in text and "no relevance threshold" in text


def test_system_prompt_points_aggregate_questions_to_breakdown_lines():
    text = system_prompt(POLICY_AREAS)
    assert "By country" in text and "By policy area" in text
    assert "which countries" in text.lower() and "truncated" in text and "limit" in text


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


def test_logged_generator_round_trip(monkeypatch):
    # from_dict direct (hors Pipeline.loads) : Haystack 3.2 exige que tsi_agent soit dans son allowlist.
    monkeypatch.setenv("HAYSTACK_DESERIALIZATION_ALLOWLIST", "tsi_agent")
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
                 "ProjectReader", "DeliverableSearch", "ProjectFinder", "LocalQdrantEmbeddingRetriever", "LocalQdrantDocumentStore"):
        assert name in text
    reloaded = load_pipeline(text)
    tools = reloaded.get_component("agent").tools
    assert [t.name for t in tools] == ["search_projects", "find_projects", "search_deliverables", "list_projects", "get_project"]

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


from tsi_agent.chat.service import ask, cited_sources, find_cause
from tsi_agent.components.claude_cli import LLMQuotaExceeded


def test_cited_sources_keeps_cited_refs_in_order_else_all_unique():
    srcs = [{"ref": 1, "doc_id": "a", "url": "u1"}, {"ref": 2, "doc_id": "b", "url": "u2"},
            {"ref": 3, "doc_id": "a", "url": "u1"}]
    assert [s["ref"] for s in cited_sources("See [2] and [1][2].", srcs)] == [1, 2]
    assert [s["ref"] for s in cited_sources("No refs.", srcs)] == [1, 2]


def test_find_cause():
    try:
        try:
            raise LLMQuotaExceeded("limit")
        except LLMQuotaExceeded as inner:
            raise RuntimeError("wrapped") from inner
    except RuntimeError as exc:
        assert isinstance(find_cause(exc, LLMQuotaExceeded), LLMQuotaExceeded)
        assert find_cause(exc, KeyError) is None


def pipeline_with(tmp_path, replies, max_steps=6):
    s = chat_settings(tmp_path)
    s.chat_max_steps = max_steps
    gen = ScriptedGenerator(replies)
    return s, gen, build_chat_pipeline(s, POLICY_AREAS, chat_generator=gen,
                                       text_embedder=MockTextEmbedder(model="mock", dimension=8))


def test_ask_returns_answer_sources_steps_and_logs(tmp_path):
    s, gen, pipe = pipeline_with(tmp_path, [[("search_projects", {"query": "energy", "countries": ["GR"]})],
                                            [("list_projects", {"programme": "SRSP"})], "Answer [1]."])
    res = ask(pipe, "Quels projets ?", [], logs_dir=s.logs_dir)
    assert res["answer"] == "Answer [1]."
    assert [x["ref"] for x in res["sources"]] == [1]
    assert [st["tool"] for st in res["steps"]] == ["search_projects", "list_projects"]
    assert res["steps"][1]["summary"].startswith("Total matching projects: 2")
    assert res["cost_usd"] == pytest.approx(0.003) and res["run_id"]
    import json
    summary = json.loads((tmp_path / "logs" / "runs" / f"{res['run_id']}.summary.json").read_text(encoding="utf-8"))
    assert summary["mode"] == "chat" and summary["counts"]["tool_call:ok"] == 2
    assert summary["counts"]["llm_call:ok"] == 3 and summary["counts"]["question:ok"] == 1


def test_ask_sends_history_as_messages(tmp_path):
    s, gen, pipe = pipeline_with(tmp_path, ["A1"])
    ask(pipe, "Et en Slovénie ?", [{"role": "user", "text": "Projets santé en Croatie ?"},
                                   {"role": "assistant", "text": "Deux projets [1]."}], logs_dir=s.logs_dir)
    roles = [m.role.value for m in gen.received[0]]
    assert roles == ["system", "user", "assistant", "user"]
    assert gen.received[0][-1].text == "Et en Slovénie ?"


def test_ask_when_step_limit_is_reached(tmp_path):
    s, gen, pipe = pipeline_with(tmp_path, [[("list_projects", {})]] * 5, max_steps=2)
    res = ask(pipe, "q", [], logs_dir=s.logs_dir)
    assert "limite" in res["answer"]


def test_ask_propagates_quota(tmp_path):
    s, gen, pipe = pipeline_with(tmp_path, [LLMQuotaExceeded("usage limit reached")])
    with pytest.raises(Exception) as err:
        ask(pipe, "q", [], logs_dir=s.logs_dir)
    assert find_cause(err.value, LLMQuotaExceeded) is not None
