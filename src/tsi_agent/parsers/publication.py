"""Pages de publication (livrables des projets) et liens de publication des pages detail."""

import re
from urllib.parse import parse_qs, unquote, urljoin, urlsplit

from tsi_agent.ids import clean_text, sha256_text
from tsi_agent.models import PublicationFile, PublicationParse
from tsi_agent.parsers import main_of, soup_of

SITE = "https://reforms-investments.ec.europa.eu"
_PUBLICATION_PATH = re.compile(r"^/publications-0/[^/?#]+$")
_LANG = re.compile(r"_([a-z]{2})$")


def publication_links(detail_html: bytes | str) -> list[str]:
    """Liens /publications-0/... du contenu principal d'une page detail (section « More about the project »)."""
    links: list[str] = []
    for a in main_of(soup_of(detail_html)).select("a[href]"):
        url = urljoin(SITE, a["href"].split("#")[0])
        parts = urlsplit(url)
        if parts.netloc == urlsplit(SITE).netloc and _PUBLICATION_PATH.match(parts.path) and url not in links:
            links.append(url)
    return links


def _file(document: str, a, size_label: str) -> PublicationFile:
    url = urljoin(SITE, a["href"])
    parts = urlsplit(url)
    lang = _LANG.search(parts.path)
    filename = parse_qs(parts.query).get("filename", [parts.path.rsplit("/", 1)[-1]])[0]
    return PublicationFile(document=document, url=url, language=lang.group(1) if lang else None,
                           filename=unquote(filename), size_label=size_label)


def parse_publication(html: bytes | str) -> PublicationParse:
    soup = soup_of(html)
    main = main_of(soup)
    h1 = soup.find("h1")
    meta: dict[str, str] = {}
    for dt in main.select("dt"):
        dd = dt.find_next("dd")
        if dd is not None:
            meta[clean_text(dt.get_text(" "))] = clean_text(dd.get_text(" "))
    documents: list[list[PublicationFile]] = []
    for block in main.select("div.ecl-file"):
        title_el = block.select_one(".ecl-file__title")
        document = clean_text(title_el.get_text(" ")) if title_el else ""
        variants: list[PublicationFile] = []
        primary = block.select_one("a.ecl-file__download")
        if primary is not None and primary.get("href"):
            size = block.select_one(".ecl-file__meta")
            variants.append(_file(document, primary, clean_text(size.get_text(" ")) if size else ""))
        for item in block.select("li.ecl-file__translation-item"):
            a = item.select_one("a.ecl-file__translation-download")
            if a is not None and a.get("href"):
                size = item.select_one(".ecl-file__translation-meta")
                variants.append(_file(document, a, clean_text(size.get_text(" ")) if size else ""))
        if variants:
            documents.append(variants)
    return PublicationParse(title=clean_text(h1.get_text(" ")) if h1 else "", date=meta.get("Publication date"),
                            meta=meta, documents=documents)


def select_files(parse: PublicationParse) -> list[PublicationFile]:
    """Par document : la version anglaise si elle existe, sinon la version principale."""
    return [next((v for v in variants if v.language == "en"), variants[0]) for variants in parse.documents]


def files_key(files: list[PublicationFile]) -> str:
    """Empreinte de la liste des fichiers : chaque URL porte un uuid, un fichier remplace change la cle."""
    return sha256_text("\n".join(sorted(f.url for f in files)))
