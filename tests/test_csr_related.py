import json

import pytest
from haystack import Document
from haystack.dataclasses import ChatMessage

from tsi_agent import __main__ as cli
from tsi_agent.components.claude_cli import LLMCallError, LLMQuotaExceeded
from tsi_agent.csr_related import (RELATED_SCHEMA, KeywordCandidates, Recommendation, VectorCandidates, build_prompt,
                                   input_hash, load_recommendations, load_related_rows, parse_related,
                                   recommendation_text, run_related, sentences)

CSR_MD = """---
csr_id: {csr_id}
title: Lithuania {year} CSR {number}
countries:
- {country}
year: {year}
number: '{number}'
csr_level: recommendation
stage: Commission recommendation
---

# Lithuania {year} CSR {number}

{text}

Commission recommendation of 2026 (not yet adopted by the Council). Not yet assessed.
"""


def write_csr(out, country, year, number, text):
    (out / "csr").mkdir(parents=True, exist_ok=True)
    csr_id = f"csr-{country}-{year}-{number}"
    (out / "csr" / f"{csr_id}.md").write_text(CSR_MD.format(csr_id=csr_id, country=country, year=year,
                                                            number=number, text=text), encoding="utf-8")


def rec(csr_id="csr-LT-2026-1", text="Improve tax compliance and the pension system."):
    country, year, number = csr_id.split("-")[1], int(csr_id.split("-")[2]), csr_id.split("-")[3]
    return Recommendation(csr_id, country, year, number, f"Lithuania {year} CSR {number}", text)


def proj(pid, countries=("LT",), year=2022, title=None, summary=""):
    return {"project_id": pid, "title": title or pid, "year": year, "programme": "TSI", "countries": list(countries),
            "policy_area": "revenue-administration-and-public-financial-management", "summary": summary}


def test_load_recommendations_year_country_and_order(tmp_path):
    write_csr(tmp_path, "LT", 2026, "10", "Ten.")
    write_csr(tmp_path, "LT", 2026, "2", "Strengthen the pension system.")
    write_csr(tmp_path, "LV", 2026, "1", "Latvia text.")
    write_csr(tmp_path, "LT", 2025, "1", "Older.")
    write_csr(tmp_path, "LT", 2026, "2.1", "A sub-part.")
    (tmp_path / "csr" / "csrdoc-LT-2026.md").write_text("---\ndoc_id: csrdoc-LT-2026\n---\n", encoding="utf-8")
    recs = load_recommendations(tmp_path, 2026, ["LT"])
    assert [r.csr_id for r in recs] == ["csr-LT-2026-2", "csr-LT-2026-10"]
    assert recs[0].text == "Strengthen the pension system." and recs[0].country == "LT"
    assert [r.csr_id for r in load_recommendations(tmp_path, 2026)] == [
        "csr-LT-2026-2", "csr-LT-2026-10", "csr-LV-2026-1"]


def test_recommendation_text_and_sentences():
    assert recommendation_text("# Title\n\nFirst   para\nline two.\n\nSecond.") == "First para line two."
    text = ("Take action to control net expenditure growth. Reinforce defence spending and readiness efficiently; "
            "Broaden general government revenue by improving tax compliance. Short.")
    assert sentences(text) == ["Take action to control net expenditure growth.",
                               "Reinforce defence spending and readiness efficiently;",
                               "Broaden general government revenue by improving tax compliance."]


def test_input_hash_depends_on_text_only():
    assert input_hash(rec()) == input_hash(rec())
    assert input_hash(rec()) != input_hash(rec(text="Other text."))


def test_keyword_candidates_split_by_country_and_skip_later_years():
    projects = [proj("lt-tax", title="Tax compliance in Lithuania", summary="tax compliance risk"),
                proj("lt-roads", title="Road maintenance"),
                proj("lv-tax", countries=("LV",), title="Tax compliance Latvia"),
                proj("lt-future", year=2030, title="Tax compliance")]
    finder = KeywordCandidates(projects, max_same=5, max_other=5)
    same, other = finder.find(rec(text="Improve tax compliance and simplify special tax regimes."))
    assert same == ["lt-tax"] and other == ["lv-tax"]


class FakeEmbedder:
    def __init__(self):
        self.texts = []

    def run(self, text):
        self.texts.append(text)
        return {"embedding": [0.1, 0.2]}


class FakeRetriever:
    """Renvoie des morceaux ; le filtre pays (meta.countries) est applique comme Qdrant."""

    def __init__(self, docs):
        self.docs = docs
        self.calls = []

    def run(self, query_embedding, filters, top_k):
        self.calls.append((filters, top_k))
        wanted = None
        for cond in filters["conditions"]:
            if cond["field"] == "meta.countries":
                wanted = set(cond["value"])
        return {"documents": [d for d in self.docs if wanted is None or wanted & set(d.meta["countries"])]}


def test_vector_candidates_best_score_per_project_same_and_other():
    docs = [Document(content="x", meta={"doc_id": "lt-a", "countries": ["LT"], "doc_type": "project"}, score=0.8),
            Document(content="x", meta={"doc_id": "lt-a", "countries": ["LT"], "doc_type": "project"}, score=0.9),
            Document(content="x", meta={"doc_id": "lt-b", "countries": ["LT", "LV"], "doc_type": "project"},
                     score=0.85),
            Document(content="x", meta={"doc_id": "lv-c", "countries": ["LV"], "doc_type": "project"}, score=0.7),
            Document(content="x", meta={"doc_id": "unknown", "countries": ["LV"], "doc_type": "project"}, score=1)]
    projects = [proj("lt-a"), proj("lt-b", countries=("LT", "LV")), proj("lv-c", countries=("LV",))]
    embedder, retriever = FakeEmbedder(), FakeRetriever(docs)
    same, other = VectorCandidates(embedder, retriever, projects).find(
        rec(text="Control net expenditure growth this year. Improve tax compliance and the pension system."))
    assert same == ["lt-a", "lt-b"] and other == ["lv-c"]
    assert len(embedder.texts) == 6  # texte entier + 2 phrases, pour chacune des deux recherches
    assert all(f["conditions"][0] == {"field": "meta.doc_type", "operator": "==", "value": "project"}
               for f, _ in retriever.calls)


def test_schema_and_prompt():
    assert RELATED_SCHEMA["properties"]["related"]["items"]["required"] == ["project_id", "relevance", "scope",
                                                                            "rationale"]
    prompt = build_prompt(rec(), [proj("lt-a", summary="Helped the tax authority.")],
                          [proj("lv-c", countries=("LV",))])
    assert "## Candidate projects in Lithuania (scope same_country)" in prompt
    assert "- lt-a — lt-a (TSI 2022; Lithuania; revenue-administration" in prompt and "Helped the tax" in prompt
    assert "- lv-c — lv-c (TSI 2022; Latvia;" in prompt and "Improve tax compliance" in prompt


def test_parse_related_filters_and_forces_scope():
    reply = json.dumps({"related": [
        {"project_id": "lt-a", "relevance": "high", "scope": "other_country",
         "rationale": "First sentence. Second one!  Third is dropped."},
        {"project_id": "lt-a", "relevance": "medium", "scope": "same_country", "rationale": "dup"},
        {"project_id": "lv-c", "relevance": "low", "scope": "other_country", "rationale": "low"},
        {"project_id": "invented", "relevance": "high", "scope": "same_country", "rationale": "x"},
        {"project_id": "lv-d", "relevance": "MEDIUM", "scope": "same_country", "rationale": "ok"}]})
    out = parse_related(reply, ["lt-a"], ["lv-c", "lv-d"])
    assert out == [{"project_id": "lt-a", "relevance": "high", "scope": "same_country",
                    "rationale": "First sentence. Second one!"},
                   {"project_id": "lv-d", "relevance": "medium", "scope": "other_country", "rationale": "ok"}]
    with pytest.raises(ValueError):
        parse_related("no json", [], [])
    with pytest.raises(ValueError):
        parse_related('{"other": 1}', [], [])


class Gen:
    def __init__(self, replies):
        self.replies = list(replies)
        self.calls = []

    def run(self, messages):
        self.calls.append(messages)
        reply = self.replies.pop(0) if len(self.replies) > 1 else self.replies[0]
        if isinstance(reply, Exception):
            raise reply
        return {"replies": [ChatMessage.from_assistant(reply, meta={"model": "claude-cli/sonnet"})]}


class Finder:
    source = "fake"

    def __init__(self, result=(["lt-a"], ["lv-c"])):
        self.result = result
        self.calls = []

    def find(self, r):
        self.calls.append(r.csr_id)
        return self.result


PROJECTS = [proj("lt-a"), proj("lv-c", countries=("LV",))]
OK = json.dumps({"related": [{"project_id": "lt-a", "relevance": "high", "scope": "same_country",
                              "rationale": "Same reform."}]})


def test_run_related_writes_cache_and_rerun_skips(tmp_path):
    recs = [rec("csr-LT-2026-1"), rec("csr-LT-2026-2")]
    finder, gen = Finder(), Gen([OK])
    summary = run_related(tmp_path, recs, PROJECTS, gen, lambda: finder, log=lambda s: None)
    assert summary["done"] == ["csr-LT-2026-1", "csr-LT-2026-2"] and summary["related"] == 2
    record = json.loads((tmp_path / "csr_related" / "csr-LT-2026-1.json").read_text(encoding="utf-8"))
    assert record["status"] == "ok" and record["input_hash"] == input_hash(recs[0])
    assert record["candidates"] == {"same_country": ["lt-a"], "other_country": ["lv-c"]}
    assert record["related"][0]["project_id"] == "lt-a" and record["model"] == "claude-cli/sonnet"

    def no_finder():
        raise AssertionError("finder built although everything is cached")
    again = run_related(tmp_path, recs, PROJECTS, Gen([OK]), no_finder, log=lambda s: None)
    assert again["cached"] == 2 and again["done"] == []
    changed = run_related(tmp_path, [rec("csr-LT-2026-1", text="New text.")], PROJECTS, Gen([OK]),
                          lambda: Finder(), log=lambda s: None)
    assert changed["done"] == ["csr-LT-2026-1"]
    redo = run_related(tmp_path, recs[1:], PROJECTS, Gen([OK]), lambda: Finder(), redo=True, log=lambda s: None)
    assert redo["done"] == ["csr-LT-2026-2"]


def test_run_related_quota_stops_and_resumes(tmp_path):
    recs = [rec("csr-LT-2026-1"), rec("csr-LT-2026-2"), rec("csr-LT-2026-3")]
    wrapped = LLMCallError("wrapper")
    wrapped.__cause__ = LLMQuotaExceeded("usage limit")
    summary = run_related(tmp_path, recs, PROJECTS, Gen([OK, wrapped]), lambda: Finder(), log=lambda s: None)
    assert summary["quota_exceeded"] and summary["done"] == ["csr-LT-2026-1"]
    assert summary["remaining"] == ["csr-LT-2026-2", "csr-LT-2026-3"]
    assert not (tmp_path / "csr_related" / "csr-LT-2026-2.json").exists()
    resumed = run_related(tmp_path, recs, PROJECTS, Gen([OK]), lambda: Finder(), log=lambda s: None)
    assert resumed["cached"] == 1 and resumed["done"] == ["csr-LT-2026-2", "csr-LT-2026-3"]


def test_run_related_limit_bad_reply_and_no_candidates(tmp_path):
    recs = [rec("csr-LT-2026-1"), rec("csr-LT-2026-2"), rec("csr-LT-2026-3")]
    gen = Gen(["not json", "still not json"])
    summary = run_related(tmp_path, recs, PROJECTS, gen, lambda: Finder(), limit=1, log=lambda s: None)
    assert len(gen.calls) == 2 and summary["failed"][0]["csr_id"] == "csr-LT-2026-1"
    assert summary["remaining"] == ["csr-LT-2026-2", "csr-LT-2026-3"]
    assert not (tmp_path / "csr_related" / "csr-LT-2026-1.json").exists()
    empty = run_related(tmp_path, recs[:1], PROJECTS, Gen([OK]), lambda: Finder(([], [])), log=lambda s: None)
    assert empty["no_candidates"] == ["csr-LT-2026-1"]
    record = json.loads((tmp_path / "csr_related" / "csr-LT-2026-1.json").read_text(encoding="utf-8"))
    assert record["status"] == "no_candidates" and record["related"] == []


def test_run_related_cli_error_fails_that_recommendation_only(tmp_path):
    recs = [rec("csr-LT-2026-1"), rec("csr-LT-2026-2")]
    summary = run_related(tmp_path, recs, PROJECTS, Gen([LLMCallError("timeout after 180s"), OK]), lambda: Finder(),
                          log=lambda s: None)
    assert summary["failed"][0]["csr_id"] == "csr-LT-2026-1" and summary["done"] == ["csr-LT-2026-2"]


def test_load_related_rows(tmp_path):
    d = tmp_path / "csr_related"
    d.mkdir()
    (d / "csr-LT-2026-1.json").write_text(json.dumps({"csr_id": "csr-LT-2026-1", "status": "ok", "related": [
        {"project_id": "a", "relevance": "high", "scope": "same_country", "rationale": "R."},
        {"project_id": "a", "relevance": "high", "scope": "same_country", "rationale": "dup"},
        {"project_id": "b", "relevance": "low", "scope": "same_country", "rationale": "x"}]}), encoding="utf-8")
    (d / "csr-LT-2026-2.json").write_text(json.dumps({"csr_id": "csr-LT-2026-2", "status": "no_candidates",
                                                      "related": []}), encoding="utf-8")
    (d / "broken.json").write_text("{", encoding="utf-8")
    assert load_related_rows(tmp_path) == [{"csr_id": "csr-LT-2026-1", "project_id": "a", "relevance": "high",
                                            "scope": "same_country", "rationale": "R."}]


def _cli_setup(tmp_path):
    out = tmp_path / "output"
    write_csr(out, "LT", 2026, "1", "Improve tax compliance and the pension system.")
    (out / "projects").mkdir()
    (out / "projects" / "lt-tax.md").write_text(
        "---\nproject_id: lt-tax\ntitle: Tax compliance and pension reform\nyear: 2022\ncountries:\n- LT\n---\n\n"
        "# Tax compliance\n\nImprove tax compliance.\n", encoding="utf-8")
    settings = tmp_path / "settings.yaml"
    settings.write_text(f"output_dir: {out.as_posix()}\nlogs_dir: {(tmp_path / 'logs').as_posix()}\n",
                        encoding="utf-8")
    return out, settings


def test_cli_csr_related_keyword_only(tmp_path, capsys, monkeypatch):
    out, settings = _cli_setup(tmp_path)
    import tsi_agent.components.claude_cli as claude_cli
    seen = {}

    class FakeCli:
        def __init__(self, model, timeout_s, json_schema):
            seen.update(model=model, schema=json_schema)

        def run(self, messages):
            return {"replies": [ChatMessage.from_assistant(json.dumps({"related": [
                {"project_id": "lt-tax", "relevance": "high", "scope": "same_country", "rationale": "Same."}]}),
                meta={"model": "claude-cli/sonnet", "cost_usd": 0.01})]}
    monkeypatch.setattr(claude_cli, "ClaudeCliChatGenerator", FakeCli)
    monkeypatch.setattr(cli, "_claude_cli_missing", lambda: False)
    assert cli.main(["--settings", str(settings), "csr-related", "--keyword-only", "--country", "Lithuania"]) == 0
    summary = json.loads(capsys.readouterr().out)
    assert summary["done"] == ["csr-LT-2026-1"] and summary["candidates_source"] == "keyword"
    assert seen["model"] == "sonnet" and seen["schema"] == RELATED_SCHEMA
    assert (out / "csr_related" / "csr-LT-2026-1.json").exists()


def test_cli_csr_related_without_recommendations_is_config_error(tmp_path, capsys):
    _, settings = _cli_setup(tmp_path)
    assert cli.main(["--settings", str(settings), "csr-related", "--year", "2019"]) == 2
    assert "csr-build" in capsys.readouterr().err
