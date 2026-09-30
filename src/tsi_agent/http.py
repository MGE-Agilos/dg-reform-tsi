"""Client HTTP poli : débit limité, cache disque ETag/Last-Modified, robots.txt, retry, mémo."""

import hashlib
import json
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Callable
from urllib.parse import urlsplit
from urllib.robotparser import RobotFileParser

import httpx

from tsi_agent import runlog

DEFAULT_USER_AGENT = "TSI-Extraction-Agent/0.1 (DG REFORM project; polite crawler)"
COMPONENT = "PoliteFetcher"


@dataclass
class FetchResult:
    url: str
    final_url: str
    status: int
    content: bytes
    from_cache: bool


class FetchError(Exception):
    def __init__(self, url: str, reason: str, status: int | None = None):
        super().__init__(f"{url}: {reason}")
        self.url = url
        self.reason = reason
        self.status = status


def _retry_after(resp: httpx.Response) -> float | None:
    value = resp.headers.get("retry-after", "")
    return float(value) if value.strip().isdigit() else None


class PoliteFetcher:
    def __init__(self, cache_dir: str | Path, min_interval_s: float = 1.0, user_agent: str = DEFAULT_USER_AGENT,
                 retries: int = 3, backoff_s: float = 2.0, rate_limit_backoff_s: float = 30.0, timeout_s: float = 30.0,
                 transport: httpx.BaseTransport | None = None, respect_robots: bool = True,
                 sleep: Callable[[float], None] = time.sleep):
        self.cache_dir = Path(cache_dir)
        self.cache_dir.mkdir(parents=True, exist_ok=True)
        self.min_interval_s = min_interval_s
        self.user_agent = user_agent
        self.retries = retries
        self.backoff_s = backoff_s
        self.rate_limit_backoff_s = rate_limit_backoff_s
        self.respect_robots = respect_robots
        self._sleep = sleep
        self._client = httpx.Client(headers={"User-Agent": user_agent}, follow_redirects=True,
                                    timeout=timeout_s, transport=transport)
        self._memo: dict[str, FetchResult] = {}
        self._robots: dict[str, RobotFileParser | None] = {}
        self._last_request = 0.0
        self._lock = threading.Lock()

    # --- API publique -------------------------------------------------------------------
    def get(self, url: str) -> FetchResult:
        if url in self._memo:
            runlog.event(COMPONENT, "cache_hit", url, "memo")
            return self._memo[url]
        if not self._allowed(url):
            runlog.event(COMPONENT, "fetch", url, "robots_disallowed")
            raise FetchError(url, "disallowed by robots.txt")
        meta, body = self._cache_read(url)
        headers: dict[str, str] = {}
        if meta and body is not None:
            if meta.get("etag"):
                headers["If-None-Match"] = meta["etag"]
            if meta.get("last_modified"):
                headers["If-Modified-Since"] = meta["last_modified"]
        last_error = "unknown error"
        wait_s = self.backoff_s
        for attempt in range(1, self.retries + 1):
            t0 = time.monotonic()
            try:
                resp = self._request(url, headers)
            except httpx.HTTPError as exc:
                resp, last_error = None, f"{type(exc).__name__}: {exc}"
            ms = int((time.monotonic() - t0) * 1000)
            if resp is not None:
                if resp.status_code == 304 and meta and body is not None:
                    result = FetchResult(url, meta["final_url"], 200, body, True)
                    runlog.event(COMPONENT, "fetch", url, "not_modified", duration_ms=ms)
                    self._memo[url] = result
                    return result
                if resp.status_code < 400:
                    result = FetchResult(url, str(resp.url), resp.status_code, resp.content, False)
                    self._cache_write(url, result, resp.headers)
                    runlog.event(COMPONENT, "fetch", url, "ok", duration_ms=ms, status=resp.status_code,
                                 final_url=result.final_url, bytes=len(resp.content))
                    self._memo[url] = result
                    return result
                if resp.status_code != 429 and resp.status_code < 500:
                    runlog.event(COMPONENT, "fetch", url, f"http_{resp.status_code}", duration_ms=ms)
                    raise FetchError(url, f"HTTP {resp.status_code}", resp.status_code)
                last_error = f"HTTP {resp.status_code}"
                if resp.status_code == 429:
                    wait_s = max(wait_s, self.rate_limit_backoff_s, _retry_after(resp) or 0)
            if attempt < self.retries:
                runlog.event(COMPONENT, "fetch", url, "retry", attempt=attempt, wait_s=wait_s, error=last_error)
                self._sleep(wait_s)
                wait_s *= 2
        runlog.event(COMPONENT, "fetch", url, "error", error=last_error, attempts=self.retries)
        raise FetchError(url, last_error)

    # --- internes -----------------------------------------------------------------------
    def _request(self, url: str, headers: dict[str, str]) -> httpx.Response:
        with self._lock:
            wait = self.min_interval_s - (time.monotonic() - self._last_request)
            if wait > 0:
                self._sleep(wait)
            self._last_request = time.monotonic()
        return self._client.get(url, headers=headers)

    def _allowed(self, url: str) -> bool:
        if not self.respect_robots:
            return True
        parts = urlsplit(url)
        root = f"{parts.scheme}://{parts.netloc}"
        if root not in self._robots:
            parser: RobotFileParser | None = None
            try:
                resp = self._request(root + "/robots.txt", {})
                if resp.status_code == 200:
                    parser = RobotFileParser()
                    parser.parse(resp.text.splitlines())
            except httpx.HTTPError:
                parser = None
            self._robots[root] = parser
        parser = self._robots[root]
        return True if parser is None else parser.can_fetch(self.user_agent, url)

    def _cache_paths(self, url: str) -> tuple[Path, Path]:
        key = hashlib.sha1(url.encode("utf-8")).hexdigest()
        return self.cache_dir / f"{key}.json", self.cache_dir / f"{key}.bin"

    def _cache_read(self, url: str) -> tuple[dict | None, bytes | None]:
        meta_path, body_path = self._cache_paths(url)
        if not meta_path.exists() or not body_path.exists():
            return None, None
        return json.loads(meta_path.read_text(encoding="utf-8")), body_path.read_bytes()

    def _cache_write(self, url: str, result: FetchResult, headers: httpx.Headers) -> None:
        meta_path, body_path = self._cache_paths(url)
        body_path.write_bytes(result.content)
        meta = {"url": url, "final_url": result.final_url, "etag": headers.get("etag"),
                "last_modified": headers.get("last-modified")}
        meta_path.write_text(json.dumps(meta), encoding="utf-8")


_shared: dict[tuple, PoliteFetcher] = {}


def shared_fetcher(cache_dir: str, min_interval_s: float, user_agent: str) -> PoliteFetcher:
    """Une instance par configuration dans le processus : mémo et débit partagés entre composants."""
    key = (str(cache_dir), min_interval_s, user_agent)
    if key not in _shared:
        _shared[key] = PoliteFetcher(cache_dir, min_interval_s=min_interval_s, user_agent=user_agent)
    return _shared[key]
