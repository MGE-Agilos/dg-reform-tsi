# Lignage CSR → projets en réseau biparti — design et plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Objectif :** un onglet **Lineage** dans l'explorateur web, avec un réseau biparti par pays. Les CSR sont à gauche, groupées par année ; les projets à droite. Les liens projet → CSR évalués par IA relient les deux colonnes.

**Design validé le 2026-10-16.** Seule la page `web/` change : les données existent déjà dans Supabase (`csrs`, `csr_parts`, `project_csr_links`, `projects`, `impact_reports`).

**Route et commandes.**
- Route : `#lineage?country=XX&from=YYYY&to=YYYY&unlinked=1`.
- Pays : obligatoire. Sans pays, la page propose le choix ; par défaut, le pays qui a le plus de liens.
- Années : plage des CSR. Par défaut, 2019 à 2025.
- `unlinked=1` : affiche les CSR sans projet (grisées) ; par défaut, elles sont masquées.

**Accès à l'onglet.** Il est placé après « CSRs » et on y arrive aussi :
- depuis la barre « CSR coverage by country » du Dashboard, qui pointe désormais vers `#lineage?country=XX` au lieu de `#csrs?country=XX` ;
- depuis la fiche CSR (« Show in lineage network ») ;
- depuis la fiche projet (« Show lineage »).

Dans les deux derniers cas, l'élément d'origine est sélectionné à l'ouverture.

**Données, deux requêtes par pays.**
1. Requête principale :
   ```
   sb.from("csrs").select("csr_id,year,number,title,assessment,assessment_year,csr_parts(part_id,text,project_csr_links(level,rationale,basis,projects(project_id,title,programme,year,results,url)))").eq("country",XX).gte("year",from).lte("year",to)
   ```
   Elle s'appuie sur les clés étrangères existantes. En cas d'échec de l'imbrication, faire trois requêtes simples à la place.
2. Requête des scores : `impact_reports` filtrés sur les `project_id` trouvés (`in.(...)`), triés par `created_at` décroissant, pour ne garder que le dernier score de chaque projet.

**Agrégation.** Une arête relie une CSR et un projet ; plusieurs sous-parties peuvent être derrière.
- Niveau de l'arête : `direct` si au moins une sous-partie est directe.
- L'infobulle de l'arête liste les sous-parties, avec leur niveau et leur justification.

**Mise en page (SVG fait main, sans bibliothèque).**
- **Colonne gauche : les CSR.** Groupées par année, avec un intertitre d'année, et triées par numéro.
  - Nœud : rectangle à coins arrondis. Libellé « HR 2022 · CSR 3 », plus l'évaluation abrégée.
  - Couleur de remplissage : celle de l'évaluation, sur l'échelle ordinale déjà validée du Dashboard.
- **Colonne droite : les projets.** Triés pour réduire les croisements, par ordre barycentrique : la moyenne des positions des CSR auxquelles ils sont liés. Une ou deux passes suffisent.
  - Nœud : titre tronqué, plus « TSI 2023 ».
  - Couleur : celle du programme (palette catégorielle validée).
  - Si le projet a un score d'impact, une pastille « 6/10 » est ajoutée.
- **Arêtes :** courbes de Bézier cubiques.
  - Trait plein pour `direct`, pointillé pour `partial`. Le type de trait double la couleur, qui n'est donc jamais seule à porter l'information.
  - Épaisseur 1,5 à 2 px. Couleur discrète au repos, couleur d'accent quand l'arête est mise en évidence.
- **Hauteur :** proportionnelle au nombre de nœuds de la plus longue colonne (environ 34 px par nœud, plus les intertitres d'année). La vue défile verticalement dans la page.
- **Largeur :** celle du conteneur.
  - En dessous de 600 px, les colonnes rétrécissent, les libellés sont tronqués avec « … » et les marges deviennent minimales. Il n'y a jamais de défilement horizontal de la page.
  - Si les libellés deviennent illisibles, afficher plutôt la vue liste (vue table).

**Interaction (skill dataviz, `references/interaction.md`).**
- **Survol ou focus clavier d'un nœud** :
  - ses arêtes et ses voisins s'éclairent, et le reste passe à 15 % d'opacité ;
  - l'infobulle donne :
    - pour une CSR : le texte (tronqué), l'évaluation et le nombre de projets ;
    - pour un projet : le programme, l'année, le résultat, le score d'impact et le nombre de CSR.
- **Clic sur un nœud** : la mise en évidence est épinglée. Un second clic ou la touche Échap la retire. Le panneau sous le graphique montre le détail, avec des liens vers la fiche CSR ou la fiche projet.
- **Clavier :** les nœuds sont focusables (`tabindex=0`, `role="button"`, `aria-label`). Entrée vaut un clic.
- **Survol d'une arête :** infobulle avec les sous-parties, leur niveau et leur justification.

**Autres éléments de la page.**
- **Légende :** le trait plein et le pointillé ; l'échelle des évaluations ; les programmes ; la pastille du score.
- **Résumé en tête :** « N CSRs (M with at least one project) · P projects · L links (D direct) ».
- **Vue table** (`<details>`) : une ligne par lien (CSR, sous-partie, projet, niveau, justification), exportable en CSV, en réutilisant l'export CSV existant (`;` et BOM).
- **Mention :** « Links assessed by AI from the project text: verify before use. »
- **États vides :**
  - aucun lien pour le pays ou la période : un message, plus un lien vers les projets du pays ;
  - pays inconnu : retour au sélecteur.

**Règles inchangées.**
- Tout texte venant de la base passe par `esc()`. L'infobulle s'affiche via `textContent`.
- Les URL des liens externes sont filtrées par `safeUrl`.
- La garde de séquence s'applique. Le cache est conservé par pays et par période et vidé au changement d'utilisateur.

**Couleurs.** On réutilise les tokens `--viz-*` déjà validés. Si une nouvelle couleur est ajoutée (accent des arêtes), la passer au validateur de la skill dataviz (`scripts/validate_palette.js`), en mode clair et sombre.

**Vérifications.**
1. `node --check web/app.js`.
2. Test de rendu Node avec des données factices :
   - 1, 15 et 60 CSR, et 1, 40 et 120 projets ;
   - libellés très longs ;
   - un projet lié à 6 CSR.

   Contrôler l'absence de `NaN` et de `undefined`, la hauteur, la troncature, l'ordre barycentrique et l'échappement.
3. Capture d'écran dans un navigateur sans interface, avec un client Supabase factice, à 1 200 px et 390 px, en thème clair et sombre.

**Commit :** `feat(web): onglet Lineage (reseau biparti CSR -> projets par pays)`.
