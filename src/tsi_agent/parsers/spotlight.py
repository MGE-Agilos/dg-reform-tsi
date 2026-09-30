"""Parser de la page « Projects in the spotlight »."""

import re

from tsi_agent.countries import iso_from_name
from tsi_agent.ids import clean_text, slug_from_url
from tsi_agent.models import ProjectRef
from tsi_agent.parsers import main_of, soup_of
from tsi_agent.urls import absolutize, is_detail_candidate

# Le texte alternatif des drapeaux est parfois faux ; le nom de fichier est fiable.
_FLAG = re.compile(r"/([A-Za-z]+)_flag_photo")


def parse_spotlight(html: bytes | str, url: str) -> list[ProjectRef]:
    refs = []
    for card in main_of(soup_of(html)).select("article.ecl-content-item"):
        a = card.find("a", href=True)
        if a is None:
            continue
        detail = absolutize(a["href"])
        if not is_detail_candidate(detail):
            continue
        countries, multi = [], False
        img = card.find("img", src=True)
        m = _FLAG.search(img["src"]) if img else None
        if m and m.group(1) == "Europe":
            multi = True
        elif m and iso_from_name(m.group(1)):
            countries = [iso_from_name(m.group(1))]
        refs.append(ProjectRef(
            project_id=slug_from_url(detail), title=clean_text(a.get_text(" ")), countries=countries,
            multi_country=multi, detail_url=detail, spotlight=True, listing_urls=[url],
        ))
    return refs
