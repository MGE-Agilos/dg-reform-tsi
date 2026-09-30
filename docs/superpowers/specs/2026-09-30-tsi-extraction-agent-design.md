# Agent d'extraction des projets TSI / SRSP — Design

**Date :** 2026-09-30 (révisé le même jour après analyse détaillée des sources)
**Statut :** validé
**Contexte :** projet pour la Commission européenne (DG REFORM). Pipeline Haystack contenant une architecture d'agents, destiné à être importé dans deepset Studio. Trois agents sont prévus :

1. **Extraction** (ce document) — extrait les projets publiés sur les sites publics, un fichier par projet, relançable pour ne traiter que les nouveautés.
2. **RAG / chatbot** — répond aux questions sur les projets (hors périmètre ici ; consomme les fichiers produits).
3. **Impact analysis** — explore le web pour évaluer l'impact d'un projet passé (hors périmètre ici).

## 1. Objectifs et contraintes

- Produire **un fichier Markdown par projet** (TSI 2021+ et SRSP 2017–2020), exploitable par le futur agent RAG.
- **Exécutions incrémentales** : chaque run détecte les projets nouveaux ou dont la page a changé, et n'extrait que ceux-là.
- **Traçabilité et coûts** : chaque action est journalisée ; chaque appel LLM enregistre tokens et coût.
- **Phase actuelle : exécution locale** sur le PC de l'utilisateur. Le LLM est **Claude via le CLI `claude`** (souscription Claude, pas l'API).
- **Phase suivante : deepset Studio.** Le LLM passera à l'**API OpenAI** (`OpenAIChatGenerator`, clé API OpenAI) et le stockage à l'espace de travail deepset. Pour le LLM, ce passage n'est qu'un remplacement de composant dans le YAML du pipeline (vérifié : `OpenAIChatGenerator` se recharge bien depuis le YAML). Ce n'est **pas** le cas du stockage ni du journal de run : voir le README (« Ce qui ne passe pas par le YAML »).
- Respect du site de la Commission : 1 requête toutes les 2 s (le site renvoie des HTTP 429 au-delà d'environ 1 requête/s soutenue), cache HTTP, `robots.txt`.

Hors périmètre : RAG, chatbot, impact analysis, enrichissement par recherche web, déploiement deepset.

## 2. Sources (structure vérifiée le 2026-09-30)

| Source | URL | Ce qu'on en tire |
|---|---|---|
| TSI Country Factsheets (27 pages) | `https://reforms-investments.ec.europa.eu/technical-support-instrument-0/our-projects/tsi-country-factsheets/<pays>_en` | **Listing principal TSI.** Accordéons `details.ecl-accordion__item`, un par année (2021–2026). Dans chacun : une `<ul>` de projets single-country, puis un titre « Multi-country projects: » et une `<ul>` multi-country. Un `<li>` contient un titre, avec parfois un lien vers la page détail. Section « Highlighted projects » : cartes `article.ecl-card` avec lien. Section « Completed and ongoing » : chiffres (`.ecl-fact-figures__item`). |
| Liste SRSP (PDF, 39 pages) | `https://commission.europa.eu/system/files/2020-03/rss-technical-support-per-country_en.pdf` | **Listing SRSP.** Les factsheets ne listent pas les projets SRSP en HTML : elles renvoient vers ce PDF. Tableaux « Funding source \| Title of the request » groupés par pays (titres de pays en gras). **871 projets**, tous sans page détail. |
| Projects in the spotlight | `…/technical-support-instrument-0/our-projects/projects-spotlight_en` | ~24 cartes `article.ecl-content-item`. Le pays se lit dans le nom du fichier du drapeau (`Croatia_flag_photo.jpg`, `Europe_flag_photo.jpg` pour un projet multi-pays). Donne le drapeau `spotlight`. |
| Pages domaines politiques (10) | `…/technical-support-instrument-0/<policy-area>_en` | Liens vers des projets clés (découverte) et texte descriptif du domaine (document programme). |
| TSI performance | `https://commission.europa.eu/strategy-and-policy/eu-budget/performance-and-reporting/programme-performance-statements/technical-support-instrument-performance_en` | Document programme. **Au 2026-09-30, la page ne publie aucun contenu** (placeholder `PPS_TSI_26:doc:html`). Le pipeline le détecte et lève une alerte, sans écrire de fichier vide. |

Pages détail projet : `…/technical-support-instrument-0/<policy-area>/<slug>_en`. On y trouve `h1` (titre), une liste de description `dl.ecl-description-list` (« Funding Programme », « Year »), les sections *Context / Support Delivered / Results achieved*, puis « More about the project ». Le texte de `main#main-content` est propre et sert de source.

Constats de volumétrie, mesurés sur les 27 factsheets :

- 1 727 entrées, qui donnent environ **587 projets TSI avec page détail** et **595 sans page** une fois les doublons multi-pays fusionnés.
- Avec le SRSP (871), on arrive à **environ 2 050 fichiers projet**, pour environ 600 à 650 appels LLM au premier run.
- Environ 24 liens pointent vers l'ancien domaine (`reform-support.ec.europa.eu/what-we-do/…`) ou vers des chemins non standard. Ils redirigent vers la page canonique, parfois avec un slug différent.
- Le nombre de projets annoncé par année (« the Commission approved the following **N** projects ») diffère souvent d'une unité du nombre réellement listé. C'est une incohérence du site : ce contrôle n'est qu'informatif.
- Un même projet multi-pays peut avoir un lien dans une factsheet et pas dans une autre.

## 3. Modèle de données

### Identité d'un projet et fusion

**Un projet correspond à une page détail du site.** Ses composantes nationales, listées dans les factsheets sous d'autres titres (par exemple les 25 projets « PACE 2023 »), sont conservées sous forme de `listing_titles` sur ce même projet, pas comme des projets séparés.

- **Avec page détail :** `project_id` = dernier segment de l'**URL canonique**, sans `_en` (ex. `establishment-helicopter-emergency-medical-services-croatia`). Un lien non canonique (ancien domaine, chemin inhabituel) est résolu en suivant ses redirections pendant la découverte. Le lien est traité comme un projet sans page s'il aboutit à une erreur ou à autre chose qu'une page `reforms-investments.ec.europa.eu/…_en`.
- **Sans page détail (« thin ») :** `project_id` = `<programme>-<année>-<slug du titre>`. Le slug est obtenu en passant en minuscules, en retirant les accents et le préfixe « PACE - », en remplaçant tout caractère non alphanumérique par « - », et en limitant à 80 caractères. Exemple : `tsi-2025-automation-of-routine-tasks-in-judicial-proceedings-artjp`.
- **Fusion dans un run, transitive et indépendante de l'ordre :** deux entrées désignent le même projet si elles partagent une URL détail canonique **ou** la même clé titre `(programme, année, slug du titre)`. La fusion se fait par union-find sur les entrées découvertes : si A et B se lient par une URL, et B et C par une clé titre, les trois fusionnent en un seul projet, quel que soit l'ordre de traitement des entrées. Garde-fou : on ne fusionne jamais deux groupes si cela combinerait **deux URLs détail distinctes** — deux pages détail différentes restent deux projets différents, même si leur clé titre coïncide. Le projet fusionné cumule les `countries`, les `listing_titles`, les `listing_urls` et les drapeaux, et prend l'URL détail dès qu'une des entrées en a une.
- **Titres « flagship » ambigus :** une clé titre associée à plusieurs URLs détail distinctes (un même titre générique repris par plusieurs pays, chacun avec sa propre page) est dite ambiguë. Pour une clé ambiguë, les entrées **avec** page détail ne se lient plus entre elles par cette clé (seulement par URL) : les pages détail restent distinctes. Les entrées **sans** page détail qui partagent cette clé se lient entre elles et forment leur propre projet thin, séparé des pages détail des différents pays. La clé ambiguë est retirée des `aliases` des pages détail concernées.
- **Passage de thin à détaillé :** chaque projet détaillé issu d'une factsheet porte comme alias son id thin. Si cet alias existe dans l'état, l'ancien fichier thin est supprimé, l'alias est conservé dans `aliases` et l'historique (`first_seen`) est repris. C'est le seul cas où le pipeline supprime un fichier : il remplace un fichier qu'il a lui-même généré.

### Fichier projet : `output/projects/<project_id>.md`

```yaml
---
project_id: establishment-helicopter-emergency-medical-services-croatia
title: Establishment of Helicopter Emergency Medical Services in Croatia
listing_titles: []          # titres sous lesquels ce projet apparaît dans les factsheets, s'ils diffèrent du titre
programme: TSI              # TSI | SRSP | null (inconnu)
year: 2022
countries: [HR]             # codes ISO 3166-1 alpha-2 (Grèce = GR)
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
content_hash: sha256:…      # hash du texte de la page détail ; null pour un thin
first_seen: 2026-09-30
last_updated: 2026-09-30
extraction:
  run_id: 20260930-091000
  model: claude-cli/sonnet  # "none" pour un projet thin
---

# Establishment of Helicopter Emergency Medical Services in Croatia

## Summary
## Context
## Support delivered
## Results achieved
## Listed in country factsheets as   # optionnelle : seulement si un listing_titles diffère du titre retenu
## Sources
```

- **Le LLM restructure, il n'invente pas.** Chaque section reprend uniquement le contenu de la page source. Une section absente de la source vaut « Not reported on the source page. ».
- **Pays d'un projet détaillé :** on prend ceux des listings. Si aucun listing ne les donne (projet trouvé seulement via un domaine politique), on prend les pays que le LLM a identifiés dans le texte.
- **Programme et année d'un projet détaillé :** la liste de description de la page détail est prioritaire sur le listing.
- **Un projet thin** a le front matter complet, et comme corps une ligne générée sans LLM, dans le format produit par `render.thin_sentence` (ex. « Project listed in the TSI country factsheet for Croatia under TSI 2025 (single-country). No detail page is published. », ou pour un projet SRSP « Project listed in the SRSP list of approved technical support requests (Croatia, 2019). No detail page is published. »), suivie de la section « Listed in country factsheets as » (optionnelle) puis des Sources.

### Documents programme : `output/programme/<doc_id>.md`

Ils sont produits sans LLM, à partir du texte de la page, et réécrits seulement si leur hash change. Il y en a trois sortes :

- `tsi-performance` : la page performance.
- `policy-area-<slug>` : une page par domaine politique.
- `country-<slug>` : les chiffres « Completed and ongoing » et la description des projets mis en avant de chaque factsheet.

Un texte de moins de 300 caractères est considéré comme vide : on lève une alerte et on n'écrit pas de fichier.

### Index d'état : `output/state/index.json`

C'est le mapping `project_id → {file, programme, detail_page, content_hash, listing_hash, source_urls, countries, first_seen, last_updated, listed, aliases}`, plus une entrée pour les hash des documents programme. On y accède via l'interface `ProjectStore`. L'index est réécrit (de façon atomique) après chaque projet détaillé et tous les 100 projets thin, ce qui rend un run interrompu reprenable.

Nom de fichier : `project_id` si l'id fait 100 caractères au plus, sinon ses 91 premiers caractères suivis d'un hash court. La limite de chemin Windows est de 260 caractères, et le dossier du dépôt en consomme déjà environ 110. L'id complet reste dans le front matter.

## 4. Architecture

Un seul **pipeline Haystack** composé de composants custom `@component`, sérialisable en YAML (`pipeline.dumps()`) pour l'import dans deepset.

```
SourceListingFetcher → ProjectDiscoverer → ChangeDetector ─┬─ thin ────────────────────────────────┐
                                        │                  └─ detail → DetailPageFetcher → ProjectExtractor ─┤
                                        └─ programme_docs, alerts ──────────────────────────────────→ MarkdownProjectWriter
```

| Composant | Entrée → Sortie | Responsabilité |
|---|---|---|
| `SourceListingFetcher` | `countries?` → `pages: list[RawPage]` | Récupère les factsheets, le spotlight, les domaines politiques, le PDF SRSP et les documents programme via `PoliteFetcher`. |
| `ProjectDiscoverer` | `pages` → `refs`, `programme_docs`, `suspect_countries`, `scope`, `deferred_ids` | Parsing déterministe (BeautifulSoup, pdfplumber), résolution des liens non canoniques, fusion (§3). Deux cas lèvent une alerte : une factsheet avec 0 projet, et un document programme vide. Un lien dont la résolution échoue temporairement (429, 5xx, réseau) est **reporté** : il n'est ni écrit en thin ni dé-listé, et ses ids vont dans `deferred_ids`. `scope` indique les pays et le PDF réellement récupérés, pour que le dé-listage ne touche que ce périmètre. Met en cache, par instance (donc par process), les résolutions de lien déjà effectuées et le PDF SRSP déjà analysé, pour ne pas les refaire à chaque lot d'un même run. |
| `ChangeDetector` | `refs`, `run_id`, `scope`, `suspect_countries`, `deferred_ids`, `limit` → `thin`, `detail`, `delisted`, `remaining` | Retient les thin nouveaux, ou dont le `listing_hash` a changé. Retient les projets détaillés pas encore proposés dans ce run (ensemble mémorisé par `run_id` dans l'instance), dans la limite de `limit`. Sur un run pilote (scope partiel), complète chaque ref connue avec les sources de listing **non récupérées ce run** (`countries`, `listing_urls` de l'état), pour ne pas réécrire hors périmètre les projets simplement absents du run. Ne dé-liste (`delisted`) que les projets connus dont **toutes** les URLs de listing (hors page détail) ont été récupérées ce run (`scope["fetched"]`), et seulement s'ils ne sont ni dans un pays suspect ni concernés par un id reporté (`deferred_ids`, y compris via leurs `aliases` ou `source_urls`). |
| `DetailPageFetcher` | `detail` refs, `full` → `pages: list[DetailPage]`, `unchanged: list[ProjectRef]`, `failed: list[str]` | Récupère la page et calcule le hash. Si le hash est inchangé et que `full` est faux, le projet va dans `unchanged` et ne passe pas par le LLM. Un échec de récupération va dans `failed` (projet ni écrit ni dé-listé, retenté au run suivant) ; un 404 ou 410 lève en plus une alerte (page détail disparue). |
| `ProjectExtractor` | `pages` → `records`, `failed`, `quota_exceeded` | Construit le prompt et appelle un **chat generator Haystack injecté** (`ClaudeCliChatGenerator` en local, `OpenAIChatGenerator` dans deepset). Valide le JSON avec Pydantic, avec un seul nouvel essai. Parallélisme borné (`max_concurrency`). S'arrête proprement en cas de quota. |
| `MarkdownProjectWriter` | `records`, `thin`, `unchanged`, `delisted`, `programme_docs`, `run_id` → `written`, `stats` | Écrit les fichiers et met à jour `ProjectStore`, en isolant les erreurs disque par projet (`write_failed`, compté dans `stats`) pour qu'une panne sur un fichier ne fasse pas perdre les autres. Marque `listed: false` pour les delisted. Pour les unchanged dont les métadonnées de listing ont changé (nouveau pays, spotlight…), réécrit le front matter et **régénère** les sections du corps dérivées du listing (« Listed in country factsheets as », Sources), sans appel LLM, en conservant les pays déjà trouvés par le LLM si le listing n'en fournit pas. Stocke le hash de la vue listing (`listing_hash`), comparé au run suivant. Les fichiers thin sont les **seuls jamais supprimés**, et seulement quand ils sont remplacés par leur page détail. |

Choix d'implémentation :

- **Le prompt et le parsing sont dans `ProjectExtractor`,** qui itère sur une liste de pages. Un chat generator Haystack traite une seule conversation par appel, donc un `PromptBuilder` et un parser séparés n'apportent rien ici. `ProjectExtractor` sérialise son generator via `component_to_dict`, comme le fait le composant `Agent` de Haystack.
- **Le journal (`RunLog`) n'est pas un paramètre d'init,** ce qui garde les composants sérialisables. Il passe par un `contextvar` : le runner ouvre le run, et les composants appellent `runlog.event(...)`. Hors run, les événements vont au `logging` standard.
- **Les composants construisent leur `PoliteFetcher` et leur `LocalProjectStore` à partir de paramètres simples** (`cache_dir`, `min_interval_s`, `output_dir`), via `shared_fetcher`/`shared_store` : une seule instance par configuration et par process, partagée entre composants et entre les lots d'un même run (voir « Exécution par lots » ci-dessous). Les tests injectent des faux via un attribut.
- **Haystack 3.1 refuse de désérialiser un module inconnu.** Le rechargement du YAML passe donc `allowed_modules=["tsi_agent"]`.

### Exécution par lots (reprenabilité)

Le runner exécute le pipeline en **lots** de `chunk_size` projets détaillés (25 par défaut), jusqu'à ce que `remaining == 0`, que la `limit` globale soit atteinte ou que le quota soit épuisé. Le `PoliteFetcher` et le `LocalProjectStore` sont partagés dans le processus (un par configuration), ce qui permet au détecteur de voir, d'un lot à l'autre, ce que le writer vient d'écrire.

- Chaque lot finit par l'écriture de ses fichiers, donc une interruption brutale ne perd qu'un lot au plus.
- `PoliteFetcher` mémorise en mémoire les listings déjà récupérés pendant le processus. Les lots suivants ne refont donc aucune requête de listing.

## 5. LLM : `ClaudeCliChatGenerator`

- Composant Haystack avec la même interface que `OpenAIChatGenerator` : `run(messages: list[ChatMessage]) -> {"replies": list[ChatMessage]}`. L'usage est dans `reply.meta["usage"]` au format OpenAI (`prompt_tokens`, `completion_tokens`), plus `cost_usd` et `duration_ms`.
- **Commande**, vérifiée avec le CLI 2.1.119 :
  `claude -p --output-format json --model <model> --tools "" --strict-mcp-config --mcp-config {"mcpServers":{}} --setting-sources "" --no-session-persistence --system-prompt "<une ligne>" [--json-schema <schema>]`
  - Sur cette machine, `claude` du `PATH` résout vers un shim npm (`claude.CMD`). Le composant résout plutôt l'exécutable natif `claude.exe`, situé à côté du shim (`node_modules/@anthropic-ai/claude-code/bin/claude.exe`), et l'appelle directement quand il est présent ; `cmd.exe` (via le shim) n'est utilisé qu'en repli, s'il est absent. Cela évite le passage par `cmd.exe`, qui développe les `%VAR%` dans les arguments, réinterprète les guillemets et limite la ligne de commande à 8191 caractères.
  - Le prompt passe par stdin. Le texte des messages, et donc les sauts de ligne, n'est jamais passé en argument. Pour la même raison (repli `cmd.exe` possible), le JSON schema passé en argument est sérialisé en ASCII.
  - `--bare` n'est pas utilisable : il impose une clé API et ignore l'OAuth de la souscription.
  - Avec ces options, le contexte de base mesuré tombe à environ **1 950 tokens d'entrée** (contre environ 24 000 sans elles), soit environ 0,003 $ par appel avec haiku hors texte de la page.
- **Sortie :** si `--json-schema` est fourni, le CLI renvoie `structured_output`, un JSON déjà validé, qui est ré-sérialisé en texte dans la réponse. Sinon, c'est `result`. On lit aussi `usage` (input, output, cache_read, cache_creation), `total_cost_usd`, `duration_ms`, `num_turns` et `session_id`.
- **Paramètres d'init :** `model` (défaut `sonnet`), `timeout_s` (180), `json_schema` (optionnel), `executable` (défaut `claude`, résolu comme ci-dessus). Le prompt système, lui, est reconstruit à chaque appel à partir des messages système passés à `run()`.
- **Erreurs :** un `is_error`, un `api_error_status == 429`, ou un JSON de sortie non parsable dont le message contient une limite (« rate limit », « usage limit », « limit reached », « quota », « too many requests », ou `429`) lève `LLMQuotaExceeded`. Un timeout du sous-processus ou toute autre erreur (y compris une `OSError` au lancement du CLI) lève `LLMCallError`.
- **Coût :** sur la souscription, `total_cost_usd` est un **coût équivalent API** (notionnel), journalisé pour projeter le budget API. Mesuré au smoke test : environ 0,02 $ par projet avec haiku. Avec sonnet, on estime environ 0,06 $, soit environ 35 à 40 $ notionnels pour les ~620 appels du premier run complet.
- **Côté deepset (OpenAI) :** l'API OpenAI ne renvoie pas de coût. `ProjectExtractor` le calcule à partir d'une table `pricing` ($ par million de tokens, par préfixe de modèle) déclarée dans les réglages ou le YAML. Les tokens en cache sont lus dans `usage.prompt_tokens_details.cached_tokens`. La sortie JSON s'obtient via `generation_kwargs.response_format`.

## 6. Exécution

```
python -m tsi_agent run [--full] [--countries FR,HR] [--limit N] [--chunk-size 25] [--dry-run] [--model sonnet]
python -m tsi_agent export-yaml pipeline.yaml
```

- **Par défaut, le run est incrémental.** `--full` ré-extrait toutes les pages détail, même celles dont le hash n'a pas changé.
- **`--dry-run`** fait la découverte et la détection, puis affiche les compteurs `detail_candidates` (pages détail candidates, avant vérification du hash — celle-ci n'a lieu qu'au run réel), `thin_to_write` et `delisted`, plus les alertes. Il n'appelle pas le LLM et n'écrit rien.
- **`--countries`** (codes ISO) restreint les factsheets traitées et exclut le PDF SRSP, pour les runs pilotes. **`--limit`** plafonne le nombre de projets **envoyés au LLM** (les pages détail dont le hash n'a pas changé n'en font pas partie) ; comme un JSON invalide déclenche un nouvel essai, le nombre d'appels LLM peut dépasser légèrement `--limit`.
- **Codes de sortie :** `0` run terminé, `1` arrêt sur la limite de la souscription/quota (le run suivant reprend), `2` erreur inattendue (`status: error` et `error` dans `logs/runs/<run_id>.summary.json`).
- **Planification locale :** Planificateur de tâches Windows (commande PowerShell fournie dans le README).
- **Configuration :** `config/sources.yaml` (URLs, pays, domaines politiques, documents programme) et `config/settings.yaml` (débit, modèle, chemins, taille des lots).
- **Dry run réel du 2026-09-30** (tous pays, avec le PDF SRSP) : 667 pages détail candidates, 1 433 thin, 16 clés titre « flagship » ambiguës détectées, en 15 min 34 s.

## 7. Traçabilité et coûts

- **`logs/runs/<run_id>.jsonl`** reçoit un événement par ligne : `ts`, `run_id`, `component`, `action`, `target` (URL ou project_id), `result`, `duration_ms`, et des champs libres (`error`, `model`, `input_tokens`, `output_tokens`, `cache_read_tokens`, `cache_creation_tokens`, `cost_usd`…). Actions : `fetch`, `cache_hit` (mémo en mémoire, sans requête), `throttle` (`slowed_down` / `recovered`, voir §8), `resolve`, `discover`, `alert`, `classify`, `llm_call`, `write`, `delist`, `error`.
- **`logs/runs/<run_id>.summary.json`** contient : les compteurs par action et par résultat, les alertes, le nombre d'appels LLM, les tokens totaux, le coût total et la durée. Ce résumé est aussi affiché en fin de run.
- **`logs/costs.csv`** reçoit une ligne par run : `run_id, started_at, llm_calls, input_tokens, output_tokens, cache_read_tokens, cache_creation_tokens, cost_usd, duration_s`.
- **Lien avec les fichiers :** le front matter de chaque fichier projet porte le `run_id` qui l'a produit.

## 8. Gestion des erreurs

| Situation | Comportement |
|---|---|
| Échec d'un fetch (3 tentatives, sur 5xx, 429 ou erreur réseau) | Backoff croissant **pour cette requête** : sur un 429, l'attente est d'au moins 30 s, ou la valeur de `Retry-After`, puis double à chaque tentative. Si l'échec persiste, erreur loggée : le projet n'est pas touché et sera retenté au prochain run. Un 404 n'est pas retenté. |
| Un 429 est reçu | En plus du backoff de la requête en cours, ralentissement **durable** : l'intervalle minimum entre requêtes passe à `max(2×, 5 s)`, plafonné à 15 s (événement `throttle`/`slowed_down`). Il redescend progressivement vers sa valeur de base après 50 réponses réussies (non-429) d'affilée (`throttle`/`recovered`). |
| Lien non canonique non résolu temporairement | Projet reporté (`deferred_ids`) : ni écrit, ni dé-listé. |
| URL interdite par `robots.txt` | Non récupérée ; événement `fetch` avec `result=robots_disallowed`. |
| Page détail introuvable (404/410) ou échec de récupération | Projet dans `failed` (ni écrit, ni dé-listé : retenté au run suivant). Un 404/410 lève en plus une alerte (page détail disparue). |
| JSON LLM invalide (après 1 nouvel essai) | Erreur loggée. Aucun fichier écrit et état inchangé, donc retenté au prochain run. |
| Limite de souscription ou quota CLI | Arrêt propre à la fin du lot en cours, résumé écrit. Le prochain run reprend. |
| Factsheet avec 0 projet, ou chute de plus de 30 % des projets d'un pays par rapport à l'état | Alerte. Le pays est « suspect » et aucun delist n'est appliqué pour lui dans ce run. |
| Document programme vide (moins de 300 caractères) | Alerte, pas de fichier. |
| Projet disparu des listings | `listed: false`. Le fichier n'est pas supprimé. Uniquement si ses URLs de listing ont bien été récupérées ce run (voir `ChangeDetector`, §4). |
| Erreur disque à l'écriture d'un fichier projet ou programme (verrou OneDrive/antivirus compris) | Isolée par projet : comptée dans `stats["write_failed"]`, événement `error`/`write_failed`. Les autres projets du lot continuent d'être écrits. |
| Erreur inattendue non rattrapée pendant le run | Le run se termine en erreur : `status: error` et `error` (repr tronqué) dans `logs/runs/<run_id>.summary.json` ; code de sortie `2`. |

## 9. Tests (pytest, 136 tests)

- **Parsers** testés sur des fixtures réelles enregistrées dans `tests/fixtures/` : factsheets France et Croatie, spotlight, page projet, domaine politique, page performance, index des factsheets et PDF SRSP. Les valeurs attendues ont été mesurées sur ces fixtures.
- **`PoliteFetcher`** avec `httpx.MockTransport` : cache ETag et 304, retry, 404 sans retry, robots, mémo.
- **`ChangeDetector`, `LocalProjectStore`, fusion, passage de thin à détaillé et garde anti-delist** : tests unitaires.
- **`ClaudeCliChatGenerator`** : subprocess mocké (succès, `structured_output`, erreur de quota, autre erreur, timeout).
- **Rendu Markdown** : aller-retour du front matter YAML, thin comme détaillé.
- **Pipeline de bout en bout** avec des faux fetchers et un faux generator. On vérifie deux runs successifs : le premier écrit, le second n'écrit rien.
- **Sérialisation :** `pipeline.dumps()` puis `Pipeline.loads(..., allowed_modules=["tsi_agent"])`.
- **Smoke test manuel :** run réel `--countries HR --limit 2 --model haiku`, réalisé le 2026-09-30 pendant la conception : 2 appels LLM, 31 thin, 11 documents programme, 0,04 $ notionnels. Le second run (dry-run) ne propose aucun thin à réécrire.

## 10. Structure du dépôt

```
Haystack Pipeline/            (dépôt git dédié)
├── pyproject.toml            Python ≥ 3.11, haystack-ai ≥ 3.1, httpx, beautifulsoup4, pdfplumber, pydantic, pyyaml
├── config/sources.yaml, settings.yaml
├── src/tsi_agent/
│   ├── models.py             ProjectRef, RawPage, DetailPage, ProjectContent, ProjectRecord, ProgrammeDoc
│   ├── ids.py                slugs, ids, clés titre
│   ├── countries.py          référentiel des 27 pays
│   ├── urls.py               canonisation des liens
│   ├── runlog.py             RunLog (contextvar), résumé, costs.csv
│   ├── http.py               PoliteFetcher
│   ├── store.py              ProjectStore, LocalProjectStore
│   ├── render.py             rendu Markdown
│   ├── config.py             chargement YAML
│   ├── parsers/              factsheet, spotlight, policy_area, detail, programme_doc, srsp_pdf
│   ├── components/           un fichier par composant
│   ├── pipeline.py           construction du pipeline + export YAML
│   ├── runner.py             boucle par lots, résumé
│   └── __main__.py           CLI
├── tests/ (+ fixtures/)
├── output/                   (ignoré par git)
└── logs/                     (ignoré par git)
```
