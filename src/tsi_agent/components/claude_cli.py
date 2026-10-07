"""Chat generator Haystack qui passe par le CLI `claude` (souscription Claude, sans clé API).

Même interface que les chat generators Haystack (`OpenAIChatGenerator`...) :
`run(messages) -> {"replies": [ChatMessage]}`, avec l'usage au format OpenAI dans `meta["usage"]`.
"""

import json
import re
import shutil
import subprocess
import uuid
from pathlib import Path
from typing import Any

from haystack import component
from haystack.dataclasses import ChatMessage, ChatRole, ToolCall
from haystack.tools import Tool, flatten_tools_or_toolsets

DEFAULT_SYSTEM_PROMPT = "You are a precise assistant. Follow the user's instructions exactly."
_QUOTA = re.compile(r"rate.?limit|usage limit|limit reached|quota|too many requests|\b429\b", re.IGNORECASE)
# Avec --json-schema, le CLI ne donne au modele qu'un seul outil natif, StructuredOutput : il ne peut pas
# appeler nos "outils" directement. On les lui presente comme des actions externes a demander via cet outil,
# et on insiste pour qu'il ne le rappelle pas une fois qu'il a "reussi" (le modele peut sinon croire que l'appel
# a produit un resultat, et rappeler StructuredOutput avec une reponse fabriquee).
TOOLS_INTRO = ("Your only callable tool in this session is StructuredOutput. The actions listed below are NOT "
               "tools you can call directly: an external program runs them for you. To request actions, call "
               "StructuredOutput with the actions in `tool_calls`; their results will be sent back to you in the "
               "next message. Actions, with their arguments as JSON schema:")
TOOLS_INSTRUCTION = ("Call StructuredOutput exactly once. If you need information, put the requested actions in "
                     "`tool_calls` and leave `answer` empty. Only when the conversation above already contains "
                     "the results you need, leave `tool_calls` empty and write the final answer in `answer`. "
                     "Never invent results.")


class LLMCallError(RuntimeError):
    pass


class LLMQuotaExceeded(LLMCallError):
    pass


@component
class ClaudeCliChatGenerator:
    def __init__(self, model: str = "sonnet", timeout_s: int = 180, json_schema: dict[str, Any] | None = None,
                 executable: str = "claude", allowed_tools: list[str] | None = None):
        self.model = model
        self.timeout_s = timeout_s
        self.json_schema = json_schema
        self.executable = executable
        self.allowed_tools = allowed_tools

    def _tools_args(self) -> list[str]:
        """Outils natifs du CLI : aucun par defaut ; `allowed_tools` (ex. ["WebSearch"]) les active sans demande."""
        if not self.allowed_tools:
            return ["--tools", ""]
        names = ",".join(self.allowed_tools)
        return ["--tools", names, "--allowedTools", names]

    def _resolve_executable(self) -> str:
        path = shutil.which(self.executable)
        if path is None:
            raise LLMCallError(f"'{self.executable}' introuvable dans le PATH")
        # Sur cette machine, `claude` resout vers un shim npm (.CMD/.BAT). Le lancer
        # passe par cmd.exe, qui developpe les %VAR% dans les arguments, reinterprete
        # les guillemets et limite la ligne de commande a 8191 caracteres. On appelle
        # directement l'executable natif quand il est present a cote du shim.
        if path.lower().endswith((".cmd", ".bat")):
            native = Path(path).parent / "node_modules" / "@anthropic-ai" / "claude-code" / "bin" / "claude.exe"
            if native.exists():
                return str(native)
        return path

    def _command(self, system_prompt: str) -> list[str]:
        exe = self._resolve_executable()
        cmd = [exe, "-p", "--output-format", "json", "--model", self.model,
               *self._tools_args(), "--strict-mcp-config", "--mcp-config", '{"mcpServers":{}}',
               "--setting-sources", "", "--no-session-persistence", "--system-prompt", system_prompt]
        if self.json_schema:
            cmd += ["--json-schema", json.dumps(self.json_schema, separators=(",", ":"))]
        return cmd

    def _command_for_tools(self, system_prompt: str, schema: dict[str, Any]) -> list[str]:
        # stream-json + --verbose : on doit lire les evenements assistant un par un pour retrouver le PREMIER
        # appel de l'outil natif StructuredOutput (voir TOOLS_INTRO). --output-format json ne donnerait que le
        # dernier appel, potentiellement une reponse fabriquee par le modele apres un premier appel correct.
        exe = self._resolve_executable()
        return [exe, "-p", "--output-format", "stream-json", "--verbose", "--model", self.model,
                *self._tools_args(), "--strict-mcp-config", "--mcp-config", '{"mcpServers":{}}',
                "--setting-sources", "", "--no-session-persistence", "--system-prompt", system_prompt,
                "--json-schema", json.dumps(schema, separators=(",", ":")),
                # Un seul tour : sans limite, le CLI continue apres le premier StructuredOutput et le modele
                # enchaine des tours inventes (jusqu'a ~4 min et ~13 000 tokens perdus par etape).
                "--max-turns", "1"]

    def _run_cli(self, cmd: list[str], prompt: str) -> subprocess.CompletedProcess:
        try:
            return subprocess.run(cmd, input=prompt, capture_output=True, text=True, encoding="utf-8",
                                  errors="replace", timeout=self.timeout_s)
        except subprocess.TimeoutExpired as exc:
            raise LLMCallError(f"timeout after {self.timeout_s}s") from exc
        except OSError as exc:
            raise LLMCallError(f"cannot run claude CLI: {exc}") from exc

    def _meta_from_result(self, result: dict[str, Any]) -> dict[str, Any]:
        usage = result.get("usage") or {}
        # Avec --json-schema, le CLI ajoute un petit appel Haiku, liste en premier : on retient
        # le modele qui a coute le plus, c'est-a-dire celui qui a fait l'extraction.
        model_usage = result.get("modelUsage") or {}
        model_used = max(model_usage, key=lambda m: (model_usage[m] or {}).get("costUSD") or 0, default=self.model)
        return {
            "model": f"claude-cli/{model_used}",
            "usage": {
                "prompt_tokens": int(usage.get("input_tokens") or 0),
                "completion_tokens": int(usage.get("output_tokens") or 0),
                "cache_read_input_tokens": int(usage.get("cache_read_input_tokens") or 0),
                "cache_creation_input_tokens": int(usage.get("cache_creation_input_tokens") or 0),
            },
            "cost_usd": result.get("total_cost_usd"),
            "duration_ms": result.get("duration_ms"),
            "num_turns": result.get("num_turns"),
            "session_id": result.get("session_id"),
        }

    @staticmethod
    def _raise_for_error(result: dict[str, Any], returncode: int, stderr: str) -> None:
        if result.get("is_error") or returncode != 0:
            message = str(result.get("result") or stderr or "unknown CLI error")[:500]
            if result.get("api_error_status") == 429 or _QUOTA.search(message):
                raise LLMQuotaExceeded(message)
            raise LLMCallError(message)

    def _invoke(self, system: str, prompt: str) -> tuple[dict[str, Any], dict]:
        proc = self._run_cli(self._command(system or DEFAULT_SYSTEM_PROMPT), prompt)
        try:
            data = json.loads(proc.stdout)
        except json.JSONDecodeError as exc:
            message = (proc.stderr or proc.stdout or "").strip()[:500]
            if _QUOTA.search(message):
                raise LLMQuotaExceeded(message) from exc
            raise LLMCallError(f"exit {proc.returncode}: {message}") from exc
        self._raise_for_error(data, proc.returncode, proc.stderr)
        return data, self._meta_from_result(data)

    def _invoke_tools(self, system: str, prompt: str, schema: dict[str, Any]) -> tuple[dict[str, Any] | None, dict]:
        proc = self._run_cli(self._command_for_tools(system or DEFAULT_SYSTEM_PROMPT, schema), prompt)
        events: list[dict[str, Any]] = []
        for line in proc.stdout.splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                events.append(json.loads(line))
            except json.JSONDecodeError:
                continue  # ligne non-JSON (rare en stream-json) : ignoree
        result = next((e for e in events if e.get("type") == "result"), None)
        if result is None:
            message = (proc.stderr or proc.stdout or "").strip()[:500]
            if _QUOTA.search(message):
                raise LLMQuotaExceeded(message)
            raise LLMCallError(f"exit {proc.returncode}: {message}")
        structured = None
        for event in events:
            if event.get("type") != "assistant":
                continue
            for block in event.get("message", {}).get("content") or []:
                if block.get("type") == "tool_use" and block.get("name") == "StructuredOutput":
                    structured = block.get("input")
                    break
            if structured is not None:
                break  # on ignore tout appel StructuredOutput suivant (reponse fabriquee)
        # --max-turns 1 : le CLI termine en "error_max_turns" une fois StructuredOutput appele ; ce n'est pas
        # une erreur quand l'appel structure est la.
        if not (structured is not None and result.get("subtype") == "error_max_turns"):
            self._raise_for_error(result, proc.returncode, proc.stderr)
        if structured is None:
            structured = result.get("structured_output")
        return structured, self._meta_from_result(result)

    @staticmethod
    def _system(messages: list[ChatMessage]) -> str:
        # Le prompt systeme passe en argument de ligne de commande : une seule ligne.
        return " ".join(" ".join(m.text.split()) for m in messages if m.is_from(ChatRole.SYSTEM) and m.text)

    @component.output_types(replies=list[ChatMessage])
    def run(self, messages: list[ChatMessage], tools: Any = None) -> dict[str, list[ChatMessage]]:
        if tools:
            return self._run_with_tools(messages, list(flatten_tools_or_toolsets(tools)))
        prompt = "\n\n".join(m.text for m in messages if not m.is_from(ChatRole.SYSTEM) and m.text)
        data, meta = self._invoke(self._system(messages), prompt)
        structured = data.get("structured_output")
        text = json.dumps(structured, ensure_ascii=False) if structured is not None else data.get("result", "")
        return {"replies": [ChatMessage.from_assistant(text, meta=meta)]}

    @staticmethod
    def _tools_schema(tools: list[Tool]) -> dict[str, Any]:
        call = {"type": "object", "properties": {"tool_name": {"type": "string", "enum": [t.name for t in tools]},
                                                 "arguments": {"type": "object"}},
                "required": ["tool_name", "arguments"]}
        return {"type": "object", "properties": {"tool_calls": {"type": "array", "items": call},
                                                 "answer": {"type": "string"}},
                "required": ["tool_calls", "answer"]}

    @staticmethod
    def _transcript(messages: list[ChatMessage]) -> str:
        parts: list[str] = []
        for m in messages:
            if m.is_from(ChatRole.SYSTEM):
                continue
            if m.is_from(ChatRole.USER):
                parts.append(f"## User\n{m.text or ''}")
            elif m.is_from(ChatRole.TOOL):
                for r in m.tool_call_results:
                    flag = " — ERROR" if r.error else ""
                    parts.append(f"## Tool result ({r.origin.tool_name}){flag}\n{r.result}")
            elif m.tool_calls:
                calls = "\n".join(f"- {c.tool_name} {json.dumps(c.arguments, ensure_ascii=False)}"
                                  for c in m.tool_calls)
                parts.append(f"## Assistant (tool calls)\n{calls}")
            else:
                parts.append(f"## Assistant\n{m.text or ''}")
        return "\n\n".join(parts)

    def _run_with_tools(self, messages: list[ChatMessage], tools: list[Tool]) -> dict[str, list[ChatMessage]]:
        """Le CLI n'appelle pas nos outils Python : on lui fait choisir, via son seul outil natif
        StructuredOutput, entre demander des actions et repondre (voir TOOLS_INTRO)."""
        names = {t.name for t in tools}
        described = "\n\n".join(f"### {t.name}\n{t.description}\nParameters: "
                                f"{json.dumps(t.parameters, ensure_ascii=False)}" for t in tools)
        prompt = (f"{TOOLS_INTRO}\n\n{described}\n\n# Conversation\n\n{self._transcript(messages)}\n\n"
                  f"{TOOLS_INSTRUCTION}")
        schema = self._tools_schema(tools)
        system = self._system(messages)
        for _ in range(2):  # une nouvelle tentative si la reponse n'est ni appel d'outil ni texte
            out, meta = self._invoke_tools(system, prompt, schema)
            out = out or {}
            calls = [c for c in out.get("tool_calls") or [] if isinstance(c, dict) and c.get("tool_name") in names]
            if calls:
                tool_calls = [ToolCall(tool_name=c["tool_name"], arguments=c.get("arguments") or {},
                                       id=f"call_{uuid.uuid4().hex[:12]}") for c in calls]
                return {"replies": [ChatMessage.from_assistant(tool_calls=tool_calls, meta=meta)]}
            answer = str(out.get("answer") or "").strip()
            if answer:
                return {"replies": [ChatMessage.from_assistant(answer, meta=meta)]}
        raise LLMCallError("CLI reply has neither tool_calls nor answer")
