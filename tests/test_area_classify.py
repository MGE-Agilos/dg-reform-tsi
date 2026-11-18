import json

from haystack.dataclasses import ChatMessage

from tsi_agent.area_classify import (AREAS, RECORDS_FILE, SCHEMA, UNCLEAR, apply_area, build_prompt, eligible,
                                     parse_reply, project_input, run_classification)
from tsi_agent.components.claude_cli import LLMQuotaExceeded
from tsi_agent.config import Settings
from tsi_agent.render import parse_front_matter, render


class FakeGenerator:
    def __init__(self, replies):
        self.replies = list(replies)
        self.prompts = []

    def run(self, messages):
        self.prompts.append(messages[-1].text)
        reply = self.replies.pop(0)
        if isinstance(reply, Exception):
            raise reply
        if callable(reply):
            reply = reply(messages[-1].text)
        return {"replies": [ChatMessage.from_assistant(json.dumps(reply), meta={"model": "claude-cli/sonnet"})]}


def project(pid, title, area=None, source=None, detail=False, body="# T\n\n## Sources\n\n- u"):
    fm = {"project_id": pid, "title": title, "programme": "TSI", "year": 2024, "countries": ["HR"],
          "policy_area": area, "detail_page": detail}
    if source:
        fm["policy_area_source"] = source
    return render(fm, body)


def setup(tmp_path, files):
    (tmp_path / "projects").mkdir()
    for pid, text in files.items():
        (tmp_path / "projects" / f"{pid}.md").write_text(text, encoding="utf-8")
    return Settings(output_dir=str(tmp_path), logs_dir=str(tmp_path / "logs"), max_concurrency=1)


def answer_all(area):
    def reply(prompt):
        ids = [line.split("id ", 1)[1].split(":", 1)[0] for line in prompt.splitlines() if line.startswith("- id ")]
        return {"items": [{"id": pid, "policy_area": area} for pid in ids]}
    return reply


def test_schema_lists_the_site_areas_and_unclear():
    enum = SCHEMA["properties"]["items"]["items"]["properties"]["policy_area"]["enum"]
    assert len(AREAS) == 10 and enum == [*AREAS, UNCLEAR]
    assert "green-transition" in build_prompt([("p", "Title: x")]) and "- id p: Title: x" in build_prompt([("p", "Title: x")])


def test_eligible_only_without_official_area():
    assert eligible({"policy_area": None})
    assert eligible({"policy_area": "competitiveness", "policy_area_source": "ai"})
    assert not eligible({"policy_area": "competitiveness"})


def test_project_input_titles_and_summary():
    fm = {"title": "Wildfires", "listing_titles": ["Wildfires", "Rural fires"], "programme": "TSI", "year": 2026,
          "countries": ["ES", "IT"], "detail_page": True}
    text = project_input(fm, "Intro.\n\n## Summary (AI-generated)\n\nPrevent rural fires.")
    assert text == "Title: Wildfires | also listed as: Rural fires | TSI 2026 | Spain, Italy | summary: Prevent rural fires."
    assert "summary" not in project_input({**fm, "detail_page": False}, "")


def test_parse_reply_keeps_known_ids_and_areas():
    raw = {"items": [{"id": "a", "policy_area": "green-transition"}, {"id": "a", "policy_area": "competitiveness"},
                     {"id": "b", "policy_area": "space"}, {"id": "z", "policy_area": "green-transition"},
                     {"id": "c", "policy_area": UNCLEAR}]}
    assert parse_reply(json.dumps(raw), {"a", "b", "c"}) == {"a": "green-transition", "c": UNCLEAR}


def test_apply_area_sets_replaces_and_removes_but_never_touches_official():
    thin = project("p", "Wildfires")
    out = apply_area(thin, {"policy_area": "green-transition"})
    fm, _ = parse_front_matter(out)
    assert fm["policy_area"] == "green-transition" and fm["policy_area_source"] == "ai"
    keys = list(fm)
    assert keys.index("policy_area_source") == keys.index("policy_area") + 1
    fm2, _ = parse_front_matter(apply_area(out, {"policy_area": UNCLEAR}))
    assert fm2["policy_area"] is None and "policy_area_source" not in fm2
    official = project("q", "Tax", area="revenue-administration-and-public-financial-management")
    assert apply_area(official, {"policy_area": "green-transition"}) == official
    assert apply_area(out, {"policy_area": "green-transition"}) == out  # idempotent


def test_run_classifies_caches_and_reapplies(tmp_path):
    settings = setup(tmp_path, {"a": project("a", "Wildfire prevention"), "b": project("b", "Tax reform"),
                                "c": project("c", "Official", area="competitiveness")})
    gen = FakeGenerator([answer_all("green-transition")])
    s = run_classification(settings, gen)
    assert s["eligible"] == 2 and s["classified"] == 2 and s["files_updated"] == 2 and len(gen.prompts) == 1
    assert "Official" not in gen.prompts[0]
    records = json.loads((tmp_path / RECORDS_FILE).read_text(encoding="utf-8"))
    assert records["a"]["policy_area"] == "green-transition" and records["a"]["model"] == "claude-cli/sonnet"
    # `run` reecrit le front matter d'un projet thin sans le domaine : rejoue depuis le cache, sans LLM
    (tmp_path / "projects" / "a.md").write_text(project("a", "Wildfire prevention"), encoding="utf-8")
    s = run_classification(settings, FakeGenerator([]))
    assert s["cached"] == 2 and s["files_updated"] == 1
    fm, _ = parse_front_matter((tmp_path / "projects" / "a.md").read_text(encoding="utf-8"))
    assert fm["policy_area"] == "green-transition" and fm["policy_area_source"] == "ai"
    # titre change : redemande
    (tmp_path / "projects" / "b.md").write_text(project("b", "Customs reform"), encoding="utf-8")
    gen = FakeGenerator([answer_all("revenue-administration-and-public-financial-management")])
    s = run_classification(settings, gen)
    assert s["classified"] == 1 and "Customs reform" in gen.prompts[0] and "Wildfire" not in gen.prompts[0]


def test_quota_stops_and_missing_items_are_retried(tmp_path):
    settings = setup(tmp_path, {"a": project("a", "A"), "b": project("b", "B")})
    s = run_classification(settings, FakeGenerator([LLMQuotaExceeded("limit")]))
    assert s["quota_exceeded"] and s["classified"] == 0
    gen = FakeGenerator([{"items": [{"id": "a", "policy_area": "competitiveness"}]}])
    s = run_classification(settings, gen)
    assert s["classified"] == 1 and s["failed"] == 1
    gen = FakeGenerator([answer_all(UNCLEAR)])
    s = run_classification(settings, gen)
    assert s["unclear"] == 1 and "- id b:" in gen.prompts[0] and "- id a:" not in gen.prompts[0]
