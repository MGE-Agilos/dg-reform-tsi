"""Parsers déterministes des sources (HTML et PDF)."""

from bs4 import BeautifulSoup, Tag


def soup_of(html: bytes | str) -> BeautifulSoup:
    return BeautifulSoup(html, "html.parser")


def main_of(soup: BeautifulSoup) -> Tag:
    return soup.find("main") or soup.body or soup


def main_text(html: bytes | str) -> str:
    """Texte du contenu principal, une ligne par bloc, sans les marqueurs techniques du CMS."""
    lines = []
    for line in main_of(soup_of(html)).get_text("\n", strip=True).splitlines():
        line = " ".join(line.split())
        if not line or line == "Page contents" or line.startswith("[notranslate]"):
            continue
        lines.append(line)
    return "\n".join(lines)
