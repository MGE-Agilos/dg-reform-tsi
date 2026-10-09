# Déploiement de test sur Haystack Enterprise Platform (trial « Studio »)

Ce dossier contient tout ce qu'il faut pour tester le chatbot et l'analyse d'impact sur la plateforme deepset,
dans les limites du plan d'essai.

| Fichier | Rôle |
|---|---|
| `index-tsi-projects.yaml` | index `tsi-projects` : découpage, vectorisation e5 hébergée, stockage OpenSearch |
| `pipeline-tsi-chat.yaml` | chatbot : Agent Claude + outil `search_projects` |
| `pipeline-tsi-impact.yaml` | analyse d'impact allégée : Agent Claude + `search_projects` + `web_search` (SearchApi) |
| `generator-openai.yaml` | bloc à substituer pour passer les deux pipelines sur OpenAI (production) |
| `build/` (non versionné) | fichiers à téléverser, générés par `python -m tsi_agent trial-bundle` |

## Limites du trial et choix faits

Source : page Pricing de deepset (plan « Studio ») et doc de la plateforme ; l'écran *Usage* n'affiche pas de
plafond, l'essai se termine fin décembre 2026.

- **50 fichiers de 10 MB au plus** → les 2 100 projets et les 37 fiches programme sont regroupés en **29
  fichiers** (un par pays, un pour les projets multi-pays, un pour le programme ; < 1 MB chacun). Les
  livrables ne sont pas inclus dans ce premier test (95 MB de texte : une dizaine de fichiers en plus, à
  ajouter dans un second temps).
- **2 pipelines de développement** → `tsi-chat` et `tsi-impact`, rien d'autre.
- **100 crédits** (1 crédit = 1 heure de pipeline déployé ou d'indexation) → retirer le déploiement
  (*Undeploy*) des pipelines à la fin de chaque séance de test ; un pipeline non déployé ne consomme rien.
- **Une recherche est coupée après 2 minutes** → analyse d'impact allégée : Claude Sonnet, 4 recherches web
  au plus, pages lues directement par l'outil de recherche, score 0-10 donné par l'agent dans le rapport.
- **Clés** : jamais dans les fichiers. Anthropic et SearchApi via *Settings > Integrations* ; les composants
  les lisent sous les noms `ANTHROPIC_API_KEY` et `SEARCHAPI_API_KEY`. Si la recherche web signale une clé
  manquante, créer en plus un secret `SEARCHAPI_API_KEY` (*Settings > Workspace > Secrets*).

Le découpage est celui de la couche locale : chaque section `# ...` d'un fichier est un morceau de 250 mots au
plus, dont le titre porte le contexte (titre du projet — programme année — pays — section) et dont la dernière
ligne donne l'id du projet, le domaine, le résultat et l'URL source. L'index découpe uniquement au niveau `#`
et retrouve exactement ces 6 318 morceaux (vérifié en local).

## Étapes

### 1. Intégrations (une fois)

*Settings > Organization > Integrations* :
- **Anthropic** → *Connect* → coller votre clé API Anthropic.
- **SearchApi** → *Connect* → coller une clé SearchApi (compte gratuit sur searchapi.io : 100 recherches).
- OpenAI est déjà connecté (servira pour la production).

### 2. Libérer le workspace

L'index `standard-index-english` existant indexerait aussi nos fichiers (stockage et crédits en double) :
dans *Indexes*, le **désactiver** ou le supprimer avant de téléverser.

### 3. Générer et téléverser les fichiers

```powershell
.venv\Scripts\python.exe -m tsi_agent trial-bundle
```

Résultat dans `deploy/trial/build/` : 29 fichiers `tsi-*.md` et leurs `tsi-*.md.meta.json` (pays, nombre de
morceaux). Les régénérer après chaque nouvelle extraction.

Téléversement, au choix :
- **Interface** : *Files > Upload* → glisser les 58 fichiers du dossier `build/` (les `.meta.json` sont
  associés automatiquement à leur fichier).
- **SDK** (environnement Python séparé) : `pip install haystack-enterprise-sdk`, puis dans la même session
  PowerShell, sans rien écrire dans un fichier :

  ```powershell
  $env:API_KEY = "<clé API du workspace>"      # Settings > Workspace > API Keys
  $env:DEFAULT_WORKSPACE_NAME = "default"
  haystack-enterprise upload deploy\trial\build --write-mode OVERWRITE
  ```

### 4. Créer l'index

*Indexes > Create index* → vue YAML → coller `index-tsi-projects.yaml` → nommer l'index **`tsi-projects`**
(même nom que `index:` dans le YAML) → *Save* → menu ⋯ → **Enable**. Attendre que les 29 fichiers soient
indexés (statut de l'index). Coût observé pour l'index standard : environ 0,03 crédit pour 100 fichiers.

### 5. Créer et déployer les pipelines

Pour chacun des deux fichiers `pipeline-tsi-chat.yaml` et `pipeline-tsi-impact.yaml` :
*Pipeline Templates > Create empty pipeline* → vue YAML → coller le fichier → nommer (`tsi-chat`,
`tsi-impact`) → *Save*. Si un écran *Pipeline Settings* existe sur le pipeline créé, vérifier que la version de
Haystack est **3.2.0** (versions 3.x requises). Puis **Deploy**, et tester dans le **Playground**.

Si l'éditeur signale une erreur de composant, la copier telle quelle : les trois YAML se chargent dans
Haystack 3.2 en local avec les vraies intégrations (Anthropic, OpenSearch, SearchApi) ; seuls les composants
propres à deepset (parseur d'historique, embedders e5 hébergés, rappel de streaming) n'ont pas pu être
vérifiés hors plateforme.

### 6. Questions de test

Chatbot (`tsi-chat`) :
- « Quels projets ont soutenu la transformation numérique de l'administration grecque ? »
- « Compare the support to digital transformation in Greece and Croatia. »
- « Le projet Barnahus en Slovénie a-t-il atteint ses objectifs ? » (puis une relance : « Et en Croatie ? »)

Analyse d'impact (`tsi-impact`), avec le titre du projet comme requête ; référence : rapports locaux Opus
dans `output/impact/` :
- « Barnahus Slovenia: Supporting children victims of violence » — local : 8/10.
- « Technical Support in transforming the existing fully state-administered funded pension scheme in
  Lithuania » — local : 2/10.

À noter : durée de chaque réponse (limite de 2 minutes), sources citées, cohérence du score avec le local.

### 7. Fin de séance

*Undeploy* les deux pipelines. Consulter la consommation dans *Settings > Organization > Usage*.

## Différences avec la version locale

| Local (CLI Claude) | Trial |
|---|---|
| `list_projects`, `find_projects` : comptes et listes exacts sur tout le corpus | recherche seulement : listes non exhaustives (le prompt le fait dire à l'agent) |
| `get_project`, `search_deliverables` | absents (livrables non téléversés) |
| analyse d'impact Opus, 10 recherches, `fetch_page`, score par un second appel Opus | Sonnet, 4 recherches, score dans le rapport |
| sources web numérotées [n] | sources web en liens Markdown, passages projet en [n] |

Ces outils peuvent revenir plus tard comme composants custom (l'onglet *Components* de l'organisation le
permet), si le test est concluant.

## Passage en production (OpenAI)

Remplacer, dans les deux pipelines, le bloc `chat_generator:` de l'agent par celui de `generator-openai.yaml`
(la clé vient de l'intégration OpenAI). Le reste (outils, index, prompts) ne change pas. Avant la mise en
production, refaire les questions de test ci-dessus et comparer les scores d'impact aux rapports locaux.
