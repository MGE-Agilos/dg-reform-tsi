# Agent RAG / chatbot — Design

**Date :** 2026-09-30
**Statut :** validé en conversation, en relecture
**Contexte :** troisième brique du pipeline DG REFORM. Elle répond aux questions sur les projets TSI/SRSP à partir
de la couche vectorisée ([design](2026-09-30-tsi-vector-index-design.md)), elle-même construite à partir des
fichiers extraits ([design](2026-09-30-tsi-extraction-agent-design.md)).

## 1. Objectifs et contraintes

- Questions à couvrir :
  - **contenu d'un projet** : « Que fait le projet HEMS en Croatie ? » ;
  - **recherche thématique** avec filtres : « Projets de digitalisation fiscale depuis 2022 ? » ;
  - **comptages et listes** sur tout le corpus : « Combien de projets TSI en Grèce en 2023 ? » ;
  - **comparaisons** : « Compare les projets santé de la Croatie et de la Slovénie ».
- **Conversation multi-tours** : les questions de suivi (« Et en Slovénie ? ») fonctionnent.
- **Réponses sourcées** : chaque affirmation renvoie à une page de la Commission, et le contenu issu du résumé IA
  est signalé comme tel.
- **Local :** LLM Claude via le CLI `claude` (souscription), embedder de requête e5 local, interface web locale.
- **deepset :** `OpenAIChatGenerator`, `OpenAITextEmbedder` et le document store de l'espace de travail, par
  simple remplacement de composants dans le YAML. Les outils ne changent pas.
- **Traçabilité et coûts :** même journal que les autres agents.

Hors périmètre : authentification, historique persistant côté serveur, streaming des réponses, reranking,
évaluation automatique de la qualité des réponses, agent d'analyse d'impact.

## 2. Architecture

Pipeline Haystack `chat` construit autour du composant `haystack.components.agents.Agent` :

```
messages ──▶ Agent(chat_generator, tools=[search_projects, list_projects, get_project], system_prompt)
                 │                          │
                 │                          └─ outils = ComponentTool(<composant tsi_agent>)
                 │                                 └─ document_store + text_embedder
                 └──▶ messages (dont last_message)
```

- **Chat generator** : `ClaudeCliChatGenerator` étendu pour les outils (§4) en local, `OpenAIChatGenerator`
  dans deepset.
- **Outils** (§3) : trois composants Haystack sérialisables, exposés par `ComponentTool`.
- **Document store** :
  - en local, `PersistedInMemoryDocumentStore(path)`, une sous-classe d'`InMemoryDocumentStore` qui charge
    `output/vectors/store.json` à l'initialisation et dont `to_dict` ne contient que le chemin ;
  - dans deepset, le store OpenSearch de l'espace de travail.
- **Embedder de requête** :
  - en local, `SentenceTransformersTextEmbedder(model=multilingual-e5-small, prefix="query: ",
    normalize_embeddings=True)` ;
  - dans deepset, `OpenAITextEmbedder(text-embedding-3-small)`.
- **Limites** : `max_agent_steps = 6` ; réponse forcée au-delà (le LLM est prévenu dans le prompt).

## 3. Outils

Filtres communs, tous optionnels :
- `countries` : liste de codes ISO, par exemple `["HR"]` ;
- `year_from` et `year_to` ;
- `programme` : `TSI` ou `SRSP` ;
- `policy_area` : slug, par exemple `green-transition` ;
- `detail_only` : ne garder que les projets avec page détail.

Portabilité des filtres : `year`, `programme`, `policy_area`, `detail_page` et `doc_type` sont traduits en filtres
Haystack standards (même comportement dans l'InMemory et dans OpenSearch). **Le filtre pays est appliqué en Python
dans l'outil**, parce qu'un filtre sur un champ liste ne se comporte pas pareil dans les deux stores.

### 3.1 `search_projects(query, filtres…, top_k=8)` — `ProjectSearch`

- Embedde la requête, fait une recherche vectorielle avec les filtres portables sur 100 candidats si un filtre pays
  est présent (sinon `top_k × 3`), applique le filtre pays et garde `top_k` morceaux.
- Renvoie une liste numérotée. Pour chaque morceau :
  - projet (`doc_id`), titre, pays, programme et année ;
  - section, `ai_generated`, URL source, score ;
  - le texte du morceau, tronqué à 1 200 caractères.
- Ne renvoie que des morceaux de `doc_type == "project"`, sauf si `include_programme=true` (fiches pays et
  domaines politiques, utiles pour « combien de projets la Croatie a-t-elle reçus selon la Commission »).

### 3.2 `list_projects(filtres…, query_text?, limit=50)` — `ProjectLister`

- Parcourt les métadonnées de tous les morceaux `doc_type == "project"` correspondant aux filtres portables,
  applique le filtre pays, et **dédoublonne par `doc_id`**.
- `query_text` optionnel : filtre simple sans casse sur le titre (« energy », « tax »).
- Renvoie le **nombre total** de projets correspondants, la répartition par programme et par année, puis au plus
  `limit` projets triés par année décroissante. Chaque projet est donné avec son `doc_id`, son titre, ses pays, son
  programme, son année, `detail_page` et son URL.
- Implémentation : `document_store.filter_documents(filters)` (portable). Environ 5 000 morceaux : acceptable en
  local comme dans OpenSearch.

### 3.3 `get_project(doc_id)` — `ProjectReader`

- `filter_documents` sur `doc_id`, tri par `chunk_index`, puis reconstitution du texte par section avec les
  marqueurs `## <section>` et `(AI-generated summary)`, et les métadonnées en tête.
- Renvoie « introuvable » si l'id n'existe pas.

## 4. Outils avec le CLI `claude`

Le CLI n'appelle pas d'outils Python. `ClaudeCliChatGenerator.run(messages, tools=None)` émule donc l'appel
d'outils :

- Sans `tools` : comportement actuel, inchangé (utilisé par l'extraction).
- Avec `tools` :
  - le prompt système reçoit en plus la description de chaque outil et son schéma de paramètres ;
  - l'entrée standard contient la conversation sous forme de transcription : messages utilisateur, réponses de
    l'assistant, appels d'outils avec leurs arguments, résultats d'outils ;
  - `--json-schema` impose `{"tool_calls": [{"tool_name": <enum des outils>, "arguments": {...}}], "answer": string}`,
    avec la consigne : soit des `tool_calls` non vides, soit une `answer` ;
  - la réponse devient un `ChatMessage` assistant avec des `ToolCall` Haystack (id généré) ou avec le texte de
    `answer`.
- Chaque appel au CLI est indépendant : la transcription complète est renvoyée à chaque étape.
- Les métadonnées (`usage`, `cost_usd`, `model`) sont remplies comme aujourd'hui.

## 5. Prompt système (résumé des règles)

- Répondre dans la langue de la question.
- N'utiliser que les résultats des outils. Si l'information n'y est pas, le dire.
- Citer avec `[n]` et terminer par la liste des sources (titre + URL).
- Signaler ce qui vient d'un résumé IA (`ai_generated`).
- Pour un nombre ou une liste, utiliser `list_projects`, ne jamais estimer.
- Pour une comparaison, faire une recherche par élément comparé.
- Connaître les valeurs possibles : codes pays ISO des 27 États membres (GR pour la Grèce), programmes TSI (2021+)
  et SRSP (2017-2020), slugs des 10 domaines politiques.

## 6. Conversation et interface web locale

- `python -m tsi_agent chat [--port 8765] [--no-browser]` lance un serveur **Flask** (ajouté à l'extra `[local]`),
  charge le document store et le modèle une seule fois, puis ouvre le navigateur.
- `GET /` : une page HTML unique avec fil de conversation, zone de saisie et bouton « Nouvelle conversation ».
- `POST /api/chat` reçoit `{"history": [{"role": "user"|"assistant", "text": ...}], "question": ...}` et renvoie :
  - `answer` ;
  - `sources` : titre, URL et `ai_generated` ;
  - `steps` : outil, arguments, nombre de résultats et durée ;
  - `duration_s`, `cost_usd` et `run_id`.
- Côté client, l'historique vit dans la page. Seuls les questions et réponses finales sont renvoyées au tour suivant,
  sans les traces d'outils.
- `sources` est déduit des résultats d'outils cités dans la réponse, via les numéros `[n]`. À défaut, on renvoie
  les sources des résultats d'outils.
- Erreurs affichées dans la page : limite de la souscription atteinte, erreur du CLI, délai dépassé.
- Le serveur n'écoute que sur `127.0.0.1`.

## 7. Traçabilité et coûts

- Chaque question est un run `runlog` (`logs/runs/<run_id>.jsonl`, résumé, `costs.csv`, `mode: "chat"`).
  Événements journalisés :
  - `question` ;
  - `llm_call` (via le generator : tokens, coût, modèle, durée) ;
  - `tool_call` (outil, arguments, résultats, durée, erreur éventuelle) ;
  - `answer` (longueur, nombre de sources, étapes).
- Le journal est ouvert par le serveur autour de chaque appel à l'Agent. Les outils et le generator journalisent via
  le contextvar existant, qui doit être propagé si l'Agent exécute les outils dans des threads.

## 8. Configuration, CLI et deepset

- `config/settings.yaml` : `chat_model` (défaut `sonnet`), `chat_max_steps: 6`, `chat_port: 8765`.
- `python -m tsi_agent export-yaml --chat PATH` exporte le pipeline de chat.
- README :
  - usage ;
  - exemples de questions ;
  - passage à deepset : remplacer le chat generator, l'embedder de requête (sans le préfixe `query: `) et le
    document store ; les outils restent identiques.

## 9. Erreurs

- `output/vectors/store.json` absent ou illisible : le serveur refuse de démarrer, avec le message « lancez d'abord
  `python -m tsi_agent index` ».
- Outil inconnu, arguments invalides ou exception dans un outil : l'erreur est renvoyée au LLM sous forme de
  résultat d'outil (`raise_on_tool_invocation_failure=False`), qui peut corriger dans la limite des étapes.
- JSON du CLI non conforme (ni `tool_calls` ni `answer`) : une nouvelle tentative, puis `LLMCallError`.
- `LLMQuotaExceeded` : réponse d'erreur explicite, HTTP 429 côté API locale.

## 10. Tests

- Outils, sur un petit store construit à partir des fixtures `vec_*.md` et d'un embedder factice :
  - filtres portables ;
  - filtre pays ;
  - dédoublonnage et comptage ;
  - répartition par programme et par année ;
  - `get_project` qui reconstitue le texte, et le cas d'un id inconnu.
- `ClaudeCliChatGenerator` avec outils (faux sous-processus) :
  - commande et schéma JSON ;
  - transcription avec appels et résultats d'outils ;
  - réponse `tool_calls` convertie en `ToolCall`, réponse `answer` convertie en texte ;
  - JSON non conforme ;
  - sans outils, comportement inchangé.
- Agent de bout en bout avec un generator scénarisé (appel d'outil, puis réponse) : outils réellement exécutés,
  événements journalisés.
- API Flask (client de test) : réponse, sources, étapes ; erreur de quota convertie en 429 ; store absent.
- Pipeline : aller-retour `dumps()` / `loads()`, et remplacement par `OpenAIChatGenerator` et `OpenAITextEmbedder`
  dans le YAML.
- Manuel (réel) : cinq questions types (contenu, thématique, comptage, comparaison, suivi), avec la durée et le coût
  notés.
