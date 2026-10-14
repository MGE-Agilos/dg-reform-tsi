import functools
import json

import pytest
from haystack.components.generators.chat import OpenAIChatGenerator
from haystack.core.serialization import component_from_dict, component_to_dict
from haystack.dataclasses import ChatMessage

from tsi_agent.components.claude_cli import ClaudeCliChatGenerator, LLMCallError, LLMQuotaExceeded
from tsi_agent.config import Settings
from tsi_agent import llm
from tsi_agent.llm import (LLMConfigError, OpenAIJsonChatGenerator, check_provider, load_dotenv,
                           make_json_generator, ping, read_dotenv)
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
    state: dict = {"error": None, "text": json.dumps({"summary": "S.", "countries": ["HR"]}), "finish": "stop"}

    @functools.wraps(OpenAIChatGenerator.run)  # meme signature : Haystack compare run et run_async
    def run(self, messages, streaming_callback=None, generation_kwargs=None, **kw):
        calls.append({"messages": messages, "generation_kwargs": self.generation_kwargs, "model": self.model,
                      "api_base_url": self.api_base_url, "timeout": self.timeout,
                      "max_retries": self.max_retries})
        if state["error"] is not None:
            raise state["error"]
        meta = {"model": "gpt-5.1-2025-11-13", "finish_reason": state["finish"],
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
    monkeypatch.setenv("OPENAI_API_KEY", "test-key")
    monkeypatch.setattr(llm, "ping", lambda settings: {"model": "openai/gpt-5.1"})
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


def test_read_dotenv_bom_quotes_and_inline_comments(tmp_path):
    env = tmp_path / ".env"
    env.write_bytes("﻿KEY_A=abc # commentaire\nKEY_B='x # pas un commentaire'\nKEY_C=\"mixed'\n"
                    "KEY_D=\"q\"\nKEY_E=a#b\nKEY_F=\"sk-abc\nKEY_G='sk-def # commentaire\n".encode("utf-8"))
    # guillemet ouvrant sans fermeture : retire (KEY_C, KEY_F, KEY_G)
    assert read_dotenv(env) == {"KEY_A": "abc", "KEY_B": "x # pas un commentaire", "KEY_C": "mixed'",
                                "KEY_D": "q", "KEY_E": "a#b", "KEY_F": "sk-abc", "KEY_G": "sk-def"}


def test_check_provider(monkeypatch):
    check_provider(Settings())  # claude_cli : rien a verifier
    with pytest.raises(ValueError, match="llm_provider"):
        check_provider(Settings(llm_provider="mistral"))
    monkeypatch.delenv("ECGPT_KEY", raising=False)
    with pytest.raises(ValueError, match="ECGPT_KEY"):
        check_provider(openai_settings())
    monkeypatch.setenv("ECGPT_KEY", "  ")
    with pytest.raises(ValueError, match="ECGPT_KEY"):
        check_provider(openai_settings())
    monkeypatch.setenv("ECGPT_KEY", "k")
    check_provider(openai_settings())


def test_ping_makes_one_small_json_call(fake_openai):
    calls, state = fake_openai
    state["text"] = '{"ok": true}'
    result = ping(openai_settings())
    assert len(calls) == 1 and calls[0]["timeout"] == 60.0
    assert 'Return the JSON object {"ok": true}.' in calls[0]["messages"][-1].text
    assert '"ok"' in calls[0]["messages"][0].text  # schema dans le systeme
    assert result["model"] == "openai/gpt-5.1"
    state["text"] = '{"ok": false}'
    with pytest.raises(LLMCallError):
        ping(openai_settings())
    state["text"] = "not json"
    with pytest.raises(LLMCallError):
        ping(openai_settings())


@pytest.mark.parametrize("status", [401, 403, 404])
def test_auth_and_not_found_errors_are_config_errors(fake_openai, status):
    _, state = fake_openai

    class ApiError(Exception):
        def __init__(self):
            super().__init__(f"status {status}")
            self.status_code = status

    state["error"] = ApiError()
    with pytest.raises(LLMConfigError, match="configuration") as info:
        OpenAIJsonChatGenerator(api_key_env="ECGPT_KEY").run(messages=[ChatMessage.from_user("x")])
    assert isinstance(info.value, LLMQuotaExceeded)  # les lots s'arretent comme sur un quota


@pytest.mark.parametrize("name", ["AuthenticationError", "PermissionDeniedError", "NotFoundError"])
def test_openai_config_exception_names_are_config_errors(fake_openai, name):
    _, state = fake_openai
    state["error"] = type(name, (Exception,), {})("bad")
    with pytest.raises(LLMConfigError):
        OpenAIJsonChatGenerator(api_key_env="ECGPT_KEY").run(messages=[ChatMessage.from_user("x")])


def test_missing_key_at_call_time_is_a_config_error(monkeypatch):
    monkeypatch.delenv("ECGPT_KEY", raising=False)
    with pytest.raises(LLMConfigError):
        OpenAIJsonChatGenerator(api_key_env="ECGPT_KEY").run(messages=[ChatMessage.from_user("x")])


@pytest.mark.parametrize("error", [
    type("APIError", (Exception,), {"code": "insufficient_quota"})("x"),
    type("APIError", (Exception,), {"code": "rate_limit_exceeded"})("x"),
    Exception("You exceeded your current Quota, please check your plan"),
])
def test_quota_codes_and_messages_are_quota_errors(fake_openai, error):
    _, state = fake_openai
    state["error"] = error
    with pytest.raises(LLMQuotaExceeded) as info:
        OpenAIJsonChatGenerator(api_key_env="ECGPT_KEY").run(messages=[ChatMessage.from_user("x")])
    assert not isinstance(info.value, LLMConfigError)


def test_max_retries_and_max_completion_tokens(fake_openai):
    calls, _ = fake_openai
    make_json_generator(openai_settings(), SCHEMA, timeout_s=10).run(messages=[ChatMessage.from_user("x")])
    assert calls[-1]["max_retries"] == 2
    assert "max_completion_tokens" not in calls[-1]["generation_kwargs"]
    gen = make_json_generator(openai_settings(openai_max_retries=5, openai_max_completion_tokens=4000), SCHEMA,
                              timeout_s=10)
    gen.run(messages=[ChatMessage.from_user("x")])
    assert calls[-1]["max_retries"] == 5
    assert calls[-1]["generation_kwargs"] == {"response_format": {"type": "json_object"},
                                              "max_completion_tokens": 4000}
    back = component_from_dict(OpenAIJsonChatGenerator, component_to_dict(obj=gen, name="g"), "g")
    assert (back.max_retries, back.max_completion_tokens) == (5, 4000)


def test_truncated_reply_is_an_error(fake_openai):
    _, state = fake_openai
    state["finish"] = "length"
    with pytest.raises(LLMCallError, match="openai_max_completion_tokens"):
        OpenAIJsonChatGenerator(api_key_env="ECGPT_KEY").run(messages=[ChatMessage.from_user("x")])


def test_returned_model_is_kept(fake_openai):
    reply = OpenAIJsonChatGenerator(api_key_env="ECGPT_KEY", json_schema=SCHEMA).run(
        messages=[ChatMessage.from_user("x")])["replies"][0]
    assert reply.meta["model"] == "openai/gpt-5.1" and reply.meta["model_returned"] == "gpt-5.1-2025-11-13"


def test_without_schema_the_system_still_asks_for_json(fake_openai):
    calls, _ = fake_openai
    OpenAIJsonChatGenerator(api_key_env="ECGPT_KEY").run(
        messages=[ChatMessage.from_system("Be precise."), ChatMessage.from_user("x")])
    system = calls[0]["messages"][0].text
    assert system.startswith("Be precise.") and system.endswith("Answer with a single JSON object.")


def _settings_file(tmp_path, extra=""):
    out = tmp_path / "output"
    (out / "projects").mkdir(parents=True, exist_ok=True)
    path = tmp_path / "settings.yaml"
    path.write_text(f"output_dir: {out.as_posix()}\nlogs_dir: {(tmp_path / 'logs').as_posix()}\n"
                    f"cache_dir: {(tmp_path / 'cache').as_posix()}\n{extra}", encoding="utf-8")
    return str(path)


@pytest.mark.parametrize("command", [["run"], ["deliverables"], ["csr-link"], ["llm-check"]])
def test_cli_stops_before_any_work_when_the_key_is_missing(tmp_path, monkeypatch, capsys, command):
    import tsi_agent.__main__ as cli

    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    monkeypatch.setattr(llm, "ping", lambda s: pytest.fail("pas de ping sans cle"))
    monkeypatch.setattr(cli, "run_extraction", lambda *a, **k: pytest.fail("aucun travail"))
    monkeypatch.setattr(cli, "run_deliverables", lambda *a, **k: pytest.fail("aucun travail"))
    settings = _settings_file(tmp_path, "llm_provider: openai\n")
    assert cli.main(["--settings", settings, *command]) == 2
    err = capsys.readouterr().err
    assert "ERREUR (fournisseur LLM)" in err and "OPENAI_API_KEY" in err


def test_cli_stops_when_the_ping_fails(tmp_path, monkeypatch, capsys):
    import tsi_agent.__main__ as cli

    monkeypatch.setenv("OPENAI_API_KEY", "k")

    def bad_ping(settings):
        raise LLMConfigError("erreur de configuration : 401")

    monkeypatch.setattr(llm, "ping", bad_ping)
    monkeypatch.setattr(cli, "run_extraction", lambda *a, **k: pytest.fail("aucun travail"))
    assert cli.main(["--settings", _settings_file(tmp_path, "llm_provider: openai\n"), "run"]) == 2
    assert "ERREUR (fournisseur LLM) : erreur de configuration : 401" in capsys.readouterr().err


def test_cli_unknown_provider_stops(tmp_path, monkeypatch, capsys):
    import tsi_agent.__main__ as cli

    monkeypatch.setattr(cli, "run_extraction", lambda *a, **k: pytest.fail("aucun travail"))
    assert cli.main(["--settings", _settings_file(tmp_path, "llm_provider: mistral\n"), "run"]) == 2
    assert "ERREUR (fournisseur LLM)" in capsys.readouterr().err


def test_cli_does_not_ping_in_claude_cli_mode(tmp_path, monkeypatch):
    import tsi_agent.__main__ as cli

    monkeypatch.setattr(llm, "ping", lambda s: pytest.fail("pas de ping en mode claude_cli"))
    monkeypatch.setattr(cli, "run_extraction", lambda *a, **k: {"quota_exceeded": False})
    assert cli.main(["--settings", _settings_file(tmp_path), "run"]) == 0


def test_cli_pings_once_in_openai_mode(tmp_path, monkeypatch):
    import tsi_agent.__main__ as cli

    monkeypatch.setenv("OPENAI_API_KEY", "k")
    pings = []
    monkeypatch.setattr(llm, "ping", lambda s: pings.append(s.openai_model) or {"model": "openai/gpt-5.1"})
    monkeypatch.setattr(cli, "run_extraction", lambda *a, **k: {"quota_exceeded": False})
    assert cli.main(["--settings", _settings_file(tmp_path, "llm_provider: openai\n"), "run"]) == 0
    assert pings == ["gpt-5.1"]
