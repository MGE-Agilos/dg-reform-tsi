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

# Run complet incrémental (le premier envoie environ 620 projets au LLM et dure plusieurs heures)
.venv\Scripts\python -m tsi_agent run

# Tout ré-extraire, même les pages inchangées
.venv\Scripts\python -m tsi_agent run --full

# Exporter le pipeline pour deepset Studio
.venv\Scripts\python -m tsi_agent export-yaml pipeline.yaml
```

Un run interrompu (Ctrl-C, limite de souscription) reprend là où il s'était arrêté au run suivant.

`--limit` compte les projets réellement envoyés au LLM : les pages inchangées depuis le dernier run ne l'entament pas.

Codes de sortie : `0` run terminé, `1` arrêt sur la limite de la souscription (le run suivant reprend), `2` erreur inattendue (détail dans `logs/runs/<run_id>.summary.json`, champ `error`).

## Ce qu'est un « projet »

Un projet correspond à une page détail du site de la Commission. Une même page peut couvrir plusieurs composantes nationales, listées dans les factsheets sous des titres différents (par exemple les 25 projets « PACE 2023 »). Ces titres sont conservés dans `listing_titles` et dans la section « Listed in country factsheets as ». Un titre générique de projet *flagship* qui pointe vers une page différente dans chaque pays donne un projet par page. Les mentions de ce titre sans lien forment un projet « thin » distinct. Un projet sans page détail (« thin ») est décrit à partir du listing seul.

## Sorties

| Chemin | Contenu |
|---|---|
| `output/projects/<project_id>.md` | Un fichier par projet : front matter (pays, année, programme, titres de listing nationaux, sources, hash, run) + Summary / Context / Support delivered / Results achieved |
| `output/programme/*.md` | Documents programme : chiffres par pays, domaines politiques, performance TSI |
| `output/state/index.json` | État utilisé pour l'incrémental. Ne pas éditer à la main. |
| `logs/runs/<run_id>.jsonl` | Journal : une action par ligne (fetch, classification, appel LLM avec tokens et coût, écriture…) |
| `logs/runs/<run_id>.summary.json` | Résumé du run, aussi affiché en fin de run |
| `logs/costs.csv` | Une ligne par run : appels LLM, tokens, coût |

Avec la souscription, `cost_usd` est un **coût équivalent API** (notionnel), calculé par le CLI.
Il sert à estimer le budget du passage à l'API.

## Planification (Windows)

```powershell
schtasks /Create /TN "TSI Extraction Agent" /SC WEEKLY /D MON /ST 07:00 /TR "cmd /c cd /d \"%CD%\" && .venv\Scripts\python -m tsi_agent run >> logs\scheduled.log 2>&1"
```

Code de sortie `1` : le run s'est arrêté sur la limite de la souscription, et le suivant reprendra. Code `2` : erreur à examiner dans le journal.

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

## Tests

```powershell
.venv\Scripts\python -m pytest -q
```

Les fixtures de `tests/fixtures/` sont des copies réelles des pages du site (septembre 2026).
