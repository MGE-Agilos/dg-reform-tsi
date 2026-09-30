"""Parser d'une page de domaine politique : projets clés liés + texte descriptif."""

from tsi_agent.ids import clean_text, slug_from_url
from tsi_agent.models import ProjectRef
from tsi_agent.parsers import main_of, main_text, soup_of
from tsi_agent.urls import absolutize, is_canonical_detail


def parse_policy_area(html: bytes | str, url: str) -> tuple[list[ProjectRef], str]:
    titles: dict[str, str] = {}
    for a in main_of(soup_of(html)).find_all("a", href=True):
        detail = absolutize(a["href"])
        if not is_canonical_detail(detail):
            continue
        title = clean_text(a.get_text(" "))
        if len(title) > len(titles.get(detail, "")):
            titles[detail] = title
    refs = [
        ProjectRef(project_id=slug_from_url(d), title=t or slug_from_url(d), detail_url=d, listing_urls=[url])
        for d, t in titles.items()
    ]
    return refs, main_text(html)
