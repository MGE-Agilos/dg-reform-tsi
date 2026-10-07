import json
import subprocess

import pytest
from haystack.dataclasses import ChatMessage, ToolCall
from haystack.tools import Tool

from tsi_agent.components import claude_cli
from tsi_agent.components.claude_cli import ClaudeCliChatGenerator, LLMCallError, LLMQuotaExceeded


def cli_output(**overrides):
    data = {"type": "result", "is_error": False, "api_error_status": None, "result": "plain text",
            "total_cost_usd": 0.0068, "duration_ms": 3500, "num_turns": 2, "session_id": "s1",
            "usage": {"input_tokens": 4272, "output_tokens": 427, "cache_read_input_tokens": 10,
                      "cache_creation_input_tokens": 0},
            "modelUsage": {"claude-haiku-4-5-20251001": {}}}
    data.update(overrides)
    return json.dumps(data)


@pytest.fixture
def fake_run(monkeypatch):
    calls = {}

    def install(stdout="", stderr="", returncode=0, exc=None):
        def run(cmd, **kw):
            calls["cmd"], calls["input"] = cmd, kw["input"]
            if exc:
                raise exc
            return subprocess.CompletedProcess(cmd, returncode, stdout=stdout, stderr=stderr)
        monkeypatch.setattr(claude_cli.subprocess, "run", run)
        monkeypatch.setattr(claude_cli.shutil, "which", lambda exe: "C:/npm/claude.CMD")
        return calls
    return install


MESSAGES = [ChatMessage.from_system("Line one.\nLine two."), ChatMessage.from_user("Hello\nworld")]


def test_builds_isolated_command_and_sends_prompt_on_stdin(fake_run):
    calls = fake_run(stdout=cli_output())
    ClaudeCliChatGenerator(model="haiku", json_schema={"type": "object"}).run(messages=MESSAGES)
    cmd = calls["cmd"]
    assert cmd[:4] == ["C:/npm/claude.CMD", "-p", "--output-format", "json"]
    for flag in ("--strict-mcp-config", "--no-session-persistence", "--setting-sources", "--tools"):
        assert flag in cmd
    assert cmd[cmd.index("--system-prompt") + 1] == "Line one. Line two."
    assert cmd[cmd.index("--json-schema") + 1] == '{"type":"object"}'
    assert calls["input"] == "Hello\nworld"


def test_reply_text_and_openai_style_usage(fake_run):
    fake_run(stdout=cli_output(structured_output={"summary": "S"}))
    reply = ClaudeCliChatGenerator().run(messages=MESSAGES)["replies"][0]
    assert json.loads(reply.text) == {"summary": "S"}
    assert reply.meta["model"] == "claude-cli/claude-haiku-4-5-20251001"
    assert reply.meta["usage"] == {"prompt_tokens": 4272, "completion_tokens": 427,
                                   "cache_read_input_tokens": 10, "cache_creation_input_tokens": 0}
    assert reply.meta["cost_usd"] == 0.0068


def test_plain_result_when_no_schema(fake_run):
    fake_run(stdout=cli_output())
    assert ClaudeCliChatGenerator().run(messages=MESSAGES)["replies"][0].text == "plain text"


def test_quota_error(fake_run):
    fake_run(stdout=cli_output(is_error=True, result="Claude AI usage limit reached|1759230000"), returncode=1)
    with pytest.raises(LLMQuotaExceeded):
        ClaudeCliChatGenerator().run(messages=MESSAGES)


def test_429_status_is_quota(fake_run):
    fake_run(stdout=cli_output(is_error=True, api_error_status=429, result="API Error"), returncode=1)
    with pytest.raises(LLMQuotaExceeded):
        ClaudeCliChatGenerator().run(messages=MESSAGES)


def test_other_error_and_non_json_output(fake_run):
    fake_run(stdout=cli_output(is_error=True, result="Invalid model"), returncode=1)
    with pytest.raises(LLMCallError) as err:
        ClaudeCliChatGenerator().run(messages=MESSAGES)
    assert not isinstance(err.value, LLMQuotaExceeded)
    fake_run(stdout="", stderr="boom", returncode=2)
    with pytest.raises(LLMCallError, match="boom"):
        ClaudeCliChatGenerator().run(messages=MESSAGES)


def test_timeout(fake_run):
    fake_run(exc=subprocess.TimeoutExpired("claude", 1))
    with pytest.raises(LLMCallError, match="timeout"):
        ClaudeCliChatGenerator(timeout_s=1).run(messages=MESSAGES)


def test_missing_executable(monkeypatch):
    monkeypatch.setattr(claude_cli.shutil, "which", lambda exe: None)
    with pytest.raises(LLMCallError, match="introuvable"):
        ClaudeCliChatGenerator().run(messages=MESSAGES)


def test_npm_cmd_shim_is_bypassed_for_the_native_exe(monkeypatch, tmp_path):
    shim = tmp_path / "claude.CMD"
    shim.write_text("@echo off")
    exe = tmp_path / "node_modules" / "@anthropic-ai" / "claude-code" / "bin" / "claude.exe"
    exe.parent.mkdir(parents=True)
    exe.write_bytes(b"")
    monkeypatch.setattr(claude_cli.shutil, "which", lambda name: str(shim))
    assert ClaudeCliChatGenerator()._command("s")[0] == str(exe)


def test_cmd_shim_kept_when_no_native_exe(monkeypatch, tmp_path):
    shim = tmp_path / "claude.CMD"
    shim.write_text("@echo off")
    monkeypatch.setattr(claude_cli.shutil, "which", lambda name: str(shim))
    assert ClaudeCliChatGenerator()._command("s")[0] == str(shim)


def test_os_error_becomes_llm_call_error(fake_run):
    fake_run(exc=OSError("The command line is too long"))
    with pytest.raises(LLMCallError, match="cannot run claude CLI"):
        ClaudeCliChatGenerator().run(messages=MESSAGES)


def test_default_system_prompt_and_nonzero_exit_with_success_json(fake_run):
    calls = fake_run(stdout=cli_output(), returncode=1)
    with pytest.raises(LLMCallError):
        ClaudeCliChatGenerator().run(messages=[ChatMessage.from_user("hi")])
    assert calls["cmd"][calls["cmd"].index("--system-prompt") + 1] == claude_cli.DEFAULT_SYSTEM_PROMPT


def test_model_is_the_one_that_did_the_work_not_the_schema_side_call(fake_run):
    # Avec --json-schema, le CLI ajoute un petit appel Haiku, liste en premier dans modelUsage.
    fake_run(stdout=cli_output(modelUsage={"claude-haiku-4-5-20251001": {"costUSD": 0.0004},
                                           "claude-sonnet-4-6": {"costUSD": 0.006}}))
    reply = ClaudeCliChatGenerator(model="sonnet").run(messages=MESSAGES)["replies"][0]
    assert reply.meta["model"] == "claude-cli/claude-sonnet-4-6"


SEARCH = Tool(name="search_projects", description="Semantic search.",
              parameters={"type": "object", "properties": {"query": {"type": "string"}}, "required": ["query"]},
              function=lambda query: query)
LISTER = Tool(name="list_projects", description="Count projects.",
              parameters={"type": "object", "properties": {}}, function=lambda: "")


def tool_use_event(structured, tool_id="toolu_1"):
    """Evenement stream-json d'un appel de l'outil natif StructuredOutput (voir --output-format stream-json)."""
    return {"type": "assistant", "message": {"content": [
        {"type": "tool_use", "id": tool_id, "name": "StructuredOutput", "input": structured}]}}


def result_event(**overrides):
    """Evenement stream-json final `result` : memes champs que la sortie --output-format json."""
    data = {"type": "result", "is_error": False, "api_error_status": None, "result": "",
            "total_cost_usd": 0.0068, "duration_ms": 3500, "num_turns": 2, "session_id": "s1",
            "usage": {"input_tokens": 4272, "output_tokens": 427, "cache_read_input_tokens": 10,
                      "cache_creation_input_tokens": 0},
            "modelUsage": {"claude-sonnet-4-6": {"costUSD": 0.0068}}}
    data.update(overrides)
    return data


def stream_output(*events):
    return "\n".join(json.dumps(e) for e in events)


@pytest.fixture
def scripted_run(monkeypatch):
    """subprocess.run renvoie, pour chaque appel, un flux stream-json avec un seul appel StructuredOutput
    (le structured_output donne) suivi de l'evenement result."""
    calls = []

    def install(*outputs):
        queue = list(outputs)

        def run(cmd, **kw):
            calls.append({"cmd": cmd, "input": kw["input"]})
            structured = queue.pop(0)
            stdout = stream_output(tool_use_event(structured), result_event(structured_output=structured))
            return subprocess.CompletedProcess(cmd, 0, stdout=stdout, stderr="")
        monkeypatch.setattr(claude_cli.subprocess, "run", run)
        monkeypatch.setattr(claude_cli.shutil, "which", lambda exe: "C:/npm/claude.CMD")
        return calls
    return install


def test_tool_call_reply(scripted_run):
    calls = scripted_run({"tool_calls": [{"tool_name": "search_projects", "arguments": {"query": "santé"}}],
                          "answer": ""})
    reply = ClaudeCliChatGenerator().run(messages=MESSAGES, tools=[SEARCH, LISTER])["replies"][0]
    assert reply.text is None and reply.tool_calls[0].tool_name == "search_projects"
    assert reply.tool_calls[0].arguments == {"query": "santé"} and reply.tool_calls[0].id.startswith("call_")
    cmd = calls[0]["cmd"]
    assert cmd[cmd.index("--output-format") + 1] == "stream-json" and "--verbose" in cmd
    schema = json.loads(cmd[cmd.index("--json-schema") + 1])
    assert schema["properties"]["tool_calls"]["items"]["properties"]["tool_name"]["enum"] == \
        ["search_projects", "list_projects"]
    prompt = calls[0]["input"]
    assert "### search_projects" in prompt and "Semantic search." in prompt and '"query"' in prompt
    assert "## User\nHello\nworld" in prompt
    assert reply.meta["usage"]["prompt_tokens"] == 4272


def test_answer_reply(scripted_run):
    scripted_run({"tool_calls": [], "answer": "Bonjour [1]"})
    reply = ClaudeCliChatGenerator().run(messages=MESSAGES, tools=[SEARCH])["replies"][0]
    assert reply.text == "Bonjour [1]" and not reply.tool_calls


def test_second_structured_output_call_is_ignored(monkeypatch):
    """Le modele peut croire que StructuredOutput est une action qui s'execute, et le rappeler avec une reponse
    fabriquee une fois que le premier appel a "reussi" : seul le PREMIER appel est retenu."""
    first = {"tool_calls": [{"tool_name": "search_projects", "arguments": {"query": "x"}}], "answer": ""}
    fabricated = {"tool_calls": [], "answer": "13 projets TSI en Grece (invente)"}
    stdout = stream_output(tool_use_event(first, tool_id="t1"), tool_use_event(fabricated, tool_id="t2"),
                           result_event(structured_output=fabricated))

    def run(cmd, **kw):
        return subprocess.CompletedProcess(cmd, 0, stdout=stdout, stderr="")
    monkeypatch.setattr(claude_cli.subprocess, "run", run)
    monkeypatch.setattr(claude_cli.shutil, "which", lambda exe: "C:/npm/claude.CMD")
    reply = ClaudeCliChatGenerator().run(messages=MESSAGES, tools=[SEARCH])["replies"][0]
    assert reply.tool_calls and reply.tool_calls[0].tool_name == "search_projects"
    assert reply.text is None


def test_single_turn_stop_is_not_an_error(monkeypatch):
    """--max-turns 1 : le CLI s'arrete apres le premier StructuredOutput avec subtype error_max_turns."""
    first = {"tool_calls": [{"tool_name": "search_projects", "arguments": {"query": "x"}}], "answer": ""}
    stdout = stream_output(tool_use_event(first), result_event(is_error=True, subtype="error_max_turns"))
    calls = []

    def run(cmd, **kw):
        calls.append(cmd)
        return subprocess.CompletedProcess(cmd, 1, stdout=stdout, stderr="")
    monkeypatch.setattr(claude_cli.subprocess, "run", run)
    monkeypatch.setattr(claude_cli.shutil, "which", lambda exe: "C:/npm/claude.CMD")
    reply = ClaudeCliChatGenerator().run(messages=MESSAGES, tools=[SEARCH])["replies"][0]
    assert reply.tool_calls[0].tool_name == "search_projects"
    assert calls[0][calls[0].index("--max-turns") + 1] == "1"
    # sans appel structure, error_max_turns reste une erreur
    monkeypatch.setattr(claude_cli.subprocess, "run", lambda cmd, **kw: subprocess.CompletedProcess(
        cmd, 1, stdout=stream_output(result_event(is_error=True, subtype="error_max_turns")), stderr=""))
    with pytest.raises(LLMCallError):
        ClaudeCliChatGenerator().run(messages=MESSAGES, tools=[SEARCH])


def test_quota_error_in_tool_mode(monkeypatch):
    stdout = stream_output(result_event(is_error=True, result="Claude AI usage limit reached|1759230000"))
    monkeypatch.setattr(claude_cli.subprocess, "run",
                        lambda cmd, **kw: subprocess.CompletedProcess(cmd, 1, stdout=stdout, stderr=""))
    monkeypatch.setattr(claude_cli.shutil, "which", lambda exe: "C:/npm/claude.CMD")
    with pytest.raises(LLMQuotaExceeded):
        ClaudeCliChatGenerator().run(messages=MESSAGES, tools=[SEARCH])


def test_transcript_includes_tool_calls_and_results(scripted_run):
    call = ToolCall(tool_name="search_projects", arguments={"query": "x"}, id="c1")
    messages = [ChatMessage.from_system("Sys."), ChatMessage.from_user("Q"),
                ChatMessage.from_assistant(tool_calls=[call]), ChatMessage.from_tool("R1 text", origin=call),
                ChatMessage.from_assistant("Previous answer")]
    calls = scripted_run({"tool_calls": [], "answer": "ok"})
    ClaudeCliChatGenerator().run(messages=messages, tools=[SEARCH])
    prompt = calls[0]["input"]
    assert '## Assistant (tool calls)\n- search_projects {"query": "x"}' in prompt
    assert "## Tool result (search_projects)\nR1 text" in prompt
    assert "## Assistant\nPrevious answer" in prompt
    assert prompt.index("## User\nQ") < prompt.index("## Tool result")


def test_invalid_reply_is_retried_once_then_fails(scripted_run):
    calls = scripted_run({"tool_calls": [], "answer": ""}, {"tool_calls": [], "answer": "fine"})
    assert ClaudeCliChatGenerator().run(messages=MESSAGES, tools=[SEARCH])["replies"][0].text == "fine"
    assert len(calls) == 2
    scripted_run({"tool_calls": [{"tool_name": "unknown", "arguments": {}}], "answer": ""},
                 {"tool_calls": [], "answer": "  "})
    with pytest.raises(LLMCallError, match="neither"):
        ClaudeCliChatGenerator().run(messages=MESSAGES, tools=[SEARCH])


def test_without_tools_behaviour_is_unchanged(fake_run):
    calls = fake_run(stdout=cli_output(structured_output={"summary": "S"}))
    ClaudeCliChatGenerator(json_schema={"type": "object"}).run(messages=MESSAGES)
    assert calls["cmd"][calls["cmd"].index("--output-format") + 1] == "json"
    assert "--verbose" not in calls["cmd"]
    assert calls["input"] == "Hello\nworld"
    assert calls["cmd"][calls["cmd"].index("--json-schema") + 1] == '{"type":"object"}'


@pytest.mark.integration
def test_real_cli_answers_greece_question_via_list_projects_tool():
    """Appel reel du CLI claude (souscription) : question numerique -> doit demander list_projects, jamais
    repondre directement (regression du bug StructuredOutput/tool_use natif diagnostique en revue)."""
    tools = [
        Tool(name="search_projects", description="Semantic search in the TSI/SRSP project texts.",
             parameters={"type": "object",
                        "properties": {"query": {"type": "string"},
                                       "countries": {"type": "array", "items": {"type": "string"}}},
                        "required": ["query"]}, function=lambda **kw: ""),
        Tool(name="list_projects", description="Count and list distinct projects matching filters. Use it for "
                                               "any number or list, never estimate.",
             parameters={"type": "object",
                        "properties": {"countries": {"type": "array", "items": {"type": "string"}},
                                       "programme": {"type": "string"}}}, function=lambda **kw: ""),
    ]
    system = ("You only know what the tools return ; never invent facts. For any number, count or list of "
              "projects, call list_projects ; never estimate.")
    messages = [ChatMessage.from_system(system), ChatMessage.from_user("Combien de projets TSI en Grèce ?")]
    gen = ClaudeCliChatGenerator()
    reply = gen.run(messages=messages, tools=tools)["replies"][0]
    assert reply.tool_calls, f"pas d'appel d'outil, reponse directe : {reply.text!r}"
    call = next((c for c in reply.tool_calls if c.tool_name == "list_projects"), None)
    assert call is not None, f"pas d'appel a list_projects : {[c.tool_name for c in reply.tool_calls]}"

    messages += [ChatMessage.from_assistant(tool_calls=reply.tool_calls),
                ChatMessage.from_tool("Total matching projects: 20 (TSI: 20)", origin=call)]
    reply2 = gen.run(messages=messages, tools=tools)["replies"][0]
    assert not reply2.tool_calls, f"outil rappele au lieu de repondre : {reply2.tool_calls}"
    assert "20" in (reply2.text or ""), f"reponse sans le chiffre attendu : {reply2.text!r}"


def test_allowed_tools_enable_native_cli_tools(monkeypatch):
    gen = ClaudeCliChatGenerator(model="haiku", allowed_tools=["WebSearch"])
    monkeypatch.setattr(gen, "_resolve_executable", lambda: "claude")
    cmd = gen._command("sys")
    assert cmd[cmd.index("--tools") + 1] == "WebSearch"
    assert cmd[cmd.index("--allowedTools") + 1] == "WebSearch"
    plain = ClaudeCliChatGenerator(model="haiku")
    monkeypatch.setattr(plain, "_resolve_executable", lambda: "claude")
    assert plain._command("sys")[plain._command("sys").index("--tools") + 1] == ""
    assert "--allowedTools" not in plain._command("sys")
