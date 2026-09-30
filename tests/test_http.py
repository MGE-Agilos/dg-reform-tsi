import httpx
import pytest

from tsi_agent.http import FetchError, PoliteFetcher

URL = "https://example.test/page_en"


def make(tmp_path, handler, **kw):
    return PoliteFetcher(tmp_path / "cache", min_interval_s=0, transport=httpx.MockTransport(handler),
                         sleep=lambda s: None, **kw)


def robots_ok(request):
    return httpx.Response(200, text="User-agent: *\nDisallow: /private/\n")


def test_fetch_caches_and_uses_etag(tmp_path):
    seen = []

    def handler(request):
        if request.url.path == "/robots.txt":
            return robots_ok(request)
        seen.append(request.headers.get("if-none-match"))
        if request.headers.get("if-none-match") == '"v1"':
            return httpx.Response(304)
        return httpx.Response(200, content=b"hello", headers={"ETag": '"v1"'})

    first = make(tmp_path, handler).get(URL)
    assert (first.content, first.from_cache) == (b"hello", False)
    second = make(tmp_path, handler).get(URL)  # nouveau processus : cache disque
    assert (second.content, second.from_cache) == (b"hello", True)
    assert seen == [None, '"v1"']


def test_memo_avoids_second_request(tmp_path):
    calls = []

    def handler(request):
        calls.append(request.url.path)
        return robots_ok(request) if request.url.path == "/robots.txt" else httpx.Response(200, content=b"x")

    f = make(tmp_path, handler)
    f.get(URL)
    f.get(URL)
    assert calls == ["/robots.txt", "/page_en"]


def test_retries_on_5xx_then_succeeds(tmp_path):
    attempts = []

    def handler(request):
        if request.url.path == "/robots.txt":
            return robots_ok(request)
        attempts.append(1)
        return httpx.Response(503) if len(attempts) < 3 else httpx.Response(200, content=b"ok")

    assert make(tmp_path, handler).get(URL).content == b"ok"
    assert len(attempts) == 3


def test_404_is_not_retried(tmp_path):
    attempts = []

    def handler(request):
        if request.url.path == "/robots.txt":
            return robots_ok(request)
        attempts.append(1)
        return httpx.Response(404)

    with pytest.raises(FetchError) as err:
        make(tmp_path, handler).get(URL)
    assert err.value.status == 404 and len(attempts) == 1


def test_robots_disallow(tmp_path):
    f = make(tmp_path, lambda r: robots_ok(r) if r.url.path == "/robots.txt" else httpx.Response(200))
    with pytest.raises(FetchError, match="robots"):
        f.get("https://example.test/private/x_en")


def test_redirect_final_url(tmp_path):
    def handler(request):
        if request.url.path == "/robots.txt":
            return robots_ok(request)
        if request.url.path == "/old_en":
            return httpx.Response(301, headers={"Location": "https://example.test/new_en"})
        return httpx.Response(200, content=b"new")

    result = make(tmp_path, handler).get("https://example.test/old_en")
    assert result.final_url == "https://example.test/new_en"


def test_429_waits_long_and_honours_retry_after(tmp_path):
    waits, attempts = [], []

    def handler(request):
        if request.url.path == "/robots.txt":
            return robots_ok(request)
        attempts.append(1)
        if len(attempts) == 1:
            return httpx.Response(429, headers={"Retry-After": "90"})
        if len(attempts) == 2:
            return httpx.Response(429)
        return httpx.Response(200, content=b"ok")

    f = PoliteFetcher(tmp_path / "cache", min_interval_s=0, transport=httpx.MockTransport(handler),
                      sleep=waits.append, rate_limit_backoff_s=30)
    assert f.get(URL).content == b"ok"
    assert waits == [90, 180]  # Retry-After, puis le délai double (jamais de retour en arrière)
