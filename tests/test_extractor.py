from conftest import CONTENT_JSON, FakeChatGenerator
from tsi_agent import runlog
from tsi_agent.components.claude_cli import LLMQuotaExceeded
from tsi_agent.components.extractor import ProjectExtractor, parse_content
from tsi_agent.models import DetailPage, ProjectRef


def page(pid="hems", countries=None):
    ref = ProjectRef(project_id=pid, title="Listing title", programme="TSI", year=2021,
                     countries=countries if countries is not None else ["HR"],
                     detail_url=f"https://x/{pid}_en")
    return DetailPage(ref=ref, url=f"https://x/{pid}_en", title="Page title", text="Four HEMS bases.",
                      content_hash="sha256:1", programme="TSI", year=2022)


def test_parse_content_accepts_fences_and_filters_countries():
    content = parse_content('```json\n{"summary":"s","context":"c","support_delivered":"d",'
                            '"results_achieved":"r","countries":["hr","XX","GR"]}\n```')
    assert content.countries == ["HR", "GR"]


def test_record_merges_detail_metadata(tmp_path):
    gen = FakeChatGenerator()
    with runlog.open_run(tmp_path, "r1") as run:
        out = ProjectExtractor(chat_generator=gen).run(pages=[page()])
    rec = out["records"][0]
    assert (rec.ref.title, rec.ref.year, rec.ref.programme) == ("Page title", 2022, "TSI")
    assert rec.content.results_achieved == "Four HEMS bases were established."
    assert rec.model == "fake/model" and out["failed"] == [] and out["quota_exceeded"] is False
    assert run.llm_calls == 1 and run.cost_usd == 0.01
    prompt = gen.calls[0][1].text
    assert "Four HEMS bases." in prompt and "https://x/hems_en" in prompt


def test_countries_fallback_to_llm_when_listing_has_none(tmp_path):
    out = ProjectExtractor(chat_generator=FakeChatGenerator()).run(pages=[page(countries=[])])
    assert out["records"][0].ref.countries == ["HR"]


def test_one_retry_on_invalid_json_then_failure():
    ok = ProjectExtractor(chat_generator=FakeChatGenerator(replies=["not json", CONTENT_JSON])).run(pages=[page()])
    assert len(ok["records"]) == 1
    gen = FakeChatGenerator(replies=["not json"])
    ko = ProjectExtractor(chat_generator=gen).run(pages=[page()])
    assert ko["records"] == [] and ko["failed"] == ["hems"] and len(gen.calls) == 2


def test_quota_stops_the_batch():
    gen = FakeChatGenerator(error=LLMQuotaExceeded("usage limit reached"))
    out = ProjectExtractor(chat_generator=gen, max_concurrency=1).run(pages=[page("a"), page("b"), page("c")])
    assert out["quota_exceeded"] is True
    assert len(gen.calls) == 1 and out["records"] == []


def test_cost_from_pricing_when_generator_gives_none(tmp_path):
    gen = FakeChatGenerator(cost_usd=None)
    extractor = ProjectExtractor(chat_generator=gen, pricing={"fake/": {"input": 1.0, "output": 10.0}})
    with runlog.open_run(tmp_path, "r2") as run:
        extractor.run(pages=[page()])
    assert abs(run.cost_usd - (100 * 1.0 + 20 * 10.0) / 1_000_000) < 1e-12


def test_openai_style_meta_is_logged(tmp_path):
    from haystack import component
    from haystack.dataclasses import ChatMessage

    from conftest import CONTENT_JSON

    @component
    class OpenAILike:
        @component.output_types(replies=list[ChatMessage])
        def run(self, messages: list[ChatMessage]):
            meta = {"model": "gpt-4o-mini-2024-07-18", "usage": {
                "prompt_tokens": 1000, "completion_tokens": 200, "total_tokens": 1200,
                "prompt_tokens_details": {"cached_tokens": 400}}}
            return {"replies": [ChatMessage.from_assistant(CONTENT_JSON, meta=meta)]}

    extractor = ProjectExtractor(chat_generator=OpenAILike(), pricing={"gpt-4o-mini": {"input": 0.15, "output": 0.6}})
    with runlog.open_run(tmp_path, "r3") as run:
        out = extractor.run(pages=[page()])
    assert out["records"][0].model == "gpt-4o-mini-2024-07-18"
    assert run.llm_tokens["cache_read_tokens"] == 400
    assert abs(run.cost_usd - (1000 * 0.15 + 200 * 0.6) / 1_000_000) < 1e-12


def test_longest_pricing_prefix_wins(tmp_path):
    from haystack import component
    from haystack.dataclasses import ChatMessage

    @component
    class Mini:
        @component.output_types(replies=list[ChatMessage])
        def run(self, messages: list[ChatMessage]):
            meta = {"model": "gpt-4o-mini-2024-07-18", "usage": {"prompt_tokens": 1_000_000, "completion_tokens": 0}}
            return {"replies": [ChatMessage.from_assistant(CONTENT_JSON, meta=meta)]}

    pricing = {"gpt-4o": {"input": 2.5, "output": 10.0}, "gpt-4o-mini": {"input": 0.15, "output": 0.6}}
    with runlog.open_run(tmp_path, "rp") as run:
        ProjectExtractor(chat_generator=Mini(), pricing=pricing).run(pages=[page()])
    assert abs(run.cost_usd - 0.15) < 1e-9


def test_pages_skipped_after_quota_are_logged(tmp_path):
    gen = FakeChatGenerator(error=LLMQuotaExceeded("usage limit reached"))
    with runlog.open_run(tmp_path, "rq") as run:
        out = ProjectExtractor(chat_generator=gen, max_concurrency=1).run(pages=[page("a"), page("b"), page("c")])
    assert out["failed"] == ["a"]
    assert run.counts["error:skipped_quota"] == 2
