import ast
import dataclasses
from pathlib import Path

from haystack import Document

from tsi_agent.platform_code import filters, find_aggregate, list_summary

CODE_DIR = Path(__file__).resolve().parents[1] / "src" / "tsi_agent" / "platform_code"


def project(pid, title, countries, year=2022, programme="TSI", area="green-transition", split_id=0, **extra):
    meta = {"doc_type": "project", "doc_id": pid, "title": title, "countries": countries, "year": year,
            "programme": programme, "policy_area": area, "detail_page": True, "source_url": f"https://x.eu/{pid}",
            "projects": [pid], "split_id": split_id, **extra}
    return Document(content=f"# {title} — {programme} {year}\n\nText about {title}.\n\nProject id: {pid}", meta=meta)


def test_each_module_is_a_single_code_component_with_only_allowed_imports():
    for path in CODE_DIR.glob("*.py"):
        if path.name == "__init__.py":
            continue
        tree = ast.parse(path.read_text(encoding="utf-8"))
        classes = [n for n in tree.body if isinstance(n, ast.ClassDef)]
        assert [c.name for c in classes] == ["Code"], path.name
        assert any(getattr(d, "id", None) == "component" for d in classes[0].decorator_list), path.name
        modules = {a.name.split(".")[0] for n in ast.walk(tree) if isinstance(n, ast.Import) for a in n.names}
        modules |= {n.module.split(".")[0] for n in ast.walk(tree) if isinstance(n, ast.ImportFrom)}
        assert modules <= {"haystack", "typing", "collections", "re"}, (path.name, modules)
        # Le validateur du composant Code de la plateforme signale les noms des fonctions definies dans une
        # methode (« Undefined name(s) ») : fonctions utilitaires au niveau du module seulement.
        nested = [n.name for m in classes[0].body if isinstance(m, ast.FunctionDef)
                  for n in ast.walk(m) if isinstance(n, ast.FunctionDef) and n is not m]
        assert nested == [], (path.name, nested)


def test_filters_keep_countries_out_of_the_catalog():
    out = filters.Code().run(countries=["gr", " HR "], year_from=2021, year_to=2023, programme="tsi",
                             policy_area="green-transition", detail_only=True)
    catalog = out["catalog_filters"]["conditions"]
    assert {"field": "meta.doc_type", "operator": "==", "value": "project"} in catalog
    assert {"field": "meta.year", "operator": ">=", "value": 2021} in catalog
    assert {"field": "meta.programme", "operator": "==", "value": "TSI"} in catalog
    assert {"field": "meta.detail_page", "operator": "==", "value": True} in catalog
    assert not any(c["field"] == "meta.countries" for c in catalog)
    by_country = {"field": "meta.countries", "operator": "in", "value": ["GR", "HR"]}
    assert by_country in out["project_filters"]["conditions"]
    deliverable = out["deliverable_filters"]["conditions"]
    assert {"field": "meta.doc_type", "operator": "==", "value": "deliverable"} in deliverable and by_country in deliverable
    assert not any(c["field"] == "meta.detail_page" for c in deliverable)
    assert filters.Code().run()["catalog_filters"] == {
        "operator": "AND", "conditions": [{"field": "meta.doc_type", "operator": "==", "value": "project"}]}


def test_list_summary_counts_distinct_projects_and_filters_countries_in_code():
    docs = [project("a", "Alpha health", ["GR"], split_id=0), project("a", "Alpha health", ["GR"], split_id=1),
            project("b", "Beta", ["GR", "IT"], year=2023), project("c", "Gamma", ["IT"], programme="SRSP", year=2019)]
    text = list_summary.Code().run(documents=docs, countries=["GR"], limit=1)["result"]
    assert text.splitlines()[0] == "Total matching projects: 2 (TSI: 2)"
    assert "By country: GR 2, IT 1" in text and "By policy area: green-transition 2" in text
    assert "Showing 1 of 2:" in text and "- Beta (b) — TSI 2023 — GR, IT — detail page: yes — https://x.eu/b" in text
    text = list_summary.Code().run(documents=docs, title_contains="HEALTH", policy_area="green-transition")["result"]
    assert text.startswith("Total matching projects: 1") and "By policy area" not in text


def test_list_summary_warns_when_the_search_limit_is_hit():
    docs = [project("a", "Alpha", ["GR"])] * list_summary.MAX_RESULTS
    assert list_summary.Code().run(documents=docs)["result"].startswith("WARNING")


def test_find_aggregate_pools_title_project_and_deliverable_hits():
    catalog = [project("aml", "Fighting money laundering", ["GR"]), project("tax", "Tax administration", ["GR"]),
               project("fiu", "Financial intelligence unit", ["IT"], listing_titles=["FIU reform"]),
               project("other", "Unrelated", ["GR"])]
    hit = dataclasses.replace(project("tax", "Tax administration", ["GR"], split_id=2), score=0.8)
    deliverable = Document(content="# Report — p. 3\n\nSuspicious transactions.", score=0.9,
                           meta={"doc_type": "deliverable", "projects": ["other", "unknown"]})
    text = find_aggregate.Code().run(topic="anti-money laundering", keywords=["money laundering", "FIU"],
                                     catalog=catalog, project_hits=[hit], deliverable_hits=[deliverable])["result"]
    lines = text.splitlines()
    assert lines[0].startswith("Candidate projects for 'anti-money laundering': 4 (2 title matches, "
                               "1 from project texts, 1 from deliverables)")
    assert lines[1].startswith("- Fighting money laundering (aml) — TSI 2022 — GR — via: title — Text about")
    assert lines[2].startswith("- Financial intelligence unit (fiu)")          # titre de listing
    assert lines[3].startswith("- Unrelated (other)") and "via: deliverables — Suspicious transactions." in lines[3]
    assert lines[4].startswith("- Tax administration (tax)") and "via: projects" in lines[4]
    only_gr = find_aggregate.Code().run(topic="anti-money laundering", keywords=["FIU", "money laundering"],
                                        countries=["GR"], catalog=catalog, project_hits=[], deliverable_hits=[])
    assert "(fiu)" not in only_gr["result"] and "(aml)" in only_gr["result"]
