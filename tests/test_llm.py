import functools
import json

import pytest
from haystack.components.generators.chat import OpenAIChatGenerator
from haystack.core.serialization import component_from_dict, component_to_dict
from haystack.dataclasses import ChatMessage

from tsi_agent.components.claude_cli import ClaudeCliChatGenerator, LLMCallError, LLMQuotaExceeded
from tsi_agent.config import Settings
from tsi_agent.llm import OpenAIJsonChatGenerator, load_dotenv, make_json_generator
from tsi_agent.models import ProjectContent

SCHEMA = ProjectContent.model_json_schema()
EC_GPT = "https://api.tech.ec.europa.eu/ecgpt/v1"


def openai_settings(**kw) -> Settings:
    return Settings(llm_provider="openai", openai_base_url=EC_GPT, openai_api_key_env="ECGPT_KEY", **kw)


def test_claude_cli_provider_is_the_default_and_keeps_the_claude_model():
    gen = make_json_generator(Settings(model="opus"), SCHEMA, timeout_s=180)
    assert isinstance(gen, ClaudeCliChatGenerator)
    assert (gen.model, gen.json_schema, gen.timeout_s) == ("opus", SCHEMA, 180)
    assert make_json_generator(Settings(), SCHEMA, timeout_s=300, model="sonnet").model == "sonnet"


def test_openai_provider_builds_the_openai_generator():
    gen = make_json_generator(openai_settings(), SCHEMA, timeout_s=600, model="sonnet")
    assert isinstance(gen, OpenAIJsonChatGenerator)
    assert (gen.model, gen.api_base_url, gen.api_key_env, gen.json_schema) == ("gpt-5.1", EC_GPT, "ECGPT_KEY", SCHEMA)
    assert gen.timeout_s == 600
    assert make_json_generator(openai_settings(), SCHEMA, timeout_s=180).timeout_s == 300  # openai_timeout_s


def test_unknown_provider_raises():
    with pytest.raises(ValueError, match="llm_provider"):
        make_json_generator(Settings(llm_provider="mistral"), SCHEMA, timeout_s=10)


@pytest.fixture
def fake_openai(monkeypatch):
    monkeypatch.setenv("ECGPT_KEY", "test-key")
    calls: list[dict] = []
    state: dict = {"error": None, "text": json.dumps({"summary": "S.", "countries": ["HR"]})}

    @functools.wraps(OpenAIChatGenerator.run)  # meme signature : Haystack compare run et run_async
    def run(self, messages, streaming_callback=None, generation_kwargs=None, **kw):
        calls.append({"messages": messages, "generation_kwargs": self.generation_kwargs, "model": self.model,
                      "api_base_url": self.api_base_url, "timeout": self.timeout})
        if state["error"] is not None:
            raise state["error"]
        meta = {"model": "gpt-5.1-2025-11-13", "finish_reason": "stop",
                "usage": {"prompt_tokens": 1200, "completion_tokens": 300, "total_tokens": 1500}}
        return {"replies": [ChatMessage.from_assistant(state["text"], meta=meta)]}

    monkeypatch.setattr(OpenAIChatGenerator, "run", run)
    return calls, state


def test_openai_generator_sends_the_schema_and_json_mode(fake_openai):
    calls, state = fake_openai
    gen = make_json_generator(openai_settings(), SCHEMA, timeout_s=180)
    out = gen.run(messages=[ChatMessage.from_system("You summarise."), ChatMessage.from_user("Page text")])
    reply = out["replies"][0]
    assert reply.text == state["text"]
    assert reply.meta["model"] == "openai/gpt-5.1"
    assert reply.meta["usage"]["prompt_tokens"] == 1200 and reply.meta["usage"]["completion_tokens"] == 300
    call = calls[0]
    assert call["generation_kwargs"] == {"response_format": {"type": "json_object"}}
    assert (call["model"], call["api_base_url"], call["timeout"]) == ("gpt-5.1", EC_GPT, 300.0)
    system = call["messages"][0].text
    assert system.startswith("You summarise.")
    assert "Answer with a single JSON object that conforms to this JSON schema:" in system
    assert json.dumps(SCHEMA, separators=(",", ":")) in system
    assert call["messages"][1].text == "Page text" and len(call["messages"]) == 2


def test_openai_generator_adds_a_system_message_when_missing(fake_openai):
    calls, _ = fake_openai
    OpenAIJsonChatGenerator(api_key_env="ECGPT_KEY", json_schema=SCHEMA).run(
        messages=[ChatMessage.from_user("Page text")])
    assert calls[0]["messages"][0].is_from("system") and "JSON schema" in calls[0]["messages"][0].text


def test_openai_rate_limit_becomes_quota_error(fake_openai):
    _, state = fake_openai

    class RateLimitError(Exception):
        pass

    state["error"] = RateLimitError("Error code: 429 - insufficient_quota")
    gen = OpenAIJsonChatGenerator(api_key_env="ECGPT_KEY", json_schema=SCHEMA)
    with pytest.raises(LLMQuotaExceeded):
        gen.run(messages=[ChatMessage.from_user("x")])


def test_openai_status_429_becomes_quota_and_other_errors_call_errors(fake_openai):
    _, state = fake_openai

    class ApiError(Exception):
        def __init__(self, status):
            super().__init__(f"status {status}")
            self.status_code = status

    gen = OpenAIJsonChatGenerator(api_key_env="ECGPT_KEY", json_schema=SCHEMA)
    state["error"] = ApiError(429)
    with pytest.raises(LLMQuotaExceeded):
        gen.run(messages=[ChatMessage.from_user("x")])
    state["error"] = ApiError(500)
    with pytest.raises(LLMCallError) as info:
        gen.run(messages=[ChatMessage.from_user("x")])
    assert not isinstance(info.value, LLMQuotaExceeded)


def test_missing_api_key_is_a_clear_error(monkeypatch):
    monkeypatch.delenv("ECGPT_KEY", raising=False)
    with pytest.raises(LLMCallError, match="ECGPT_KEY"):
        OpenAIJsonChatGenerator(api_key_env="ECGPT_KEY").run(messages=[ChatMessage.from_user("x")])


def test_openai_generator_serialization_round_trip():
    gen = OpenAIJsonChatGenerator(model="gpt-5.1", api_base_url=EC_GPT, api_key_env="ECGPT_KEY", timeout_s=400,
                                  json_schema=SCHEMA)
    data = component_to_dict(obj=gen, name="gen")
    assert "test-key" not in json.dumps(data)
    back = component_from_dict(OpenAIJsonChatGenerator, data, "gen")
    assert (back.model, back.api_base_url, back.api_key_env, back.timeout_s, back.json_schema) == (
        "gpt-5.1", EC_GPT, "ECGPT_KEY", 400, SCHEMA)


def test_pipelines_use_the_configured_provider(tmp_path):
    from tsi_agent.deliverables_pipeline import build_deliverables_pipeline
    from tsi_agent.pipeline import build_pipeline, load_pipeline

    settings = openai_settings(output_dir=str(tmp_path / "output"))
    pipe = build_pipeline(settings, {})
    assert isinstance(pipe.get_component("extractor").chat_generator, OpenAIJsonChatGenerator)
    judge = pipe.get_component("judge").chat_generator.chat_generator
    assert isinstance(judge, OpenAIJsonChatGenerator) and judge.json_schema["required"] == ["decisions"]
    reloaded = load_pipeline(pipe.dumps())
    assert reloaded.get_component("extractor").chat_generator.api_base_url == EC_GPT
    summarizer = build_deliverables_pipeline(settings).get_component("summarizer")
    assert isinstance(summarizer.chat_generator, OpenAIJsonChatGenerator)
    assert summarizer.chat_generator.timeout_s == settings.deliverables_llm_timeout_s


def test_csr_link_uses_the_factory(tmp_path, monkeypatch):
    import tsi_agent.__main__ as cli
    import tsi_agent.csr_link as csr_link

    seen = {}
    monkeypatch.setattr("tsi_agent.csr.read_rows", lambda path: [])
    monkeypatch.setattr("tsi_agent.csr.build_csrs", lambda rows, *a: [])

    def fake_run_linking(settings, csrs, generator, **kw):
        seen["generator"] = generator
        return {"quota_exceeded": False, "failed": []}

    monkeypatch.setattr(csr_link, "run_linking", fake_run_linking)
    out = tmp_path / "output"
    (out / "csr").mkdir(parents=True)
    (out / "csr" / "csr_full.xlsx").write_bytes(b"")
    settings_file = tmp_path / "settings.yaml"
    settings_file.write_text(f"output_dir: {out.as_posix()}\nlogs_dir: {(tmp_path / 'logs').as_posix()}\n"
                             f"llm_provider: openai\nopenai_base_url: {EC_GPT}\n", encoding="utf-8")
    assert cli.main(["--settings", str(settings_file), "csr-link"]) == 0
    inner = seen["generator"].chat_generator
    assert isinstance(inner, OpenAIJsonChatGenerator) and inner.json_schema == csr_link.LINK_SCHEMA


def test_load_dotenv_never_overwrites(tmp_path, monkeypatch):
    env = tmp_path / ".env"
    env.write_text("# comment\nTSI_TEST_A='from-file'\nexport TSI_TEST_B=\"b\"\n\nTSI_TEST_C=c=d\nnot a line\n",
                   encoding="utf-8")
    monkeypatch.setenv("TSI_TEST_A", "from-env")
    for key in ("TSI_TEST_B", "TSI_TEST_C"):  # restaurees (absentes) a la fin du test
        monkeypatch.setenv(key, "x")
        monkeypatch.delenv(key)
    added = load_dotenv(env)
    import os
    assert os.environ["TSI_TEST_A"] == "from-env"
    assert os.environ["TSI_TEST_B"] == "b" and os.environ["TSI_TEST_C"] == "c=d"
    assert set(added) == {"TSI_TEST_B", "TSI_TEST_C"}
    assert load_dotenv(tmp_path / "missing.env") == {}
