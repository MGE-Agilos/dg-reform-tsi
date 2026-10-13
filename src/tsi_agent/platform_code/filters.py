from typing import Any, Optional

from haystack import component


@component
class Code:
    """Filtres de metadonnees (index tsi-corpus) construits a partir des arguments des outils list/find_projects.

    catalog_filters : tous les morceaux projet correspondant aux filtres SANS les pays ; le filtrage par pays se
    fait ensuite dans le code (un projet multi-pays porte une liste de pays : le resultat ne depend pas de la
    facon dont le document store compare une liste). project_filters / deliverable_filters : memes filtres AVEC
    les pays, pour les recherches par similarite (le pool doit deja etre restreint aux pays demandes).
    """

    @component.output_types(catalog_filters=dict[str, Any], project_filters=dict[str, Any],
                            deliverable_filters=dict[str, Any])
    def run(self, countries: Optional[list[str]] = None, year_from: Optional[int] = None,
            year_to: Optional[int] = None, programme: Optional[str] = None, policy_area: Optional[str] = None,
            detail_only: bool = False) -> dict:
        common = []
        if year_from:
            common.append({"field": "meta.year", "operator": ">=", "value": int(year_from)})
        if year_to:
            common.append({"field": "meta.year", "operator": "<=", "value": int(year_to)})
        if programme:
            common.append({"field": "meta.programme", "operator": "==", "value": programme.strip().upper()})
        if policy_area:
            common.append({"field": "meta.policy_area", "operator": "==", "value": policy_area.strip()})
        isos = [c.strip().upper() for c in (countries or []) if c and c.strip()]
        by_country = [{"field": "meta.countries", "operator": "in", "value": isos}] if isos else []
        project = [{"field": "meta.doc_type", "operator": "==", "value": "project"}, *common]
        if detail_only:
            project.append({"field": "meta.detail_page", "operator": "==", "value": True})
        deliverable = [{"field": "meta.doc_type", "operator": "==", "value": "deliverable"}, *common, *by_country]
        return {"catalog_filters": {"operator": "AND", "conditions": project},
                "project_filters": {"operator": "AND", "conditions": [*project, *by_country]},
                "deliverable_filters": {"operator": "AND", "conditions": deliverable}}
