"""Objets échangés entre les composants du pipeline."""

import json
from typing import Literal

from pydantic import BaseModel, Field

from tsi_agent.ids import sha256_text

# SRSS : assistance technique a la Grece anterieure au SRSP (2012-2016, annexe du PDF SRSP).
Programme = Literal["TSI", "SRSP", "SRSS"]
PageKind = Literal["factsheet", "spotlight", "policy_area", "srsp_pdf", "programme_doc", "sitemap"]


class RawPage(BaseModel):
    """Page source brute. `key` = slug pays, slug domaine, id de document ou URL de l'index du sitemap."""

    kind: PageKind
    key: str
    url: str
    content: bytes
    title: str | None = None


class ProjectRef(BaseModel):
    """Projet tel que vu dans les listings (avant extraction)."""

    project_id: str
    title: str
    listing_titles: list[str] = Field(default_factory=list)
    programme: Programme | None = None
    year: int | None = None
    countries: list[str] = Field(default_factory=list)
    multi_country: bool = False
    detail_url: str | None = None
    policy_area: str | None = None
    spotlight: bool = False
    highlighted: bool = False
    listing_urls: list[str] = Field(default_factory=list)
    aliases: list[str] = Field(default_factory=list)
    # Date <lastmod> du sitemap pour la page detail (mode incremental) ; hors listing_hash : ce n'est pas du listing.
    sitemap_lastmod: str | None = None
    # Liens /publications-0/ lus sur la page detail par `run` (None : page non lue ce run) ; hors listing_hash.
    publication_urls: list[str] | None = None

    @property
    def detail_page(self) -> bool:
        return self.detail_url is not None

    def listing_hash(self) -> str:
        """Hash des métadonnées de listing : change si pays, drapeaux ou sources changent."""
        payload = {
            "title": self.title,
            "listing_titles": sorted(self.listing_titles),
            "programme": self.programme,
            "year": self.year,
            "countries": sorted(self.countries),
            "multi_country": self.multi_country,
            "detail_url": self.detail_url,
            "policy_area": self.policy_area,
            "spotlight": self.spotlight,
            "highlighted": self.highlighted,
            "listing_urls": sorted(self.listing_urls),
        }
        return sha256_text(json.dumps(payload, sort_keys=True))


class DetailPage(BaseModel):
    ref: ProjectRef
    url: str
    title: str
    text: str
    content_hash: str
    programme: Programme | None = None
    year: int | None = None
    intro: str = ""  # texte officiel, verbatim (Markdown)
    sections: list[tuple[str, str]] = Field(default_factory=list)  # (titre h3 officiel, corps Markdown verbatim)
    results: str | None = None  # "achieved" | "expected" | None


class ProjectContent(BaseModel):
    # Docstring en ASCII : le schema JSON est passe en argument de ligne de commande.
    # Le LLM ne produit que le resume et les pays : le texte officiel est repris tel quel de la page.
    """Content generated from a project page: a summary and, when stated, the beneficiary countries."""

    summary: str
    countries: list[str] = Field(default_factory=list)


class ProjectRecord(BaseModel):
    ref: ProjectRef
    listing_hash: str  # hash de la vue listing (avant enrichissement par la page détail et le LLM)
    content: ProjectContent
    content_hash: str
    model: str
    intro: str = ""
    sections: list[tuple[str, str]] = Field(default_factory=list)
    results: str | None = None  # "achieved" | "expected" | None
    page_text: str = ""  # texte brut de la page : repli du rendu quand aucune section n'a été reconnue


class ProgrammeDoc(BaseModel):
    doc_id: str
    title: str
    url: str
    text: str

    @property
    def content_hash(self) -> str:
        return sha256_text(self.text)


class PublicationFile(BaseModel):
    document: str  # titre du document sur la page de publication (ex. "Final Report")
    url: str
    language: str | None = None  # code ISO 639-1 lu dans l'URL (/document/download/<uuid>_<lang>)
    filename: str = ""
    size_label: str = ""  # ex. "(569.01 KB - PDF)"


class PublicationParse(BaseModel):
    title: str
    date: str | None = None
    meta: dict[str, str] = Field(default_factory=dict)
    documents: list[list[PublicationFile]] = Field(default_factory=list)  # par document : version principale d'abord


class Publication(BaseModel):
    publication_id: str
    url: str
    project_ids: list[str] = Field(default_factory=list)
    title: str = ""
    date: str | None = None
    meta: dict[str, str] = Field(default_factory=dict)
    files: list[PublicationFile] = Field(default_factory=list)  # fichiers retenus (anglais en priorite)
    files_key: str | None = None
    sitemap_lastmod: str | None = None  # date <lastmod> du sitemap de la page de publication (mode incremental)


class DeliverableFileText(BaseModel):
    file: PublicationFile
    pages: int
    words: int
    text: str


class DeliverableText(BaseModel):
    publication: Publication
    files: list[DeliverableFileText]
    full_text_path: str  # relatif a output_dir, ex. "deliverables/<id>.md"


class DeliverableContent(BaseModel):
    # Docstring en ASCII : le schema JSON est passe en argument de ligne de commande.
    """Content generated from the deliverables (final reports, presentations, factsheets) of a project."""

    summary: str
    key_outputs: list[str] = Field(default_factory=list)
    results: list[str] = Field(default_factory=list)
    figures: list[str] = Field(default_factory=list)


class DeliverableRecord(BaseModel):
    text: DeliverableText
    content: DeliverableContent
    model: str
