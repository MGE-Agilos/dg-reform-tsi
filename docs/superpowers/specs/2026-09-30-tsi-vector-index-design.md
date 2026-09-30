# Agent de vectorisation (couche RAG) — Design

**Date :** 2026-09-30
**Statut :** validé en conversation, en relecture
**Contexte :** deuxième brique du pipeline DG REFORM. Elle prépare les fichiers produits par l'agent d'extraction
([design](2026-09-30-tsi-extraction-agent-design.md)) pour le futur agent RAG / chatbot.

## 1. Objectifs et contraintes

- Construire une **couche vectorisée séparée** à partir des fichiers extraits. Les fichiers extraits restent la
  source de vérité : la couche vectorisée peut être supprimée et reconstruite à tout moment.
- **Premier run : full.** Les runs suivants ne vectorisent que les fichiers **nouveaux ou modifiés**, et retirent
  ceux qui ont disparu.
- **Pipeline Haystack** importable dans deepset Studio, sous la forme d'un *index* (pipeline d'indexation). Le
  chatbot sera un pipeline de requête distinct (hors périmètre).
- **Traçabilité et coûts :** même journal que l'extraction (`logs/runs/<run_id>.jsonl`, résumé, `costs.csv`).
- **Phase locale :** la souscription Claude ne fournit pas d'embeddings (Anthropic n'a pas de modèle d'embedding).
  Cet agent n'utilise **aucun LLM** : découpage et métadonnées sont déterministes, seul un modèle d'embedding
  travaille. En local, c'est un **modèle open source gratuit** qui tourne sur le PC, sans API.
- **Phase deepset :** l'embedder devient `OpenAIDocumentEmbedder` (`text-embedding-3-small`, clé OpenAI) et le
  store devient le document store de l'espace de travail. Les vecteurs des deux modèles sont incompatibles :
  le changement de modèle déclenche une revectorisation complète (~730 k tokens, ~0,02 $).
- Tout reste dans le dossier du projet (OneDrive), comme pour l'extraction.

Hors périmètre : pipeline de requête / chatbot, reranking, recherche hybride BM25, déploiement deepset.

## 2. Corpus (mesuré le 2026-09-30)

| Dossier | Fichiers | Forme |
|---|---|---|
| `output/projects/` (détail) | 668 | Front matter YAML + intro officielle + `## Summary (AI-generated)` + sections officielles (`## Context`, `## Support delivered`, `## Results achieved` / `## Expected results`, ou `## Page text`) + `## Listed in country factsheets as` + `## Sources` |
| `output/projects/` (thin) | 1 432 | Front matter + une phrase (« … No detail page is published. ») + `## Sources` |
| `output/programme/` | 37 | Front matter (`doc_id`, `title`, `source_url`) + texte continu sans sections `##` |

Environ 550 000 mots au total (~730 k tokens).

## 3. Architecture

Pipeline Haystack `index`, distinct du pipeline d'extraction, lancé par `python -m tsi_agent index [--full]` :

```
ProjectFileLoader ──changed──▶ ProjectChunker ──▶ Embedder ──▶ VectorLayerWriter
        └────────────removed──────────────────────────────────────▲
```

### 3.1 `ProjectFileLoader` (local uniquement)

- Entrées : `full: bool`. Paramètres : `output_dir`, `vectors_dir`, `embedder_id`, `chunker_version`.
- Lit `output/projects/*.md` et `output/programme/*.md`, calcule le `sha256` de chaque fichier.
- Compare à `output/vectors/state.json` : `{files: {relpath: {hash, chunk_ids}}, embedder_id, chunker_version}`.
- Sorties : `files` (nouveaux ou modifiés : chemin relatif + texte + hash) et `removed` (chemins disparus, avec
  leurs `chunk_ids`).
- **Full forcé** si `--full`, si `state.json` ou `store.json` est absent, ou si `embedder_id` ou `chunker_version` diffèrent de
  l'état : tous les fichiers sont renvoyés et le writer vide la couche avant d'écrire.
- Dans deepset, ce composant disparaît : l'index reçoit les fichiers de l'espace de travail et deepset gère
  lui-même l'incrémental.

### 3.2 `ProjectChunker` (portable)

- Entrée : `files`. Sortie : `documents` (Haystack `Document`).
- Parse le front matter (réutilise `render.parse_front_matter`). Un fichier sans front matter valide est ignoré
  avec une alerte.
- Découpe le corps par section `##`. Le texte avant la première section (intro officielle) forme la section
  `intro`. Les sections `## Sources` et `## Listed in country factsheets as` ne sont pas vectorisées (liste d'URL
  et titres déjà en métadonnées).
- Une section de plus de 300 mots est redécoupée en fenêtres de ~250 mots avec 40 mots de recouvrement, en
  coupant de préférence en fin de phrase. Les documents programme (sans sections) passent directement par ce
  découpage.
- Un projet thin donne un seul morceau.
- Identifiant stable par morceau : `<chemin relatif sans .md>#<index>` (unique par fichier ; `doc_id` reste en
  métadonnée). `doc_id` est `project_id` ou `doc_id` du front matter : deux fichiers peuvent le partager sans
  faire collision sur leurs identifiants de morceaux.
- Métadonnées de chaque morceau :
  - `doc_id`, `doc_type` (`project` | `programme`), `source_file` (chemin relatif), `section`, `chunk_index` ;
  - `title`, `programme`, `year`, `countries` (liste ISO), `multi_country`, `policy_area`, `detail_page`,
    `results`, `spotlight`, `highlighted`, `listed` ;
  - `ai_generated` (vrai pour la section Summary), `source_url` (page détail, sinon première source).
- `context_header` : une ligne « Titre — Programme Année — Pays » ajoutée au texte vectorisé via
  `meta_fields_to_embed=["context_header"]`, pour qu'un morceau isolé garde son contexte. Le `content` du
  document reste le texte officiel, sans ajout.
- `chunker_version` (constante) : à incrémenter à chaque changement de règle de découpage, ce qui force un full.

### 3.3 Embedder (remplaçable par YAML)

- Local : `SentenceTransformersDocumentEmbedder` (paquet `sentence-transformers-haystack`), modèle
  `intfloat/multilingual-e5-small` (384 dimensions, ~470 Mo). Multilingue : une question en français retrouve un
  texte anglais. Préfixe `"passage: "` exigé par e5 ; le futur pipeline de requête utilisera le même modèle avec
  le préfixe `"query: "`. `normalize_embeddings=True`.
- deepset : `OpenAIDocumentEmbedder`, `model: text-embedding-3-small`, `meta_fields_to_embed` identique, sans
  préfixe.
- `embedder_id` = `"<type>:<model>"`, calculé depuis le composant, enregistré dans l'état et dans la
  métadonnée `embedder` de chaque morceau.

### 3.4 `VectorLayerWriter` (local ; remplacé par `DocumentWriter` dans deepset)

- Entrées : `documents`, `removed`, `files` (pour l'état), `full`.
- Ordre : charge `output/vectors/store.json` (`InMemoryDocumentStore.load_from_disk`) ; en full, vide le store ;
  supprime les `chunk_ids` des fichiers modifiés et disparus ; écrit les nouveaux morceaux ; sauvegarde
  `store.json` (écriture dans un fichier temporaire puis remplacement, avec `_retry_on_lock` pour OneDrive) ;
  puis seulement met à jour `state.json`.
- Un run interrompu avant la sauvegarde laisse l'ancien état intact : le run suivant refait le travail.
- Sorties : `stats` (`files_indexed`, `files_removed`, `chunks_written`, `chunks_deleted`, `chunks_total`).

## 4. Traçabilité et coûts

- Réutilise `runlog` : un run `index` a son `run_id`, son JSONL, son résumé et une ligne dans `costs.csv`.
- Événements : `load` (par fichier : `new` | `changed` | `removed` | `unchanged`, résumé agrégé pour
  `unchanged`), `chunk` (par fichier : nombre de morceaux, mots), `embed` (par lot : nombre de morceaux, tokens
  estimés, durée, modèle, coût), `write` (stats), `alert`.
- Coût : 0 $ en local ; avec OpenAI, `tokens × tarif` configuré dans `settings.yaml` (`pricing`), comme pour
  l'extraction. Les tokens viennent de la réponse OpenAI (`meta.usage`) quand elle est disponible, sinon d'une
  estimation (mots × 1,33).

## 5. Configuration et CLI

- `config/settings.yaml` : `vectors_dir: output/vectors`, `embedding_model: intfloat/multilingual-e5-small`,
  `embedding_batch_size: 32`.
- `python -m tsi_agent index [--full]` : codes de sortie 0 (ok) et 2 (erreur), comme `run`.
- `python -m tsi_agent export-yaml --index PATH` : exporte le pipeline d'indexation.
- README : section « Couche vectorisée » (usage, sorties, passage à OpenAI dans deepset).

## 6. Erreurs

- Fichier illisible ou front matter invalide : alerte, fichier ignoré, son état n'est pas mis à jour (retenté
  au run suivant).
- Échec de l'embedder (modèle introuvable, erreur OpenAI) : le run s'arrête en erreur (code 2) sans écrire le
  store ni l'état.
- Store illisible (fichier corrompu) : le run s'arrête en erreur avec un message invitant à relancer avec `--full`, qui reconstruit la couche depuis les fichiers.

## 7. Tests

- Chunker : sur de vrais fichiers exportés copiés en fixtures (un détail avec sections, un détail « Page text »,
  un thin, un document programme) : sections, métadonnées, identifiants, redécoupage des longues sections,
  exclusion de Sources et Listed.
- Loader et writer : nouveau, modifié, supprimé, inchangé, changement de modèle ou de version (full), `--full`,
  interruption avant sauvegarde. Embedder factice (`MockDocumentEmbedder` ou vecteurs déterministes).
- Pipeline : construction, run de bout en bout sur un petit dossier, aller-retour `dumps()` / `loads()`, et
  remplacement de l'embedder par `OpenAIDocumentEmbedder` dans le YAML.
- Intégration (marquée, lancée à la main) : vrai modèle e5 sur 3 fichiers, puis recherche d'une question en
  français qui retrouve le bon projet.

## 8. Dépendances

`sentence-transformers-haystack` (installe `sentence-transformers` et PyTorch CPU, ~1 à 2 Go avec le modèle).
Mise en extra `[local]` du `pyproject.toml`, pour que l'image deepset n'installe pas PyTorch.
