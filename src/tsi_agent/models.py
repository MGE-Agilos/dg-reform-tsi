"""Objets échangés entre les composants du pipeline."""

import json
from typing import Literal

from pydantic import BaseModel, Field

from tsi_agent.ids import sha256_text

Programme = Literal["TSI", "SRSP"]
PageKind = Literal["factsheet", "spotlight", "policy_area", "srsp_pdf", "programme_doc"]


class RawPage(BaseModel):
    """Page source brute. `key` = slug pays, slug domaine ou id de document."""

    kind: PageKind
    key: str
    url: str
    content: bytes
    title: str | None = None


class ProjectRef(BaseModel):
    """Projet tel que vu dans les listings (avant extraction)."""

    project_id: str
    title: str
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

    @property
    def detail_page(self) -> bool:
        return self.detail_url is not None

    def listing_hash(self) -> str:
        """Hash des métadonnées de listing : change si pays, drapeaux ou sources changent."""
        payload = {
            "title": self.title,
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


class ProjectContent(BaseModel):
    # Docstring en ASCII : il part dans le JSON schema passé en argument au CLI (via cmd.exe).
    """Structured content extracted from a project page."""

    summary: str
    context: str
    support_delivered: str
    results_achieved: str
    countries: list[str] = Field(default_factory=list)


class ProjectRecord(BaseModel):
    ref: ProjectRef
    content: ProjectContent
    content_hash: str
    model: str


class ProgrammeDoc(BaseModel):
    doc_id: str
    title: str
    url: str
    text: str

    @property
    def content_hash(self) -> str:
        return sha256_text(self.text)
