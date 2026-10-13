import json

from haystack.dataclasses import ChatMessage

from tsi_agent.components.claude_cli import LLMQuotaExceeded
from tsi_agent.config import Settings
from tsi_agent.csr import build_csrs
from tsi_agent.csr_link import (LINK_SCHEMA, SECTION_HEADING, Candidate, apply_links, build_prompt, candidates_hash,
                                index_parts, link_project, links_section, parse_links, project_input, run_linking,
                                select_candidates, write_sample)


def row(country="Croatia", year="2022", nbr="1.1", text="Reform the public administration.", areas="Public administration",
        version="2023 - Semester Cycle (May)", assessment="Some Progress"):
    return {"Country": country, "Year": year, "CSR Nbr": nbr, "Version": version, "Text": text,
            "Policy Area(s)": areas, "Assessment": assessment}


ROWS = [
    row(nbr="1", text="Reform the public administration. Improve the business environment."),
    row(nbr="1.1", text="Reform the public administration."),
    row(nbr="1.2", text="Improve the business environment.", areas="Business environment"),
    row(year="2020", nbr="2.1", text="Strengthen the health system.", areas="Healthcare"),
    row(year="2024", nbr="1.1", text="Too recent for a 2024 project."),
    row(country="Slovenia", year="2023", nbr="3.1", text="Reform pensions.", areas="Pensions"),
]


def parts():
    return index_parts(build_csrs(ROWS, year_from=2016, year_to=2025))


def test_index_parts_by_country_and_year():
    idx = parts()
    assert sorted(idx) == [("HR", 2020), ("HR", 2022), ("HR", 2024), ("SI", 2023)]
    hr = idx[("HR", 2022)]
    assert [c.part_id for c in hr] == ["csr-HR-2022-1.1", "csr-HR-2022-1.2"]
    assert hr[0] == Candidate(part_id="csr-HR-2022-1.1", csr_id="csr-HR-2022-1", country="HR", year=2022,
                              text="Reform the public administration.", policy_areas=["Public administration"],
                              assessment="Some Progress")


def test_window_is_three_years_before_the_project():
    fm = {"project_id": "p", "title": "Public administration reform", "year": 2024, "countries": ["HR", "SI"]}
    ids = [c.part_id for c in select_candidates(fm, "", parts())]
    assert ids == ["csr-HR-2022-1.1", "csr-HR-2022-1.2", "csr-SI-2023-3.1"]  # 2021-2023 ; ni 2020 ni 2024


def test_no_year_or_country_gives_no_candidates():
    assert select_candidates({"project_id": "p", "title": "x", "year": None, "countries": ["HR"]}, "", parts()) == []
    assert select_candidates({"project_id": "p", "title": "x", "year": 2024, "countries": []}, "", parts()) == []


def test_many_countries_keep_closest_parts_per_country(monkeypatch):
    import tsi_agent.csr_link as mod

    monkeypatch.setattr(mod, "MAX_PER_COUNTRY", 1)
    fm = {"project_id": "p", "title": "Business environment", "year": 2024, "countries": ["HR", "SI", "GR"]}
    ids = [c.part_id for c in select_candidates(fm, "improve the business environment", parts())]
    assert ids == ["csr-HR-2022-1.2", "csr-SI-2023-3.1"]


def test_candidates_hash_ignores_order():
    a, b = parts()[("HR", 2022)]
    assert candidates_hash([a, b]) == candidates_hash([b, a]) != candidates_hash([a])


def test_project_input_detail_and_title_only():
    body = ("# Title\n\nIntro text.\n\n## Summary (AI-generated)\n\nSummary.\n\n## Objectives\n\nGoals.\n\n"
            f"{SECTION_HEADING}\n\n- old link\n\n## Deliverables (AI-generated summary)\n\nDeliv.\n\n"
            "## Sources\n\n- https://x")
    text, basis = project_input({"title": "Title", "detail_page": True}, body)
    assert basis == "full_text"
    assert "Intro text." in text and "Goals." in text and "Summary." in text
    assert "old link" not in text and "https://x" not in text and "Deliv." not in text
    text, basis = project_input({"title": "Thin", "detail_page": False, "listing_titles": ["Thin", "Thin bis"]},
                                "# Thin\n\n## Sources\n\n- u")
    assert basis == "title_only" and text == "Title: Thin\nListed as: Thin; Thin bis"


class FakeGenerator:
    def __init__(self, replies):
        self.replies = list(replies)
        self.prompts = []

    def run(self, messages):
        self.prompts.append(messages[-1].text)
        reply = self.replies.pop(0)
        if isinstance(reply, Exception):
            raise reply
        return {"replies": [ChatMessage.from_assistant(json.dumps(reply), meta={"model": "claude-cli/sonnet"})]}


def hr_candidates():
    return parts()[("HR", 2022)]


def test_schema_and_prompt():
    assert LINK_SCHEMA["required"] == ["links"]
    prompt = build_prompt({"title": "Public administration reform", "programme": "TSI", "year": 2024,
                           "countries": ["HR"]}, "Intro: x", "full_text", hr_candidates())
    assert "csr-HR-2022-1.1 (Croatia 2022; Public administration; latest assessment: Some Progress): " \
           "Reform the public administration." in prompt
    assert "Title: Public administration reform" in prompt and "TSI 2024" in prompt and "Intro: x" in prompt
    assert "only the title" not in prompt
    assert "only the title" in build_prompt({"title": "T", "year": 2024, "countries": ["HR"]}, "Title: T",
                                            "title_only", hr_candidates())


def test_parse_links_keeps_valid_unique_and_caps():
    cands = hr_candidates()
    raw = {"links": [
        {"csr_id": "csr-HR-2022-1.1", "level": "direct", "rationale": "Reforms the administration."},
        {"csr_id": "csr-HR-2022-1.1", "level": "partial", "rationale": "dup"},
        {"csr_id": "csr-XX-2022-9.9", "level": "direct", "rationale": "unknown"},
        {"csr_id": "csr-HR-2022-1.2", "level": "weak", "rationale": "bad level"},
        {"csr_id": "csr-HR-2022-1.2", "level": "partial", "rationale": " Helps firms. "}]}
    assert parse_links(json.dumps(raw), cands) == [
        {"csr_id": "csr-HR-2022-1.1", "level": "direct", "rationale": "Reforms the administration."},
        {"csr_id": "csr-HR-2022-1.2", "level": "partial", "rationale": "Helps firms."}]
    cands6 = index_parts(build_csrs([row(nbr=f"1.{i}", text=f"T{i}.") for i in range(1, 8)]))[("HR", 2022)]
    many = {"links": [{"csr_id": c.part_id, "level": "partial", "rationale": "r"} for c in cands6]}
    assert len(parse_links(json.dumps(many), cands6)) == 5


def test_link_project_record():
    gen = FakeGenerator([{"links": [{"csr_id": "csr-HR-2022-1.1", "level": "direct", "rationale": "R."}]}])
    fm = {"project_id": "p", "title": "Public administration reform", "year": 2024, "countries": ["HR"],
          "detail_page": True, "content_hash": "sha256:abc"}
    rec = link_project(gen, fm, "# P\n\nIntro.", hr_candidates())
    assert rec["status"] == "ok" and rec["basis"] == "full_text" and rec["content_hash"] == "sha256:abc"
    assert rec["candidates_hash"] == candidates_hash(hr_candidates()) and rec["model"] == "claude-cli/sonnet"
    assert rec["links"] == [{"csr_id": "csr-HR-2022-1.1", "level": "direct", "rationale": "R."}]
    assert rec["candidates"] == ["csr-HR-2022-1.1", "csr-HR-2022-1.2"]


def test_links_section_and_apply_is_idempotent():
    rec = {"basis": "title_only", "links": [{"csr_id": "csr-HR-2022-1.1", "level": "direct", "rationale": "R."}]}
    section = links_section(rec)
    assert section.startswith(SECTION_HEADING + "\n\n")
    assert "- csr-HR-2022-1.1 (direct): R." in section and "based on the project title only" in section
    assert links_section({"basis": "full_text", "links": []}) is None
    text = "---\nproject_id: p\ntitle: P\nresults: achieved\n---\n\n# P\n\nIntro.\n\n## Sources\n\n- u\n"
    once = apply_links(text, rec)
    assert once == apply_links(once, rec)
    assert "csrs:\n- csr-HR-2022-1.1\n" in once
    assert once.index(SECTION_HEADING) < once.index("## Sources")
    cleared = apply_links(once, {"basis": "full_text", "links": []})
    assert "csrs" not in cleared and SECTION_HEADING not in cleared and "## Sources" in cleared


PROJECT = """---
project_id: {pid}
title: Public administration reform {pid}
programme: TSI
year: 2024
countries:
- HR
detail_page: true
content_hash: sha256:{pid}
---

# Public administration reform {pid}

Intro {pid}.

## Sources

- https://example.eu/{pid}
"""


def setup_output(tmp_path, pids=("a", "b", "c")):
    out = tmp_path / "output"
    (out / "projects").mkdir(parents=True)
    for pid in pids:
        (out / "projects" / f"{pid}.md").write_text(PROJECT.format(pid=pid), encoding="utf-8")
    (out / "projects" / "nocountry.md").write_text(
        PROJECT.format(pid="nocountry").replace("countries:\n- HR\n", ""), encoding="utf-8")
    settings = Settings(output_dir=str(out), logs_dir=str(tmp_path / "logs"))
    return out, settings


def answer(cid="csr-HR-2022-1.1"):
    return {"links": [{"csr_id": cid, "level": "direct", "rationale": "R."}]}


def test_run_links_caches_and_resumes(tmp_path):
    out, settings = setup_output(tmp_path)
    csrs = build_csrs(ROWS, year_from=2016, year_to=2025)
    gen = FakeGenerator([answer(), LLMQuotaExceeded("limit reached")])
    s1 = run_linking(settings, csrs, gen, concurrency=1)
    assert s1["quota_exceeded"] is True and s1["linked"] == 1 and s1["no_candidates"] == 1
    assert sorted(p.name for p in (out / "csr_links").glob("*.json")) == ["a.json", "nocountry.json"]
    assert "csrs:\n- csr-HR-2022-1.1" in (out / "projects" / "a.md").read_text(encoding="utf-8")
    gen2 = FakeGenerator([answer(), answer("csr-HR-2022-1.2")])
    s2 = run_linking(settings, csrs, gen2, concurrency=1)
    assert s2["linked"] == 2 and s2["cached"] == 1 and s2["no_candidates"] == 1 and s2["quota_exceeded"] is False and len(gen2.prompts) == 2
    s3 = run_linking(settings, csrs, FakeGenerator([]), concurrency=1)
    assert s3["linked"] == 0 and s3["cached"] == 3 and s3["no_candidates"] == 1


def test_changed_project_is_relinked_and_limit(tmp_path):
    out, settings = setup_output(tmp_path, pids=("a", "b"))
    csrs = build_csrs(ROWS, year_from=2016, year_to=2025)
    run_linking(settings, csrs, FakeGenerator([answer(), answer()]), concurrency=1)
    path = out / "projects" / "a.md"
    path.write_text(path.read_text(encoding="utf-8").replace("Intro a.", "Intro a, changed."), encoding="utf-8")
    gen = FakeGenerator([answer("csr-HR-2022-1.2")])
    s = run_linking(settings, csrs, gen, concurrency=1, limit=1)
    assert s["linked"] == 1 and "csr-HR-2022-1.2" in path.read_text(encoding="utf-8")


def test_bad_reply_is_skipped_not_cached(tmp_path):
    out, settings = setup_output(tmp_path, pids=("a",))
    csrs = build_csrs(ROWS, year_from=2016, year_to=2025)
    s = run_linking(settings, csrs, FakeGenerator([{"oops": 1}, {"oops": 1}]), concurrency=1)
    assert s["failed"] == ["a"] and not (out / "csr_links" / "a.json").exists()


def test_write_sample(tmp_path):
    out, settings = setup_output(tmp_path)
    csrs = build_csrs(ROWS, year_from=2016, year_to=2025)
    run_linking(settings, csrs, FakeGenerator([answer(), answer(), answer()]), concurrency=1)
    path = write_sample(settings, csrs, 2, tmp_path / "sample.csv")
    import csv as _csv
    with path.open(encoding="utf-8-sig", newline="") as fh:
        rows = list(_csv.DictReader(fh))
    assert len({r["project_id"] for r in rows}) == 2
    assert list(rows[0]) == ["project_id", "title", "programme", "year", "countries", "basis", "csr_id", "level",
                             "rationale", "csr_text", "csr_assessment", "verdict"]
    assert rows[0]["csr_text"] == "Reform the public administration." and rows[0]["verdict"] == ""


def test_worker_events_reach_the_run_log(tmp_path):
    from tsi_agent import runlog

    out, settings = setup_output(tmp_path)
    csrs = build_csrs(ROWS, year_from=2016, year_to=2025)
    with runlog.open_run(tmp_path / "logs") as run:
        run_linking(settings, csrs, FakeGenerator([answer(), answer(), answer()]), concurrency=2)
    assert run.counts["link:ok"] == 3


def test_generator_error_fails_that_project_only(tmp_path):
    out, settings = setup_output(tmp_path, pids=("a", "b"))
    csrs = build_csrs(ROWS, year_from=2016, year_to=2025)
    s = run_linking(settings, csrs, FakeGenerator([RuntimeError("boom"), answer()]), concurrency=1)
    assert s["failed"] == ["a"] and s["linked"] == 1


def test_base_exception_stops_the_batch_and_propagates(tmp_path):
    import pytest

    class Stop(BaseException):
        pass

    out, settings = setup_output(tmp_path, pids=("a", "b", "c"))
    csrs = build_csrs(ROWS, year_from=2016, year_to=2025)
    gen = FakeGenerator([answer()] * 3)
    gen.run = lambda messages: (gen.prompts.append(1), (_ for _ in ()).throw(Stop()))
    with pytest.raises(Stop):
        run_linking(settings, csrs, gen, concurrency=1)
    assert len(gen.prompts) <= 2


def test_record_write_failure_is_not_fatal(tmp_path, monkeypatch):
    import tsi_agent.csr_link as mod

    out, settings = setup_output(tmp_path, pids=("a",))
    csrs = build_csrs(ROWS, year_from=2016, year_to=2025)

    def broken(path, data):
        if path.name == "a.json":
            raise OSError("disk")

    monkeypatch.setattr(mod, "_write_json", broken)
    s = run_linking(settings, csrs, FakeGenerator([answer()]), concurrency=1)
    assert s["failed"] == ["a"] and s["linked"] == 0


def test_section_is_replaced_in_place_and_stable_with_deliverables():
    from tsi_agent.render import parse_front_matter, render, replace_deliverables_section

    rec = {"basis": "full_text", "links": [{"csr_id": "csr-HR-2022-1.1", "level": "direct", "rationale": "R."}]}
    deliv = "## Deliverables (AI-generated summary)\n\n### D\n\nText."
    text = ("---\nproject_id: p\ntitle: P\n---\n\n# P\n\nIntro.\n\n" + deliv + "\n\n" + SECTION_HEADING +
            "\n\n- old\n\n## Sources\n\n- u\n")
    once = apply_links(text, rec)
    assert once.index("## Deliverables") < once.index(SECTION_HEADING) < once.index("## Sources")

    def cycle(t):
        fm, body = parse_front_matter(t)
        return apply_links(render(fm, replace_deliverables_section(body, deliv)), rec)

    first = cycle(once)
    assert cycle(first) == first


def test_changed_listing_titles_relink_a_title_only_project(tmp_path):
    out, settings = setup_output(tmp_path, pids=("a",))
    csrs = build_csrs(ROWS, year_from=2016, year_to=2025)
    path = out / "projects" / "a.md"
    path.write_text(path.read_text(encoding="utf-8").replace("detail_page: true", "detail_page: false"),
                    encoding="utf-8")
    run_linking(settings, csrs, FakeGenerator([answer()]), concurrency=1)
    assert run_linking(settings, csrs, FakeGenerator([]), concurrency=1)["cached"] == 1
    path.write_text(path.read_text(encoding="utf-8").replace("detail_page: false",
                                                             "detail_page: false\nlisting_titles:\n- Other title"),
                    encoding="utf-8")
    gen = FakeGenerator([answer()])
    assert run_linking(settings, csrs, gen, concurrency=1)["linked"] == 1 and len(gen.prompts) == 1


def test_global_cap_keeps_closest_then_recent_in_original_order(monkeypatch):
    import tsi_agent.csr_link as mod

    monkeypatch.setattr(mod, "MAX_CANDIDATES", 2)
    rows = [row(year="2021", nbr="1.1", text="Alpha beta."), row(year="2022", nbr="1.1", text="Gamma delta."),
            row(year="2023", nbr="1.1", text="Gamma delta.")]
    idx = index_parts(build_csrs(rows, year_from=2016, year_to=2025))
    fm = {"project_id": "p", "title": "x", "year": 2024, "countries": ["HR"]}
    ids = [c.part_id for c in select_candidates(fm, "alpha beta", idx)]
    assert ids == ["csr-HR-2021-1.1", "csr-HR-2023-1.1"]


def test_apply_failure_is_logged_and_skipped(tmp_path):
    out, settings = setup_output(tmp_path, pids=("a", "b"))
    csrs = build_csrs(ROWS, year_from=2016, year_to=2025)
    (out / "projects" / "a.md").write_text("no front matter", encoding="utf-8")
    s = run_linking(settings, csrs, FakeGenerator([answer()]), concurrency=1)
    assert s["linked"] == 1 and s["files_updated"] >= 1


def test_input_hash_covers_candidate_text_but_not_assessment():
    from tsi_agent.csr_link import input_hash

    base = Candidate("csr-HR-2022-1.1", "csr-HR-2022-1", "HR", 2022, "Text.", ["A"], "Some Progress")
    reassessed = Candidate("csr-HR-2022-1.1", "csr-HR-2022-1", "HR", 2022, "Text.", ["A"], "Limited Progress")
    reworded = Candidate("csr-HR-2022-1.1", "csr-HR-2022-1", "HR", 2022, "Other text.", ["A"], "Some Progress")
    assert input_hash("full_text", "t", [base]) == input_hash("full_text", "t", [reassessed])
    assert input_hash("full_text", "t", [base]) != input_hash("full_text", "t", [reworded])
