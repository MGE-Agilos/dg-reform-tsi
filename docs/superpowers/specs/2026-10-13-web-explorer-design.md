# Explorateur web TSI × CSR (GitHub Pages + Supabase) — design (lot 1)

Date : 2026-10-13 · Dépôt : https://github.com/MGE-Agilos/dg-reform-tsi (public)
Supabase : projet `vxwmhsgxoomcbiukeinp`

## Objectif

Une page web, réservée à des testeurs connectés, pour parcourir sans installation :
- les projets TSI et SRSP ;
- les CSR du Semestre européen ;
- les liens projet → CSR évalués par IA, qui donnent le lignage CSR → projets ;
- le rapport d'anomalies.

Le lot 2, hors de ce document, ajoutera le chat : pgvector, une Edge Function et une clé Anthropic gardée en secret Supabase.

## Architecture

```
output/ (local) ──supabase-sync (service_role, .env)──▶ Supabase Postgres (RLS)
                                                          ▲
GitHub Pages (web/, clé anon) ── supabase-js ── Auth ─────┘
```

- **Données :** Supabase Postgres, sous le schéma `public`.
- **Création des tables :** le script `supabase/migrations/001_explorer.sql`, collé dans le *SQL Editor*.
- **Alimentation :** la commande `python -m tsi_agent supabase-sync`, qui fait un remplacement complet.
- **Front-end :** `web/`, en HTML, CSS et JavaScript simples. `supabase-js` v2, version figée, est chargé depuis jsDelivr. Il n'y a pas d'étape de build.
- **Déploiement :** GitHub Actions publie `web/` sur GitHub Pages à chaque push sur `main` qui touche `web/`.

## Schéma

| Table | Clé | Colonnes |
|---|---|---|
| `projects` | `project_id` | title, programme, year, countries `text[]`, policy_area, results, detail_page, url, summary, deliverables `int`, csrs `text[]` |
| `csrs` | `csr_id` | country, year, number, title, text, policy_areas `text[]`, assessment, assessment_year, source_url |
| `csr_parts` | `part_id` | csr_id → csrs, country, year, text, policy_areas `text[]`, assessment |
| `project_csr_links` | (project_id, part_id) | project_id → projects, part_id → csr_parts, level, rationale, basis |
| `anomalies` | `id` (identity) | project_id, title, programme, year, countries, type, severity, detail, url |
| `testers` | `email` | e-mails autorisés, en minuscules |

Les clés étrangères permettent les jointures imbriquées de PostgREST, par exemple les liens d'un projet accompagnés du texte de la sous-partie et de la CSR.

## Sécurité

- **Lecture des données.** Le RLS est activé sur toutes les tables. Une seule règle `SELECT` par table de données : `using (public.is_tester())`.
  - `is_tester()` est une fonction `security definer stable`. Elle renvoie vrai si l'utilisateur courant (`auth.uid()`) a, dans `auth.users`, un e-mail **confirmé** (`email_confirmed_at` renseigné) qui, mis en minuscules, figure dans `testers`. Le droit `execute` n'est accordé qu'à `authenticated` (retiré à `public` et à `anon`).
  - `anon` n'a pas le droit `select` sur les tables de données : un appel anonyme renvoie une erreur de permission (ou `[]`).
  - `testers` n'a aucune règle : elle est illisible pour `anon` comme pour `authenticated`.
- **Aucune écriture depuis la page.** Ni `anon` ni `authenticated` n'ont de règle d'insertion, de modification ou de suppression. Seule la clé `service_role`, qui contourne le RLS, écrit.
- **Clé `anon`.** Elle est publique, dans `web/config.js`. Sans session de testeur, elle ne donne accès à rien.
- **Clé `service_role`.** Elle est lue depuis l'environnement ou `.env` (`SUPABASE_URL`, `SUPABASE_SERVICE_KEY`), jamais écrite dans un fichier suivi par Git, et `.env` est déjà dans `.gitignore`.
- **Connexion.** Par lien magique (`signInWithOtp` avec `shouldCreateUser: false`) : l'inscription se fait **sur invitation seulement** (*Authentication > Users > Invite user*, « Allow new users to sign up » désactivé, « Confirm email » actif). Un e-mail non invité reçoit « This e-mail is not invited ». Un utilisateur invité mais absent de `testers` peut se connecter, mais ne voit aucune donnée : la page affiche alors « Accès non autorisé », d'après l'appel RPC `is_tester()`.
- **Configuration côté utilisateur.** Dans *Auth > URL Configuration*, l'utilisateur met l'URL GitHub Pages (`https://mge-agilos.github.io/dg-reform-tsi/`) comme *Site URL*, et `https://mge-agilos.github.io/dg-reform-tsi/**` (plus `http://localhost:8000/**` pour les essais) comme *Redirect URLs*.

## Synchronisation (`supabase-sync`)

- **Lignes construites, sans réseau** (`build_rows(output_dir)`) :
  - **projets :** le front matter de `output/projects/*.md`. Le résumé est la section « Summary (AI-generated) », ou à défaut l'introduction, coupée à 1 500 caractères. L'URL est la première de `source_urls`.
  - **CSR et sous-parties :** `csr.build_csrs(read_rows(output/csr/csr_full.xlsx))`.
  - **liens :** les enregistrements `output/csr_links/*.json` de statut `ok`. On ne garde que ceux dont le projet et la sous-partie existent.
  - **anomalies :** `anomalies.detect(...)`, comme la commande `anomalies`.
- **Envoi** (`push(rows, url, key, client)`), par l'API REST de PostgREST :
  1. supprimer les lignes dans l'ordre des dépendances (liens, sous-parties, CSR, projets, anomalies), avec un filtre qui couvre toutes les lignes ;
  2. insérer par lots de 500, dans l'ordre inverse.
  - En-têtes : `apikey` et `Authorization: Bearer <service_role>`. `testers` n'est jamais touchée.
- **Robustesse :**
  - une erreur HTTP arrête la commande avec un code 1 et la réponse de PostgREST ;
  - les tables peuvent rester partiellement vides, mais il suffit de relancer la commande ;
  - avant toute suppression, une sauvegarde JSON des lignes envoyées est écrite dans `output/state/supabase_rows.json`.
- **Volume :** environ 2 200 projets, 900 CSR, 3 200 sous-parties, 1 300 liens et 1 900 anomalies, soit environ 10 000 lignes et quelques Mo. C'est compatible avec l'offre gratuite.

## Page

Une page unique avec trois onglets. L'état est conservé dans l'URL (`#projects?...`), ce qui rend les liens partageables.

1. **Projets**
   - Recherche dans les titres (`ilike`).
   - Filtres : pays, programme, année de début et de fin, domaine, « liée à une CSR », « page détail ».
   - Liste paginée par 50, avec le total.
   - Fiche projet : titre, programme, année, pays, domaine, résultat, nombre de livrables, lien vers le site, résumé (marqué « généré par IA »). Elle liste ensuite les CSR soutenues, avec pour chacune : sous-partie, niveau, justification, texte de la sous-partie, évaluation, et lien vers la fiche CSR.
2. **CSR**
   - Filtres : pays, année, texte.
   - Fiche CSR : texte, domaines, dernière évaluation et son année, puis les sous-parties. Pour chaque sous-partie, les **projets liés**, avec leur niveau et leur justification ; c'est le lignage.
3. **Anomalies**
   - Filtres : type, gravité, programme, année, pays.
   - Liste paginée.
   - Export CSV de la sélection, jusqu'à 5 000 lignes, lues par pages de 1 000 (limite *max-rows* de PostgREST) ; séparateur `;` (Excel en paramètres belges ou français), UTF-8 avec BOM, cellules commençant par `= + - @` neutralisées par une apostrophe.

**Interface :**
- en-tête avec l'état de connexion ;
- mention « Liens et résumés générés par IA, à vérifier » ;
- largeur mobile prise en charge ;
- thème clair et thème sombre.

## Tests

- **Python**, avec pytest, sans réseau :
  - `build_rows` sur un dossier `output/` factice ;
  - `push` avec `httpx.MockTransport`, pour vérifier l'ordre des suppressions et insertions, les lots, les en-têtes et l'erreur HTTP ;
  - lecture de la clé depuis l'environnement ou `.env`, avec une erreur claire si elle manque.
- **SQL :** le script est relu, puis exécuté par l'utilisateur. On le vérifie par un appel REST anonyme, qui ne doit renvoyer aucune ligne, puis par un appel avec une session de testeur.
- **Page :** vérification manuelle par l'utilisateur après déploiement, avec une liste de contrôle dans le README.

## Hors lot 1

- le chat (lot 2) ;
- l'écriture depuis la page, par exemple des verdicts des métiers sur les liens ;
- la recherche plein texte dans les résumés (`tsvector`).
