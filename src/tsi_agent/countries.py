"""Référentiel des 27 États membres : code ISO, slug de factsheet, noms rencontrés dans les sources."""

COUNTRIES: dict[str, tuple[str, tuple[str, ...]]] = {
    "AT": ("austria", ("Austria",)),
    "BE": ("belgium", ("Belgium",)),
    "BG": ("bulgaria", ("Bulgaria",)),
    "HR": ("croatia", ("Croatia",)),
    "CY": ("cyprus", ("Cyprus",)),
    "CZ": ("czechia", ("Czechia", "Czech Republic")),
    "DK": ("denmark", ("Denmark",)),
    "EE": ("estonia", ("Estonia",)),
    "FI": ("finland", ("Finland",)),
    "FR": ("france", ("France",)),
    "DE": ("germany", ("Germany",)),
    "GR": ("greece", ("Greece",)),
    "HU": ("hungary", ("Hungary",)),
    "IE": ("ireland", ("Ireland",)),
    "IT": ("italy", ("Italy",)),
    "LV": ("latvia", ("Latvia",)),
    "LT": ("lithuania", ("Lithuania",)),
    "LU": ("luxembourg", ("Luxembourg",)),
    "MT": ("malta", ("Malta",)),
    "NL": ("netherlands", ("Netherlands", "The Netherlands")),
    "PL": ("poland", ("Poland",)),
    "PT": ("portugal", ("Portugal",)),
    "RO": ("romania", ("Romania",)),
    "SK": ("slovakia", ("Slovakia",)),
    "SI": ("slovenia", ("Slovenia",)),
    "ES": ("spain", ("Spain",)),
    "SE": ("sweden", ("Sweden",)),
}

_BY_SLUG = {slug: iso for iso, (slug, _) in COUNTRIES.items()}
_BY_NAME = {name.lower(): iso for iso, (_, names) in COUNTRIES.items() for name in names}


def iso_from_slug(slug: str) -> str | None:
    return _BY_SLUG.get(slug)


def slug_from_iso(iso: str) -> str:
    return COUNTRIES[iso][0]


def name_from_iso(iso: str) -> str:
    return COUNTRIES[iso][1][0]


def iso_from_name(name: str) -> str | None:
    return _BY_NAME.get(" ".join(name.split()).lower())
