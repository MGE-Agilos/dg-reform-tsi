"""Chat generator Haystack qui passe par le CLI `claude` (souscription Claude, sans clé API).

Même interface que les chat generators Haystack (`OpenAIChatGenerator`...) :
`run(messages) -> {"replies": [ChatMessage]}`, avec l'usage au format OpenAI dans `meta["usage"]`.
"""

import json
import re
import shutil
import subprocess
from pathlib import Path
from typing import Any

from haystack import component
from haystack.dataclasses import ChatMessage, ChatRole

DEFAULT_SYSTEM_PROMPT = "You are a precise assistant. Follow the user's instructions exactly."
_QUOTA = re.compile(r"rate.?limit|usage limit|limit reached|quota|too many requests|\b429\b", re.IGNORECASE)


class LLMCallError(RuntimeError):
    pass


class LLMQuotaExceeded(LLMCallError):
    pass


@component
class ClaudeCliChatGenerator:
    def __init__(self, model: str = "sonnet", timeout_s: int = 180, json_schema: dict[str, Any] | None = None,
                 executable: str = "claude"):
        self.model = model
        self.timeout_s = timeout_s
        self.json_schema = json_schema
        self.executable = executable

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
               "--tools", "", "--strict-mcp-config", "--mcp-config", '{"mcpServers":{}}',
               "--setting-sources", "", "--no-session-persistence", "--system-prompt", system_prompt]
        if self.json_schema:
            cmd += ["--json-schema", json.dumps(self.json_schema, separators=(",", ":"))]
        return cmd

    @component.output_types(replies=list[ChatMessage])
    def run(self, messages: list[ChatMessage]) -> dict[str, list[ChatMessage]]:
        # Le prompt système passe en argument (via cmd.exe sous Windows) : une seule ligne.
        system = " ".join(" ".join(m.text.split()) for m in messages if m.is_from(ChatRole.SYSTEM) and m.text)
        prompt = "\n\n".join(m.text for m in messages if not m.is_from(ChatRole.SYSTEM) and m.text)
        try:
            proc = subprocess.run(self._command(system or DEFAULT_SYSTEM_PROMPT), input=prompt,
                                  capture_output=True, text=True, encoding="utf-8", errors="replace",
                                  timeout=self.timeout_s)
        except subprocess.TimeoutExpired as exc:
            raise LLMCallError(f"timeout after {self.timeout_s}s") from exc
        except OSError as exc:
            raise LLMCallError(f"cannot run claude CLI: {exc}") from exc
        try:
            data = json.loads(proc.stdout)
        except json.JSONDecodeError as exc:
            message = (proc.stderr or proc.stdout or "").strip()[:500]
            if _QUOTA.search(message):
                raise LLMQuotaExceeded(message) from exc
            raise LLMCallError(f"exit {proc.returncode}: {message}") from exc
        if data.get("is_error") or proc.returncode != 0:
            message = str(data.get("result") or proc.stderr or "unknown CLI error")[:500]
            if data.get("api_error_status") == 429 or _QUOTA.search(message):
                raise LLMQuotaExceeded(message)
            raise LLMCallError(message)
        structured = data.get("structured_output")
        text = json.dumps(structured, ensure_ascii=False) if structured is not None else data.get("result", "")
        usage = data.get("usage") or {}
        # Avec --json-schema, le CLI ajoute un petit appel Haiku, liste en premier : on retient
        # le modele qui a coute le plus, c'est-a-dire celui qui a fait l'extraction.
        model_usage = data.get("modelUsage") or {}
        model_used = max(model_usage, key=lambda m: (model_usage[m] or {}).get("costUSD") or 0, default=self.model)
        meta = {
            "model": f"claude-cli/{model_used}",
            "usage": {
                "prompt_tokens": int(usage.get("input_tokens") or 0),
                "completion_tokens": int(usage.get("output_tokens") or 0),
                "cache_read_input_tokens": int(usage.get("cache_read_input_tokens") or 0),
                "cache_creation_input_tokens": int(usage.get("cache_creation_input_tokens") or 0),
            },
            "cost_usd": data.get("total_cost_usd"),
            "duration_ms": data.get("duration_ms"),
            "num_turns": data.get("num_turns"),
            "session_id": data.get("session_id"),
        }
        return {"replies": [ChatMessage.from_assistant(text, meta=meta)]}
