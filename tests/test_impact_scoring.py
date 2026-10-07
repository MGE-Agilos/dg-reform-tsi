import json

import pytest

from test_chat_agent import ScriptedGenerator
from test_impact_report import META, REPORT, RUN, SOURCES
from tsi_agent import runlog
from tsi_agent.components.claude_cli import LLMCallError
from tsi_agent.config import Settings
from tsi_agent.impact.report import render_report
from tsi_agent.impact.scoring import (IMPACT_LEVEL_LABELS, SCORE_RUBRIC, SCORE_SCHEMA, ScoringError, apply_score,
                                      default_scorer, impact_level, score_report)
from tsi_agent.render import parse_front_matter


def reply(score, rationale="Adopted reform [3]."):
    return json.dumps({"impact_score": score, "impact_rationale": rationale})


def report_md():
    return render_report(META, REPORT, SOURCES, RUN)


def test_schema_and_rubric():
    assert SCORE_SCHEMA["required"] == ["impact_score", "impact_rationale"]
    assert SCORE_SCHEMA["properties"]["impact_score"] == {"type": "integer", "minimum": 0, "maximum": 10}
    for word in ("POSITIVE impact", "0-10", "9-10", "7-8", "4-6", "1-3", "[n]", "attribution"):
        assert word in SCORE_RUBRIC


def test_score_report_sends_rubric_and_body_without_front_matter(tmp_path):
    gen = ScriptedGenerator([reply(7)])
    body = parse_front_matter(report_md())[1]
    with runlog.open_run(tmp_path / "logs") as run:
        result = score_report(body, gen, project_id="hosp-hr")
    assert result == {"impact_score": 7, "impact_rationale": "Adopted reform [3]."}
    system, user = gen.received[0]
    assert system.text == SCORE_RUBRIC and user.text == body and "project_id:" not in user.text
    events = [json.loads(line) for line in run.path.read_text(encoding="utf-8").splitlines()]
    scored = [e for e in events if e["action"] == "impact_score"]
    assert scored and scored[0]["component"] == "ImpactScoring" and scored[0]["score"] == 7
    assert scored[0]["target"] == "hosp-hr"


def test_score_report_strips_front_matter_if_given():
    gen = ScriptedGenerator([reply(5)])
    score_report(report_md(), gen)
    assert not gen.received[0][1].text.startswith("---")


@pytest.mark.parametrize("value, expected", [(7, 7), ("8", 8), (6.6, 7), (12, 10), (-3, 0), (0, 0), (10, 10)])
def test_score_report_coerces_and_clamps(value, expected):
    assert score_report("# R", ScriptedGenerator([reply(value)]))["impact_score"] == expected


def test_score_report_accepts_fenced_json():
    assert score_report("# R", ScriptedGenerator(["```json\n" + reply(4) + "\n```"]))["impact_score"] == 4


@pytest.mark.parametrize("text", ["not json", "[1, 2]", json.dumps({"impact_rationale": "x"}),
                                  reply("high"), reply(None), reply(True), reply(float("nan")), ""])
def test_score_report_invalid_reply_raises(text):
    with pytest.raises(ScoringError):
        score_report("# R", ScriptedGenerator([text]))


def test_score_report_propagates_llm_errors():
    with pytest.raises(LLMCallError):
        score_report("# R", ScriptedGenerator([LLMCallError("timeout")]))


def test_impact_level_boundaries():
    assert [impact_level(s) for s in range(11)] == ["weak"] * 4 + ["moderate"] * 3 + ["strong"] * 4
    assert IMPACT_LEVEL_LABELS == {"strong": "Strong", "moderate": "Moderate", "weak": "Weak"}


def test_apply_score_front_matter_and_body_line():
    md = apply_score(report_md(), 7, "Arrears fell after the reform [3][9].")
    front, body = parse_front_matter(md)
    keys = list(front)
    i = keys.index("confidence")
    assert keys[i + 1:i + 4] == ["impact_score", "impact_level", "impact_rationale"]
    assert front["impact_score"] == 7 and front["impact_level"] == "strong"
    assert front["impact_rationale"] == "Arrears fell after the reform [3]."  # [9] absent des Sources
    verdict = "**Overall verdict:** Positive but partly attributable. (confidence: medium)"
    line = "**Positive impact:** 7/10 (Strong) — Arrears fell after the reform [3]."
    assert verdict + "\n\n" + line + "\n\n## 1. Project overview" in body
    assert apply_score(md, 7, "Arrears fell after the reform [3][9].") == md  # idempotent
    again = parse_front_matter(apply_score(md, 2, "Little uptake."))
    assert again[0]["impact_level"] == "weak" and again[1].count("**Positive impact:**") == 1
    assert "**Positive impact:** 2/10 (Weak) — Little uptake." in again[1]


def test_apply_score_replaces_old_scale():
    old = report_md().replace("confidence: medium\n",
                              "confidence: medium\nimpact_score: 1\nimpact_category: positive\nimpact_rationale: x\n")
    old = old.replace("(confidence: medium)\n\n",
                      "(confidence: medium)\n\n**Impact:** Positive (score +1 on a -2…+2 scale) — x\n\n")
    assert "**Impact:**" in old and "impact_category" in old
    front, body = parse_front_matter(apply_score(old, 9, "Clear outcomes [1]."))
    assert "impact_category" not in front and front["impact_score"] == 9 and front["impact_level"] == "strong"
    assert "**Impact:**" not in body and "**Positive impact:** 9/10 (Strong) — Clear outcomes [1]." in body


def test_apply_score_clamps_and_handles_empty_rationale():
    front, body = parse_front_matter(apply_score(report_md(), 15, "  "))
    assert front["impact_score"] == 10 and front["impact_rationale"] == ""
    assert "**Positive impact:** 10/10 (Strong)\n" in body


def test_default_scorer_uses_score_model():
    s = Settings(impact_score_model="sonnet", impact_llm_timeout_s=42)
    gen = default_scorer(s)
    assert gen.chat_generator.model == "sonnet" and gen.chat_generator.timeout_s == 42
    assert gen.chat_generator.json_schema == SCORE_SCHEMA
    assert Settings().impact_score_model == "opus"
