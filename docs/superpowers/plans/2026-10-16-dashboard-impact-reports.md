# Dashboard et conservation des analyses d'impact — design et plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Objectif :**
- ajouter à l'explorateur web (https://mge-agilos.github.io/dg-reform-tsi/) un onglet **Dashboard** : métadonnées des projets, statistiques des scores et de la confiance des analyses d'impact, CSR et lignage, qualité des données ;
- **conserver** les analyses d'impact, qui aujourd'hui s'affichent puis se perdent, et montrer la dernière analyse dans la fiche du projet.

**Design validé le 2026-10-16 :**
- **Table `impact_reports`** : une ligne par analyse. Elle est alimentée de deux façons :
  - par la fonction `tsi-assistant`, à chaque analyse lancée depuis la page (`source = 'web'`) ;
  - par `supabase-sync`, à partir des rapports locaux `output/impact/*.md` (`source = 'local'`).
- **Fonction SQL `tsi_dashboard()`** (jsonb, security invoker, donc soumise au RLS des testeurs) : elle renvoie tous les agrégats en un appel.
- **Onglet Dashboard** en SVG fait main, sans bibliothèque, selon la skill `dataviz` :
  - méthode : `C:\Users\MGE\AppData\Local\Temp\claude\bundled-skills\2.1.300\738e74fca38b3a39e86e399630e83ac8\dataviz\SKILL.md`, à lire avant d'écrire le moindre graphique ;
  - références : `references/choosing-a-form.md`, `color-formula.md`, `marks-and-anatomy.md`, `interaction.md`, `components.md`, `anti-patterns.md`, `palette.md` ;
  - validateur : `scripts/validate_palette.js`.
- **Clic** sur une barre : ouvre la liste filtrée correspondante (`#projects?country=HR`, `#anomalies?type=...`, `#csrs?country=...`).

**Conventions :**
- branche `feature/dashboard`, créée depuis `main` ;
- commits en français, terminés par la ligne `Co-Authored-By: Claude Opus 5.5 (1M context) <noreply@anthropic.com>` ;
- aucun secret ;
- tests sans réseau.

---

### Task 1 : SQL `003_dashboard.sql`

**Fichier à créer :** `supabase/migrations/003_dashboard.sql`, idempotent, avec un en-tête qui rappelle que le projet Supabase est **partagé** : tout nouvel objet est préfixé (`tsi_`) ou porte un nom propre à l'explorateur.

- **Table `public.impact_reports`.** Colonnes :
  - `id bigint generated always as identity primary key` ;
  - `project_id text not null`, **sans clé étrangère** : `supabase-sync` vide et recharge `projects` à chaque fois, et une cascade effacerait les rapports ;
  - `title text` ;
  - `score int check (score between 0 and 10)` ;
  - `level text check (level in ('strong','moderate','weak'))` ;
  - `confidence text check (confidence in ('high','medium','low'))` ;
  - `verdict text` ;
  - `report text not null` ;
  - `model text` ;
  - `source text not null check (source in ('web','local'))` ;
  - `created_by uuid` ;
  - `created_at timestamptz not null default now()` ;
  - index sur `(project_id, created_at desc)`.
- **Sécurité de la table.**
  - RLS activé, avec une seule règle : `tester_read` en select pour `authenticated`, `using ((select public.is_tester()))` ;
  - droits : `grant select ... to authenticated` ; `revoke insert, update, delete, truncate ... from anon, authenticated` ; tout pour `service_role` ;
  - les écritures se font uniquement avec la clé `service_role`, depuis la fonction ou la synchronisation.
- **Fonction `public.tsi_dashboard() returns jsonb`.** Langage `sql`, `stable security invoker set search_path = public`. Elle renvoie un objet `jsonb_build_object(...)` avec :
  - `totals` : projets, projets avec page détail, total des livrables, CSR, sous-parties, liens, projets avec au moins un lien, analyses d'impact, anomalies ;
  - `projects_by_programme` : `[{key, n}]` ;
  - `projects_by_year_programme` : `[{year, programme, n}]` ;
  - `projects_by_country` : `[{key, n}]`, avec `unnest(countries)` et tri décroissant ;
  - `projects_by_policy_area` ;
  - `projects_by_results` : `achieved`, `expected`, et `none` pour les valeurs nulles ;
  - `csrs_by_year` ;
  - `csr_assessments` : la dernière évaluation de chaque CSR, `none` pour les valeurs nulles ;
  - `csr_coverage_by_country` : `[{country, csrs, csrs_with_project}]`, où une CSR est couverte si l'une de ses sous-parties a au moins un lien ;
  - `links_by_level` ;
  - `impact_scores` : `[{score, n}]`, scores de 0 à 10 ;
  - `impact_by_level` ;
  - `impact_by_confidence` ;
  - `impact_mean_by_policy_area` et `impact_mean_by_country` : `[{key, mean, n}]`. Les deux se calculent par **jointure** avec `projects`, à partir de la **dernière** analyse de chaque projet (`distinct on (project_id) ... order by created_at desc`). Toutes les statistiques d'impact utilisent cette dernière analyse par projet ;
  - `impact_recent` : les 10 analyses les plus récentes, avec `project_id`, `title`, `score`, `level`, `confidence`, `created_at` et `source` ;
  - `anomalies_by_type_severity` : `[{type, severity, n}]`.
- **Droits sur la fonction :** `revoke all ... from public, anon` ; `grant execute ... to authenticated, service_role`.
- **Commit :** `feat(web): table impact_reports et fonction tsi_dashboard (agregats du tableau de bord)`.

### Task 2 : conservation des analyses

**Fichiers à modifier :**
- `src/tsi_agent/supabase_sync.py` ;
- `supabase/functions/tsi-assistant/index.ts` ;
- les tests correspondants.

**Côté `supabase-sync`.**
- Une nouvelle fonction `impact_rows(output_dir)` lit `output/impact/*.md` : front matter (`project_id`, `title`, `impact_score`, `impact_level`, `confidence`, `overall_verdict`, `model`, `generated`) et corps du rapport. Elle ignore les fichiers illisibles et les scores absents.
- `push` gère `impact_reports` **à part** des tables remplacées :
  - d'abord `DELETE impact_reports?source=eq.local` ;
  - puis un `POST` des lignes locales, avec `created_at` = `generated`.
- Les rapports `web` ne sont **jamais** supprimés.
- La sauvegarde JSON inclut ces lignes.

**Tests.**
- lecture d'un dossier d'impact factice ;
- `push` : la suppression ne vise que `source=eq.local` et se fait après les autres tables ; aucune suppression de `web`.

**Côté `tsi-assistant`.**
- Une nouvelle fonction exportée `parseImpact(answer)` lit la ligne finale `Impact score: X/10 (strong|moderate|weak) — Confidence: high|medium|low`.
  - Elle tolère `-` ou `–` à la place de `—`, les majuscules et les espaces.
  - Elle renvoie `{score, level, confidence}`, ou `null`.
- Après une analyse **impact** réussie, la fonction fait une insertion en « best effort » :
  - `POST ${SUPABASE_URL}/rest/v1/impact_reports` avec `apikey` et `Authorization: Bearer ${env.SUPABASE_SERVICE_ROLE_KEY}`, `Prefer: return=minimal` ;
  - corps : `project_id`, `title`, `score`, `level`, `confidence`, `verdict`, `report`, `model`, `source: "web"` et `created_by`.
  - **Identification du projet :** le message utilisateur a la forme `Project: <title> (id <project_id>; ...)`. On extrait `project_id` et le titre par une expression régulière. Sans `id`, on n'insère rien.
  - **`created_by` :** c'est le `sub` du JWT de l'utilisateur. On décode la partie payload du jeton, déjà vérifiée par la passerelle et par `is_tester`, sans revérifier la signature, et on l'utilise seulement comme information.
  - **`verdict` :** l'éventuelle ligne « Overall verdict », sinon `null`.
  - Délai de 10 s. Un échec n'est que journalisé, sans contenu : il ne modifie pas la réponse.
  - La réponse devient `{answer, saved: boolean}`.
- **Tests :**
  - `parseImpact`, avec des variantes de la ligne ;
  - insertion appelée avec la clé `service_role` et les bons champs ;
  - échec de l'insertion : la réponse reste 200 avec `saved: false` ;
  - chat : aucune insertion ;
  - message sans `id` : aucune insertion.
- **Commits :**
  - `feat(web): analyses d'impact conservees (impact_reports) depuis la page et les rapports locaux` ;
  - un commit séparé par fichier si c'est plus clair.

### Task 3 : onglet Dashboard et dernière analyse dans la fiche projet

**Fichiers à modifier :** `web/index.html`, `web/app.js`, `web/style.css`.

- **Onglet `#dashboard`.**
  - Il est placé en premier dans la barre d'onglets, mais la route par défaut reste `#projects`.
  - Il appelle `sb.rpc("tsi_dashboard")` une seule fois, avec un cache en mémoire et un bouton « Refresh ».
- **Rangée de tuiles** (stat tiles) :
  - projets, avec la part ayant une page détail ;
  - livrables ;
  - CSR (2016–2025) ;
  - liens projet → CSR, avec la part de projets liés ;
  - analyses d'impact, avec le score moyen ;
  - anomalies, avec le nombre de gravité haute.
- **Sections et graphiques.** La forme se choisit d'après `references/choosing-a-form.md`, par défaut des barres horizontales triées pour les catégories :
  1. **Projects** :
     - par année, en barres verticales **empilées** par programme (TSI, SRSP, SRSS ; couleurs catégorielles dans un ordre fixe, légende) ;
     - par pays, en barres horizontales triées ;
     - par domaine ;
     - par résultat.
  2. **Impact analyses** :
     - distribution des scores de 0 à 10, en barres verticales, couleur séquentielle ou unique ;
     - par niveau et par confiance, en petites barres ou tuiles ;
     - score moyen par domaine et par pays, avec `n` affiché ; masquer ou griser quand `n < 2` ;
     - tableau des 10 dernières analyses, avec un lien vers le projet.
     - S'il y a moins de 5 analyses, afficher en tête une note : « Few analyses so far: statistics are indicative ».
  3. **CSRs & lineage** :
     - CSR par année ;
     - évaluations, sur une échelle ordonnée de « Full implementation » à « No progress », plus « Not assessed » et « none » en gris ;
     - couverture par pays (pourcentage de CSR soutenues par au moins un projet), en barres triées ;
     - liens directs et partiels.
  4. **Data quality** : anomalies par type, empilées par gravité. Les couleurs de gravité sont celles déjà utilisées pour les badges du statut, toujours accompagnées du libellé.
- **Exigences de la skill dataviz :**
  - **Couleurs :** tokens CSS sur `:root`, avec un thème sombre propre. Valider la palette catégorielle utilisée (programmes) et celle des évaluations avec `node <skill>/scripts/validate_palette.js "<hex,...>" --mode light`, puis `--mode dark`, en donnant la surface. Corriger tout FAIL, puis noter le résultat dans le compte rendu.
  - **Lisibilité :** marques fines, extrémités arrondies, 2 px d'espace entre les barres, axes et grilles discrets, étiquettes directes choisies avec parcimonie, aucun texte de la couleur de la série.
  - **Interaction :** une infobulle au survol de chaque barre (et au focus clavier), avec la catégorie, la valeur et la part.
  - **Accessibilité :** une vue « Table » pour chaque graphique (`<details>` avec le tableau des données) et une légende dès qu'il y a au moins 2 séries.
  - **Mise en page :** largeur mobile, sans défilement horizontal de la page ; le SVG se redimensionne avec `viewBox` et `width: 100%`.
- **Clic sur les barres :** pays vers `#projects?country=XX`, programme vers `#projects?programme=...`, domaine vers `#projects?area=...`, année vers `#projects?from=Y&to=Y`, type d'anomalie vers `#anomalies?type=...`, pays de la couverture CSR vers `#csrs?country=XX`. Ces clics réutilisent les filtres existants.
- **Fiche projet :**
  - charger `impact_reports` filtrés sur `project_id`, du plus récent au plus ancien, 1 ligne (avec les colonnes `score`, `level`, `confidence`, `created_at` et `source`, puis `report` à la demande) ;
  - afficher un encadré « Latest impact analysis: X/10 (level, confidence) — date » avec un bouton « Show report », qui rend le Markdown par la fonction `md()` existante ;
  - dans l'onglet Impact, afficher pour le projet sélectionné « Previous analyses » (au plus 5) avant le bouton « Run ».
- **Règles conservées :** `esc()` partout, `safeUrl`, garde de séquence et gestion du focus.
- **Vérifications :**
  - `node --check web/app.js` ;
  - rendu réel : servir `web/` sur le port 8012 avec des données factices injectées et faire une capture si un outil le permet. Sinon, tester la fonction de rendu SVG dans Node avec un DOM factice (chevauchements de libellés, dépassements).
- **Commit :** `feat(web): onglet Dashboard (metadonnees, impact, CSR, qualite) et derniere analyse d'impact dans la fiche projet`.

### Task 4 : mise en service (contrôleur)

1. Appliquer `003_dashboard.sql` par l'API de gestion.
2. Redéployer `tsi-assistant`.
3. Lancer `supabase-sync`, qui charge aussi les 5 rapports locaux.
4. Vérifier :
   - `rpc/tsi_dashboard` avec la clé `service_role` ;
   - l'accès anonyme refusé ;
   - les droits d'insertion sur `impact_reports` (refusés pour un utilisateur testeur ou anonyme).
5. Fusionner, pousser et vérifier le déploiement Pages.
