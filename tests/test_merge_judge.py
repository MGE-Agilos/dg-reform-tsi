"""Arbitrage LLM des pages du sitemap restees sans fusion : candidats, decisions, cache, effet sur les refs."""

import json

from haystack import component
from haystack.dataclasses import ChatMessage

from conftest import FakeFetcher, fixture_bytes
from tsi_agent import runlog
from tsi_agent.components.claude_cli import LLMQuotaExceeded
from tsi_agent.components.merge_judge import (DECISIONS_FILE, SitemapMergeJudge, listing_source, select_candidates)
from tsi_agent.models import ProjectRef

BASE = "https://reforms-investments.ec.europa.eu/technical-support-instrument-0"
SITEMAP = "https://reforms-investments.ec.europa.eu/sitemap.xml"
PDF = "https://commission.europa.eu/system/files/2020-03/rss-technical-support-per-country_en.pdf"
FACTSHEET = f"{BASE}/our-projects/tsi-country-factsheets/cyprus_en"


def page(slug, title, programme, year, area="labour-market-and-social-protection"):
    return ProjectRef(project_id=slug, title=title, programme=programme, year=year,
                      detail_url=f"{BASE}/{area}/{slug}_en", policy_area=area, listing_urls=[SITEMAP])


def thin(pid, title, programme, year, countries, url=PDF):
    return ProjectRef(project_id=pid, title=title, programme=programme, year=year, countries=countries,
                      listing_urls=[url])


# Paires reelles (dry-run du 2026-10-08) : reformulations que difflib ne rattrape pas.
HU_PAGE = page("reducing-employment-gender-gap-hungarian-labour-market",
               "Reducing the employment gender gap in the Hungarian labour market", "SRSP", 2019)
HU_THIN = thin("srsp-2019-reducing-gender-employment-gap-in-the-hungarian-labour-market",
               "Reducing gender employment gap in the Hungarian labour market", "SRSP", 2019, ["HU"])
CY_PAGE = page("adultdigitalup-improvement-digital-skills-adults-cyprus",
               "ADULTDIGITALUP - Improvement of the Digital Skills of Adults in Cyprus", "TSI", 2021,
               area="skills-education-and-training")
CY_THIN = thin("tsi-2021-improve-the-digital-skills-of-adults-in-cyprus",
               "Improve the digital skills of adults in Cyprus", "TSI", 2021, ["CY"], url=FACTSHEET)
HEALTH_PAGE = page("health-system-performance-assessment-several-countries",
                   "Health system performance assessment in several countries", "SRSP", 2019,
                   area="health-and-long-term-care")
HEALTH_THIN = thin("srsp-2019-health-system-performance-assessment-in-ireland",
                   "Health system performance assessment in Ireland", "SRSP", 2019, ["IE"])


def test_candidate_filters_programme_year_country_and_floor():
    sl = page("strengthening-alternative-investment-funds-slovenia", "Strengthening alternative investment funds in "
              "Slovenia", "SRSP", 2019, area="financial-sector-and-access-finance")
    good = thin("srsp-2019-alternative-investment-funds", "Alternative investment funds", "SRSP", 2019, ["SI"])
    next_year = thin("srsp-2020-alternative-investment-funds", "Alternative investment funds", "SRSP", 2020, ["SI"])
    far = thin("srsp-2017-alternative-investment-funds", "Alternative investment funds", "SRSP", 2017, ["SI"])
    other_prog = thin("tsi-2019-alternative-investment-funds", "Alternative investment funds", "TSI", 2019, ["SI"])
    other_country = thin("srsp-2019-alternative-investment-funds-x", "Alternative investment funds", "SRSP", 2019,
                         ["HR"])
    unrelated = thin("srsp-2019-waste-management", "Waste management", "SRSP", 2019, ["SI"])
    got = select_candidates(sl, ["SI"], [good, next_year, far, other_prog, other_country, unrelated])
    assert [c.project_id for c, _ in got] == ["srsp-2019-alternative-investment-funds",
                                              "srsp-2020-alternative-investment-funds"]
    assert got[0][1] == 0.75  # {alter, inves, funds} / {stren, alter, inves, funds}


def test_candidates_are_the_top_three_above_the_jaccard_floor():
    p = page("p", "Improving the provision of active labour market policies in Estonia", "SRSP", 2019)
    thins = [thin(f"srsp-2019-t{i}", t, "SRSP", 2019, ["EE"]) for i, t in enumerate([
        "Improving the provision of labour market services",  # 4/7
        "Active labour market policies",                        # 4/6
        "Labour market policies",                               # 3/6
        "Improving labour market statistics",                   # 3/7 : 4e, ecarte (3 au plus)
        "Pension reform",                                       # 0
    ])]
    got = select_candidates(p, ["EE"], thins)
    assert [c.project_id for c, _ in got] == ["srsp-2019-t1", "srsp-2019-t0", "srsp-2019-t2"]
    assert all(score >= 0.25 for _, score in got)
    assert select_candidates(p, ["EE"], [thins[4]]) == []
    # Pays de la page inconnus : compatibles avec tous.
    assert len(select_candidates(p, [], [thin("srsp-2019-x", "Labour market policies", "SRSP", 2019, ["FR"])])) == 1


def test_listing_source():
    assert listing_source(HU_THIN) == "SRSP PDF"
    assert listing_source(CY_THIN) == "factsheet"
    assert listing_source(thin("x", "x", "TSI", 2022, [], url=f"{BASE}/green-transition_en")) == "policy page"


# --- juge scripte -------------------------------------------------------------------------------------------------

@component
class ScriptedJudge:
    """Repond par les decisions prevues (une liste par appel) ; garde les prompts recus."""

    def __init__(self, answers=None, error=None):
        self.answers = answers or []
        self.error = error
        self.prompts: list[str] = []

    @component.output_types(replies=list[ChatMessage])
    def run(self, messages: list[ChatMessage]):
        self.prompts.append(messages[-1].text)
        if self.error is not None:
            raise self.error
        decisions = self.answers[min(len(self.prompts), len(self.answers)) - 1]
        return {"replies": [ChatMessage.from_assistant(json.dumps({"decisions": decisions}),
                                                       meta={"model": "fake", "cost_usd": 0.0})]}


def decision(sitemap_id, match, confidence="high", reason="same request"):
    return {"sitemap_id": sitemap_id, "match": match, "confidence": confidence, "reason": reason}


def discovery(*refs):
    pages = [r for r in refs if r.detail_url]
    return {"refs": len(refs), "detail": len(pages), "sitemap": {}, "merged_into_thin": [],
            "duplicates_merged": [],
            "new_projects": [{"project_id": r.project_id, "title": r.title, "programme": r.programme,
                              "year": r.year, "url": r.detail_url} for r in pages]}


def judge(tmp_path, gen, **kw):
    j = SitemapMergeJudge(chat_generator=gen, output_dir=str(tmp_path / "output"), **kw)
    j._fetcher = FakeFetcher(cached_pages={CY_PAGE.detail_url: fixture_bytes("detail_hems_croatia.html")})
    return j


def run(j, refs, excluded=None):
    return j.run(refs=[r.model_copy(deep=True) for r in refs], report=discovery(*refs),
                 excluded_thin_ids=excluded or [])


def test_high_and_medium_matches_merge_low_and_null_do_not(tmp_path):
    other = page("implementation-high-speed-rail-system-poland", "Implementation of the high-speed rail system "
                 "in Poland", "SRSP", 2020, area="skills-education-and-training")
    other_thin = thin("srsp-2020-implementation-of-the-high-speed-rail-system-capacity-building",
                      "Implementation of the high-speed rail system - capacity building", "SRSP", 2020, ["PL"])
    gen = ScriptedJudge([[decision(HU_PAGE.project_id, HU_THIN.project_id, "high"),
                          decision(CY_PAGE.project_id, CY_THIN.project_id, "medium", "reworded title"),
                          decision(HEALTH_PAGE.project_id, None, "high", "multi-country vs Ireland only"),
                          decision(other.project_id, other_thin.project_id, "low", "unsure")]])
    refs = [HU_PAGE, HU_THIN, CY_PAGE, CY_THIN, HEALTH_PAGE, HEALTH_THIN, other, other_thin]
    out = run(judge(tmp_path, gen), refs)
    ids = {r.project_id: r for r in out["refs"]}
    assert len(gen.prompts) == 1  # 4 pages a juger : un seul lot
    assert HU_THIN.project_id not in ids and CY_THIN.project_id not in ids
    assert ids[HU_PAGE.project_id].aliases == [HU_THIN.project_id]
    assert ids[HU_PAGE.project_id].countries == ["HU"] and PDF in ids[HU_PAGE.project_id].listing_urls
    assert ids[CY_PAGE.project_id].aliases == [CY_THIN.project_id]
    assert HEALTH_THIN.project_id in ids and other_thin.project_id in ids
    j = out["report"]["judge"]
    assert j["judged"] == 4 and j["llm_calls"] == 1
    assert [(m["sitemap_slug"], m["thin_id"], m["confidence"]) for m in j["merged_by_judge"]] == [
        (HU_PAGE.project_id, HU_THIN.project_id, "high"), (CY_PAGE.project_id, CY_THIN.project_id, "medium")]
    assert j["merged_by_judge"][1]["reason"] == "reworded title"
    assert {r["sitemap_slug"] for r in j["rejected"]} == {HEALTH_PAGE.project_id, other.project_id}
    report = out["report"]
    assert [p["project_id"] for p in report["new_projects"]] == [HEALTH_PAGE.project_id, other.project_id]
    assert report["refs"] == 6


def test_prompt_carries_page_snippet_candidates_and_rules(tmp_path):
    gen = ScriptedJudge([[decision(CY_PAGE.project_id, None, "low")]])
    run(judge(tmp_path, gen), [CY_PAGE, CY_THIN])
    prompt = gen.prompts[0]
    assert CY_PAGE.title in prompt and CY_THIN.project_id in prompt and CY_THIN.title in prompt
    assert "factsheet" in prompt and "TSI 2021" in prompt
    assert "Countries (guessed): CY" in prompt
    assert "Croatia" in prompt  # extrait du texte de la page (fixture HEMS), tronque
    assert "different phase" in prompt and "umbrella" in prompt


def test_decisions_are_cached_and_reused(tmp_path):
    gen = ScriptedJudge([[decision(HU_PAGE.project_id, HU_THIN.project_id, "high")]])
    run(judge(tmp_path, gen), [HU_PAGE, HU_THIN])
    path = tmp_path / "output" / DECISIONS_FILE
    saved = json.loads(path.read_text(encoding="utf-8"))
    key = f"{HU_PAGE.project_id}|{HU_THIN.project_id}"
    assert saved["decisions"][key]["match"] == HU_THIN.project_id
    assert saved["decisions"][key]["candidates"] == [HU_THIN.project_id]
    gen2 = ScriptedJudge(error=AssertionError("no LLM call expected"))
    out = run(judge(tmp_path, gen2), [HU_PAGE, HU_THIN])
    assert gen2.prompts == []
    assert out["report"]["judge"]["merged_by_judge"][0]["cached"] is True
    assert out["report"]["judge"]["llm_calls"] == 0


def test_manual_override_wins(tmp_path):
    path = tmp_path / "output" / DECISIONS_FILE
    path.parent.mkdir(parents=True)
    key_h = f"{HEALTH_PAGE.project_id}|{HEALTH_THIN.project_id}"
    key_hu = f"{HU_PAGE.project_id}|{HU_THIN.project_id}"
    path.write_text(json.dumps({"decisions": {
        key_h: {"match": None, "confidence": "high", "reason": "llm said no", "override": HEALTH_THIN.project_id},
        key_hu: {"match": HU_THIN.project_id, "confidence": "high", "reason": "llm said yes", "override": "none"},
    }}), encoding="utf-8")
    gen = ScriptedJudge(error=AssertionError("no LLM call expected"))
    out = run(judge(tmp_path, gen), [HEALTH_PAGE, HEALTH_THIN, HU_PAGE, HU_THIN])
    ids = {r.project_id for r in out["refs"]}
    assert HEALTH_THIN.project_id not in ids and HU_THIN.project_id in ids
    merged = out["report"]["judge"]["merged_by_judge"]
    assert [(m["sitemap_slug"], m["confidence"]) for m in merged] == [(HEALTH_PAGE.project_id, "override")]


def test_pages_without_candidates_and_excluded_thins_are_not_judged(tmp_path):
    gen = ScriptedJudge(error=AssertionError("no LLM call expected"))
    malta = page("sustainable-development-strategy-malta-2050", "Sustainable Development Strategy for Malta for 2050",
                 "SRSP", 2019, area="green-transition")
    out = run(judge(tmp_path, gen), [malta, CY_PAGE, CY_THIN], excluded=[CY_THIN.project_id])
    assert gen.prompts == [] and out["report"]["judge"]["judged"] == 0
    assert len(out["refs"]) == 3


def test_batches_of_ten(tmp_path):
    pages, thins, answers = [], [], [[], []]
    for i in range(12):
        p = page(f"pension-reform-{i}", f"Pension reform phase {i}", "SRSP", 2019)
        t = thin(f"srsp-2019-pension-reform-phase-{i}", f"Pension reform phase {i}", "SRSP", 2019, ["HU"])
        pages.append(p)
        thins.append(t)
        answers[i // 10].append(decision(p.project_id, None, "high", "different"))
    gen = ScriptedJudge(answers)
    out = run(judge(tmp_path, gen), pages + thins)
    assert len(gen.prompts) == 2 and out["report"]["judge"]["llm_calls"] == 2
    assert out["report"]["judge"]["judged"] == 12


def test_judge_failure_merges_nothing_and_caches_nothing(tmp_path):
    with runlog.open_run(str(tmp_path / "logs"), "r1"):
        out = run(judge(tmp_path, ScriptedJudge(error=LLMQuotaExceeded("limit"))), [HU_PAGE, HU_THIN])
    assert len(out["refs"]) == 2 and out["report"]["judge"]["errors"] == 1
    assert not (tmp_path / "output" / DECISIONS_FILE).exists()


def test_invalid_match_is_ignored_and_a_thin_goes_to_one_page(tmp_path):
    hu2 = page("reducing-gender-employment-gap-hungary", "Reducing the gender employment gap in Hungary", "SRSP", 2019)
    gen = ScriptedJudge([[decision(HU_PAGE.project_id, HU_THIN.project_id, "high"),
                          decision(hu2.project_id, HU_THIN.project_id, "high"),
                          decision(CY_PAGE.project_id, "not-a-candidate", "high")]])
    with runlog.open_run(str(tmp_path / "logs"), "r2"):
        out = run(judge(tmp_path, gen), [HU_PAGE, hu2, HU_THIN, CY_PAGE, CY_THIN])
    ids = {r.project_id: r for r in out["refs"]}
    assert ids[HU_PAGE.project_id].aliases == [HU_THIN.project_id] and ids[hu2.project_id].aliases == []
    assert CY_THIN.project_id in ids
    events = (tmp_path / "logs" / "runs" / "r2.jsonl").read_text(encoding="utf-8")
    assert '"merge_judge"' in events
