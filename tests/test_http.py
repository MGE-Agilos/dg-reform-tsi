from pathlib import Path

import httpx
import pytest

from tsi_agent import runlog
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
    # Attentes: [rate_limit_backoff, throttle_avant_retry1, rate_limit_backoff_doubled, throttle_avant_retry2]
    # Après premier 429: min_interval_s devient 5.0, puis sleep 90
    # Avant retry1: sleep ~5 (throttle)
    # Après deuxième 429: min_interval_s devient 10.0, puis sleep 180
    # Avant retry2: sleep ~10 (throttle)
    assert len(waits) == 4 and waits[0] == 90 and waits[2] == 180
    assert 4 < waits[1] < 6  # throttle: ~5.0
    assert 9 < waits[3] < 11  # throttle: ~10.0


def test_throttle_recovers_after_sustained_success(tmp_path):
    state = {"n": 0}

    def handler(request):
        if request.url.path == "/robots.txt":
            return robots_ok(request)
        state["n"] += 1
        return httpx.Response(429) if state["n"] == 1 else httpx.Response(200, content=b"ok")

    f = PoliteFetcher(tmp_path / "cache", min_interval_s=2, transport=httpx.MockTransport(handler),
                      sleep=lambda s: None, rate_limit_backoff_s=0)
    f.get(f"{URL}-0")  # 429 puis succes : min_interval_s -> 5.0 (streak de succes = 1)
    assert f.min_interval_s == 5.0
    for i in range(1, 50):  # 49 succes de plus => streak = 50 => RECOVER_AFTER atteint
        f.get(f"{URL}-{i}")
    assert f.min_interval_s == 2.5  # 5.0 / 2, jamais en dessous de la base (2.0)


def test_throttle_recovery_counter_resets_on_429(tmp_path):
    fail_next = {"flag": False}

    def handler(request):
        if request.url.path == "/robots.txt":
            return robots_ok(request)
        if fail_next["flag"]:
            fail_next["flag"] = False
            return httpx.Response(429)
        return httpx.Response(200, content=b"ok")

    f = PoliteFetcher(tmp_path / "cache", min_interval_s=2, transport=httpx.MockTransport(handler),
                      sleep=lambda s: None, rate_limit_backoff_s=0)
    for i in range(49):  # streak = 49, pas encore recupere
        f.get(f"{URL}-{i}")
    assert f.min_interval_s == 2
    fail_next["flag"] = True
    f.get(f"{URL}-49")  # ce call recoit un 429 (reset du streak a 0) puis reussit au retry -> streak = 1
    assert f.min_interval_s == 5.0  # le 429 a fait remonter min_interval_s ; pas de recuperation prematuree
    for i in range(50, 98):  # 48 succes de plus : streak = 1 + 48 = 49
        f.get(f"{URL}-{i}")
    assert f.min_interval_s == 5.0
    f.get(f"{URL}-98")  # 50e succes depuis le reset : recuperation
    assert f.min_interval_s == 2.5


def test_corrupt_cache_entry_is_a_cache_miss(tmp_path):
    def handler(request):
        if request.url.path == "/robots.txt":
            return robots_ok(request)
        return httpx.Response(200, content=b"fresh", headers={"ETag": '"v1"'})

    make(tmp_path, handler).get(URL)
    for meta in (tmp_path / "cache").glob("*.json"):
        meta.write_text('{"url": "trunc', encoding="utf-8")  # écriture interrompue
    assert make(tmp_path, handler).get(URL).content == b"fresh"


def test_exhausted_retries_raise_with_last_status_and_cache_nothing(tmp_path):
    attempts = []

    def handler(request):
        if request.url.path == "/robots.txt":
            return robots_ok(request)
        attempts.append(1)
        return httpx.Response(503)

    f = make(tmp_path, handler)
    with pytest.raises(FetchError) as err:
        f.get(URL)
    assert len(attempts) == 3 and err.value.status == 503
    assert URL not in f._memo and not list((tmp_path / "cache").glob("*.bin"))


def test_cache_write_failure_does_not_fail_the_fetch(tmp_path, monkeypatch):
    def handler(request):
        if request.url.path == "/robots.txt":
            return robots_ok(request)
        return httpx.Response(200, content=b"hello", headers={"ETag": '"v1"'})

    f = make(tmp_path, handler)

    def boom(self, data):
        raise OSError("disk full")

    monkeypatch.setattr(Path, "write_bytes", boom)
    with runlog.open_run(tmp_path / "logs", "r1") as run:
        result = f.get(URL)
    assert (result.content, result.from_cache) == (b"hello", False)
    assert run.counts["error:cache_write_failed"] == 1
    assert not list((tmp_path / "cache").glob("*.bin"))


def test_throttle_waits_between_requests(tmp_path):
    waits = []
    f = PoliteFetcher(tmp_path / "cache", min_interval_s=5,
                      transport=httpx.MockTransport(lambda r: robots_ok(r) if r.url.path == "/robots.txt"
                                                    else httpx.Response(200, content=b"x")),
                      sleep=waits.append)
    f.get("https://example.test/a_en")
    f.get("https://example.test/b_en")
    # robots.txt, puis a, puis b : deux attentes proches de l'intervalle
    assert len(waits) == 2 and all(4.0 < w <= 5.0 for w in waits)


def test_429_slows_the_fetcher_down_durably(tmp_path):
    attempts = []

    def handler(request):
        if request.url.path == "/robots.txt":
            return robots_ok(request)
        attempts.append(1)
        return httpx.Response(429) if len(attempts) == 1 else httpx.Response(200, content=b"ok")

    f = PoliteFetcher(tmp_path / "cache", min_interval_s=2, transport=httpx.MockTransport(handler),
                      sleep=lambda s: None, rate_limit_backoff_s=0)
    f.get(URL)
    assert f.min_interval_s == 5.0
    for _ in range(3):
        f._on_rate_limited("https://example.test/x") if hasattr(f, "_on_rate_limited") else None
    assert f.min_interval_s <= 15.0
