# Déploiement en production sur Haystack Enterprise Platform

Corpus complet (projets, fiches programme, livrables), **gpt-5.1 via la passerelle EC GPT** de la Commission
(`https://api.tech.ec.europa.eu/ecgpt/v1`, API compatible OpenAI, clé dans le secret `GPT_EC`), publication par
la Gateway. Validé au
préalable sur le trial (`deploy/trial/`) : même découpage, mêmes prompts, mêmes structures de pipelines.

| Fichier | Rôle |
|---|---|
| `index-tsi-corpus.yaml` | index `tsi-corpus` : découpage par section `#`, e5 multilingue hébergé, OpenSearch |
| `pipeline-tsi-chat.yaml` | chatbot : Agent gpt-5.1 + `search_projects` + `search_deliverables` + `list_projects` + `find_projects` (fichiers téléversés par le SDK, avec leurs métadonnées) |
| `pipeline-tsi-chat-ui.yaml` | chatbot sans `list_projects` ni `find_projects` : à utiliser quand les fichiers sont téléversés par l'interface (métadonnées communes par lot seulement) |
| `pipeline-tsi-impact.yaml` | analyse d'impact : Agent gpt-5.1 + `search_projects` + `search_deliverables` + `web_search` (SearchApi) |
| `pipeline-tsi-impact-openai-search.yaml` | variante non retenue : recherche web intégrée d'OpenAI (trop lente pour la limite de 2 minutes) |
| `build_pipelines.py` | **génère** les trois `pipeline-*.yaml` (ne pas les modifier à la main) ; code des composants `Code` dans `src/tsi_agent/platform_code/` |
| `upload.ps1` | téléversement par le SDK, avec contrôles ; clé lue dans l'environnement |
| `build/` (non versionné) | `files/` à téléverser et `manifest.json` (chiffres attendus), générés par `prod-export` |

## Corpus

`python -m tsi_agent prod-export` écrit un fichier par document : `tsi-project-<id>.md` (2 100),
`tsi-programme-<id>.md` (37), `tsi-deliverable-<id>.md` (498), soit **2 635 fichiers et 83 900 morceaux**
(6 212 projet, 106 programme, 77 582 livrable ; plus gros fichier 3,3 MB). Chiffres exacts de l'export courant
dans `build/manifest.json`.

- Chaque section `# ...` est un morceau de la couche locale (250 mots au plus) : titre de contexte
  (projet — programme année — pays — section, ou publication — document, p. N — programme année — pays), texte,
  puis id, domaine, résultat, projets liés et URL source. L'index découpe au niveau `#` seulement et retrouve
  exactement ces morceaux (vérifié en local sur les 2 635 fichiers).
- Chaque fichier a son `.meta.json`, recopié par la plateforme sur ses morceaux : `Topic` (`TSI-BDG`), `corpus` (`dg-reform-tsi`),
  `doc_type` (`project`, `programme`, `deliverable`), `doc_id`, `title`, `countries` (codes ISO), `programme`,
  `year`, `policy_area`, `results`, `projects`, `multi_country`, `detail_page`, `source_url`, `chunks`. Une
  valeur inconnue est omise ; le type de chaque clé est le même dans tous les fichiers (contrôlé à l'export).
- Les deux outils de recherche filtrent sur `doc_type` : `search_projects` (projets et programme),
  `search_deliverables` (livrables, cités avec le document et la page).

## Questions d'agrégation (list_projects, find_projects)

Une recherche ne renvoie que les 8 à 10 morceaux les plus proches de la question : pour « combien de projets »,
« quels pays », la réponse est répartie sur des dizaines de projets que la recherche ne voit pas. Comme en local,
le chatbot a deux outils qui ne sont pas une recherche par similarité :

- **`list_projects`** (comptes exacts sur des critères officiels : pays, domaine, programme, année, page de
  détail, mots du titre) : `Code` (filtres) → `FilterRetriever` (**tous** les morceaux projet correspondant aux
  métadonnées, sans top-k) → `Code` (regroupement par projet, totaux par programme, année, pays, domaine, liste
  tronquée). Le filtre par pays est fait dans le code : résultat exact quelle que soit la façon dont le document
  store compare une liste de pays.
- **`find_projects`** (sujet libre, ex. blanchiment) : `Code` (filtres) → embedder → 400 morceaux projet et 300
  morceaux livrable les plus proches + catalogue des projets → `Code` (regroupement par projet : 60 depuis les
  textes, 30 depuis les livrables, plus les projets dont le titre contient un mot-clé) → **candidats que l'agent
  juge un par un** (règle 6 du prompt), puis comptes calculés par l'agent sur les projets retenus.

Le code des composants `Code` est dans `src/tsi_agent/platform_code/` (testé : `tests/test_platform_code.py`),
inséré tel quel dans le YAML par `build_pipelines.py`. Vérifié en local de bout en bout : le YAML de production
chargé avec un stockage en mémoire à la place d'OpenSearch, les totaux de `list_projects` égaux à ceux du catalogue.
Limite : une requête filtrée OpenSearch renvoie au plus 10 000 documents ; le corpus compte ~6 200 morceaux projet
(l'outil signale un dépassement). Prérequis : composant `Code` et `FilterRetriever` disponibles dans le plan.

## Isolation dans l'organisation du client

Les fichiers d'un workspace ne sont pas rangés en dossiers et **un index traite par défaut tous les fichiers de
son workspace**. Trois protections :

1. **Workspace dédié `tsi-reform`** : fichiers, index, pipelines, secrets et clés API propres au projet.
2. **Routeur d'entrée de l'index** (`input_topic_router`, `MetadataRouter`) : seuls les fichiers dont la
   métadonnée `Topic` vaut **`TSI-BDG`** sont indexés ; les autres partent sur la sortie `unmatched`, non connectée
   (même convention que les autres corpus du client, par exemple `RRP-CID`).
3. **Filtre de fichiers de l'index** (*Index > Settings*, à régler avant *Enable*) : préfixe `tsi-` **et**
   métadonnée `corpus == dg-reform-tsi`. Un fichier étranger déposé dans le workspace n'est pas indexé.
4. Si un autre index partage un jour ce workspace : lui donner le filtre inverse `corpus != dg-reform-tsi`
   (possible seulement par l'API, `PATCH /api/v1/workspaces/{workspace}/indexes/{index}`).

## À fournir ou vérifier côté client

- **Plan** : embeddings hébergés `DeepsetNvidia*` (modèle `intfloat/multilingual-e5-base`) disponibles ;
  durée maximale d'une recherche (2 minutes dans la doc : l'analyse d'impact est limitée à 4 recherches web
  pour cette raison) ; volume de documents de l'index (~84 000).
- **URL de l'API** de la plateforme du client (région) et une **clé API du workspace** pour le téléversement.
- **LLM** : secret **`GPT_EC`** (clé de la passerelle EC GPT) dans *Settings > Workspace > Secrets* du workspace,
  nom identique à la variable lue par les pipelines ; modèle `gpt-5.1` proposé par la passerelle, avec appel
  d'outils. Le connecteur OpenAI de la plateforme n'est pas utilisé : le modèle est déclaré par l'adresse de la
  passerelle. Si l'interface affiche « Model (custom) is not connected », c'est que le pipeline collé déclare encore
  le modèle OpenAI direct : coller la version générée actuelle.
- **Intégration SearchApi** (*Settings > Integrations*, compte du client,
  offre payante : l'essai gratuit se limite à 100 recherches ; offres à partir de 40 $/mois pour 10 000
  recherches ; une analyse d'impact en fait 4 au plus). Si la recherche web signale une clé manquante, ajouter
  aussi un secret `SEARCHAPI_API_KEY` dans le workspace.
- **Validation** : la configuration a été validée sur le trial avec gpt-5.4 ; gpt-5.1 via EC GPT est à revalider
  avec la recette (scores d'impact, citations, durée).

## Étapes

### 1. Workspace et intégrations

Créer le workspace `tsi-reform` (*Settings > Organization > Workspaces*), ajouter le secret `GPT_EC` (*Settings >
Workspace > Secrets*), connecter SearchApi, créer une clé API
pour ce workspace (*Settings > Workspace > API Keys*).

### 2. Générer et téléverser le corpus

```powershell
.venv\Scripts\python.exe -m tsi_agent prod-export        # code 1 si types de métadonnées incohérents
pip install haystack-enterprise-sdk                       # dans un environnement Python séparé
$s = Read-Host -AsSecureString "Clé API du workspace"
$env:API_KEY = [Runtime.InteropServices.Marshal]::PtrToStringAuto([Runtime.InteropServices.Marshal]::SecureStringToBSTR($s))
$env:API_URL = "<URL de l'API du client>"
$env:DEFAULT_WORKSPACE_NAME = "tsi-reform"
.\deploy\prod\upload.ps1
```

`upload.ps1` vérifie le manifeste et le nombre de fichiers, puis lance `haystack-enterprise upload ...
--write-mode OVERWRITE`. Le SDK associe chaque `.meta.json` à son fichier (le glisser-déposer de l'interface
ne le fait pas : les `.meta.json` y deviennent des fichiers ordinaires). Contrôle dans *Files* : 2 635
fichiers, et les métadonnées visibles sur l'un d'eux.

### 3. Index

*Indexes > Create Index* → vue YAML → coller `index-tsi-corpus.yaml` → nom **`tsi-corpus`** → *Save*. Avant
d'activer : *Settings* de l'index → *File Name Prefix Filter* `tsi-` et *File Meta Filter* `corpus` =
`dg-reform-tsi`. Puis ⋯ → **Enable**. Contrôle une fois l'état *Indexed* : *Indexed files* = 2 635,
*Documents in index* = 83 900, *Failed files* = 0.

### 4. Pipelines

Pour `pipeline-tsi-chat.yaml` puis `pipeline-tsi-impact.yaml` : *Pipeline Templates > Create empty pipeline* →
vue YAML → coller → nommer `tsi-chat` / `tsi-impact` → *Save*. Vérifier la version de Haystack dans les
réglages du pipeline (3.2.0). Accepter les mises à jour proposées par la plateforme seulement après un premier
test, puis reporter le YAML obtenu dans ce dossier.

**Publish > Publish through Gateway > Publish to new service** : un service par pipeline (`tsi-chat`,
`tsi-impact`). La Gateway donne à chacun une adresse stable (dont un endpoint compatible OpenAI
`/chat/completions`), un historique de publication et les réglages d'hébergement : en production, fixer le
nombre minimal d'instances et le délai de mise en veille selon l'usage attendu (une instance en veille ne
consomme pas, mais la première requête est plus lente).

### 5. Recette

Dans le Playground de chaque service, une nouvelle conversation par test. Relever pour chacun la durée et les
tokens (*Analytics* : traces).

Chatbot (`tsi-chat`) :
- « Quels projets ont soutenu la transformation numérique de l'administration grecque ? » puis « Et en
  Croatie ? » : réponse en français, citations [n], liste « Sources », historique pris en compte.
- « Le projet Barnahus en Slovénie a-t-il atteint ses objectifs ? » : « achieved », accord de 2018, loi de
  2021, ouverture le 27 mai 2022.
- « What did the deliverables of the Spanish primary care project recommend? » : passages de livrables cités
  avec le document et la page (outil `search_deliverables`).

Agrégation (réponses exactes connues, banc du 8 octobre 2026 ; vérifier dans *Analytics* l'outil appelé) :
- « Quels pays ont des projets TSI ou SRSP dans le domaine « gestion des migrations » ? » : 3 pays
  (`list_projects`).
- « Combien de projets dans le domaine « compétences, éducation et formation » pour le pays Grèce ? » : 3.
- « How many projects does Latvia have in revenue administration and public financial management? » : 5.
- « Quels pays ont des projets sur la lutte contre le blanchiment de capitaux ? » : 21 pays (`find_projects`,
  28 projets pertinents ; local : 21/21).
- « Which countries have projects on wildfires or forest fires? » : 8 pays.

Analyse d'impact (`tsi-impact`), le titre du projet comme requête :
- « Barnahus Slovenia: Supporting children victims of violence » : attendu 7-8/10 (trial 7, local Opus 8).
- « Technical Support in transforming the existing fully state-administered funded pension scheme in
  Lithuania » : attendu ~2/10 (trial 2, local 2).
- Sources web en liens Markdown cliquables ; dernière ligne « Impact score: X/10 … — Confidence: … ».

## Recherche web

`web_search` interroge SearchApi (moteur Google, 5 résultats), lit les pages trouvées et renvoie leur URL et le
début de leur texte : c'est la configuration validée sur le trial (~30 s par analyse, scores cohérents avec la
version locale). L'analyse elle-même est faite par le LLM du client (gpt-5.1 via EC GPT).

Variante `pipeline-tsi-impact-openai-search.yaml` : recherche web intégrée d'OpenAI, par un sous-pipeline
(gpt-5.4 + outil `web_search` de l'API Responses ; un Agent Haystack ne peut pas mélanger cet outil intégré et
ses propres outils). Plus de compte SearchApi, mais chaque recherche prend plusieurs dizaines de secondes : sur
le trial, l'analyse a été coupée après 2 à 3 minutes (`CancelledError`), même allégée (3 recherches, contexte
réduit). À n'envisager que si le plan du client autorise une durée plus longue ou les Jobs.

## Validation sur le trial (9 octobre 2026)

Configuration de `tsi-impact` (gpt-5.4, SearchApi, grille de notation) essayée sur le trial avec l'index de projets
(`deploy/trial/pipeline-tsi-impact-openai.yaml`) :

| Projet | Score | Référence (local Opus / trial Claude) |
|---|---|---|
| Barnahus Slovenia | 7/10 (strong) | 8/10 / 7/10 |
| Pension scheme Lithuania | 2/10 (weak), deux exécutions | 2/10 / 2/10 |

Sans la grille détaillée, gpt-5.4 notait la Lituanie 4/10 (livrables comptés comme impact). Durée bien en dessous
de la limite de 2 minutes. Chatbot gpt-5.4 : réponses correctes et citées, listes moins complètes qu'avec Claude
d'où la règle 6 (2 à 3 recherches reformulées) ; livrables et filtres `doc_type` à valider chez le client.

## Mises à jour du corpus

Relancer l'extraction (`run`, `deliverables`), puis `prod-export` et `upload.ps1` : `OVERWRITE` remplace les
fichiers modifiés et la plateforme ne réindexe que ceux-là. Un document supprimé localement n'est pas supprimé
sur la plateforme : le supprimer dans *Files* (son nom figure dans l'export précédent, pas dans le nouveau).

## Différences avec la version locale

| Local (CLI Claude) | Production |
|---|---|
| `list_projects`, `find_projects` : comptes et listes exacts sur tout le corpus | recherche seulement : listes non exhaustives (le prompt le fait dire à l'agent) |
| `get_project` : texte complet d'un projet | passages les plus pertinents seulement |
| impact : Opus, 10 recherches (WebSearch du CLI), `fetch_page`, score par un second appel | gpt-5.1 (EC GPT), 4 recherches web SearchApi, score dans le rapport |

Ces outils peuvent revenir comme composants custom si le besoin est confirmé en production.
