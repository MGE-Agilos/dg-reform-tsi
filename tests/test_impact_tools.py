from pathlib import Path

import pytest
from haystack import Document, component
from haystack.dataclasses import ByteStream

from tsi_agent import runlog
from tsi_agent.components.claude_cli import LLMQuotaExceeded
from tsi_agent.impact.tools import PageReader, WebSearchTool, page_text

FIX = Path(__file__).parent / "fixtures"


@component
class FakeEngine:
    def __init__(self, results=None, error=None):
        self.results = results or []
        self.error = error
        self.queries = []

    @component.output_types(documents=list[Document], links=list[str])
    def run(self, query: str):
        self.queries.append(query)
        if self.error:
            raise self.error
        docs = [Document(content=s, meta={"title": t, "link": u}) for t, u, s in self.results]
        return {"documents": docs, "links": [d.meta["link"] for d in docs]}


@component
class FakeFetcher:
    def __init__(self, pages=None):
        self.pages = pages or {}

    @component.output_types(streams=list[ByteStream])
    def run(self, urls: list[str]):
        url = urls[0]
        if url not in self.pages:
            raise RuntimeError(f"HTTP 404 for {url}")
        data, ctype = self.pages[url]
        return {"streams": [ByteStream(data=data, meta={"url": url, "content_type": ctype})]}


def test_web_search_numbers_after_known_sources_and_reuses_known_urls():
    tool = WebSearchTool(search_engine=FakeEngine([("A", "https://a.eu", "alpha"), ("Old", "https://old.eu", "o")]))
    known = [{"ref": 1, "url": "https://project"}, {"ref": 2, "url": "https://old.eu", "title": "Old"}]
    out = tool.run(query="impact", known_sources=known, past_searches=[])
    assert out["result"].startswith("[3] A\nURL: https://a.eu\nalpha")
    assert "[2] Old" in out["result"]
    assert [s["ref"] for s in out["sources"]] == [3]
    assert out["sources"][0]["kind"] == "web" and out["searches"] == ["impact"]


def test_web_search_limit_and_errors():
    engine = FakeEngine([("A", "https://a.eu", "alpha")])
    tool = WebSearchTool(search_engine=engine, max_searches=2)
    out = tool.run(query="third", known_sources=[], past_searches=["one", "two"])
    assert "limit" in out["result"].lower() and out["sources"] == [] and engine.queries == []
    broken = WebSearchTool(search_engine=FakeEngine(error=RuntimeError("network down")))
    assert "failed" in broken.run(query="q")["result"]
    with pytest.raises(LLMQuotaExceeded):
        WebSearchTool(search_engine=FakeEngine(error=LLMQuotaExceeded("limit"))).run(query="q")


def test_page_text_html_pdf_and_plain():
    html = (FIX / "web_article.html").read_bytes()
    text = page_text(html, "text/html; charset=utf-8", "https://x.eu/a")
    assert "Hospital arrears fell by 40 percent" in text
    pdf = (FIX / "deliverable_small.pdf").read_bytes()
    assert "--- page 1 ---" in page_text(pdf, "application/pdf", "https://x.eu/r.pdf")
    assert page_text(b"plain words", "text/plain", "https://x.eu/t") == "plain words"
    with pytest.raises(ValueError):
        page_text(b"\x00\x01", "image/png", "https://x.eu/i.png")


def test_fetch_page_numbers_truncates_and_logs(tmp_path):
    html = (FIX / "web_article.html").read_bytes()
    reader = PageReader(max_words=50, fetcher=FakeFetcher({"https://x.eu/a": (html, "text/html")}))
    known = [{"ref": 1, "url": "https://p"}, {"ref": 2, "url": "https://x.eu/a", "title": "Croatia cuts"}]
    with runlog.open_run(tmp_path, "p1") as run:
        out = reader.run(url="https://x.eu/a", known_sources=known)
        miss = reader.run(url="https://missing.eu", known_sources=known)
    assert out["result"].startswith("[2] Croatia cuts\nURL: https://x.eu/a")
    assert out["sources"] == []  # URL deja numerotee : meme numero, pas de nouvelle source
    assert "truncated" in out["result"] and len(out["result"].split()) < 90
    assert miss["result"].startswith("Could not read https://missing.eu") and miss["sources"] == []
    assert run.final_summary["counts"]["tool_call:ok"] == 1 and run.final_summary["counts"]["tool_call:error"] == 1


def test_fetch_page_new_url_gets_next_number():
    reader = PageReader(fetcher=FakeFetcher({"https://n.eu": (b"some text", "text/plain")}))
    out = reader.run(url="https://n.eu", known_sources=[{"ref": 1, "url": "https://p"}])
    assert out["sources"][0]["ref"] == 2 and out["sources"][0]["url"] == "https://n.eu"


def test_tools_round_trip(monkeypatch):
    monkeypatch.setenv("HAYSTACK_DESERIALIZATION_ALLOWLIST", "tsi_agent")
    from tsi_agent.impact.web_search import ClaudeWebSearch
    tool = WebSearchTool.from_dict(WebSearchTool(search_engine=ClaudeWebSearch(), max_searches=4).to_dict())
    assert isinstance(tool.search_engine, ClaudeWebSearch) and tool.max_searches == 4
    reader = PageReader.from_dict(PageReader(max_words=10).to_dict())
    assert reader.max_words == 10 and type(reader.fetcher).__name__ == "BoundedLinkFetcher"


def test_page_text_pdf_stops_after_max_words():
    pdf = (FIX / "deliverable_small.pdf").read_bytes()
    full = page_text(pdf, "application/pdf", "https://x.eu/r.pdf")
    assert "--- page 2 ---" in full
    short = page_text(pdf, "application/pdf", "https://x.eu/r.pdf", max_words=1)
    assert "--- page 1 ---" in short and "--- page 2 ---" not in short


def bounded(handler, **kw):
    import httpx
    from tsi_agent.impact.tools import BoundedLinkFetcher
    fetcher = BoundedLinkFetcher(**kw)
    fetcher._transport = httpx.MockTransport(handler)
    return fetcher


@pytest.fixture
def public_dns(monkeypatch):
    """Resolution DNS simulee : tout nom resout vers une adresse publique, sauf internal.example (privee)."""
    import tsi_agent.impact.tools as tools
    monkeypatch.setattr(tools, "_resolve", lambda host: ["10.1.2.3"] if host == "internal.example" else ["93.184.216.34"])


def test_bounded_fetcher_reads_page_and_content_type(public_dns):
    import httpx
    fetcher = bounded(lambda req: httpx.Response(200, content=b"hello", headers={"content-type": "text/plain"}))
    stream = fetcher.run(urls=["https://a.example/x"])["streams"][0]
    assert stream.data == b"hello" and stream.meta == {"url": "https://a.example/x", "content_type": "text/plain"}


def test_bounded_fetcher_aborts_past_size_limit(public_dns):
    import httpx

    def chunks():
        for _ in range(100):
            yield b"x" * 100_000  # 10 Mo au total

    fetcher = bounded(lambda req: httpx.Response(200, content=chunks(), headers={"content-type": "text/html"}),
                      max_mb=1)
    with pytest.raises(ValueError, match="larger than"):
        fetcher.run(urls=["https://a.example/big"])
    declared = bounded(lambda req: httpx.Response(200, content=b"x", headers={"content-length": "99999999"}),
                       max_mb=1)
    with pytest.raises(ValueError, match="larger than"):
        declared.run(urls=["https://a.example/big"])


@pytest.mark.parametrize("url", ["http://127.0.0.1/", "http://10.0.0.1/admin", "http://[::1]/", "http://169.254.169.254/",
                                 "https://internal.example/x", "file:///etc/passwd", "ftp://a.example/f"])
def test_bounded_fetcher_refuses_private_hosts_and_other_schemes(public_dns, url):
    calls = []
    fetcher = bounded(lambda req: calls.append(req) or __import__("httpx").Response(200, content=b"secret"))
    with pytest.raises(ValueError):
        fetcher.run(urls=[url])
    assert calls == []


def test_bounded_fetcher_follows_redirects_but_not_to_private_ip(public_dns):
    import httpx

    def handler(req):
        if req.url.path == "/moved":
            return httpx.Response(301, headers={"location": "/final"})
        if req.url.path == "/final":
            return httpx.Response(200, content=b"ok", headers={"content-type": "text/plain"})
        return httpx.Response(302, headers={"location": "http://127.0.0.1/secret"})

    fetcher = bounded(handler)
    stream = fetcher.run(urls=["https://a.example/moved"])["streams"][0]
    assert stream.data == b"ok" and stream.meta["url"] == "https://a.example/final"
    with pytest.raises(ValueError, match="not allowed"):
        fetcher.run(urls=["https://a.example/evil"])
    loop = bounded(lambda req: httpx.Response(302, headers={"location": "/again"}), max_redirects=3)
    with pytest.raises(ValueError, match="redirects"):
        loop.run(urls=["https://a.example/start"])


def test_page_reader_reports_refused_url(public_dns):
    out = PageReader().run(url="http://127.0.0.1:9/x")
    assert out["result"].startswith("Could not read http://127.0.0.1:9/x") and out["sources"] == []


def test_bounded_fetcher_round_trip(monkeypatch):
    monkeypatch.setenv("HAYSTACK_DESERIALIZATION_ALLOWLIST", "tsi_agent")
    from haystack.components.fetchers import LinkContentFetcher
    from tsi_agent.impact.tools import BoundedLinkFetcher
    reader = PageReader.from_dict(PageReader(fetcher=BoundedLinkFetcher(max_mb=5, max_redirects=2)).to_dict())
    assert isinstance(reader.fetcher, BoundedLinkFetcher)
    assert reader.fetcher.max_mb == 5 and reader.fetcher.max_redirects == 2
    swapped = PageReader.from_dict(PageReader(fetcher=LinkContentFetcher()).to_dict())  # echange deepset
    assert isinstance(swapped.fetcher, LinkContentFetcher)
