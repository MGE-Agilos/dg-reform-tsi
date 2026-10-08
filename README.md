# TSI Extraction Agent (DG REFORM)

Premier agent du pipeline Haystack pour la Commission européenne. Il crawle les sites publics du
Technical Support Instrument (TSI) et de son prédécesseur, le SRSP, et produit **un fichier Markdown
par projet** dans `output/projects/`, avec les métadonnées dans un front matter YAML. On peut le relancer
régulièrement : il ne traite que les projets nouveaux ou modifiés.

Design : [docs/superpowers/specs/2026-09-30-tsi-extraction-agent-design.md](docs/superpowers/specs/2026-09-30-tsi-extraction-agent-design.md)

## Installation

```powershell
python -m venv .venv
.venv\Scripts\python -m pip install -e ".[dev]"
```

Pour l'exécution locale, le CLI `claude` doit être installé et connecté à une souscription Claude
(`claude --version`). Aucune clé API n'est nécessaire.

## Utilisation

```powershell
# Pilote : Croatie seulement, 3 projets envoyés au LLM au maximum
.venv\Scripts\python -m tsi_agent run --countries HR --limit 3

# Voir ce qui serait extrait, sans LLM ni écriture
.venv\Scripts\python -m tsi_agent run --dry-run

# Run complet incrémental (le premier envoie environ 620 projets au LLM ; plusieurs heures, surtout
# à cause du débit limité imposé au site — voir le throttling adaptatif plus bas)
.venv\Scripts\python -m tsi_agent run

# Tout ré-extraire, même les pages inchangées
.venv\Scripts\python -m tsi_agent run --full

# Exporter le pipeline pour deepset Studio
.venv\Scripts\python -m tsi_agent export-yaml pipeline.yaml
```

Ordre recommandé pour un cycle complet : `run` (extraction des projets), puis `deliverables` (livrables des
projets), puis `index` (couche vectorisée) — chaque étage dépend des fichiers écrits par le précédent.

Un run interrompu (Ctrl-C, limite de souscription) reprend là où il s'était arrêté au run suivant.

`--limit` compte les projets réellement envoyés au LLM : les pages inchangées depuis le dernier run ne l'entament pas.

Codes de sortie : `0` run terminé, `1` arrêt sur la limite de la souscription (le run suivant reprend), `2` erreur inattendue (détail dans `logs/runs/<run_id>.summary.json`, champ `error`).

**`--dry-run`** fait la découverte et la détection sans appeler le LLM ni rien écrire, puis affiche trois
compteurs : les pages détail candidates (`detail_candidates` — tous les projets qui ont une page détail ;
leur hash n'est vérifié qu'au run réel, donc certains peuvent en fait être déjà à jour), les projets thin à
écrire (`thin_to_write`) et les délistages (`delisted`).

**Débit adaptatif :** le site répond par des HTTP 429 au-delà d'environ 1 requête/s soutenue. Après un 429,
l'agent ralentit durablement (l'intervalle minimum entre requêtes double, jusqu'à 15 s) et ne réaccélère que
progressivement, après 50 requêtes réussies d'affilée. C'est la première cause de durée d'un run complet.

## Ce qu'est un « projet »

Un projet correspond à une page détail du site de la Commission. Une même page peut couvrir plusieurs composantes nationales, listées dans les factsheets sous des titres différents (par exemple les 25 projets « PACE 2023 »). Ces titres sont conservés dans `listing_titles` et dans la section « Listed in country factsheets as ». Un titre générique de projet *flagship* qui pointe vers une page différente dans chaque pays donne un projet par page. Les mentions de ce titre sans lien forment un projet « thin » distinct. Un projet sans page détail (« thin ») est décrit à partir du listing seul.

Le corps du fichier reprend mot pour mot les sections officielles de la page (Context, Support delivered,
puis Results achieved ou Expected results selon le titre trouvé sur la page) ; seul le résumé est généré par
le LLM, sous l'étiquette « Summary (AI-generated) ». Le front matter porte `results: achieved` ou `expected`
en conséquence (`null` si la page n'a pas de section résultats reconnue).

## Sorties

| Chemin | Contenu |
|---|---|
| `output/projects/<project_id>.md` | Un fichier par projet : front matter (pays, année, programme, titres de listing nationaux, sources, hash, run, `results: achieved\|expected\|null`) + un résumé généré par IA (« Summary (AI-generated) ») suivi des sections officielles de la page recopiées mot pour mot (Context, Support delivered, Results achieved ou Expected results) |
| `output/programme/*.md` | Documents programme : chiffres par pays, domaines politiques, performance TSI |
| `output/state/index.json` | État utilisé pour l'incrémental. Ne pas éditer à la main. |
| `logs/runs/<run_id>.jsonl` | Journal : une action par ligne (fetch, classification, appel LLM avec tokens et coût, écriture…) |
| `logs/runs/<run_id>.summary.json` | Résumé du run, aussi affiché en fin de run |
| `logs/costs.csv` | Une ligne par run : appels LLM, tokens, coût |

Avec la souscription, `cost_usd` est un **coût équivalent API** (notionnel), calculé par le CLI.
Il sert à estimer le budget du passage à l'API.

## Livrables des projets

Un second pipeline, `deliverables`, parcourt les pages détail déjà en cache HTTP (celles rafraîchies par le
dernier `run`), relève leurs liens vers des pages de publication (`/publications-0/...`), télécharge les
documents qui y sont listés (rapport final, présentation, factsheet…) et en extrait le texte pour produire,
par publication, un résumé généré par IA inséré dans le fichier du ou des projets concernés.

```powershell
# Pilote : 3 publications envoyées au LLM au maximum
.venv\Scripts\python -m tsi_agent deliverables --limit 3

# Run complet incrémental (seules les publications nouvelles ou dont les fichiers ont changé sont retraitées)
.venv\Scripts\python -m tsi_agent deliverables

# Tout retraiter, même les publications inchangées
.venv\Scripts\python -m tsi_agent deliverables --full

# Recopier pays, année, programme et domaine des projets liés dans les textes intégraux existants (sans LLM ni réseau)
.venv\Scripts\python -m tsi_agent deliverables --refresh-metadata

# Exporter le pipeline pour deepset Studio
.venv\Scripts\python -m tsi_agent export-yaml --deliverables deliverables.yaml
```

Cet étage se place après `run` (il lit le cache HTTP des pages détail que `run` vient de rafraîchir) et avant
`index` (pour que la section générée soit vectorisée avec le reste du projet).

| Chemin | Contenu |
|---|---|
| `output/deliverables/<publication_id>.md` | Texte intégral des documents d'une publication, tel que publié (non vectorisé), avec un front matter (titre, URL, date, projets concernés, fichiers) |
| `output/state/deliverables.json` | État utilisé pour l'incrémental (une entrée par publication traitée, `files_key` pour détecter un fichier ajouté ou remplacé). Ne pas éditer à la main. |

Le fichier de chaque projet concerné reçoit une section « Deliverables (AI-generated summary) » (une
sous-section par publication, la plus récente d'abord, avec liens vers les fichiers et vers le texte intégral)
insérée juste avant les sections listing/sources, et le front matter gagne un champ `deliverables: N`. Une page
de publication peut lister une traduction nationale d'un document à côté de sa version anglaise : seule la
version anglaise est retenue si elle existe, sinon la version principale de la page.

Le texte envoyé au LLM pour le résumé est limité au début des documents (`deliverables_max_words`, 15 000 mots
par défaut) ; au-delà, le texte est tronqué et le prompt le signale. Ces prompts étant longs, le délai d'appel
au CLI `claude` a son propre réglage, plus large que celui de l'extraction (`deliverables_llm_timeout_s`,
600 s par défaut). Comme pour l'extraction, aucune clé API n'est nécessaire en local : le résumé passe par le
CLI `claude` de la souscription. Les fichiers lus sont les PDF (pdfplumber) et les documents Word `.docx`
(python-docx : paragraphes et tableaux, dans l'ordre du document) ; les autres formats sont ignorés avec une
alerte. Les fichiers ne passent jamais par le cache HTTP (ils ne changent pas d'URL quand on les retélécharge,
et un cache de plusieurs centaines de PDF n'apporterait rien) ; seules les pages HTML (détail, publication)
sont mises en cache.

Une publication traitée depuis moins de `deliverables_recheck_days` jours (7 par défaut) n'est pas revérifiée :
sa page de publication n'est même pas récupérée. Les relances rapprochées (reprise après une limite de
souscription) repartent donc directement sur les publications restantes. Un run hebdomadaire revérifie tout,
et `--full` force le retraitement.

Une publication qui n'est plus liée depuis aucune page détail (document retiré du site) est nettoyée : son
entrée disparaît de `output/state/deliverables.json`, et sa sous-section disparaît du fichier des projets qui
la citaient (le compteur `deliverables` suit). Son texte intégral reste dans `output/deliverables/` (fichier
orphelin, sans effet). Ce nettoyage ne se déclenche que lorsque la découverte a pu lire toutes les pages détail
en cache pendant le run ; s'il en manque une (page pas encore récupérée par `run`), rien n'est supprimé par
prudence, et le nettoyage sera retenté au run suivant.

Premier run complet (mesuré du 30 septembre au 2 octobre 2026) : 498 publications, 806 fichiers, 13,7 millions
de mots de texte intégral, 473 appels LLM pour 57 $ en équivalent API (Sonnet). Sur la souscription, il a fallu
une vingtaine de reprises après la limite d'usage, soit environ deux jours. Comme pour `run`, un arrêt sur la
limite est repris au run suivant sans retraiter ce qui a déjà été écrit. Pour un run de plusieurs heures,
lancer la commande dans un terminal qui reste ouvert, avec une boucle de reprise (par exemple : relancer après
30 minutes tant que le code de sortie vaut `1`).

Codes de sortie : identiques à `run` (`0` terminé, `1` arrêt sur la limite de la souscription, `2` erreur
inattendue).

**Passage à deepset :** comme pour `extractor`, le `chat_generator` du composant `summarizer` est remplacé par
un `OpenAIChatGenerator` dans le YAML exporté (voir la section dédiée plus bas).

## Couche vectorisée (préparation du RAG)

Un second pipeline, `index`, découpe les fichiers de `output/projects` et `output/programme` en morceaux (un par
section, redécoupé au-delà de 300 mots), les vectorise et les range dans une base Qdrant locale, `~/tsi-vectors` (réglage `vectors_dir`, hors OneDrive).
Les fichiers extraits ne sont pas modifiés : la couche vectorisée peut être supprimée et reconstruite à tout moment.

**Un seul processus à la fois sur la base locale** : Qdrant verrouille son dossier. Arrêter le chatbot pendant `index`
(et inversement) ; le message d'erreur le rappelle.

```powershell
# Une fois : embedder local (PyTorch CPU + modèle multilingual-e5-small, ~1 à 2 Go)
.venv\Scripts\python -m pip install -e ".[local]"

# Premier run : tout ; ensuite : seulement les fichiers nouveaux, modifiés ou supprimés
.venv\Scripts\python -m tsi_agent index
.venv\Scripts\python -m tsi_agent index --full

# Export pour un index deepset
.venv\Scripts\python -m tsi_agent export-yaml --index index.yaml
```

| Chemin | Contenu |
|---|---|
| `~/tsi-vectors/qdrant/` | Base Qdrant locale (collection `tsi`, similarité cosinus, `embedding_dim: 384`) : morceaux avec texte, métadonnées et vecteur. Plus de `store.json`. |
| `~/tsi-vectors/state.json` | Hash de chaque fichier indexé et identifiants de ses morceaux. Ne pas éditer à la main. |

Chaque morceau porte en métadonnées le projet (`doc_id`, titre, programme, année, pays, domaine, `detail_page`…),
la section et `ai_generated` (vrai pour le résumé IA). Aucun LLM n'intervient : seul l'embedder travaille.
Changer de modèle d'embedding ou de règles de découpage déclenche automatiquement un full. L'identifiant d'un
morceau est `<chemin relatif sans .md>#<n>` (ex. `projects/energy-regulation-academy#0`) ; le `doc_id` du projet
ou du document programme est conservé à part dans les métadonnées.

Environ 70 000 morceaux au total, dont environ 65 000 pour le texte intégral des livrables (`output/deliverables`, découpé
par page). Un full représente environ 1 h de vectorisation (embedder e5 sur CPU) plus environ 16 min d'écriture ; le
dossier Qdrant pèse environ 300 à 500 Mo dans `~/tsi-vectors`. Un run incrémental ne touche que les fichiers modifiés
(suppression des morceaux périmés, puis écriture). La base n'est pas synchronisée par OneDrive.

Un full vide la base en supprimant puis recréant la collection ; `state.json` est invalidé avant, si bien qu'un full
interrompu est refait en entier au run suivant. Si le dossier est déjà ouvert par un autre processus (chatbot), `index`
échoue tout de suite.

Dans deepset, l'embedder devient `OpenAIDocumentEmbedder` (même `meta_fields_to_embed`, sans préfixe), et on ajoute
son tarif pour journaliser le coût :

```yaml
embedder:
  type: haystack.components.embedders.openai_document_embedder.OpenAIDocumentEmbedder
  init_parameters:
    model: text-embedding-3-small
    meta_fields_to_embed: [context_header]
    api_key: {type: env_var, env_vars: [OPENAI_API_KEY], strict: true}
```

```yaml
# config/settings.yaml
pricing:
  text-embedding-3-small: {input: 0.02}
```

Le chargeur local (`ProjectFileLoader`) et le writer local (`VectorLayerWriter`) sont remplacés dans deepset par
l'entrée fichiers de l'index et un `DocumentWriter` vers le document store de l'espace de travail ; `ProjectChunker`
reste identique.

**À savoir pour deepset :**

- `loader` (`ProjectFileLoader`) et `vector_writer` (`VectorLayerWriter`) sont **retirés** du YAML dans deepset,
  pas édités : leurs paramètres d'init contiennent des chemins locaux et l'empreinte de l'embedder utilisé.
- La connexion `embedder.meta → vector_writer.embed_meta` n'existe que si l'embedder a une sortie `meta`
  (OpenAI : oui ; sentence-transformers : non). En exportant avec l'embedder local puis en passant à OpenAI dans
  deepset, ajouter cette connexion pour garder un comptage de tokens exact (sinon le coût est estimé à partir du
  nombre de mots). Dans l'autre sens (retour à un embedder local), la retirer, sinon le YAML ne se recharge pas.
- Le futur pipeline de requête doit utiliser le même modèle d'embedding (en local, avec le préfixe `query: `).

## Chatbot (agent RAG)

Un troisième pipeline, `chat`, répond aux questions sur les projets à partir de la couche vectorisée. C'est un
`Agent` Haystack qui dispose de quatre outils :

| Outil | Rôle |
|---|---|
| `search_projects` | Recherche sémantique dans les textes des projets, filtres pays, années, programme, domaine ; renvoie des passages numérotés [n] |
| `search_deliverables` | Recherche sémantique dans le texte intégral des livrables (rapports, présentations) : méthodes, chiffres, recommandations ; renvoie des passages [n] avec document et page |
| `list_projects` | Comptage et liste exacts des projets correspondant à des filtres, sur tout le corpus |
| `get_project` | Texte complet d'un projet |

```powershell
# Prérequis : la couche vectorisée (python -m tsi_agent index, chatbot arrêté) et l'extra local
.venv\Scripts\python -m tsi_agent chat            # ouvre http://127.0.0.1:8765
.venv\Scripts\python -m tsi_agent export-yaml --chat chat.yaml
```

Exemples de questions :
- « Que fait le projet Energy Regulation Academy ? »
- « Quels projets aident la Croatie à réformer son système de santé ? »
- « Combien de projets TSI pour la Grèce en 2023 ? »
- « Compare les projets santé de la Croatie et de la Slovénie. », puis « Et en Slovaquie ? »

Chaque réponse cite ses sources [n] vers les pages de la Commission et signale ce qui vient d'un résumé IA. La page
affiche les étapes de l'agent, la durée et le coût équivalent API. Chaque question est un run du journal
(`logs/runs/`, `mode: chat`). En local, une réponse demande 2 à 4 appels au CLI `claude`, soit 20 à 60 s.

Un seul processus à la fois sur la base locale : ne pas lancer `index` pendant que le chatbot tourne.

`export-yaml --chat` construit le pipeline comme la commande `chat` : il exige donc la couche vectorisée déjà
construite et l'extra `[local]` (embedder local pour `search_projects`).

**Passage à deepset :** le `chat_generator` de l'Agent reste un `LoggedChatGenerator` (il journalise coût et
tokens quel que soit le modèle appelé) ; seul le generator qu'il enveloppe change. Dans le YAML exporté,
remplacer :
- `agent.init_parameters.chat_generator.init_parameters.chat_generator` (le `ClaudeCliChatGenerator`) par un
  `OpenAIChatGenerator`, qui appelle les outils nativement ;
- le `text_embedder` des outils `search_projects` et `search_deliverables` par un `OpenAITextEmbedder` (`text-embedding-3-small`, sans
  préfixe), cohérent avec l'embedder de l'index ;
- le `LocalQdrantDocumentStore` (dans les outils et le retriever) et le `LocalQdrantEmbeddingRetriever` par le
  document store de l'espace de travail (OpenSearch) et son retriever (par exemple `OpenSearchEmbeddingRetriever`).

Les quatre outils, le prompt et l'Agent ne changent pas.

## Analyse d'impact

L'onglet « Impact analysis » de la page du chatbot (`python -m tsi_agent chat`) évalue l'impact d'un projet choisi
dans le catalogue (`output/projects/`). Un `Agent` Haystack lit le projet (`get_project`), ses livrables
(`search_deliverables`), cherche des preuves sur le web (`web_search`) et lit les pages ou documents trouvés
(`fetch_page`, HTML, PDF, Word). Il rend un rapport à sections fixes : présentation du projet, preuves d'impact
positif, preuves d'impact négatif ou limites, impact par niveau (pays, Europe, monde), évaluation par objectif et
sources. Les sources sont reconstruites par le code à partir des outils : toute citation [n] inconnue est retirée.

```powershell
.venv\Scripts\python -m tsi_agent chat                                # onglet Impact analysis
.venv\Scripts\python -m tsi_agent export-yaml --impact impact.yaml
```

Une analyse à la fois, en arrière-plan ; la page affiche les étapes en direct. Chaque analyse est un run du journal
(`logs/runs/`, `mode: impact`) et le rapport est enregistré dans `output/impact/<project_id>.md` (front matter :
verdict, confiance, coût, durée), en remplaçant le précédent. Une réponse finale vide ou illisible n'écrase jamais
un rapport existant.

Une fois le rapport écrit, un appel LLM séparé (`impact_score_model`) lit le rapport terminé et lui attribue un score
d'impact positif de 0 à 10 (0-3 Weak, 4-6 Moderate, 7-10 Strong) avec une justification ; la page l'affiche en
jauge. Pour recalculer le score des rapports existants sans relancer l'analyse :

```powershell
.venv\Scripts\python -m tsi_agent impact-rescore                    # tous les rapports de output/impact
.venv\Scripts\python -m tsi_agent impact-rescore <project_id> ...
```

Réglages (`config/settings.yaml`) : `impact_model`, `impact_score_model`, `impact_max_steps` (appels LLM par analyse),
`impact_max_searches` (recherches web par analyse), `impact_page_words` (texte d'une page transmis à l'agent),
`impact_page_max_mb` (taille maximale d'une page téléchargée), `impact_project_chars`, `impact_llm_timeout_s`,
ainsi que `web_search_model` et `web_search_timeout_s` pour la recherche web. `fetch_page` ne lit que des URL
http(s) publiques (adresses locales et privées refusées, y compris après redirection) et s'arrête au-delà de
`impact_page_max_mb` Mo ou de 20 s.

En local, la recherche web passe par le CLI `claude` (`ClaudeWebSearch`, outil natif WebSearch) et le LLM par
`ClaudeCliChatGenerator`.

**Passage à deepset :** le YAML exporté contient `ClaudeWebSearch` et `ClaudeCliChatGenerator` ; on les remplace à la
main :
- le `search_engine` de l'outil `web_search` (`ClaudeWebSearch`) par `SerperDevWebSearch` (package
  `serperdev-haystack`, clé API Serper), qui a la même interface (`query` → `documents`, `links`) ;
- le `chat_generator` enveloppé par le `LoggedChatGenerator` de l'Agent par un `OpenAIChatGenerator` ;
- comme pour le chatbot, l'embedder et le document store des outils `get_project` et `search_deliverables`.

Le lecteur de pages (`BoundedLinkFetcher`) peut rester tel quel ou être remplacé par le `LinkContentFetcher` de
Haystack (sans limite de taille ni filtrage des adresses).

## Banc de recherche

Mesure si la recherche du chatbot retrouve le bon passage et le bon fichier (spec :
`docs/superpowers/specs/2026-10-08-tsi-retrieval-bench-design.md`). **Arrêter le chatbot avant** (une seule
ouverture de la base Qdrant).

```powershell
.venv\Scripts\python -m tsi_agent eval-retrieval                      # questions réutilisées si elles existent
.venv\Scripts\python -m tsi_agent eval-retrieval --regenerate         # nouveau jeu de questions (Haiku)
.venv\Scripts\python -m tsi_agent eval-retrieval --skip-chunk-sizes   # sans la mesure des tailles (~30 min)
```

150 morceaux tirés au hasard (graine fixe : 60 projets, 60 livrables, 30 programme ou résumés IA ; options
`--n-project`, `--n-deliverable`, `--n-other`), une question par morceau rédigée par Haiku (moitié français, moitié
anglais), enregistrée dans `output/eval/questions.jsonl` et réutilisée ensuite. Quatre variantes sont mesurées sur
l'index actuel : A sémantique (actuel), B + filtre pays, C hybride sémantique + BM25 (RRF), D au plus 2 morceaux par
fichier ; puis trois tailles de morceaux (150 / 250 / 400 mots) sur les projets et le programme, revectorisés dans un
index temporaire en mémoire. Sorties : `output/eval/retrieval-<date>.md` (rapport) et `.csv` (rangs par question).

## Planification (Windows)

À exécuter depuis le dossier du dépôt (les chemins sont résolus en absolu à partir du dossier courant) :

```powershell
$repo = (Get-Location).Path
$python = "$repo\.venv\Scripts\python.exe"
$chain = "& '$python' -m tsi_agent run; & '$python' -m tsi_agent deliverables; & '$python' -m tsi_agent index"
$action = New-ScheduledTaskAction -Execute "powershell.exe" -Argument "-NoProfile -Command `"$chain`"" -WorkingDirectory $repo
$trigger = New-ScheduledTaskTrigger -Weekly -DaysOfWeek Monday -At 7am
Register-ScheduledTask -TaskName "TSI Extraction Agent" -Action $action -Trigger $trigger
```

Les trois commandes s'enchaînent dans cet ordre (`run`, puis `deliverables`, puis `index`) : chacune dépend des
fichiers écrits par la précédente.

Le journal du run planifié est dans `logs/` (`logs/runs/<run_id>.jsonl` et `.summary.json`), comme pour un run
manuel. Code de sortie `1` : le run s'est arrêté sur la limite de la souscription, et le suivant reprendra.
Code `2` : erreur à examiner dans le journal.

## Passage à deepset Studio (API OpenAI)

Le pipeline est un pipeline Haystack standard (`export-yaml`). En production, le LLM passe par l'API
OpenAI : dans le YAML, on remplace le `chat_generator` de `extractor` par
`haystack.components.generators.chat.openai.OpenAIChatGenerator` :

```yaml
extractor:
  type: tsi_agent.components.extractor.ProjectExtractor
  init_parameters:
    chat_generator:
      type: haystack.components.generators.chat.openai.OpenAIChatGenerator
      init_parameters:
        model: gpt-4o-mini
        api_key: {type: env_var, env_vars: [OPENAI_API_KEY], strict: true}
        generation_kwargs:
          response_format: {type: json_object}
    pricing:
      gpt-4o-mini: {input: 0.15, output: 0.60}   # $ / million de tokens, pour journaliser le coût
```

Le reste ne change pas. Le composant de stockage local (`LocalProjectStore`) sera remplacé par une
implémentation qui écrit dans l'espace de travail deepset (hors périmètre de cette version).
Le package `tsi_agent` doit être installé côté deepset en tant que composant custom. Au rechargement
d'un YAML, Haystack 3 exige `allowed_modules=["tsi_agent"]` (voir `tsi_agent.pipeline.load_pipeline`).

### Ce qui ne passe pas par le YAML

Remplacer le `chat_generator` dans le YAML suffit pour le LLM (vérifié : `OpenAIChatGenerator` se recharge
bien). Ce n'est pas le cas de tout le reste :

- **La boucle par lots, le journal de run (JSONL, résumé, `costs.csv`) et la reprenabilité vivent dans
  `runner.py`**, en dehors du pipeline Haystack. Dans deepset, ce code n'est pas exécuté : les événements de
  `runlog` retombent alors sur le `logging` standard, et il n'y a ni fichier de run ni `costs.csv`.
- **`detector.run_id` et `writer.run_id` sont des entrées obligatoires du pipeline** (pas des paramètres
  d'init) : il faut les fournir explicitement à chaque exécution du pipeline dans deepset.
- **`output_dir`, `cache_dir` et `executable` sont des paramètres d'init locaux**, propres à l'exécution sur
  le poste de l'utilisateur.
- **Remplacer le stockage local par l'espace de travail deepset demande du code**, pas seulement du YAML :
  une nouvelle implémentation de `ProjectStore` (voir `store.py`), branchée dans `ChangeDetector`,
  `DetailPageFetcher` et `MarkdownProjectWriter`.

## Tests

```powershell
.venv\Scripts\python -m pytest -q
```

Les fixtures de `tests/fixtures/` sont des copies réelles des pages du site (septembre 2026).
