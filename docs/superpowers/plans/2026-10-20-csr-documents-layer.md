# Couche « document CSR » (27 par an) et CSR 2026 — design et plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Constat (validé avec l'utilisateur le 2026-10-20).** Chaque année, la Commission publie **un document de recommandations par État membre**, soit 27 par an. Celle de 2026 se trouve ici : https://reforms-investments.ec.europa.eu/publications-0/2026-european-semester-country-specific-recommendations-commission-recommendations_en. C'est une publication avec 27 PDF en anglais (`<ISO>-COM_2026_<n>_<k>_EN_ACT_part1_v4.pdf`), plus des traductions.

Un document contient des **recommandations numérotées**, de 1 à 5. La base ECFIN, notre source actuelle, découpe chacune en **sous-parties** (1.1, 1.2…). On a donc trois niveaux :

| Niveau | Identifiant | Données actuelles |
|---|---|---|
| Document CSR (pays × année) | `csrdoc-<ISO>-<année>` | **absent** |
| Recommandation | `csr-<ISO>-<année>-<n>` | 903 (2016-2025) |
| Sous-partie | `csr-<ISO>-<année>-<n>.<m>` | 3 217 ; liens projet et évaluations |

La base ECFIN s'arrête aux CSR 2025. La publication 2026 a été extraite par erreur comme livrable du projet `energy-regulation-academy` (`output/state/deliverables.json`, publication `2026-european-semester-country-specific-recommendations-commission-recommendations`). Son texte intégral est déjà dans `output/deliverables/<publication_id>.md`, une section `## <document>` par PDF.

**Design validé.**
1. **Couche document.** Les documents sont dérivés des recommandations, regroupés par pays et année : 270 pour 2016-2025, plus 27 pour 2026. Champs : `doc_id`, `country`, `year`, `title` (« Croatia — 2025 country-specific recommendations »), `n_recommendations`, `stage`, `document_url`.
   - `stage` vaut « Council recommendation » pour 2016-2025 et « Commission recommendation » pour 2026.
   - `document_url` : pour 2026, le PDF anglais du pays ; pour 2016-2025, la base ECFIN (`csr.SOURCE_URL`), aucune URL par document n'étant disponible.
2. **CSR 2026.**
   - **Source :** le texte intégral déjà extrait du livrable mal rattaché. À défaut, télécharger les PDF de la page de publication (fetcher poli).
   - **Analyse :** on extrait le pays depuis l'ISO du nom de fichier, puis la partie qui suit « HEREBY RECOMMENDS that <Country> take action in 2026 and 2027 to: ». Les paragraphes numérotés « 1. », « 2. »… y deviennent des recommandations `csr-<ISO>-2026-<n>`, sans sous-partie : la recommandation sert alors de sous-partie candidate, comme les CSR sans sous-partie.
   - **Évaluation :** aucune (« not yet assessed »). La source est mentionnée dans les fiches.
   - **Fiabilité :** déterministe, sans LLM. Le contrôle se fait sur les 27 documents, qui doivent tous donner au moins une recommandation. Une alerte est émise pour tout document sans résultat.
   - **Liaison :** pas de liaison aux projets. La fenêtre y−3..y−1 concerne les projets de 2027 et après, mais elle s'applique d'elle-même aux projets futurs.
3. **Livrable mal rattaché.** Les publications CSR (identifiant ou titre contenant « country-specific-recommendations » ou « Country Specific Recommendation ») sont exclues des livrables de projet. Elles sont écartées par une liste dans `config/sources.yaml` (`deliverable_exclude_patterns`) et retirées du projet au prochain passage de `deliverables`.
4. **Stockage et diffusion.**
   - **Fichiers :** les fiches `output/csr/csr-<ISO>-<année>-<n>.md` gagnent `doc_id` et `stage` dans le front matter. On ajoute une fiche `output/csr/csrdoc-<ISO>-<année>.md` par document : titre, lien, liste des recommandations avec résumé et évaluation. Le chat et la production l'indexent comme `doc_type: csr` avec `csr_level: document`. Les fiches de recommandation ont `csr_level: recommendation`.
   - **Supabase :** une migration `005_csr_documents.sql` ajoute la table `csr_documents` (RLS testeurs, mêmes droits que `csrs`) et une colonne `csrs.doc_id` (FK vers `csr_documents`). `tsi_dashboard()` est mis à jour :
     - `totals` gagne `csr_documents` et `csr_documents_latest_year` ;
     - `csrs_by_year` compte les documents par année (`{year, n_documents, n_recommendations}`) ;
     - `csr_coverage_by_country` est complété par les documents couverts.
   - **Synchronisation :** `supabase-sync` alimente `csr_documents`.
   - **Assistant :** l'Edge Function `tsi-assistant` affiche, dans `search_csrs` et `get_csr`, le document d'appartenance, et accepte un identifiant `csrdoc-…` dans `get_csr`. Le prompt explique les trois niveaux.
   - **Production :** `prod-export` exporte aussi les fiches document. Une clé de métadonnées `csr_level` (str) est ajoutée à `META_TYPES`.
5. **Interface `web/`** (task 3, agent séparé).
   - **Onglet CSRs :**
     - sélecteur d'année, avec la plus récente par défaut ;
     - liste des 27 documents de l'année : pays, nombre de recommandations, évaluation dominante, nombre de projets liés ;
     - fiche document : recommandations, puis sous-parties, avec évaluations et projets liés ;
     - l'ancienne fiche de recommandation reste accessible.
   - **Dashboard :**
     - KPI « CSR documents: 27 per year (2016–2026) », sous-ligne « N recommendations · M sub-parts » ;
     - graphique « par année » exprimé en documents ;
     - bouton ⓘ qui explique les trois niveaux.
   - **Lineage :** trois colonnes (documents, recommandations, projets). La sélection d'un document met en évidence toute sa descendance.

**Conventions :**
- branche `feature/csr-documents` ;
- TDD ;
- tests dans `tmp_path`, jamais sous `output/` ;
- commits en français, terminés par le trailer Co-Authored-By ;
- pas de push.

### Task 1 : données (Python) — 2026, documents, exclusion du livrable, diffusion
### Task 2 : Supabase (migration 005, sync, `tsi_dashboard`) et Edge Function `tsi-assistant`
### Task 3 : interface `web/` (CSRs, Dashboard, Lineage)
### Task 4 : mise en service (contrôleur)

Étapes :
1. `csr-build` en local ;
2. vérification des 27 documents 2026 ;
3. migration ;
4. `supabase-sync` ;
5. redéploiement de la fonction ;
6. `index` ;
7. publication.
