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

## Couche vectorisée (préparation du RAG)

Un second pipeline, `index`, découpe les fichiers de `output/projects` et `output/programme` en morceaux (un par
section, redécoupé au-delà de 300 mots), les vectorise et les range dans `output/vectors/`. Les fichiers extraits
ne sont pas modifiés : la couche vectorisée peut être supprimée et reconstruite à tout moment.

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
| `output/vectors/store.json` | Morceaux avec texte, métadonnées et vecteur (format `InMemoryDocumentStore.save_to_disk`) |
| `output/vectors/state.json` | Hash de chaque fichier indexé et identifiants de ses morceaux. Ne pas éditer à la main. |

Chaque morceau porte en métadonnées le projet (`doc_id`, titre, programme, année, pays, domaine, `detail_page`…),
la section et `ai_generated` (vrai pour le résumé IA). Aucun LLM n'intervient : seul l'embedder travaille.
Changer de modèle d'embedding ou de règles de découpage déclenche automatiquement un full. L'identifiant d'un
morceau est `<chemin relatif sans .md>#<n>` (ex. `projects/energy-regulation-academy#0`) ; le `doc_id` du projet
ou du document programme est conservé à part dans les métadonnées.

Environ 5 000 morceaux au total (2 137 fichiers) : `store.json` pèse environ 50 Mo et est intégralement réécrit
à chaque run qui modifie quelque chose (OneDrive le resynchronise donc en entier à ce moment-là).

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
`Agent` Haystack qui dispose de trois outils :

| Outil | Rôle |
|---|---|
| `search_projects` | Recherche sémantique dans les textes des projets, filtres pays, années, programme, domaine ; renvoie des passages numérotés [n] |
| `list_projects` | Comptage et liste exacts des projets correspondant à des filtres, sur tout le corpus |
| `get_project` | Texte complet d'un projet |

```powershell
# Prérequis : la couche vectorisée (python -m tsi_agent index) et l'extra local
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

`export-yaml --chat` construit le pipeline comme la commande `chat` : il exige donc la couche vectorisée déjà
construite et l'extra `[local]` (embedder local pour `search_projects`).

**Passage à deepset :** le `chat_generator` de l'Agent reste un `LoggedChatGenerator` (il journalise coût et
tokens quel que soit le modèle appelé) ; seul le generator qu'il enveloppe change. Dans le YAML exporté,
remplacer :
- `agent.init_parameters.chat_generator.init_parameters.chat_generator` (le `ClaudeCliChatGenerator`) par un
  `OpenAIChatGenerator`, qui appelle les outils nativement ;
- le `text_embedder` de l'outil `search_projects` par un `OpenAITextEmbedder` (`text-embedding-3-small`, sans
  préfixe), cohérent avec l'embedder de l'index ;
- le `PersistedInMemoryDocumentStore`, qui apparaît dans les trois outils, par le document store de l'espace de
  travail (et `InMemoryEmbeddingRetriever` par le retriever correspondant, par exemple `OpenSearchEmbeddingRetriever`).

Les trois outils, le prompt et l'Agent ne changent pas.

## Planification (Windows)

À exécuter depuis le dossier du dépôt (les chemins sont résolus en absolu à partir du dossier courant) :

```powershell
$repo = (Get-Location).Path
$action = New-ScheduledTaskAction -Execute "$repo\.venv\Scripts\python.exe" -Argument "-m tsi_agent run" -WorkingDirectory $repo
$trigger = New-ScheduledTaskTrigger -Weekly -DaysOfWeek Monday -At 7am
Register-ScheduledTask -TaskName "TSI Extraction Agent" -Action $action -Trigger $trigger
```

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
