# Agent d'extraction des projets TSI / SRSP — Design

**Date :** 2026-09-30
**Statut :** validé en brainstorming, à relire
**Contexte :** projet pour la Commission européenne (DG REFORM). Pipeline Haystack contenant une architecture d'agents, destiné à être importé dans deepset Studio. Trois agents sont prévus :

1. **Extraction** (ce document) — extrait les projets publiés sur les sites publics, un fichier par projet, relançable pour ne traiter que les nouveautés.
2. **RAG / chatbot** — répond aux questions sur les projets (hors périmètre ici ; consomme les fichiers produits).
3. **Impact analysis** — explore le web pour évaluer l'impact d'un projet passé (hors périmètre ici).

## 1. Objectifs et contraintes

- Produire **un fichier Markdown par projet** (TSI 2021+ et SRSP 2017–2020), exploitable par le futur agent RAG.
- **Exécutions incrémentales** : chaque run détecte les projets nouveaux ou dont la page a changé, et n'extrait que ceux-là.
- **Traçabilité et coûts** : chaque action est journalisée ; chaque appel LLM enregistre tokens et coût.
- **Phase actuelle : exécution locale** sur le PC de l'utilisateur. Le LLM est **Claude via le CLI `claude`** (souscription Claude, pas l'API).
- **Phase suivante : deepset Studio.** Le LLM passera à l'API Anthropic (`AnthropicChatGenerator`) et le stockage à l'espace de travail deepset. Ce passage ne doit nécessiter que le remplacement de deux composants dans le YAML du pipeline.
- Respect du site de la Commission : débit limité, cache HTTP, `robots.txt`.

Hors périmètre : RAG, chatbot, impact analysis, enrichissement par recherche web, déploiement deepset.

## 2. Sources

| Source | URL | Rôle |
|---|---|---|
| TSI Country Factsheets (27 pages) | `https://reforms-investments.ec.europa.eu/technical-support-instrument-0/our-projects/tsi-country-factsheets_en` → `…/tsi-country-factsheets/<pays>_en` | **Listing principal.** Par pays : « Completed and ongoing reform projects » (stats), « Reform projects approved under the TSI » par année (single / multi-country), « SRSP projects (2017-2020) », « Highlighted projects ». |
| Projects in the spotlight | `…/technical-support-instrument-0/our-projects/projects-spotlight_en` | ~24 projets mis en avant → drapeau `spotlight`. |
| Pages domaines politiques | `…/technical-support-instrument-0_en` (10 domaines) | Référentiel des `policy_area` (aussi présent dans l'URL des pages projet). |
| TSI performance | `https://commission.europa.eu/strategy-and-policy/eu-budget/performance-and-reporting/programme-performance-statements/technical-support-instrument-performance_en` | Pas un projet : produit un fichier programme `_programme/tsi-performance.md` (chiffres globaux), rafraîchi si son contenu change. |

Constats sur la structure (vérifiés le 2026-09-30) :

- Les listes de projets des factsheets sont du HTML (listes `<li>`), groupées par année.
- Environ **un tiers des projets listés n'ont pas de page détail** (titre seul). Exemple France : 81 entrées, 52 avec lien.
- Les pages détail ont les sections *Context / Support delivered / Results achieved*, plus « Funding Programme » et « Year ». URL : `…/technical-support-instrument-0/<policy-area>/<slug>_en`.
- Un projet multi-pays apparaît dans plusieurs factsheets.
- La carte interactive est un widget EC webtools sans source de données exploitable. Elle n'est pas utilisée.

## 3. Modèle de données

### Identité d'un projet

- **Avec page détail :** `project_id` = slug de l'URL de la page détail (ex. `establishment-helicopter-emergency-medical-services-croatia`).
- **Sans page détail (« thin ») :** `project_id` = `<programme>-<année>-<slug du titre normalisé>`. La normalisation passe en minuscules, retire les accents, la ponctuation et les préfixes du type « PACE - », et limite la longueur à 80 caractères.
- **Fusion multi-pays :** un même projet vu dans plusieurs factsheets produit un seul enregistrement, avec `countries` cumulés. La clé de fusion est l'URL détail si elle existe, sinon l'id thin.
- **Passage de thin à détaillé :** si une page détail apparaît pour un projet thin (même titre normalisé, programme et année), le projet prend l'id de la page détail. L'ancien id va dans `aliases` et l'ancien fichier est remplacé, sans doublon.

### Fichier projet : `output/projects/<project_id>.md`

```yaml
---
project_id: establishment-helicopter-emergency-medical-services-croatia
title: Establishment of Helicopter Emergency Medical Services in Croatia
programme: TSI              # TSI | SRSP
year: 2022
countries: [HR]             # codes ISO 3166-1 alpha-2
multi_country: false
policy_area: health-and-long-term-care   # null si inconnu
spotlight: true
highlighted: false
detail_page: true           # false = projet "thin"
listed: true                # false si le projet a disparu des listings
aliases: []
source_urls:
  - https://reforms-investments.ec.europa.eu/technical-support-instrument-0/health-and-long-term-care/establishment-helicopter-emergency-medical-services-croatia_en
  - https://reforms-investments.ec.europa.eu/technical-support-instrument-0/our-projects/tsi-country-factsheets/croatia_en
content_hash: sha256:…      # hash du texte principal de la page détail
first_seen: 2026-09-30
last_updated: 2026-09-30
extraction:
  run_id: 20260930-091000
  model: claude-cli/sonnet  # ou "none" pour un projet thin
---

## Summary
(3 à 5 phrases, générées par le LLM à partir de la page source)

## Context
## Support delivered
## Results achieved
## Sources
```

- **Le LLM restructure, il n'invente pas.** Chaque section reprend uniquement le contenu de la page source. Une section absente de la source vaut « Not reported on the source page. ».
- **Un projet thin** a le front matter complet, et comme corps une seule ligne générée sans LLM (« Project listed in the <pays> TSI factsheet for <année> (single-/multi-country). No detail page published. »), suivie de la section Sources.

### Index d'état : `output/state/index.json`

C'est le mapping `project_id → {content_hash, file, source_urls, countries, first_seen, last_updated, listed, aliases}`. Il est mis à jour projet par projet, ce qui rend un run interrompu reprenable. On y accède via l'interface `ProjectStore` (§4).

## 4. Architecture

Un seul **pipeline Haystack** composé de composants custom `@component`, sérialisable en YAML (`pipeline.dumps()`) pour l'import dans deepset.

```
SourceListingFetcher → ProjectDiscoverer → ChangeDetector ─┬─(thin new/changed)──────────────────────────────┐
                                                           └─(detail new/changed)→ DetailPageFetcher          │
                                                                  → ProjectPromptBuilder → ClaudeCliChatGenerator
                                                                  → ProjectRecordParser ──────────────→ MarkdownProjectWriter
RunLogger : partagé, injecté dans chaque composant
```

| Composant | Entrée → Sortie | Responsabilité |
|---|---|---|
| `SourceListingFetcher` | config des sources → `RawPage[]` | Fetch HTTP poli : ~1 requête/s, User-Agent identifiable, cache ETag/Last-Modified sur disque, `robots.txt`, retry avec backoff exponentiel (3 tentatives). |
| `ProjectDiscoverer` | `RawPage[]` → `ProjectRef[]` | Parsing déterministe (BeautifulSoup) des factsheets et du spotlight : titre, URL détail éventuelle, pays, année, programme, single/multi-country, highlighted, spotlight. Fusionne les doublons multi-pays. Extrait les stats globales de la page performance. |
| `ChangeDetector` | `ProjectRef[]` + `ProjectStore` → `new[]`, `changed[]`, `unchanged[]`, `delisted[]` | Un id inconnu est `new`. Un projet détail connu est classé `changed` si le hash du texte principal diffère (le hash est calculé après fetch, voir ci-dessous). Un projet thin connu est `changed` si ses métadonnées de listing ont changé (nouveaux pays, highlighted…). Un projet connu mais absent des listings est `delisted`. |
| `DetailPageFetcher` | refs détail → `DetailPage[]` (texte nettoyé + hash) | Récupère la page, isole le contenu principal (sans navigation ni pied de page), calcule `content_hash`. Les pages dont le hash est inchangé sont retirées ici et ne vont pas au LLM. |
| `ProjectPromptBuilder` | `DetailPage` + métadonnées → prompt | Prompt strict : restructurer en JSON (`summary`, `context`, `support_delivered`, `results_achieved`, `policy_area`, `year`, `programme`), sans rien inventer. |
| `ClaudeCliChatGenerator` | prompts → `ChatMessage[]` + `meta` | Voir §5. |
| `ProjectRecordParser` | réponses JSON → `ProjectRecord` (Pydantic) | Validation du JSON. S'il est invalide, un seul nouvel essai LLM est tenté. |
| `MarkdownProjectWriter` | `ProjectRecord[]` (+ thin refs) → fichiers + état | Écrit le `.md` et met à jour `ProjectStore`. Ne supprime jamais de fichier : un `delisted` passe à `listed: false`. |

Le **`ProjectStore`** est une interface. `LocalProjectStore` (dossier `output/` + `index.json`) sert maintenant ; `DeepsetWorkspaceStore` viendra plus tard, avec l'état lu dans les métadonnées des fichiers de l'espace de travail. La logique incrémentale ne dépend donc pas du stockage.

Les composants Haystack travaillent sur des listes : chaque composant itère en interne sur ses projets, et le graphe ne contient pas de boucle.

## 5. LLM : `ClaudeCliChatGenerator`

- Composant Haystack avec la même interface de sortie que `AnthropicChatGenerator` (`replies: list[ChatMessage]`, `meta`).
- Il appelle `claude -p --output-format json --model <model>` en subprocess, avec le prompt sur stdin. Aucun outil n'est autorisé et aucun serveur MCP n'est chargé, pour isoler l'appel.
- Il lit la réponse JSON du CLI : `result`, `usage` (input, output, cache read/creation tokens), `total_cost_usd`, `duration_ms`, `session_id`.
- Paramètres : `model` (défaut `sonnet`), `timeout_s` (défaut 180), `max_concurrency` (défaut 2).
- **Erreurs :** une limite de souscription ou une erreur de quota du CLI lève `LLMQuotaExceeded`, ce qui arrête proprement le run (le travail déjà fait est conservé). Toute autre erreur du CLI marque le projet en échec.
- Coût : sur la souscription, `total_cost_usd` est un **coût équivalent API** (notionnel). On le journalise pour projeter le budget du passage à l'API.
- Passage à deepset : on remplace ce composant par `AnthropicChatGenerator` dans le YAML, sans changer d'autre composant.

## 6. Exécution

```
python -m tsi_agent run [--full] [--countries FR,HR] [--limit N] [--dry-run] [--model sonnet]
```

- **Par défaut, le run est incrémental.** `--full` ignore l'état et ré-extrait tout.
- **`--dry-run`** exécute fetch, découverte et détection, puis affiche ce qui serait extrait, sans appel LLM ni écriture.
- **`--countries` et `--limit`** servent aux runs pilotes.
- **Premier run complet :** environ 2 000 projets, dont environ 1 300 avec page détail, donc environ 1 300 appels LLM. Cela prend plusieurs heures et peut atteindre les limites de la souscription, d'où le run reprenable. On recommande un pilote sur 2 ou 3 pays d'abord.
- **Planification locale :** Planificateur de tâches Windows (script fourni dans le README). Dans deepset, ce sera un déclenchement via l'API (hors périmètre).
- **Configuration :** `config/sources.yaml` (URLs, liste des pays) et `config/settings.yaml` (débit, modèle, chemins).

## 7. Traçabilité et coûts

- **`logs/runs/<run_id>.jsonl`** reçoit un événement par ligne : `ts`, `run_id`, `component`, `action`, `target` (URL ou project_id), `result`, `duration_ms`, `error` s'il y a lieu. Les appels LLM ajoutent `model`, `input_tokens`, `output_tokens`, `cache_read_tokens`, `cache_creation_tokens` et `cost_usd`. Actions journalisées : `fetch`, `cache_hit`, `discover`, `classify`, `llm_call`, `parse`, `write`, `delist`, `error`.
- **`logs/runs/<run_id>.summary.json`** contient : pages fetchées ou lues en cache ; projets new, changed, unchanged, thin et delisted ; erreurs ; total des tokens ; coût total ; durée. Ce résumé est aussi affiché en fin de run.
- **`logs/costs.csv`** reçoit une ligne par run (run_id, date, nombre d'appels LLM, tokens, coût, durée), pour suivre les coûts dans le temps.
- **Lien avec les fichiers :** le front matter de chaque fichier projet porte le `run_id` qui l'a produit.
- **`RunLogger`** est un objet partagé, injecté dans les composants. Côté deepset, il pourra être branché sur le tracing Haystack (OpenTelemetry).

## 8. Gestion des erreurs

| Situation | Comportement |
|---|---|
| Échec d'un fetch (après 3 tentatives) | Erreur loggée. Le projet n'est pas touché et sera retenté au prochain run. |
| JSON LLM invalide (après 1 nouvel essai) | Erreur loggée. Aucun fichier écrit et état inchangé, donc retenté au prochain run. |
| Limite de souscription ou quota CLI | Arrêt propre, résumé écrit. Le prochain run reprend. |
| Structure de site inattendue (factsheet avec 0 projet, chute de plus de 30 % du nombre de projets d'un pays) | Alerte en tête du résumé. Aucun `delist` n'est appliqué pour ce pays lors de ce run. |
| Projet disparu des listings | `listed: false`. Le fichier n'est jamais supprimé. |

## 9. Tests (pytest)

- **Parsers** testés sur des fixtures HTML réelles enregistrées dans `tests/fixtures/` : une factsheet (France), le spotlight, une page projet, la page performance.
- **`ChangeDetector`** : new, changed, unchanged, delisted, fusion multi-pays, passage de thin à détaillé.
- **`ClaudeCliChatGenerator`** : subprocess mocké (succès, JSON invalide, erreur de quota), et extraction de l'usage et du coût.
- **`MarkdownProjectWriter`** : format du front matter (aller-retour YAML) et thin comme détaillé.
- **Pipeline de bout en bout** avec un faux générateur et des fixtures, en `--dry-run` et en écriture.
- **Sérialisation :** `pipeline.dumps()` doit produire un YAML qu'on peut recharger avec `Pipeline.loads()`, ce qui garantit l'import deepset.
- **Smoke test manuel :** run réel sur un pays (`--countries HR --limit 5`).

## 10. Structure du dépôt

```
Haystack Pipeline/            (dépôt git dédié)
├── pyproject.toml            Python ≥ 3.11, haystack-ai, httpx, beautifulsoup4, pydantic, pyyaml
├── config/sources.yaml, settings.yaml
├── src/tsi_agent/
│   ├── components/           un fichier par composant
│   ├── store.py              ProjectStore, LocalProjectStore
│   ├── logging.py            RunLogger
│   ├── models.py             ProjectRef, DetailPage, ProjectRecord
│   ├── pipeline.py           construction du pipeline + export YAML
│   └── __main__.py           CLI
├── tests/ (+ fixtures/)
├── output/                   (ignoré par git)
└── logs/                     (ignoré par git)
```
