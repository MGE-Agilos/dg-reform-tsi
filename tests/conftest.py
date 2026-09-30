import json
from pathlib import Path
from typing import Callable

import pytest
from haystack import component
from haystack.dataclasses import ChatMessage

from tsi_agent.http import FetchError, FetchResult

FIXTURES = Path(__file__).parent / "fixtures"


def fixture_bytes(name: str) -> bytes:
    return (FIXTURES / name).read_bytes()


@pytest.fixture
def fixtures_dir() -> Path:
    return FIXTURES


class FakeFetcher:
    """Remplace PoliteFetcher : `pages` url -> contenu, `redirects` url -> url finale, `default` pour le reste."""

    def __init__(self, pages: dict[str, bytes] | None = None, redirects: dict[str, str] | None = None,
                 default: Callable[[str], bytes | None] | None = None):
        self.pages = pages or {}
        self.redirects = redirects or {}
        self.default = default
        self.calls: list[str] = []

    def get(self, url: str) -> FetchResult:
        self.calls.append(url)
        final = self.redirects.get(url, url)
        content = self.pages.get(final)
        if content is None and self.default is not None:
            content = self.default(final)
        if content is None:
            raise FetchError(url, "HTTP 404", 404)
        return FetchResult(url, final, 200, content, False)


CONTENT_JSON = json.dumps({
    "summary": "The Commission supported HEMS in Croatia.",
    "countries": ["HR"],
})


@component
class FakeChatGenerator:
    """Chat generator de test : renvoie les réponses prévues, dans l'ordre (la dernière est répétée)."""

    def __init__(self, replies: list[str] | None = None, cost_usd: float | None = 0.01, error: Exception | None = None):
        self.replies = replies or [CONTENT_JSON]
        self.cost_usd = cost_usd
        self.error = error
        self.calls: list[list[ChatMessage]] = []

    @component.output_types(replies=list[ChatMessage])
    def run(self, messages: list[ChatMessage]):
        self.calls.append(messages)
        if self.error is not None:
            raise self.error
        text = self.replies[min(len(self.calls), len(self.replies)) - 1]
        meta = {"model": "fake/model", "usage": {"prompt_tokens": 100, "completion_tokens": 20},
                "cost_usd": self.cost_usd}
        return {"replies": [ChatMessage.from_assistant(text, meta=meta)]}
