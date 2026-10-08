"""Jeu de questions : une question realiste par morceau tire, redigee par un LLM (Haiku via le CLI claude)."""

import json
from pathlib import Path
from typing import Any

from haystack import Document
from haystack.dataclasses import ChatMessage

from tsi_agent import runlog
from tsi_agent.components.claude_cli import LLMQuotaExceeded
from tsi_agent.eval.sampling import stratum

QUESTION_MODEL = "haiku"
KEY_WORDS = 30
LANGUAGES = {"fr": "French", "en": "English"}
QUESTION_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {"questions": {"type": "array", "items": {
        "type": "object",
        "properties": {"chunk_ref": {"type": "integer"}, "question": {"type": "string"}},
        "required": ["chunk_ref", "question"]}}},
    "required": ["questions"],
}
SYSTEM_PROMPT = ("You write evaluation questions for a search engine over EU Technical Support Instrument (TSI) and "
                 "Structural Reform Support Programme (SRSP) projects and their deliverables.")
INSTRUCTIONS = """For each numbered passage below, write ONE question that a policy officer could realistically ask a chatbot about TSI/SRSP projects, and that this passage answers.

Rules:
- Write each question in the language stated for its passage (French or English).
- Do NOT copy distinctive phrases from the passage: paraphrase with your own words, no quotes, no exact figures copied as a search key.
- You may mention the country or the topic of the project, but do not repeat the exact title of the project or document more than needed.
- The question must be answerable from the passage alone, as a user would phrase it (one sentence, no "according to the passage").
- Return one item per passage, with `chunk_ref` set to the passage number."""


def question_lang(index: int) -> str:
    """Langue imposee : morceau pair -> francais, impair -> anglais (moitie-moitie, decide ici, pas par le modele)."""
    return "fr" if index % 2 == 0 else "en"


def default_question_generator(settings: Any) -> Any:
    from tsi_agent.chat.generator import LoggedChatGenerator
    from tsi_agent.components.claude_cli import ClaudeCliChatGenerator
    return LoggedChatGenerator(ClaudeCliChatGenerator(model=QUESTION_MODEL, json_schema=QUESTION_SCHEMA,
                                                      timeout_s=300), pricing=settings.pricing)


def _prompt(batch: list[tuple[int, Document]]) -> str:
    parts = [INSTRUCTIONS]
    for ref, (index, doc) in enumerate(batch, start=1):
        lang = LANGUAGES[question_lang(index)]
        parts.append(f"### Passage {ref} — write the question in {lang}\n"
                     f"Context: {doc.meta.get('context_header') or doc.meta.get('title') or ''}\n"
                     f"Section: {doc.meta.get('section') or ''}\n\n{doc.content or ''}")
    return "\n\n".join(parts)


def _parse(text: str) -> list[dict[str, Any]]:
    """Reponse du modele -> liste {chunk_ref, question} ; ValueError si ce n'est pas le JSON attendu."""
    start, end = text.find("{"), text.rfind("}")
    if start < 0 or end < start:
        raise ValueError("no JSON object in reply")
    data = json.loads(text[start:end + 1])
    items = data.get("questions") if isinstance(data, dict) else None
    if not isinstance(items, list):
        raise ValueError("reply has no `questions` list")
    return [i for i in items if isinstance(i, dict)]


def _record(index: int, doc: Document, question: str) -> dict[str, Any]:
    meta = doc.meta
    words = (doc.content or "").split()
    return {"id": f"q{index + 1:03d}", "question": question, "lang": question_lang(index), "chunk_id": doc.id,
            "doc_id": meta.get("doc_id"), "doc_type": meta.get("doc_type"), "stratum": stratum(doc),
            "projects": list(meta.get("projects") or []), "countries": list(meta.get("countries") or []),
            "ai_generated": bool(meta.get("ai_generated")), "title": meta.get("title"),
            "section": meta.get("section"), "page": meta.get("page"), "words": len(words),
            "key_text": " ".join(words[:KEY_WORDS])}


def generate_questions(chunks: list[Document], generator: Any, batch_size: int = 10) -> list[dict[str, Any]]:
    """Une question par morceau, par lots ; un lot illisible est retente une fois puis ignore (alerte au journal)."""
    indexed = list(enumerate(chunks))
    out: list[dict[str, Any]] = []
    for start in range(0, len(indexed), batch_size):
        batch = indexed[start:start + batch_size]
        messages = [ChatMessage.from_system(SYSTEM_PROMPT), ChatMessage.from_user(_prompt(batch))]
        items = None
        for attempt in range(2):
            try:
                reply = generator.run(messages=messages)["replies"][0]
                items = _parse(reply.text or "")
                break
            except LLMQuotaExceeded:
                raise
            except Exception as exc:  # reponse illisible ou erreur du CLI : une nouvelle tentative
                runlog.event("generate_questions", "alert", f"batch {start // batch_size + 1}", "warning",
                             message=f"question batch failed (attempt {attempt + 1}): {exc!r}"[:300])
        if items is None:
            continue
        by_ref: dict[int, str] = {}
        for item in items:
            ref, question = item.get("chunk_ref"), str(item.get("question") or "").strip()
            if isinstance(ref, int) and 1 <= ref <= len(batch) and question and ref not in by_ref:
                by_ref[ref] = question
        for ref in sorted(by_ref):
            index, doc = batch[ref - 1]
            out.append(_record(index, doc, by_ref[ref]))
    return out


def save_questions(questions: list[dict[str, Any]], path: str | Path) -> None:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("".join(json.dumps(q, ensure_ascii=False) + "\n" for q in questions), encoding="utf-8")


def load_questions(path: str | Path) -> list[dict[str, Any]]:
    return [json.loads(line) for line in Path(path).read_text(encoding="utf-8").splitlines() if line.strip()]
