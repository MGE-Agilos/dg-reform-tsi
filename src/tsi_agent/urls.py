"""Reconnaissance et canonisation des liens vers les pages détail projet."""

import re
from urllib.parse import urljoin, urlsplit

BASE_URL = "https://reforms-investments.ec.europa.eu"
_HOST = "reforms-investments.ec.europa.eu"
_CANDIDATE_HOSTS = {_HOST, "reform-support.ec.europa.eu", "ec.europa.eu"}
_CANONICAL = re.compile(
    r"^https://reforms-investments\.ec\.europa\.eu/technical-support-instrument-0/"
    r"(?!our-projects/|what-we-do/)([a-z0-9-]+)/[a-z0-9-]+_en$"
)
_SECTION_INDEX = re.compile(r"^/technical-support-instrument-0(/[a-z0-9-]+)?_en$")


def absolutize(href: str, base: str = BASE_URL) -> str:
    return urljoin(base + "/", href.strip())


def is_detail_candidate(url: str) -> bool:
    """Lien qui ressemble à une page projet (y compris ancien domaine), avant résolution."""
    parts = urlsplit(url)
    if parts.scheme not in ("http", "https") or parts.netloc not in _CANDIDATE_HOSTS:
        return False
    path = parts.path
    if parts.netloc == "ec.europa.eu" and not path.startswith("/reform-support/"):
        return False
    if not path.endswith("_en") or _SECTION_INDEX.match(path):
        return False
    return not any(bit in path for bit in ("/document/", "/our-projects", "/events/", "/news/", "/publications"))


def is_canonical_detail(url: str) -> bool:
    """URL déjà canonique : pas besoin de suivre les redirections."""
    return bool(_CANONICAL.match(url))


def is_final_detail(url: str) -> bool:
    """URL finale (après redirections) acceptée comme page détail."""
    return urlsplit(url).netloc == _HOST and is_detail_candidate(url)


def policy_area_from_url(url: str) -> str | None:
    m = _CANONICAL.match(url)
    return m.group(1) if m else None
