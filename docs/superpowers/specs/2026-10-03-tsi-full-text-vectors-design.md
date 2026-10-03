# Vectorisation du texte intégral des livrables (Qdrant local) — Design

**Date :** 2026-10-03
**Statut :** validé en conversation
**Contexte :** la couche vectorisée ([design](2026-09-30-tsi-vector-index-design.md)) ne contient que les fichiers
projet et programme (6 318 morceaux, dont 1 473 de résumés de livrables). Le texte intégral des livrables
(`output/deliverables/`, 498 fichiers, 13,7 millions de mots) n'est pas vectorisé, si bien que le chatbot ne peut
pas citer un passage d'un rapport absent de son résumé. L'utilisateur demande de tout vectoriser.

## 1. Constats

- Ordres de grandeur : environ 60 000 à 65 000 morceaux supplémentaires, soit environ 1 h de vectorisation e5 sur
  CPU (~58 ms par morceau).
- Le stockage actuel (`store.json` réécrit en entier, InMemory chargé en mémoire) ne tient pas à cette échelle :
  ~300 Mo réécrits et resynchronisés par OneDrive à chaque run, et ~1 Go de mémoire pour le chatbot.
- `qdrant-haystack` 11.1 fonctionne en mode local (`QdrantDocumentStore(path=...)`), sans serveur : identifiants
  texte, filtres Haystack, suppression par id, sérialisation YAML. Mais **un seul client par dossier** : une
  deuxième instance sur le même `path` lève `RuntimeError: Storage folder ... is already accessed`, même dans le
  même processus. `QdrantEmbeddingRetriever.from_dict` ne recharge que `QdrantDocumentStore` (une sous-classe est
  refusée).

## 2. Décisions

- **Stockage :** Qdrant local dans `~/tsi-vectors`, hors OneDrive (réglage `vectors_dir`, `~` développé). Il
  remplace `store.json`. Collection `tsi`, dimension 384 (`embedding_dim`), similarité cosinus. `state.json` est
  conservé à côté, avec le même rôle (hash par fichier, identifiants des morceaux, embedder, version du
  découpage).
- **Connexion partagée :**
  - `LocalQdrantDocumentStore(QdrantDocumentStore)` réutilise un client Qdrant unique par dossier (cache de
    module, protégé par un verrou) ;
  - `LocalQdrantEmbeddingRetriever(QdrantEmbeddingRetriever)` recharge ce store depuis le YAML ;
  - dans deepset, les deux sont remplacés par le store et le retriever OpenSearch de l'espace de travail.
- **Un seul processus à la fois sur la base :** pendant `index`, le chatbot doit être arrêté. Le message d'erreur
  le dit explicitement.
- **Texte intégral vectorisé :** les fichiers `output/deliverables/*.md` sont chargés comme les fichiers projet,
  sous le dossier `deliverables`, et découpés :
  - par fichier (`## <document>`), puis par page (`--- page N ---`), en fenêtres de ~250 mots (règles actuelles) ;
  - métadonnées : `doc_type: "deliverable"`, `doc_id` (publication), `title`, `date`, `section` (document),
    `page`, `source_url` (URL du fichier), `projects` (ids des projets liés), et, quand elles sont connues,
    `countries`, `year`, `programme` et `policy_area` des projets liés (union des pays, première année, premier
    programme et premier domaine). `ai_generated: false`.
- **Métadonnées projet dans le front matter des textes intégraux :** `DeliverableTextExtractor` et un nouveau
  `deliverables --refresh-metadata` écrivent `countries`, `year`, `programme` et `policy_area` (lus dans les fichiers
  projet liés) dans le front matter de `output/deliverables/<id>.md`. `--refresh-metadata` sert une fois, pour les
  498 fichiers existants, sans LLM ni réseau.
- **Chatbot : 4ᵉ outil `search_deliverables(query, filtres…, publication_id?, project_id?)`.** Recherche dans le
  texte intégral (`doc_type == "deliverable"`), avec un post-filtre pays et projet en Python comme
  `search_projects`, et la numérotation continue des sources. Le résultat donne « publication — document, page N »,
  l'URL du fichier et l'extrait. `search_projects` reste limité à `doc_type == "project"` (et `programme` sur
  demande). Le prompt système indique d'utiliser `search_projects` / `list_projects` pour identifier les projets,
  et `search_deliverables` pour les détails (méthodes, chiffres, recommandations, résultats précis).
- `CHUNKER_VERSION` passe à 3 ; le changement de dossier et de stockage déclenche de toute façon un full.

Hors périmètre : nettoyage des colonnes mal extraites des PDF, OCR, serveur Qdrant, recherche hybride.

## 3. Tests

- Découpage d'un texte intégral (fixture réelle) : un morceau par fenêtre, `page` correct, métadonnées projet
  reprises du front matter, ids uniques.
- `LocalQdrantDocumentStore` : deux instances sur le même dossier partagent le client ; aller-retour YAML via
  `LocalQdrantEmbeddingRetriever`.
- Writer et loader sur Qdrant (dossier temporaire) : full, incrémental (modifié, supprimé), passage depuis l'ancien
  état.
- `--refresh-metadata` : front matter enrichi depuis les fichiers projet.
- `search_deliverables` : filtres (publication, projet, pays), numérotation, libellé de page.
- Pipeline chat : 4 outils, aller-retour YAML.
- Réel : full (~1 h), puis 3 questions qui exigent le texte intégral.
