# Banc de recherche (retrieval benchmark) — Design

**Date :** 2026-10-08
**Statut :** validé en conversation
**But :** mesurer si la recherche du chatbot retrouve le bon passage / le bon fichier, et quels réglages l'améliorent
(taille des morceaux, nombre de résultats, recherche hybride, diversification).

## 1. Jeu de questions

- Tirage aléatoire (graine fixe) de 150 morceaux dans la base Qdrant actuelle, stratifié : 60 `project`, 60
  `deliverable`, 30 `programme` ou résumés IA (`ai_generated`) ; morceaux de moins de 60 mots exclus.
- Haiku (CLI `claude`, sortie structurée) rédige une question réaliste par morceau, comme un utilisateur la poserait,
  moitié en français, moitié en anglais, **sans recopier les mots du texte** ; la question doit pouvoir être résolue
  par ce morceau. Lots de 10 morceaux par appel.
- Pour chaque question : id du morceau attendu, fichier (`doc_id`), projets liés, pays, type, langue.
- Le jeu est enregistré (`output/eval/questions.jsonl`) et réutilisé tel quel entre les mesures.

## 2. Mesures sur l'index actuel (sans revectorisation)

Pour chaque question, rang du bon morceau et du bon fichier dans les résultats ; recall@1/3/5/8/16/24 et MRR
(morceau et fichier), par type de document et par langue, pour quatre variantes :

- **A** sémantique seule (actuel, e5 + Qdrant) ;
- **B** sémantique + filtre pays (pays du morceau attendu, quand il est connu) ;
- **C** hybride : sémantique + BM25 (InMemoryBM25Retriever sur le texte de tous les morceaux) fusionnés par
  Reciprocal Rank Fusion ;
- **D** sémantique avec au plus 2 morceaux par fichier dans les résultats.

## 3. Taille des morceaux

Variantes 150 / 250 (actuel) / 400 mots (chevauchement proportionnel) sur le sous-corpus projets + programme
uniquement (~6 300 morceaux, index InMemory temporaire), avec les questions de projets. Le bon morceau n'existe pas
dans les autres découpages : la mesure se fait au niveau fichier, et au niveau « passage » par recouvrement de texte
(le morceau trouvé contient au moins la moitié des mots de la phrase-clé du morceau d'origine). Indicateur, pas une
preuve pour les livrables.

## 4. Sortie

- Commande `python -m tsi_agent eval-retrieval [--questions N] [--regenerate] [--skip-chunk-sizes]`.
- Rapport Markdown `output/eval/retrieval-<date>.md` : tableaux, questions ratées (question, attendu, trouvé à la
  place), recommandation ; détail par question `output/eval/retrieval-<date>.csv`.
- Le serveur du chatbot doit être arrêté pendant la mesure (un seul processus sur la base Qdrant).

## 5. Tests

Unitaires avec un petit store de test et un générateur scripté : tirage stratifié reproductible, parsing des
questions, calcul des rangs/recall/MRR, fusion RRF, diversification, recouvrement de texte, rendu du rapport.
