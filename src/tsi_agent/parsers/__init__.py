"""Parsers déterministes des sources (HTML et PDF)."""

import re
from collections.abc import Iterable

from bs4 import BeautifulSoup, NavigableString, PageElement, Tag
from bs4.element import Comment, Declaration, Doctype, ProcessingInstruction


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


# --- HTML -> Markdown (texte officiel verbatim, seuls les espaces sont normalisés) ---

_SKIP = {"img", "picture", "figure", "iframe", "video", "audio", "svg", "script", "style", "noscript", "source",
         "button", "form", "input", "template"}
_LISTS = {"ul", "ol"}
_SUBHEADINGS = {"h4", "h5", "h6"}
_BLOCKS = {"p", "div", "section", "article", "blockquote", "header", "footer", "aside", "nav", "table", "thead",
           "tbody", "tfoot", "tr", "td", "th", "dl", "dt", "dd", "figcaption", "li", "h1", "h2", "h3", "pre",
           "hr", "main", "details", "summary", *_LISTS, *_SUBHEADINGS}
_NOT_TEXT = (Comment, Declaration, Doctype, ProcessingInstruction)


def _inline_parts(node: PageElement, out: list[str]) -> None:
    """Texte d'un élément : liens et mises en forme réduits à leur texte, <br> et blocs = saut de ligne."""
    if isinstance(node, NavigableString):
        if not isinstance(node, _NOT_TEXT):
            out.append(re.sub(r"[\r\n]", " ", str(node)))  # sauts de ligne du source = espaces
        return
    if not isinstance(node, Tag) or node.name in _SKIP:
        return
    if node.name == "br":
        out.append("\n")
        return
    block = node.name in _BLOCKS
    if block:
        out.append("\n")
    for child in node.children:
        _inline_parts(child, out)
    if block:
        out.append("\n")


def _lines(nodes: Iterable[PageElement]) -> list[str]:
    out: list[str] = []
    for node in nodes:
        _inline_parts(node, out)
    return [line for line in (" ".join(raw.split()) for raw in "".join(out).split("\n")) if line]


def _list_markdown(tag: Tag, indent: int = 0) -> list[str]:
    lines: list[str] = []
    number = 0
    for li in tag.find_all("li", recursive=False):
        number += 1
        nested = [c for c in li.children if isinstance(c, Tag) and c.name in _LISTS]
        text = " ".join(_lines(c for c in li.children if all(c is not n for n in nested)))
        marker = f"{number}." if tag.name == "ol" else "-"
        if text:
            lines.append(f"{'  ' * indent}{marker} {text}")
        for sub in nested:
            lines.extend(_list_markdown(sub, indent + 1))
    return lines


def _blocks(nodes: Iterable[PageElement], out: list[str]) -> None:
    run: list[PageElement] = []  # texte et éléments en ligne consécutifs = un paragraphe

    def flush() -> None:
        text = "\n".join(_lines(run))
        if text:
            out.append(text)
        run.clear()

    for node in nodes:
        if isinstance(node, Tag) and node.name in _SKIP:
            continue
        if not isinstance(node, Tag) or node.name not in _BLOCKS:
            run.append(node)
            continue
        flush()
        if node.name in _LISTS:
            lines = _list_markdown(node)
            if lines:
                out.append("\n".join(lines))
        elif node.name in _SUBHEADINGS:
            text = " ".join(_lines([node]))
            if text:
                out.append(f"### {text}")
        elif node.name == "p":
            text = "\n".join(_lines([node]))
            if text:
                out.append(text)
        else:
            _blocks(node.children, out)
    flush()


def blocks_markdown(nodes: Iterable[PageElement]) -> str:
    """Suite de nœuds HTML -> Markdown : paragraphes, listes, sous-titres ; ni URL ni image."""
    out: list[str] = []
    _blocks(nodes, out)
    return "\n\n".join(out)
