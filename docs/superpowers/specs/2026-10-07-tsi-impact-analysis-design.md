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

## 9. Jauge d'impact (ajout du 2026-10-07)

- **Données.** Le JSON final de l'agent gagne `impact_score` (entier de -2 à +2) et `impact_rationale` (une phrase
  avec citations [n]). Échelle donnée dans le prompt : +2 impact positif net, plausiblement attribuable au projet ;
  +1 plutôt positif (signes positifs, attribution faible ou partielle) ; 0 indéterminé (preuves insuffisantes, trop
  récentes ou contradictoires) ; -1 plutôt négatif ; -2 impact négatif net (préjudice, retour en arrière, échec de
  la réforme). En cas de doute : 0.
- **Rapport.** `parse_report` convertit le score en entier borné à -2..2 (absent ou illisible : 0) ; la
  justification passe par le nettoyage des citations et compte pour les sources citées. Catégorie : >= 1 positive,
  <= -1 negative, 0 undetermined. Front matter : `impact_score`, `impact_category`, `impact_rationale` juste après
  `confidence`. Corps : ligne `**Impact:** Positive (score +1 on a -2…+2 scale) — <justification>` sous le verdict.
- **Anciens rapports** (sans `impact_score`) : servis tels quels ; la page les affiche « Not rated ».
- **API.** `GET /api/impact/reports` ajoute `summary` : `{"positive", "undetermined", "negative", "not_rated"}`.
- **Page.** Jauge SVG en demi-cercle (sans bibliothèque) en tête du rapport : zones rouge / gris / vert (variables
  CSS clair et sombre), graduations -2..+2, aiguille sur le score (-2 à gauche, +2 à droite), puis catégorie, score
  signé, confiance et justification (rendue avec les fonctions d'échappement existantes). Non noté : jauge grisée
  sans aiguille. `role="img"` et `aria-label` décrivant le score. Liste des analyses : pastille par rapport ;
  bandeau de puces (Positive / Undetermined / Negative / Not rated / All) qui filtre la liste.
- **Tests.** Conversion et bornage du score, catégorie, front matter et ligne Impact, nettoyage des citations de la
  justification, résumé de l'API avec un rapport non noté ; côté page (Node) : angle de l'aiguille, catégorie,
  balisage et échappement de la jauge.
