# Mise à jour incrémentale par le sitemap et GitHub Actions mensuel — design et plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Objectif.**
1. Ne plus retélécharger les 835 pages détail à chaque mise à jour. On se sert de la date `<lastmod>` que le sitemap donne pour chaque page (vérifié le 2026-10-16 : 4 999 dates pour 5 000 URL).
2. Lancer la chaîne complète chaque mois dans GitHub Actions :
   - l'état est restauré depuis la dernière release de données ;
   - le LLM passe par le CLI Claude avec la clé API ;
   - à la fin, `supabase-sync` et une nouvelle release.

**Design validé le 2026-10-16.**

**Mode incrémental.**
- On enregistre dans l'état la date `lastmod` du sitemap pour chaque page détail traitée. L'état des projets est `output/state/index.json`, via `StoreEntry` ; on ajoute par exemple `sitemap_lastmod: str | None`.
- Lors d'un `run` normal, une page détail **connue** dont la date du sitemap est égale ou antérieure à la date enregistrée est considérée comme **inchangée sans être téléchargée**. On la consigne dans le journal sous la forme `classify`, `unchanged_sitemap`.
- On télécharge comme aujourd'hui :
  - les pages nouvelles ;
  - les pages dont la date est plus récente ;
  - les pages sans date, dans le sitemap ou dans l'état ;
  - toutes les pages, avec `--full`.
- Après le traitement d'une page téléchargée, qu'elle ait changé ou non, on enregistre sa date du sitemap.
- **Première exécution, quand l'état n'a encore aucune date.** Deux options sont acceptables :
  - (a) tout télécharger une fois ;
  - (b) une commande ou option `--seed-sitemap-lastmod` qui enregistre les dates actuelles **sans télécharger**, ce qui suppose que le corpus est à jour (il l'est : extraction du 12-13/10).

  Implémenter (b), en option explicite, et la lancer une fois dans le workflow, si l'état n'a aucune date.
- **Ce qui ne change pas :**
  - les pages de listes (domaines, fiches pays, PDF SRSP) sont toujours lues : elles donnent les projets « titre seul » et le rattachement aux pays ;
  - la découverte par sitemap des pages hors listes ;
  - les livrables (`recheck_days`) ;
  - les CSR ;
  - la liaison CSR (cache).
- Lire le code avant de modifier : `parsers/sitemap.py`, `components/discoverer.py` (étape sitemap), `components/change_detector.py`, `components/detail_fetcher.py`, `store.py` et `runner.py`. L'endroit le plus naturel est probablement :
  - le discoverer garde la date `lastmod` sur chaque `ProjectRef` (champ nouveau, par exemple `sitemap_lastmod`) ;
  - le ChangeDetector ou le DetailPageFetcher décide de sauter le téléchargement ;
  - le writer enregistre la date.
- **Tests à prévoir :**
  - analyse du `<lastmod>` ;
  - page connue, date égale : pas de téléchargement ;
  - date plus récente : téléchargement ;
  - pas de date : téléchargement ;
  - `--full` : téléchargement ;
  - initialisation des dates ;
  - le résumé du run compte `skipped_by_sitemap`.

### Task 1 : mode incrémental par le sitemap

TDD, en suivant le design ci-dessus.

Commit : `feat(run): mode incremental par la date lastmod du sitemap (pages detail inchangees non retelechargees)`.

### Task 2 : workflow GitHub Actions `update-data.yml`

**Fichiers :** `.github/workflows/update-data.yml`, `.github/scripts/ci_summary.py`, et le README du dossier updater, auquel on ajoute une section « Automatic monthly update » en anglais.

**Déclencheurs.**
- `schedule: cron "0 3 1 * *"`, le 1er de chaque mois à 03:00 UTC.
- `workflow_dispatch`, avec les entrées :
  - `full` (booléen, par défaut false) ;
  - `detail_only` (booléen, par défaut true) ;
  - `skip_deliverables` (booléen, par défaut false) ;
  - `publish_release` (booléen, par défaut true) ;
  - `sync_supabase` (booléen, par défaut true).
- Les exécutions planifiées font une vérification complète (`--full`) en janvier, avril, juillet et octobre, et un `run` incrémental les autres mois. Le mois est calculé dans un step.

**Paramètres du job.**
- `permissions: contents: write`, pour la release.
- `concurrency: update-data`.
- `timeout-minutes: 340`.
- `runs-on: ubuntu-latest`.

**Étapes.**
1. checkout ;
2. `setup-python` 3.12 avec cache pip ;
3. `pip install .` : dépendances de base seulement ;
4. `setup-node` 20 et `npm install -g @anthropic-ai/claude-code` ;
5. vérification que `claude --version` fonctionne et que le CLI s'authentifie avec `ANTHROPIC_API_KEY`. Pour contrôler la clé avant de lancer les longues étapes, faire un ping peu coûteux, par exemple :

   ```
   echo 'Reply with the single word OK.' | claude -p --model haiku --output-format json
   ```

   Il échoue proprement si la clé est absente ou fausse.
6. **Restauration de l'état :**
   - `gh release download` de la dernière release `data-*`, avec `GH_TOKEN: ${{ github.token }}` ;
   - `tsi-output-state.zip` est décompressé dans `output/` ;
   - si aucune release n'existe, le job échoue avec un message clair.
7. Si l'état n'a encore aucune date de sitemap : lancer l'initialisation (option b de la Task 1).
8. `python -m tsi_agent run`, avec `--full` selon le mois ou l'entrée.
   - **Code 1 (quota)** : on continue, on avertit, et on note l'erreur pour la fin.
   - **Code 2 (configuration)** : échec immédiat.
9. `deliverables`, sauf si l'entrée `skip_deliverables` est vraie ; même règle de codes de sortie.
10. `csr-fetch` puis `csr-build` ;
11. `csr-link --detail-only` (si `detail_only`) ;
12. `anomalies` ;
13. `supabase-sync`, si `sync_supabase` est vrai. Il utilise `SUPABASE_URL` et `SUPABASE_SERVICE_KEY` depuis les secrets, passés en variables d'environnement, jamais affichés.
14. `prod-export` ;
15. `release-data --publish`, si `publish_release` est vrai, avec le tag `data-YYYY-MM-DD`. Si ce tag existe déjà (relance le même jour), ajouter `-HHMM`. `release.py` accepte déjà ce format (`^[A-Za-z0-9._-]+$`).
16. **Résumé :** `python .github/scripts/ci_summary.py >> $GITHUB_STEP_SUMMARY`. Il lit les fichiers `logs/runs/*.summary.json` de cette exécution et le manifeste, puis écrit un tableau Markdown :
    - par étape : projets nouveaux ou mis à jour, pages sautées grâce au sitemap, publications, CSR, liens, coût LLM et durée ;
    - le nombre de fichiers et de morceaux de la release ;
    - le lien vers la release.

    Ce step s'exécute aussi en cas d'échec (`if: always()`).
17. **Fin :** si l'une des étapes a renvoyé un quota (code 1), le job se termine en échec après la publication, pour que GitHub envoie un e-mail. On n'interrompt pas les étapes suivantes.
18. **Artefact :** `actions/upload-artifact` de `logs/runs/`, conservé 30 jours, pour le diagnostic.

**Secrets du dépôt**, lus par `secrets.*` :
- `ANTHROPIC_API_KEY` ;
- `SUPABASE_URL` ;
- `SUPABASE_SERVICE_KEY`.

**Environnement d'exécution.**
- `CLAUDE_CODE` et le CLI s'exécutent sans terminal interactif. Vérifier que `ClaudeCliChatGenerator` (`--setting-sources ""`, `--strict-mcp-config`…) fonctionne en mode clé API. Il n'y a pas de connexion par abonnement dans les runners.
- Garder `config/settings.yaml` tel quel (`llm_provider: claude_cli`, `model: sonnet`).

**Tests.**
- Un test pytest de `ci_summary.py` sur des fichiers de résumé factices.
- Une validation syntaxique du YAML (`yaml.safe_load`).
- Le workflow lui-même ne peut pas être exécuté ici : sa mise en service est faite par le contrôleur.

**Commit :** `ci: mise a jour mensuelle des donnees par GitHub Actions (incrementale, verification complete trimestrielle)`.

### Task 3 : mise en service (contrôleur)

1. Déclarer les secrets avec `gh secret set`, depuis `.env`, sans les afficher.
2. Pousser.
3. Lancer une première exécution manuelle (`workflow_dispatch`, `publish_release` = true) et la suivre jusqu'au bout.
4. Vérifier le résumé, la release et le Dashboard.
