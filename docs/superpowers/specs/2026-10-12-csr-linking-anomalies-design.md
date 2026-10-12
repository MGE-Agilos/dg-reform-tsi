# Rapport d'anomalies et liaison projets ↔ CSR — design

Date : 2026-10-12 · Branche de départ : `feature/prod-deploy`

## Contexte

Après la mise en production (workspace `tsi-reform`), le client (SG REFORM) a demandé :
- un export de reporting qui signale les anomalies, par exemple les projets qui devraient être publiés ;
- le lignage CSR → demande du pays → projet → résultats.

Dans Jira, la CSR revendiquée est auto-déclarée et de mauvaise qualité. On veut donc une liaison indépendante, construite à partir des sources publiques.

Ce document couvre trois chantiers, réalisés dans cet ordre :

0. **Prérequis** : fusionner la branche `feature/sitemap-discovery`, qui apporte environ 165 pages projet supplémentaires. Sans elle, le rapport d'anomalies compterait de faux « non publiés ».
1. **C — Rapport d'anomalies** : déterministe, sans LLM.
2. **A — Ingestion des CSR** dans le corpus.
3. **B — Liaison projet → CSR** : traitement par lots avec le CLI Claude local.

Hors périmètre :
- les CSR 2026, publiées seulement en PDF ;
- les contrôles croisés avec Jira (CSV attendu) ;
- le RRF ;
- les gabarits de briefing.

## 0. Fusion de `feature/sitemap-discovery`

- Fusionner la branche dans `feature/prod-deploy` et résoudre les conflits.
- Relancer la découverte et l'extraction : `python -m tsi_agent run`.
- Revectoriser : `python -m tsi_agent index`.
- Faire tourner la suite de tests.
- Le corpus de production est régénéré ensuite (`prod-export`) et renvoyé sur la plateforme.

## C. Rapport d'anomalies

**Commande** : `python -m tsi_agent anomalies [--out output/reports]`. Elle produit deux fichiers :
- `anomalies.csv` : une ligne par anomalie, encodée en UTF-8 avec BOM pour Excel ;
- `anomalies.md` : les totaux par type, gravité, programme et année, puis les 20 premières lignes par type.

**Colonnes du CSV** : `project_id, title, programme, year, countries, type, severity, detail, url`.

Les entrées sont les fichiers de `output/projects/*.md` (frontmatter) et l'index des livrables (`output/state/deliverables.json`).

| type | condition | gravité |
|---|---|---|
| `tsi_not_published` | programme TSI, `detail_page: false` | `high` si l'année ≤ année courante − 2, `medium` si année courante − 1, `low` sinon |
| `srsp_title_only` | programme SRSP ou SRSS, `detail_page: false` | `info` (attendu, car connus seulement par la liste PDF) |
| `published_no_deliverable` | `detail_page: true` et 0 livrable | `medium` si `results: achieved`, `low` sinon |
| `missing_results` | `detail_page: true` et `results` absent | `low` |
| `orphan_deliverable` | livrable dont aucun projet référencé n'existe | `medium` (`project_id` vide, l'identifiant du livrable va dans `detail`) |
| `possible_duplicate` | deux projets du même pays et du même programme dont les titres sont presque identiques (même mesure de similarité que la fusion floue) | `medium` (une ligne par paire, l'autre identifiant va dans `detail`) |

L'année courante est passée en paramètre, avec la date du jour par défaut, pour que les tests soient déterministes.

Un projet peut avoir plusieurs anomalies. Le tri se fait par gravité, puis par type, programme, année et `project_id`.

## A. Ingestion des CSR

**Source** : la base CSR d'ECFIN, une application Qlik Sense publique :
- base `dashboard.tech.ec.europa.eu/qs_digit_dashboard_mt/public` ;
- application `e94667ab-79dd-4636-8ba0-1ae23901f86c` ;
- objet `nDjMYs`.

Le bouton « Export Full Database » est reproduit par `ExportData('OOXML')` via le moteur Qlik : websocket JSON-RPC, après récupération du cookie et du jeton CSRF.

**Commande `python -m tsi_agent csr-fetch`** :
- télécharge `output/csr/csr_full.xlsx` ;
- est idempotente : elle écrase le fichier et journalise le nombre de lignes ;
- ajoute la dépendance `websockets` ; la lecture du fichier xlsx passe par `openpyxl`.

**Commande `python -m tsi_agent csr-build`** :
- lit le fichier xlsx ;
- produit `output/csr/<ISO>-<année>-<n>.md`, une fiche par CSR complète ;
- ne garde que les années **2016 à 2025** et les **27 États membres** (Royaume-Uni exclu ; « Czech Republic » devient `CZ` via `countries.py`).

Lecture des données source :
- **Versions** : chaque ligne est répétée par version d'évaluation. La dernière évaluation est celle de la version la plus élevée. L'historique est gardé sous forme de liste « version : évaluation ».
- **Identifiants** : `csr-<ISO>-<année>-<n>` pour une CSR complète (n.0), `csr-<ISO>-<année>-<n>.<m>` pour une sous-partie.

Contenu de chaque fiche :
- **Frontmatter** : `csr_id`, `country`, `year`, `number`, `policy_areas` (liste), `assessment` (la dernière), `subparts` (liste des identifiants).
- **Corps** : le texte de la CSR, puis une section par sous-partie avec son texte, son domaine d'action et sa dernière évaluation.

**Indexation** :
- en local, `doc_type: csr` est ajouté à l'indexation ; les fiches sont découpées par sous-partie ;
- en production, `prod-export` produit `tsi-csr-<ISO>-<année>-<n>.md` avec les métadonnées `Topic: TSI-BDG`, `doc_type: csr`, `doc_id`, `countries` ([ISO]), `year` et `policy_area` (premier domaine).

**Chatbot** :
- un nouvel outil de recherche `search_csrs` (hybride, filtre `doc_type: csr`), en local et dans `build_pipelines.py` ;
- une règle de prompt : « pour une question sur les recommandations ou les CSR, utiliser `search_csrs` ».

## B. Liaison projet → CSR

**Commande** : `python -m tsi_agent csr-link [--limit N] [--project ID] [--sample 30]`.

**Candidats** pour un projet d'année *y* :
- les **sous-parties** des CSR de ses pays adoptées entre **y−3 et y−1** ; une CSR sans sous-partie compte comme une unité. Une demande TSI de l'année *y* est déposée à l'automne de *y−1* et s'appuie sur les CSR des années précédentes ;
- si un projet a plus de 2 pays, on ne garde que les 15 sous-parties les plus proches par pays, classées par similarité cosinus avec l'embedding local e5 ;
- un projet sans candidat est marqué `no_candidates` et le LLM n'est pas appelé.

**Appel au LLM** :
- une invocation du CLI Claude par projet, avec le même mécanisme que l'extraction (`claude -p`, modèle Sonnet, sortie JSON) ;
- le prompt contient le titre, le texte du projet (ou seulement le titre si `detail_page: false`) et les candidats (identifiant et texte) ;
- le modèle rend au plus 5 liens, au format `[{csr_id, level: direct|partial, rationale}]` :
  - `direct` : le projet met en œuvre ce que la CSR recommande ;
  - `partial` : le projet contribue à un aspect de la CSR ;
  - la justification fait une phrase et cite le projet.
- Un `csr_id` absent des candidats est rejeté.

**Cache, reprise et quotas** :
- **Cache** : le fichier `output/csr_links/<project_id>.json` contient `links`, `basis` (`full_text` ou `title_only`), `candidates_hash`, `content_hash`, `model` et `date`. Un projet dont `content_hash` et `candidates_hash` n'ont pas changé est sauté.
- **Échecs** : une erreur de quota ou de CLI arrête proprement le lot ; il suffit de le relancer pour reprendre. Un JSON invalide déclenche une nouvelle tentative, puis le projet est sauté et journalisé.

**Diffusion** :
- `prod-export` ajoute la métadonnée `csrs` (liste des `csr_id`) et une section « Supported CSRs (AI-assessed) » au texte du projet. Chaque ligne de cette section donne l'identifiant, le niveau, la justification et le lien vers la fiche CSR, avec la mention « based on title only » si besoin. Le texte local du projet reçoit la même section au moment de l'indexation.
- `list_projects` accepte un paramètre optionnel `csr`, un préfixe d'identifiant (par exemple `csr-FR-2023`), en local et dans le composant de plateforme `list_summary`.
- Le rapport d'anomalies gagne le type `no_csr_link` : un projet TSI publié sans aucun lien, gravité `info`.

**Validation** : `--sample 30` produit `output/reports/csr_links_sample.csv`, pour relecture par les métiers. Les colonnes sont : projet, CSR, niveau, justification, texte de la CSR, et une colonne `verdict` vide. L'échantillon est tiré au hasard avec une graine fixe et stratifié par programme et par année. La comparaison avec Jira viendra quand le CSV sera disponible.

**Coût et durée** : environ 2 100 appels CLI, couverts par l'abonnement. Le lot se fait en plusieurs passes si le quota est atteint, la reprise est automatique.

## Tests

- **Sans réseau ni LLM** : de petits jeux de données d'exemple dans `tests/fixtures/`. Les tests écrivent dans `tmp_path` et ne touchent jamais `output/`.
- **Anomalies** : un cas par type et par seuil de gravité ; tri et format CSV.
- **CSR** : analyse d'un petit xlsx d'exemple (versions, sous-parties, Royaume-Uni exclu, « Czech Republic » → `CZ`) ; identifiants ; fiches.
- **Liaison** : fenêtre de candidats, limite multi-pays, rejet des identifiants inconnus, cache et reprise (le CLI est simulé), échantillon stratifié.
- **Production** : `prod-export` avec `csrs` et les fiches CSR ; types de métadonnées valides ; `list_summary` avec le préfixe `csr` ; les contrôles existants sur le code de plateforme (une seule classe `Code`, pas de fonctions imbriquées).
