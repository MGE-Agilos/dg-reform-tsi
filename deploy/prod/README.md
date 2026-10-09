# Déploiement en production sur Haystack Enterprise Platform

Corpus complet (projets, fiches programme, livrables), OpenAI gpt-5.4, publication par la Gateway. Validé au
préalable sur le trial (`deploy/trial/`) : même découpage, mêmes prompts, mêmes structures de pipelines.

| Fichier | Rôle |
|---|---|
| `index-tsi-corpus.yaml` | index `tsi-corpus` : découpage par section `#`, e5 multilingue hébergé, OpenSearch |
| `pipeline-tsi-chat.yaml` | chatbot : Agent gpt-5.4 + `search_projects` + `search_deliverables` |
| `pipeline-tsi-impact.yaml` | analyse d'impact : Agent gpt-5.4 + `search_projects` + `search_deliverables` + `web_search` (recherche web OpenAI) |
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
- Chaque fichier a son `.meta.json`, recopié par la plateforme sur ses morceaux : `corpus` (`dg-reform-tsi`),
  `doc_type` (`project`, `programme`, `deliverable`), `doc_id`, `title`, `countries` (codes ISO), `programme`,
  `year`, `policy_area`, `results`, `projects`, `multi_country`, `detail_page`, `source_url`, `chunks`. Une
  valeur inconnue est omise ; le type de chaque clé est le même dans tous les fichiers (contrôlé à l'export).
- Les deux outils de recherche filtrent sur `doc_type` : `search_projects` (projets et programme),
  `search_deliverables` (livrables, cités avec le document et la page).

## Isolation dans l'organisation du client

Les fichiers d'un workspace ne sont pas rangés en dossiers et **un index traite par défaut tous les fichiers de
son workspace**. Trois protections :

1. **Workspace dédié `tsi-reform`** : fichiers, index, pipelines, secrets et clés API propres au projet.
2. **Filtre de fichiers de l'index** (*Index > Settings*, à régler avant *Enable*) : préfixe `tsi-` **et**
   métadonnée `corpus == dg-reform-tsi`. Un fichier étranger déposé dans le workspace n'est pas indexé.
3. Si un autre index partage un jour ce workspace : lui donner le filtre inverse `corpus != dg-reform-tsi`
   (possible seulement par l'API, `PATCH /api/v1/workspaces/{workspace}/indexes/{index}`).

## À fournir ou vérifier côté client

- **Plan** : embeddings hébergés `DeepsetNvidia*` (modèle `intfloat/multilingual-e5-base`) disponibles ;
  durée maximale d'une recherche (2 minutes dans la doc : l'analyse d'impact est limitée à 3 recherches web
  pour cette raison) ; volume de documents de l'index (~84 000).
- **URL de l'API** de la plateforme du client (région) et une **clé API du workspace** pour le téléversement.
- **Intégration OpenAI** (*Settings > Integrations*) avec la clé du client : elle sert aussi à la recherche web
  (outil intégré d'OpenAI, facturé par OpenAI à l'appel en plus des tokens ; aucun autre compte nécessaire).
  L'organisation OpenAI du client doit avoir accès à l'outil `web_search` de l'API Responses.
- **Modèle** : `gpt-5.4` doit figurer dans la liste des modèles OpenAI de la plateforme.

## Étapes

### 1. Workspace et intégrations

Créer le workspace `tsi-reform` (*Settings > Organization > Workspaces*), connecter OpenAI, créer une clé API
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

Analyse d'impact (`tsi-impact`), le titre du projet comme requête :
- « Barnahus Slovenia: Supporting children victims of violence » : attendu 7-8/10 (trial 7, local Opus 8).
- « Technical Support in transforming the existing fully state-administered funded pension scheme in
  Lithuania » : attendu ~2/10 (trial 2, local 2).
- Sources web en liens Markdown cliquables ; dernière ligne « Impact score: X/10 … — Confidence: … ».

## Recherche web

`web_search` est un sous-pipeline : il envoie la requête de l'agent à gpt-5.4 par l'API Responses avec l'outil
intégré `web_search` d'OpenAI, et renvoie les constats avec leurs URL sources. L'agent principal ne peut pas
utiliser directement cet outil intégré : un Agent Haystack remplace les outils de son générateur par ses propres
outils (`search_projects`, `search_deliverables`), d'où ce sous-appel, sur le modèle de la version locale (sous-appel
au CLI Claude). Chaque recherche est donc un appel OpenAI de plus, qui
prend plusieurs dizaines de secondes : 3 recherches au plus par analyse, contexte de recherche réduit
(`search_context_size: low`), réponse de 200 mots. Sur le trial, 4 recherches avec le contexte par défaut ont
dépassé la limite de 2 minutes (`CancelledError`). Le générateur `OpenAIResponsesChatGenerator` avec outils intégrés demande Haystack 3.2.

## Mises à jour du corpus

Relancer l'extraction (`run`, `deliverables`), puis `prod-export` et `upload.ps1` : `OVERWRITE` remplace les
fichiers modifiés et la plateforme ne réindexe que ceux-là. Un document supprimé localement n'est pas supprimé
sur la plateforme : le supprimer dans *Files* (son nom figure dans l'export précédent, pas dans le nouveau).

## Différences avec la version locale

| Local (CLI Claude) | Production |
|---|---|
| `list_projects`, `find_projects` : comptes et listes exacts sur tout le corpus | recherche seulement : listes non exhaustives (le prompt le fait dire à l'agent) |
| `get_project` : texte complet d'un projet | passages les plus pertinents seulement |
| impact : Opus, 10 recherches (WebSearch du CLI), `fetch_page`, score par un second appel | gpt-5.4, 3 recherches web OpenAI, score dans le rapport |

Ces outils peuvent revenir comme composants custom si le besoin est confirmé en production.
