"""Identifiants stables : slugs, clés titre, hash de contenu."""

import hashlib
import re
import unicodedata
from urllib.parse import urlsplit

_PACE_PREFIX = re.compile(r"^pace\s*[-–—]\s*")
_NON_ALNUM = re.compile(r"[^a-z0-9]+")


def clean_text(text: str) -> str:
    """Réduit tous les blancs (y compris sauts de ligne) à un espace simple."""
    return " ".join(text.split())


def slugify(text: str, max_len: int = 80) -> str:
    t = _PACE_PREFIX.sub("", clean_text(text).lower())
    t = unicodedata.normalize("NFKD", t).encode("ascii", "ignore").decode()
    t = _NON_ALNUM.sub("-", t).strip("-")
    return t[:max_len].rstrip("-")


def title_key(programme: str, year: int, title: str) -> str:
    """Clé de fusion par titre, et id d'un projet sans page détail."""
    return f"{programme.lower()}-{year}-{slugify(title)}"


def slug_from_url(url: str) -> str:
    last = urlsplit(url).path.rstrip("/").rsplit("/", 1)[-1]
    return last[:-3] if last.endswith("_en") else last


def sha256_text(text: str) -> str:
    return "sha256:" + hashlib.sha256(text.encode("utf-8")).hexdigest()
