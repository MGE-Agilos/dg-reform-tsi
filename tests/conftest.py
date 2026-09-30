from pathlib import Path
from typing import Callable

import pytest

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
