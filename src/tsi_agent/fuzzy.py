"""Fusion floue d'un projet thin (listing sans page detail) avec une page detail : fonctions pures.

Regle : titres normalises (minuscules, sans ponctuation, sans mots vides ni noms/adjectifs de pays)
avec un ratio difflib >= THRESHOLD, et
- les memes marqueurs de phase/partie/annee (chiffres, chiffres romains) des deux cotes,
- au moins MIN_PREFIX_OVERLAP de mots communs (Jaccard sur les 5 premieres lettres de chaque mot :
  "supporting"/"support" comptent comme un meme mot, "waste"/"water" non),
- programme egal (si connu des deux cotes), annee egale ou a un an pres (si connue des deux cotes) ; a un an
  pres, le score doit atteindre NEAR_YEAR_THRESHOLD (cas reel : "... implement the Pillar Two Directive" 2025
  contre "... implement the FASTER Directive" 2026, ratio 0.918, deux projets distincts),
- pays communs (si connus des deux cotes).
Jamais deux pages detail ensemble, jamais deux refs de meme cle titre exacte (merge_refs a deja tranche :
fusion, ou refus pour un titre "flagship" generique), jamais un thin exclu par l'appelant (cle ambigue). Chaque page detail prend au plus un thin et inversement : le meilleur
candidat ; en cas d'egalite parfaite entre deux candidats, rien n'est fusionne (ambigu).
"""

import difflib
import re
import unicodedata
from functools import lru_cache
from dataclasses import dataclass

from tsi_agent.countries import COUNTRIES
from tsi_agent.ids import title_key
from tsi_agent.models import ProjectRef

THRESHOLD = 0.85
NEAR_YEAR_THRESHOLD = 0.95
MIN_PREFIX_OVERLAP = 0.5
PREFIX_LEN = 5

# Mots vides du generateur d'URL du site (pathauto) + "and" : un slug et un titre se normalisent pareil.
STOPWORDS = frozenset({"a", "an", "and", "as", "at", "before", "but", "by", "for", "from", "is", "in", "into",
                       "like", "of", "off", "on", "onto", "per", "since", "than", "the", "this", "that", "to",
                       "up", "via", "with"})
# Adjectifs et regions, en plus des noms de pays du referentiel.
COUNTRY_WORDS: dict[str, tuple[str, ...]] = {
    "AT": ("austrian",), "BE": ("belgian", "flanders", "flemish", "wallonia", "walloon", "brussels"),
    "BG": ("bulgarian",), "HR": ("croatian",), "CY": ("cypriot",), "CZ": ("czech", "czechia"), "DK": ("danish",),
    "EE": ("estonian",), "FI": ("finnish",), "FR": ("french",), "DE": ("german",), "GR": ("greek", "hellenic"),
    "HU": ("hungarian",), "IE": ("irish",), "IT": ("italian",), "LV": ("latvian",), "LT": ("lithuanian",),
    "LU": ("luxembourgish",), "MT": ("maltese",), "NL": ("dutch",), "PL": ("polish",), "PT": ("portuguese",),
    "RO": ("romanian",), "SK": ("slovak", "slovakian"), "SI": ("slovenian", "slovene"), "ES": ("spanish",),
    "SE": ("swedish",),
}
_NAME_TOKENS = {w for _, names in COUNTRIES.values() for n in names for w in n.lower().split()}
_DROP = STOPWORDS | _NAME_TOKENS | {w for words in COUNTRY_WORDS.values() for w in words}
_MARKER = re.compile(r"^(?:\d+|i{1,3}|iv|v|vi{1,3}|ix|x)$")
_SPLIT = re.compile(r"[^a-z0-9]+")


def _tokens(text: str) -> list[str]:
    ascii_text = unicodedata.normalize("NFKD", text.lower()).encode("ascii", "ignore").decode()
    return [w for w in _SPLIT.split(ascii_text) if w]


def normalize_title(title: str) -> list[str]:
    """Mots significatifs du titre, dans l'ordre."""
    return [w for w in _tokens(title) if w not in _DROP]


def prefix_tokens(title: str) -> frozenset[str]:
    """Mots significatifs du titre reduits a leurs PREFIX_LEN premieres lettres (ensemble)."""
    return frozenset(w[:PREFIX_LEN] for w in normalize_title(title))


def countries_in_text(text: str) -> list[str]:
    """Codes ISO des pays nommes dans le texte (noms, adjectifs, regions), dans l'ordre du referentiel."""
    padded = " " + " ".join(_tokens(text)) + " "
    found = []
    for iso, (_, names) in COUNTRIES.items():
        words = [" ".join(_tokens(n)) for n in names] + list(COUNTRY_WORDS.get(iso, ()))
        # "the netherlands" -> "netherlands" suffit ; "czech republic" est couvert par "czech".
        words += [w.removeprefix("the ") for w in words]
        if any(f" {w} " in padded for w in words):
            found.append(iso)
    return found


@dataclass(frozen=True)
class _Norm:
    text: str
    prefixes: frozenset[str]
    markers: frozenset[str]


@lru_cache(maxsize=None)
def _norm(title: str) -> _Norm:
    words = normalize_title(title)
    return _Norm(" ".join(words), prefix_tokens(title),
                 frozenset(w for w in _tokens(title) if _MARKER.match(w)))


def _similarity(a: _Norm, b: _Norm) -> float:
    if not a.text or not b.text or a.markers != b.markers:
        return 0.0
    if len(a.prefixes & b.prefixes) / len(a.prefixes | b.prefixes) < MIN_PREFIX_OVERLAP:
        return 0.0
    return difflib.SequenceMatcher(None, a.text, b.text, autojunk=False).ratio()


def title_similarity(a: str, b: str) -> float:
    """Score de 0 a 1 ; 0 si les marqueurs de phase different ou si trop peu de mots sont communs."""
    return _similarity(_norm(a), _norm(b))


@dataclass(frozen=True)
class FuzzyMatch:
    detail_index: int
    thin_index: int
    detail_id: str
    thin_id: str
    score: float


def _year_rank(a: int | None, b: int | None) -> int | None:
    """0 annee egale, 1 inconnue d'un cote, 2 a un an pres, None incompatible."""
    if a is None or b is None:
        return 1
    gap = abs(a - b)
    return 0 if gap == 0 else 2 if gap == 1 else None


def _country_rank(a: list[str], b: list[str]) -> int | None:
    """0 pays communs, 1 inconnus d'un cote, None incompatibles."""
    if not a or not b:
        return 1
    return 0 if set(a) & set(b) else None


def _rank(d: ProjectRef, t: ProjectRef) -> tuple[int, int] | None:
    if d.programme and t.programme and d.programme != t.programme:
        return None
    year = _year_rank(d.year, t.year)
    country = _country_rank(d.countries, t.countries)
    return None if year is None or country is None else (year, country)


def _exact_key(ref: ProjectRef) -> str | None:
    return title_key(ref.programme, ref.year, ref.title) if ref.programme and ref.year else None


def match_thin_refs(refs: list[ProjectRef], views: list[ProjectRef] | None = None,
                    threshold: float = THRESHOLD, excluded_thin_ids: set[str] | None = None) -> list[FuzzyMatch]:
    """Paires (page detail, thin) a fusionner, par score decroissant.

    `views` : meme longueur que `refs`, vues completees (annee/programme lus sur la page detail en cache,
    pays deduits du titre) utilisees pour les controles de compatibilite ; les refs ne sont pas modifiees.
    `excluded_thin_ids` : thin jamais rattaches (mentions sans lien d'un titre flagship generique).
    """
    views = views if views is not None else refs
    excluded = excluded_thin_ids or set()
    norms = [_norm(v.title) for v in views]
    keys = [_exact_key(r) for r in refs]
    details = [i for i, r in enumerate(refs) if r.detail_url]
    thins = [i for i, r in enumerate(refs) if not r.detail_url and r.project_id not in excluded]
    by_prefix: dict[str, list[int]] = {}
    for t in thins:
        for p in norms[t].prefixes:
            by_prefix.setdefault(p, []).append(t)
    candidates: list[tuple[tuple[float, int, int], int, int]] = []
    for d in details:
        seen: set[int] = set()
        for p in norms[d].prefixes:
            for t in by_prefix.get(p, ()):
                if t in seen:
                    continue
                seen.add(t)
                if keys[d] is not None and keys[d] == keys[t]:
                    continue
                rank = _rank(views[d], views[t])
                if rank is None:
                    continue
                score = _similarity(norms[d], norms[t])
                if score >= (max(threshold, NEAR_YEAR_THRESHOLD) if rank[0] == 2 else threshold):
                    candidates.append(((round(score, 9), -rank[0], -rank[1]), d, t))
    # Egalite parfaite entre les deux meilleurs candidats d'une page (ou d'un thin) : ambigu, on s'abstient.
    best: dict[tuple[str, int], list[tuple[float, int, int]]] = {}
    for key, d, t in candidates:
        best.setdefault(("d", d), []).append(key)
        best.setdefault(("t", t), []).append(key)
    ambiguous = set()
    for side, keys in best.items():
        keys.sort(reverse=True)
        if len(keys) > 1 and keys[0] == keys[1]:
            ambiguous.add(side)
    candidates.sort(key=lambda c: (c[0], -c[1], -c[2]), reverse=True)
    used_d: set[int] = set()
    used_t: set[int] = set()
    out: list[FuzzyMatch] = []
    for key, d, t in candidates:
        if d in used_d or t in used_t or ("d", d) in ambiguous or ("t", t) in ambiguous:
            continue
        top_d, top_t = best[("d", d)][0], best[("t", t)][0]
        if key != top_d or key != top_t:
            continue  # pas le meilleur choix de l'un des deux cotes
        used_d.add(d)
        used_t.add(t)
        out.append(FuzzyMatch(d, t, refs[d].project_id, refs[t].project_id, key[0]))
    return out
