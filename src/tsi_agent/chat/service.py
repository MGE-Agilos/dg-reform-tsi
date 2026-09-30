"""Une question au chatbot = un run journalise : historique -> Agent -> reponse, sources citees, etapes."""

import re
from typing import Any

from haystack import Pipeline
from haystack.dataclasses import ChatMessage, ChatRole

from tsi_agent import runlog

STEP_LIMIT_ANSWER = ("L'agent a atteint la limite d'etapes sans conclure. Reformulez ou precisez la question "
                     "(pays, annee, programme).")
_REF = re.compile(r"\[(\d+)\]")


def find_cause(exc: BaseException | None, cls: type) -> BaseException | None:
    """Premiere exception de type `cls` dans la chaine (__cause__ / __context__) : l'Agent et le Pipeline enveloppent."""
    seen: set[int] = set()
    while exc is not None and id(exc) not in seen:
        if isinstance(exc, cls):
            return exc
        seen.add(id(exc))
        exc = exc.__cause__ or exc.__context__
    return None


def root_cause(exc: BaseException) -> BaseException:
    """Exception la plus profonde de la chaine (__cause__ / __context__) : deroule les enveloppes du Pipeline/Agent."""
    seen: set[int] = set()
    current = exc
    while True:
        nxt = current.__cause__ or current.__context__
        if nxt is None or id(nxt) in seen:
            return current
        seen.add(id(nxt))
        current = nxt


def cited_sources(answer: str, sources: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Sources citees [n] dans la reponse, dans l'ordre des numeros ; a defaut, toutes (une par projet)."""
    cited = {int(n) for n in _REF.findall(answer)}
    picked = [s for s in sources if s.get("ref") in cited]
    if picked:
        return sorted(picked, key=lambda s: s["ref"])
    unique: dict[Any, dict[str, Any]] = {}
    for s in sources:
        unique.setdefault(s.get("doc_id"), s)
    return list(unique.values())


def extract_steps(messages: list[ChatMessage]) -> list[dict[str, Any]]:
    results = {r.origin.id: r for m in messages if m.is_from(ChatRole.TOOL) for r in m.tool_call_results}
    steps = []
    for m in messages:
        for call in m.tool_calls if m.is_from(ChatRole.ASSISTANT) else []:
            r = results.get(call.id)
            text = str(r.result) if r else ""
            steps.append({"tool": call.tool_name, "arguments": call.arguments,
                          "summary": text.splitlines()[0][:200] if text else "", "error": bool(r and r.error)})
    return steps


def _history_messages(history: list[dict[str, str]]) -> list[ChatMessage]:
    messages = []
    for turn in history:
        text = str(turn.get("text") or "")
        if turn.get("role") == "user":
            messages.append(ChatMessage.from_user(text))
        elif turn.get("role") == "assistant":
            messages.append(ChatMessage.from_assistant(text))
    return messages


def ask(pipeline: Pipeline, question: str, history: list[dict[str, str]], *, logs_dir: str) -> dict[str, Any]:
    messages = _history_messages(history) + [ChatMessage.from_user(question)]
    with runlog.open_run(logs_dir) as run:
        runlog.event("ChatService", "question", None, text=question, history_turns=len(history))
        out = pipeline.run({"agent": {"messages": messages}})["agent"]
        last = out.get("last_message")
        ok = last is not None and last.is_from(ChatRole.ASSISTANT) and bool(last.text) and not last.tool_calls
        answer = last.text if ok else STEP_LIMIT_ANSWER
        sources = cited_sources(answer, out.get("sources") or []) if ok else []
        steps = extract_steps(out.get("messages") or [])
        runlog.event("ChatService", "answer", None, "ok" if ok else "step_limit", chars=len(answer),
                     sources=len(sources), steps=len(steps), exit_reason=out.get("exit_reason"))
        run.extra.update({"mode": "chat", "question": question, "steps": len(steps)})
    summary = run.final_summary
    return {"answer": answer, "sources": sources, "steps": steps, "duration_s": summary["duration_s"],
            "cost_usd": summary["cost_usd"], "run_id": run.run_id}
