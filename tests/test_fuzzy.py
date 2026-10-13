"""Regle de fusion floue thin <-> page detail, sur des exemples reels du site et du PDF SRSP."""

import itertools

from tsi_agent.components.discoverer import fuzzy_merge
from tsi_agent.fuzzy import THRESHOLD, countries_in_text, match_thin_refs, normalize_title, title_similarity
from tsi_agent.models import ProjectRef

BASE = "https://reforms-investments.ec.europa.eu/technical-support-instrument-0"
PDF = "https://commission.europa.eu/system/files/2020-03/rss-technical-support-per-country_en.pdf"
SITEMAP = "https://reforms-investments.ec.europa.eu/sitemap.xml"


def detail(slug: str, title: str, area: str = "competitiveness", **kw) -> ProjectRef:
    return ProjectRef(project_id=slug, title=title, detail_url=f"{BASE}/{area}/{slug}_en", **kw)


def thin(pid: str, title: str, programme: str, year: int, countries: list[str], **kw) -> ProjectRef:
    return ProjectRef(project_id=pid, title=title, programme=programme, year=year, countries=countries, **kw)


# --- paires reelles ---------------------------------------------------------------------------------------------

ROMANIA_DETAIL = detail("comprehensive-redesign-licensing-system-romania",
                        "Comprehensive redesign of the licensing system in Romania", programme="SRSP", year=2019,
                        listing_urls=[SITEMAP])
ROMANIA_THIN = thin("srsp-2019-comprehensive-redesign-of-the-licensing-system-in-romania",
                    "Comprehensive redesign of the licensing system in Romania", "SRSP", 2019, ["RO"],
                    listing_urls=[PDF])
BIM_DETAIL = detail("implementation-bim-roadmap-greece-including-pilot-projects",
                    "Implementation of the BIM roadmap in Greece, including pilot projects", programme="TSI",
                    year=2023, listing_urls=[SITEMAP])
BIM_THIN = thin("tsi-2023-implementation-of-the-bim-roadmap-in-greece-including-pilot-projects",
                "Implementation of the BIM Roadmap in Greece, including pilot projects.", "TSI", 2023, ["GR"])
# Doublon (c) : page detail vue dans le spotlight (ni programme ni annee au moment de la decouverte).
POLAND_DETAIL = detail("modernization-national-data-information-infrastructure-statistics-poland",
                       "Modernization of the national data-information infrastructure - Statistics Poland",
                       area="digital-transition", countries=["PL"], spotlight=True)
POLAND_THIN = thin("srsp-2019-modernization-of-the-national-data-information-infrastructure-statistics-poland",
                   "Modernization of the national data-information infrastructure - statistics Poland", "SRSP", 2019,
                   ["PL"], listing_urls=[PDF])


def test_normalize_title_drops_articles_stopwords_and_country_words():
    assert normalize_title("The Comprehensive redesign of the licensing system in Romania") == [
        "comprehensive", "redesign", "licensing", "system"]
    assert normalize_title("Digital transformation of the Greek industry") == ["digital", "transformation",
                                                                               "industry"]
    assert normalize_title("Effective induction system for novice teachers in Belgium (Flanders)") == [
        "effective", "induction", "system", "novice", "teachers"]


def test_countries_in_text_reads_names_and_adjectives():
    assert countries_in_text("Implementation of the BIM roadmap in Greece") == ["GR"]
    assert countries_in_text("Digital transformation of the Hungarian higher education") == ["HU"]
    assert countries_in_text("Improving energy efficiency in the Czech Republic") == ["CZ"]
    assert countries_in_text("Developing government consulting capacities in Austria, Belgium and Latvia") == [
        "AT", "BE", "LV"]
    assert countries_in_text("Support to the Renovation Wave") == []


def test_similarity_of_real_pairs_reaches_the_threshold():
    assert title_similarity(ROMANIA_DETAIL.title, ROMANIA_THIN.title) == 1.0
    assert title_similarity(BIM_DETAIL.title, BIM_THIN.title) == 1.0
    assert title_similarity(POLAND_DETAIL.title, POLAND_THIN.title) == 1.0
    # Variantes reelles de redaction (slug sitemap -> titre du PDF SRSP).
    assert title_similarity("Marketing practices and ageing populations to avoid financial misselling",
                            "Marketing practices and ageing populations to avoid financial mis-selling") >= THRESHOLD
    assert title_similarity("Supporting public administration reform in Bulgaria",
                            "Support for public administration reform") >= THRESHOLD


def test_similarity_rejects_other_phase_and_close_spelling_with_other_words():
    assert title_similarity("Reforming the design and delivery of active labour market policies (ALMPs)",
                            "Reforming the design and delivery of active labour market policies (ALMPs) - phase II") == 0
    assert title_similarity("Better regulation - phase II", "Better regulation - phase III") == 0
    assert title_similarity("Waste management", "Water management") == 0


def discover_merge(refs):
    """Enchainement du decouvreur : cle titre exacte (merge_refs) puis regle floue."""
    from tsi_agent.components.discoverer import ambiguous_title_keys, merge_refs
    return fuzzy_merge(merge_refs(refs), excluded_thin_ids=ambiguous_title_keys(refs))


def test_real_pairs_merge_and_keep_the_detail_id():
    # Romania et BIM : meme cle titre une fois l'annee lue sur la page (regle existante) ; Poland : regle floue
    # (le spotlight ne donne ni annee ni programme).
    for d, t, rule in [(ROMANIA_DETAIL, ROMANIA_THIN, "key"), (BIM_DETAIL, BIM_THIN, "key"),
                       (POLAND_DETAIL, POLAND_THIN, "fuzzy")]:
        merged, matches = discover_merge([t, d])
        assert len(merged) == 1, d.project_id
        m = merged[0]
        assert m.project_id == d.project_id and m.detail_url == d.detail_url
        assert t.project_id in m.aliases
        assert m.programme == t.programme and m.year == t.year
        assert set(t.listing_urls) <= set(m.listing_urls)
        if rule == "fuzzy":
            assert [(x.detail_id, x.thin_id, x.score) for x in matches] == [(d.project_id, t.project_id, 1.0)]
        else:
            assert matches == []


def test_same_title_other_country_does_not_merge():
    # "Support to the Renovation Wave" : pages detail LT et MT, mention thin CY + HU.
    base = dict(programme="TSI", year=2022, area="green-transition")
    lt = detail("support-renovation-wave", "Support to the Renovation Wave", countries=["LT"], **base)
    mt = detail("support-renovation-wave-0", "Support to the Renovation Wave", countries=["MT"], **base)
    cyhu = thin("tsi-2022-support-to-the-renovation-wave", "Support to the Renovation Wave", "TSI", 2022,
                ["CY", "HU"], multi_country=True)
    merged, matches = fuzzy_merge([lt, mt, cyhu])
    assert len(merged) == 3 and matches == []
    # "Climate adaptation" : page detail IT, mention thin MT ; "Micro data platform" : AT contre six autres pays.
    it = detail("climate-adaptation", "Climate adaptation", programme="TSI", year=2023, countries=["IT"])
    mt_thin = thin("tsi-2023-climate-adaptation", "Climate adaptation", "TSI", 2023, ["MT"])
    at = detail("micro-data-platform-productivity", "Micro data platform for productivity", programme="TSI",
                year=2023, countries=["AT"])
    six = thin("tsi-2022-micro-data-platform-for-productivity", "Micro data platform for productivity", "TSI", 2022,
               ["FR", "DE", "LV", "PT", "SK", "SI"])
    assert match_thin_refs([it, mt_thin, at, six]) == []


def test_two_detail_pages_never_merge():
    a = detail("a", "Capital market diagnostic", programme="SRSP", year=2019, countries=["HR"])
    b = detail("b", "Capital market diagnostic", area="financial-sector-and-access-finance", programme="SRSP",
               year=2019, countries=["HR"])
    assert match_thin_refs([a, b]) == []


def test_year_and_programme_compatibility():
    d = detail("capital-market-diagnostic-croatia", "Capital market diagnostic in Croatia", programme="SRSP",
               year=2019, countries=["HR"])
    same = thin("srsp-2019-capital-market-diagnostic", "Capital market diagnostic", "SRSP", 2019, ["HR"])
    next_year = thin("srsp-2020-capital-market-diagnostic", "Capital market diagnostic", "SRSP", 2020, ["HR"])
    far = thin("srsp-2017-capital-market-diagnostic", "Capital market diagnostic", "SRSP", 2017, ["HR"])
    other_programme = thin("tsi-2020-capital-market-diagnostic", "Capital market diagnostic", "TSI", 2020, ["HR"])
    assert [m.thin_id for m in match_thin_refs([d, far])] == []
    assert [m.thin_id for m in match_thin_refs([d, other_programme])] == []
    assert [m.thin_id for m in match_thin_refs([d, next_year])] == ["srsp-2020-capital-market-diagnostic"]
    # Plusieurs candidats : le meilleur score ; a score egal, l'annee exacte d'abord.
    assert [m.thin_id for m in match_thin_refs([d, next_year, same])] == ["srsp-2019-capital-market-diagnostic"]


def test_one_year_apart_needs_a_near_identical_title():
    # Cas reel : deux projets distincts, titres proches, annees voisines, pays communs.
    pillar = detail("improving-capacity-member-states-effectively-implement-pillar-two-directive",
                    "Improving Capacity in Member States to Effectively Implement the Pillar Two Directive",
                    area="revenue-administration-and-public-financial-management", programme="TSI", year=2025,
                    countries=["AT", "BE", "FR", "DE", "IE", "PL", "SE"])
    faster = thin("tsi-2026-improving-capacity-in-member-states-to-effectively-implement-the-faster-directive",
                  "Improving capacity in Member States to effectively implement the FASTER Directive", "TSI", 2026,
                  ["AT", "BE", "DK", "FR", "DE", "IE", "MT", "PL", "SE"])
    assert THRESHOLD <= title_similarity(pillar.title, faster.title) < 0.95
    assert match_thin_refs([pillar, faster]) == []
    same_year = faster.model_copy(update={"year": 2025})
    assert len(match_thin_refs([pillar, same_year])) == 1


def test_ambiguous_candidates_are_not_merged():
    # Page sans pays ni annee, deux mentions de meme titre dans deux pays : on ne choisit pas au hasard.
    d = detail("support-renovation-wave-2", "Support to the Renovation Wave", area="green-transition")
    a = thin("tsi-2022-support-to-the-renovation-wave", "Support to the Renovation Wave", "TSI", 2022, ["CY"])
    b = thin("tsi-2023-support-to-the-renovation-wave", "Support to the Renovation Wave", "TSI", 2023, ["HU"])
    assert match_thin_refs([d, a, b]) == []


def test_each_thin_ref_goes_to_one_detail_page_at_most():
    a = detail("better-regulation-austria", "Better regulation in Austria", programme="SRSP", year=2020,
               countries=["AT"])
    b = detail("better-regulation", "Better regulation", programme="SRSP")
    t = thin("srsp-2020-better-regulation", "Better regulation", "SRSP", 2020, ["AT"])
    # Meme score : la page dont le pays est connu et commun l'emporte ; l'autre page reste seule.
    assert [(m.detail_id, m.thin_id) for m in match_thin_refs([b, a, t])] == [
        ("better-regulation-austria", "srsp-2020-better-regulation")]
    # Deux pages strictement equivalentes : ambigu, aucune fusion.
    c = detail("better-regulation-0", "Better regulation", programme="SRSP")
    assert match_thin_refs([b, c, t]) == []


def test_views_supply_hints_without_changing_the_refs():
    # Le spotlight ne donne ni annee ni programme : la vue les prend sur la page detail en cache.
    d = POLAND_DETAIL
    other_year = thin("srsp-2017-modernization-of-the-national-data-information-infrastructure-statistics-poland",
                      POLAND_THIN.title, "SRSP", 2017, ["PL"])
    view = d.model_copy(update={"programme": "SRSP", "year": 2019})
    assert match_thin_refs([d, other_year], views=[view, other_year]) == []
    assert len(match_thin_refs([d, other_year])) == 1  # sans indice, annee inconnue = compatible


def test_fuzzy_merge_is_order_independent():
    refs = [ROMANIA_DETAIL, ROMANIA_THIN, BIM_DETAIL, BIM_THIN, POLAND_DETAIL, POLAND_THIN]
    expected = sorted([ROMANIA_DETAIL.project_id, BIM_DETAIL.project_id, POLAND_DETAIL.project_id])
    for perm in itertools.permutations(refs):
        merged, _ = discover_merge(list(perm))
        assert sorted(r.project_id for r in merged) == expected


def test_generic_flagship_title_stays_a_separate_thin_project():
    # Cas reel : "Industrial ecosystems" (TSI 2023) pointe vers une page detail propre au Portugal, une autre
    # propre a l'Italie, et apparait sans lien pour Chypre. merge_refs garde la mention sans lien a part
    # (cle ambigue) : la fusion floue ne doit pas la rattacher a l'une des pages.
    from tsi_agent.components.discoverer import ambiguous_title_keys, merge_refs
    base = dict(title="Industrial ecosystems", programme="TSI", year=2023, multi_country=True)
    pt = ProjectRef(project_id="strengthening-portugals-policy-frameworks-stimulate-greening-smes-and-green-entrepreneurship",
                    detail_url=f"{BASE}/green-transition/strengthening-portugals-policy-frameworks-stimulate-greening-"
                               "smes-and-green-entrepreneurship_en", countries=["PT"], **base)
    it = ProjectRef(project_id="industrial-ecosystems-italy", detail_url=f"{BASE}/competitiveness/industrial-ecosystems-italy_en",
                    countries=["IT"], **base)
    cy = ProjectRef(project_id="tsi-2023-industrial-ecosystems", countries=["CY"], **base)
    pt_thin = ProjectRef(project_id="tsi-2023-industrial-ecosystems", countries=["PT"], **base)
    refs = [pt, it, cy, pt_thin]
    merged = merge_refs(refs)
    assert len(merged) == 3
    out, matches = fuzzy_merge(merged, excluded_thin_ids=ambiguous_title_keys(refs))
    assert len(out) == 3 and matches == []


def test_same_title_key_is_left_to_merge_refs():
    # Meme cle titre (programme, annee, titre) : merge_refs a deja decide (conflit de pages detail) ; pas de 2e avis.
    d = detail("x-detail", "Better regulation", programme="SRSP", year=2020, countries=["AT"])
    t = thin("srsp-2020-better-regulation", "Better regulation", "SRSP", 2020, ["AT"])
    assert match_thin_refs([d, t]) == []
    assert len(match_thin_refs([d.model_copy(update={"year": None}), t])) == 1
