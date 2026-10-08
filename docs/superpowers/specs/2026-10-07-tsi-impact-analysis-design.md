# Analyse d'impact d'un projet (Impact Analysis) — Design

**Date :** 2026-10-07
**Statut :** validé en conversation
**Contexte :** le corpus contient 2 100 projets TSI/SRSP (objectifs, actions, résultats) et le texte intégral de leurs
livrables, vectorisé sur Qdrant local. L'utilisateur veut, pour un projet choisi, une analyse d'impact : comprendre le
projet (objectifs, actions menées), puis chercher sur le web des éléments indiquant un impact positif ou négatif sur
le pays, l'Europe ou le monde, au regard des objectifs. Les sources sont libres (articles, sites officiels, etc.) mais
toujours citées avec leur lien.

## 1. Décisions (validées)

- **Lancement :** onglet « Impact analysis » dans la page web locale existante (port 8765).
- **Rapport :** structuré, en anglais, enregistré en Markdown.
- **Approche A :** un `Agent` Haystack avec ses propres outils (même architecture que le chatbot), sérialisable en
  YAML pour deepset.

## 2. Constats techniques

- Haystack 3.2 n'embarque plus de composant de recherche web : il est dans les intégrations `serperdev-haystack` et
  `tavily-haystack` (clé API requise, à configurer dans deepset).
- Le CLI `claude` fait des recherches web en mode non interactif sur la souscription :
  `claude -p --tools WebSearch --allowedTools WebSearch …` (environ 15 s par recherche, vérifié le 2026-10-07).
- `LinkContentFetcher` et `HTMLToDocument` sont natifs Haystack ; `HTMLToDocument` exige `trafilatura` (à ajouter
  aux dépendances). Les PDF passent par `PyPDFToDocument` (`pypdf`, déjà présent).

## 3. Architecture

Nouveau paquet `src/tsi_agent/impact/` :

| Fichier | Rôle |
|---|---|
| `web_search.py` | `ClaudeWebSearch` : composant `run(query) -> {"documents": [...]}` (titre, URL, extrait) qui lance le CLI `claude` avec le seul outil WebSearch et `--json-schema` pour une liste de résultats. Interface alignée sur `SerperDevWebSearch` (sortie `documents`, meta `title`/`link`). |
| `tools.py` | `WebSearchTool` (enveloppe le moteur de recherche, numérote les résultats [n] dans l'état `sources`), `PageReader` (`fetch_page(url)` : `LinkContentFetcher` + conversion HTML ou PDF, texte tronqué à ~3 000 mots, source numérotée). |
| `prompt.py` | Prompt système de l'agent (méthode, règles de citation, structure imposée du rapport). |
| `pipeline.py` | `build_impact_pipeline(settings, …)` : `Agent` avec 4 outils et l'état `sources` partagé. |
| `report.py` | Validation des citations, rendu Markdown avec front matter, lecture des rapports existants. |
| `service.py` | `analyse(pipeline, project_id, progress)` : lance l'agent, journalise (runlog), valide, enregistre ; `ImpactJobs` : une tâche en arrière-plan à la fois, progression consultable. |

Outils de l'agent (numérotation continue des sources via l'état `sources`, comme le chatbot) :

| Outil | Rôle | Local | deepset |
|---|---|---|---|
| `get_project(project_id)` | Texte complet du projet (objectifs, actions, résultats, résumés des livrables) | `ProjectReader` existant | idem (OpenSearch) |
| `search_deliverables(query, project_id)` | Passages précis des livrables du projet | `DeliverableSearch` existant | idem |
| `web_search(query)` | 6 à 8 résultats numérotés : titre, URL, extrait | `ClaudeWebSearch` | `SerperDevWebSearch` |
| `fetch_page(url)` | Texte de la page (HTML ou PDF), tronqué | `LinkContentFetcher` + convertisseurs | idem |

Réglages (`config/settings.yaml`) : `impact_model: opus`, `impact_max_steps: 20`, `impact_max_searches: 10`,
`impact_page_words: 3000`, `impact_dir: output/impact`, `web_search_model: haiku` (la recherche elle-même n'a pas
besoin de Sonnet).

## 4. Méthode imposée par le prompt

1. Lire le projet (`get_project`), en tirer les objectifs et les actions menées ; compléter par `search_deliverables`
   si utile.
2. Rechercher sur le web des éléments d'impact : mise en œuvre de la réforme, indicateurs, évaluations, rapports
   OCDE / Commission (Semestre européen, PRR) / gouvernement, presse, critiques. Au plus `impact_max_searches`
   recherches (le plafond est aussi appliqué par l'outil, qui refuse au-delà).
3. Lire (`fetch_page`) les pages avant de s'appuyer sur elles.
4. Règles : ne citer que des sources renvoyées par les outils ; distinguer corrélation et causalité (le projet a
   rarement été la seule cause) ; dire explicitement quand les preuves manquent ; ne jamais inventer un impact.

## 5. Rapport

Fichier `output/impact/<project_id>.md` (le dernier remplace le précédent), front matter : `project_id`, `title`,
`countries`, `year`, `generated` (date), `run_id`, `model`, `cost_usd`, `duration_s`, `overall_verdict`,
`confidence` (`high` / `medium` / `low`), `sources` (nombre).

Sections, dans cet ordre :

1. **Project overview** — objectifs et actions menées (citant la page projet et les livrables).
2. **Evidence of positive impact**
3. **Evidence of negative impact or limitations**
4. **Impact by level** — country / Europe / world.
5. **Assessment against objectives** — un verdict par objectif : `achieved`, `partly achieved`, `not achieved`,
   `insufficient evidence` ; puis le verdict global et le niveau de confiance.
6. **Sources** — liste [n] titre + lien, générée par le code à partir de l'état `sources` (pas par le modèle).

La réponse finale de l'agent est produite en JSON (schéma : `sections` en Markdown + `overall_verdict` +
`confidence` + `objectives` avec leurs verdicts) ; le code assemble le Markdown. **Garde-fou :** toute citation [n]
absente des sources réellement renvoyées par les outils est retirée du texte et journalisée ; seules les sources
citées figurent dans la section Sources.

## 6. Page web

- Onglet « Impact analysis » à côté du chat, dans `static/index.html`.
- `GET /api/projects?q=` : recherche de projets par titre, pays ou année (lecture des front matter de
  `output/projects/`, en mémoire au démarrage), 20 résultats au plus.
- `POST /api/impact {project_id}` : démarre la tâche (409 si une analyse tourne déjà) ; `GET /api/impact/job` :
  état, étapes de progression (« Reading project… », « Searching: … », « Reading: domain… »), rapport à la fin ou
  erreur.
- `GET /api/impact/reports` et `GET /api/impact/reports/<project_id>` : analyses déjà faites, rouvrables sans
  relancer.
- La progression vient des outils (chaque appel publie une étape) ; la page interroge toutes les 2 s et affiche la
  même animation de chargement que le chat.
- Le chat reste utilisable pendant une analyse (verrous distincts).

## 7. Erreurs

- Page illisible, délai dépassé, type non pris en charge, recherche vide : l'outil renvoie un message à l'agent, qui
  continue.
- Limite de souscription : la tâche échoue avec un message clair (pas de rapport partiel enregistré).
- `max_agent_steps` atteint sans rapport : la tâche échoue (« analysis did not complete »), détail dans les logs.
- Projet inconnu : 404.

## 8. Tests

- `ClaudeWebSearch` : construction de la commande, parsing du résultat (sortie CLI simulée).
- `WebSearchTool` / `PageReader` : numérotation continue, plafond de recherches, troncature, HTML et PDF (fixtures
  locales, fetcher simulé), erreurs renvoyées en texte.
- Rapport : citations inventées retirées, section Sources limitée aux sources citées, front matter.
- Service et API : tâche unique (409), progression, rapport enregistré, quota, 404 ; avec un faux générateur.
- Pipeline : 4 outils, aller-retour YAML (`export-yaml --impact`).
- Intégration (marquée) : une vraie recherche web via le CLI.
- Réel : 2 à 3 projets (projet riche en résultats, projet récent, projet multi-pays).

Estimation : 0,30 à 1 $ (équivalent API) et 2 à 6 minutes par analyse.

Hors périmètre : analyses en lot, comparaison entre projets, export PDF du rapport.

## 9. Score d'impact positif 0-10 (revu le 2026-10-07)

Remplace l'échelle -2..+2 (positif / indéterminé / négatif) notée par l'agent pendant l'analyse.

- **L'agent ne note plus.** Le prompt et `REPORT_SCHEMA` ne contiennent plus `impact_score` ni
  `impact_rationale` ; `parse_report` ignore ces champs s'ils sont présents. Le rapport est rendu sans score.
- **Étape de score sur le rapport terminé** (`impact/scoring.py`). Après l'écriture du rapport, un appel LLM séparé à
  sortie structurée (`SCORE_SCHEMA` : `impact_score` entier 0..10 et `impact_rationale`, tous deux requis) reçoit la
  rubrique en prompt système et le corps du rapport (sans front matter). Modèle : réglage `impact_score_model`
  (défaut `opus`), via `default_scorer(settings)` (CLI `claude`, journalisé). La réponse est bornée à 0..10 (entier
  arrondi) ; une réponse illisible lève `ScoringError`. Événement du journal : `ImpactScoring` / `impact_score`
  avec `score`.
- **Rubrique** (anglais, prompt système) : impact POSITIF du projet tel que documenté par le rapport. 0 : aucune
  preuve d'impact positif (livrables non utilisés, réforme abandonnée, contredite ou annulée) ; 1-3 faible
  (livrables remis, peu ou pas de preuve d'appropriation) ; 4-6 modéré (réforme adoptée ou partiellement mise en
  œuvre, attribution plausible mais partielle ou précoce) ; 7-8 fort (résultats documentés plausiblement
  attribuables au projet) ; 9-10 très fort (résultats durables, bien documentés, clairement attribuables). Score
  fondé uniquement sur le rapport, en tenant compte de la confiance et des réserves d'attribution qu'il exprime ;
  justification d'une phrase citant les sources [n] du rapport.
- **Niveaux** : 0-3 `weak`, 4-6 `moderate`, 7-10 `strong` (libellés Weak / Moderate / Strong).
- **Écriture** (`apply_score`, idempotent) : front matter `impact_score`, `impact_level`, `impact_rationale` juste
  après `confidence` (l'ancien `impact_category` est retiré) ; corps : ligne
  `**Positive impact:** 7/10 (Strong) — <justification>` sous le verdict (une ancienne ligne `**Impact:**` est
  retirée). Les citations de la justification absentes de la section Sources du rapport sont retirées.
- **Erreurs.** Échec du score (`ScoringError` ou erreur du CLI hors quota) : le rapport reste enregistré sans score
  et une alerte est journalisée. Le quota remonte comme pour l'analyse (le rapport, déjà écrit, reste non noté).
- **Recalcul** : `python -m tsi_agent impact-rescore [project_id ...]` recalcule le score des rapports existants
  de `impact_dir` (tous par défaut), sans nouvelle analyse ni couche vectorisée, dans un run du journal
  (`mode: impact-rescore`) ; une ligne par rapport (`project_id 7/10 strong`) puis le coût total ; code 0, 2 en
  cas d'erreur.
- **API.** `GET /api/impact/reports` : `summary` = `{"strong", "moderate", "weak", "not_rated"}`. Non noté : pas
  d'`impact_level` ou pas d'`impact_score` entier dans 0..10 (rapports jamais notés et anciens rapports -2..+2).
- **Page.** Jauge SVG en demi-cercle de 0 (gauche) à 10 (droite), dégradé unique gris → vert clair → vert
  (variables CSS clair et sombre, sans rouge), graduations 0, 2, 4, 6, 8, 10, aiguille sur le score ; à côté :
  « 7/10 » en grand, niveau, confiance et justification (rendue avec les fonctions d'échappement existantes).
  `role="img"`, `aria-label` « Positive impact: 7 out of 10 (Strong), confidence medium ». Non noté : jauge grisée
  sans aiguille, « Not rated (run impact-rescore or the analysis again) ». Liste des analyses : pastille « 7/10 »
  colorée par niveau ; puces Strong / Moderate / Weak / Not rated / All qui filtrent la liste.
- **Tests.** Score : lecture, bornage et erreurs de la réponse (generator scripté), seuils des niveaux,
  `apply_score` (front matter, ligne du corps, idempotence, retrait de l'ancienne échelle et des citations
  inconnues) ; analyse avec et sans score, échec du score (rapport conservé, alerte), quota ; commande
  `impact-rescore` sur un dossier temporaire ; résumé de l'API avec rapports non notés ; côté page (Node) : angle de
  l'aiguille pour 0, 5 et 10, niveau, `aria-label`, échappement, non noté.
