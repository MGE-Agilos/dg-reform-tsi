# Vectorisation du texte intégral (Qdrant local) — Plan d'implémentation

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development. Steps use checkbox (`- [ ]`) syntax.

**Goal :** passer la couche vectorisée sur Qdrant local (`~/tsi-vectors`), y ajouter le texte intégral des livrables, et donner au chatbot un outil `search_deliverables`.

**Spec :** [docs/superpowers/specs/2026-10-03-tsi-full-text-vectors-design.md](../specs/2026-10-03-tsi-full-text-vectors-design.md). Lire aussi la spec et le code de la couche vectorisée (`src/tsi_agent/vector_state.py`, `components/file_loader.py`, `components/vector_writer.py`, `index_pipeline.py`, `chunking.py`) et du chatbot (`src/tsi_agent/chat/`).

**Conventions :**
- Branche `feature/full-text-vectors`. Tests : `.venv/Scripts/python -m pytest -q` (276 passent, 2 désélectionnés).
- `qdrant-haystack` 11.1 est déjà installé.
- Chaque commit se termine par `Co-Authored-By: Claude Opus 5.5 (1M context) <noreply@anthropic.com>`, sans autre attribution.
- Ne pas toucher `output/` (lecture seule), ni `~/tsi-vectors`. Les tests utilisent des dossiers temporaires.

**Faits vérifiés (prototype du 2026-10-03) :**
- `QdrantDocumentStore(path=d, index="t", embedding_dim=4, similarity="cosine")` fonctionne en local. Il accepte les ids texte (`"projects/a#0"`), les filtres Haystack (`meta.year >= 2020`), `filter_documents`, `delete_documents(ids)` et `count_documents()`. `to_dict()` contient `path`.
- Une 2ᵉ instance sur le même `path` lève `RuntimeError: Storage folder ... is already accessed by another instance of Qdrant client`. La sous-classe ci-dessous, qui surcharge `_initialize_client` pour réutiliser un client par dossier, résout le problème dans un même processus.
- `QdrantEmbeddingRetriever.from_dict` appelle `QdrantDocumentStore.from_dict`, qui refuse une sous-classe. Il faut donc une sous-classe du retriever qui surcharge `from_dict`.
- `QdrantEmbeddingRetriever(document_store=s).run(query_embedding=..., filters=..., top_k=...)` renvoie `documents`.

---

### Task 1 : stockage Qdrant local et couche vectorisée sur Qdrant

**Files :** create `src/tsi_agent/qdrant_local.py` ; modify `src/tsi_agent/config.py`, `config/settings.yaml`, `pyproject.toml`, `src/tsi_agent/vector_state.py`, `src/tsi_agent/components/file_loader.py`, `src/tsi_agent/components/vector_writer.py`, `src/tsi_agent/index_pipeline.py`, `tests/conftest.py`, `tests/test_vector_layer.py`, `tests/test_index_pipeline.py`, `README.md`.

- [ ] **`src/tsi_agent/qdrant_local.py`**

```python
"""Qdrant en mode local (dossier, sans serveur) : un seul client par dossier dans le processus.

Qdrant local verrouille son dossier : une 2e instance de QdrantDocumentStore sur le meme `path` echoue, meme dans
le meme processus. Les outils du chatbot et le writer de la couche vectorisee partagent donc un client.
Dans deepset, ces deux classes sont remplacees par le document store et le retriever de l'espace de travail.
"""

import threading
from pathlib import Path
from typing import Any

import qdrant_client
from haystack import default_from_dict
from haystack_integrations.components.retrievers.qdrant import QdrantEmbeddingRetriever
from haystack_integrations.document_stores.qdrant import QdrantDocumentStore

_CLIENTS: dict[str, qdrant_client.QdrantClient] = {}
_LOCK = threading.Lock()
COLLECTION = "tsi"


class LocalQdrantDocumentStore(QdrantDocumentStore):
    def _initialize_client(self) -> None:
        if self._client is None:
            key = str(Path(self.path).expanduser().resolve())
            with _LOCK:
                if key not in _CLIENTS:
                    try:
                        _CLIENTS[key] = qdrant_client.QdrantClient(path=key)
                    except RuntimeError as exc:  # dossier verrouille par un autre processus
                        raise RuntimeError(f"couche vectorisee {key} deja ouverte par un autre processus "
                                           "(chatbot ou index en cours ?) : arretez-le puis relancez") from exc
                self._client = _CLIENTS[key]
            self._set_up_collection(self.index, self.embedding_dim, self.recreate_index, self.similarity,
                                    self.use_sparse_embeddings, self.sparse_idf, self.on_disk,
                                    self.payload_fields_to_index)

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "LocalQdrantDocumentStore":
        return default_from_dict(cls, data)


class LocalQdrantEmbeddingRetriever(QdrantEmbeddingRetriever):
    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "LocalQdrantEmbeddingRetriever":
        params = data["init_parameters"]
        params["document_store"] = LocalQdrantDocumentStore.from_dict(params["document_store"])
        return default_from_dict(cls, data)


def open_store(vectors_dir: str, embedding_dim: int) -> LocalQdrantDocumentStore:
    path = Path(vectors_dir).expanduser() / "qdrant"
    path.mkdir(parents=True, exist_ok=True)
    return LocalQdrantDocumentStore(path=str(path), index=COLLECTION, embedding_dim=embedding_dim,
                                    similarity="cosine", progress_bar=False)
```

Vérifier dans `.venv` :
- les noms d'attributs (`self.path`, `self.payload_fields_to_index`…) et la signature de `_set_up_collection` ;
- si `QdrantEmbeddingRetriever.from_dict` fait autre chose, par exemple désérialiser `filter_policy` : le reproduire ;
- si `QdrantDocumentStore.from_dict` désérialise un `api_key` (Secret) : le reproduire dans `LocalQdrantDocumentStore.from_dict`.

- [ ] **Réglages.**
  - `Settings` : `vectors_dir: str = "~/tsi-vectors"` et `embedding_dim: int = 384`. Le dossier est développé (`expanduser`) là où il est utilisé.
  - `settings.yaml` : `vectors_dir: ~/tsi-vectors`, avec en commentaire « hors OneDrive : base Qdrant locale ». `embedding_dim: 384`.
  - `pyproject` `[local]` : ajouter `"qdrant-haystack>=11"`.

- [ ] **Couche vectorisée sur Qdrant.**
  - `VectorLayerWriter` reçoit `document_store` (paramètre d'init sérialisé avec `component_to_dict`, désérialisé dans `from_dict` via `_deserialize_object` ou équivalent). Il ne lit ni n'écrit plus `store.json`.
  - En full : vider le store (`delete_all_documents()` s'il existe dans qdrant-haystack 11.1 ; sinon supprimer les ids de `filter_documents()` ou recréer la collection), puis écrire.
  - En incrémental : `delete_documents(stale)`, puis `write_documents(docs, policy=DuplicatePolicy.OVERWRITE)`.
  - Garder l'ordre « store puis `state.json` » et les stats (`chunks_total = count_documents()`).
  - `ProjectFileLoader` : remplacer le test « store.json absent » par « dossier `<vectors_dir>/qdrant` absent » (raison `no_store`).
  - `vector_state.py` : retirer `store_path`, `load_store`, `save_store` et `new_store` s'ils ne servent plus. `read_documents` ne sert plus qu'au chatbot actuel : le supprimer à la tâche 3, quand le chatbot passe sur Qdrant.
  - `build_index_pipeline` : `store = open_store(settings.vectors_dir, settings.embedding_dim)`, passé au writer. Le loader et `state.json` utilisent `Path(settings.vectors_dir).expanduser()`.
  - Tests :
    - adapter `tests/test_vector_layer.py` et `tests/test_index_pipeline.py` (dossiers temporaires, `LocalQdrantDocumentStore` ; pour lire le résultat, `store.filter_documents()`) ;
    - ajouter un test qui vérifie que deux `open_store(d, 8)` partagent le client (pas d'erreur, même `count_documents`) ;
    - ajouter un aller-retour YAML d'un pipeline contenant un `LocalQdrantEmbeddingRetriever`.
  - `make_vector_store` (conftest) : construire la couche dans un dossier temporaire Qdrant et renvoyer le dossier `vectors_dir`. Les tests du chatbot, qui l'utilisent, sont adaptés à la tâche 3. D'ici là, ils peuvent échouer : regrouper les tâches 1 et 3 dans le même lot si nécessaire.
  - Test d'intégration (`-m integration`) : l'adapter, sans le lancer.
  - README, section « Couche vectorisée » : base Qdrant dans `~/tsi-vectors`, un seul processus à la fois (arrêter le chatbot pendant `index`), plus de `store.json`.

- [ ] Commit : `feat: couche vectorisee sur Qdrant local (hors OneDrive), client partage par dossier`

### Task 2 : texte intégral des livrables dans la couche vectorisée

**Files :** `src/tsi_agent/chunking.py`, `src/tsi_agent/components/file_loader.py`, `src/tsi_agent/components/deliverables.py`, `src/tsi_agent/runner.py`, `src/tsi_agent/__main__.py`, tests (`tests/test_chunking.py`, `tests/test_deliverables.py`, `tests/test_vector_layer.py`, `tests/test_cli.py`), fixture `tests/fixtures/vec_deliverable.md` (copie réduite d'un vrai fichier de `output/deliverables/` : 2 fichiers, 3 pages chacun environ, front matter complet).

- [ ] **Loader** : `FOLDERS = ("projects", "programme", "deliverables")`.

- [ ] **Chunking** (`CHUNKER_VERSION = 3`). Dans `chunk_file`, si le front matter a `publication_id`, appeler `chunk_deliverable(rel_path, fm, body)` :
  - découper le corps par `## <titre du document>`, et lire dans chaque bloc la ligne `Source: <url>` ;
  - découper chaque document par `--- page N ---` ;
  - dans chaque page, appliquer `split_words` (mêmes seuils) ;
  - ignorer les pages vides ou de moins de 5 mots ;
  - id : `<rel sans .md>#<n>` ;
  - meta :
    - `doc_id` = `publication_id`, `doc_type` = `"deliverable"`, `source_file`, `title`, `date`, `projects` = `fm["projects"]` ;
    - `section` = titre du document, `page` = N, `source_url` = URL du fichier, `chunk_index`, `ai_generated` = False ;
    - `countries`, `year`, `programme`, `policy_area` depuis le front matter (None si absents), `detail_page` = None ;
    - `context_header` = « <titre de la publication> — <document>, p. N — <programme année> — <pays> » (mêmes règles que `context_header`).
  - Test sur la fixture : pages et sections correctes, ids uniques, `doc_type`, meta projet reprises.

- [ ] **Métadonnées projet dans les textes intégraux** (`components/deliverables.py`) :
  - fonction `project_meta(project_ids, store) -> dict` qui lit les fichiers projet liés (front matter) et renvoie :
    - `countries` : union triée ;
    - `year` : la plus petite non nulle ;
    - `programme` et `policy_area` : la première valeur non nulle ;
  - `DeliverableTextExtractor._write` ajoute ces champs au front matter. Le store est injectable via `_store` dans les tests, sinon `shared_store(output_dir)`.
  - `refresh_deliverables_metadata(settings) -> dict` (dans `runner.py`, ou dans le module des composants) : pour chaque entrée de `DeliverableStore`, il relit le fichier `full_text_path`, met à jour les 4 champs depuis `project_meta(entry.project_ids)`, et réécrit le fichier s'il a changé (`_retry_on_lock`). Il renvoie `{"files": n, "updated": m}` et est journalisé dans un run `runlog` (`mode: "refresh_metadata"`).
  - CLI : `deliverables --refresh-metadata` appelle cette fonction (ni LLM ni réseau) et renvoie le code 0.
  - Tests : le front matter enrichi à l'écriture, le refresh sur un état existant, la CLI.

- [ ] Commit : `feat: texte integral des livrables vectorise (decoupage par page), metadonnees projet`

### Task 3 : chatbot sur Qdrant, outil `search_deliverables`

**Files :** `src/tsi_agent/chat/store.py` (supprimer `PersistedInMemoryDocumentStore` ou le garder pour les tests si utile), `src/tsi_agent/chat/tools.py`, `src/tsi_agent/chat/pipeline.py`, `src/tsi_agent/chat/prompt.py`, `src/tsi_agent/vector_state.py` (`read_documents` si devenu inutile), tests du chatbot (`tests/test_chat_store_tools.py`, `tests/test_chat_agent.py`, `tests/test_chat_web.py`), README.

- [ ] **Pipeline** :
  - `build_chat_pipeline` utilise `open_store(settings.vectors_dir, settings.embedding_dim)` et `LocalQdrantEmbeddingRetriever(document_store=store, top_k=...)` ;
  - si le dossier `<vectors_dir>/qdrant` est absent, ou si la collection est vide, lever `VectorLayerError("couche vectorisee absente (...) : lancez d'abord python -m tsi_agent index")` ;
  - garder les paramètres `document_store=` et `retriever=` injectables pour les tests.

- [ ] **Outil `DeliverableSearch`** (`tools.py`), sur le modèle de `ProjectSearch` (mêmes filtres, `known_sources`, `runlog` `tool_call`) :
  - `run(query, countries=None, year_from=None, year_to=None, programme=None, policy_area=None, publication_id=None, project_id=None, known_sources=None)` ;
  - filtre `doc_type == "deliverable"` (via `build_filters(..., doc_type="deliverable")`), plus `meta.doc_id == publication_id` si donné ;
  - post-filtre en Python : pays (`_in_countries`) et `project_id in meta["projects"]` ;
  - 100 candidats si un post-filtre est actif, sinon `top_k × 3` ;
  - bloc : `[n] <title> — <section>, p. <page> — <programme year> — <countries>\nURL: <source_url>\n<extrait 1 200 caractères>` ;
  - sources : `{"ref", "doc_id", "title", "url", "section": f"{section}, p. {page}", "ai_generated": False, "score"}` ;
  - docstring en anglais avec `:param ...:` pour chaque paramètre visible du LLM. Description de l'outil : « Search the full text of the projects' deliverables (final reports, presentations): methods, figures, recommendations, detailed results. Returns numbered passages [n] with document and page. »

- [ ] **Prompt** (`prompt.py`, sans `{{`) :
  - ajouter l'outil à la liste ;
  - règle : « Use search_projects and list_projects to identify projects; use search_deliverables for details from the reports (figures, methods, recommendations, detailed results), and cite the document and page. »

- [ ] **Tests** : adapter les tests du chatbot à Qdrant (dossier temporaire, `make_vector_store`). Ajouter des tests de `DeliverableSearch` sur un store qui contient la fixture `vec_deliverable.md` : filtres publication, projet et pays, libellé de page, numérotation. Vérifier l'aller-retour YAML du pipeline chat avec 4 outils, et le remplacement du generator et de l'embedder par OpenAI, comme avant.

- [ ] **README** :
  - chatbot : 4ᵉ outil ;
  - deepset : remplacer `LocalQdrantDocumentStore` et `LocalQdrantEmbeddingRetriever` par le store et le retriever de l'espace de travail (OpenSearch) ;
  - un seul processus sur la base locale.

- [ ] Commit : `feat: chatbot sur Qdrant, outil search_deliverables (texte integral des rapports)`

### Task 4 (contrôleur) : essai réel

1. `deliverables --refresh-metadata` ;
2. arrêter le chatbot ;
3. `index` : full sur `~/tsi-vectors`, environ 1 h, à lancer dans un terminal de l'utilisateur si c'est trop long pour une tâche d'arrière-plan ;
4. relancer le chatbot et poser 3 questions qui exigent le texte intégral ;
5. supprimer l'ancien `output/vectors/` après accord de l'utilisateur.
