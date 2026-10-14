# Kit de déploiement GitHub (PROD Haystack, mise à jour des données, front end local) — design et plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Objectif :** un collègue qui a accès au dépôt `MGE-Agilos/dg-reform-tsi` doit pouvoir faire trois choses :
1. déployer ou mettre à jour la PROD Haystack Enterprise, avec les fichiers de données et les YAML ;
2. installer sur son PC la recherche de nouveaux documents (projets, publications, CSR, liens), avec un LLM compatible OpenAI (EC GPT ou OpenAI), puis importer le résultat à la main ou par la CLI ;
3. servir l'explorateur web sur un serveur local.

L'existant ne change pas : GitHub Pages, Supabase, le CLI Claude et le chatbot local.

**Design validé le 2026-10-14 :**
- **Données de production : GitHub Releases.** La commande `python -m tsi_agent release-data [--publish]` prépare `deploy/release/<tag>/` et, avec `--publish`, appelle `gh release create`. Le tag par défaut est `data-YYYY-MM-DD`. Les fichiers joints à la release sont :
  - `tsi-prod-files.zip` : les fichiers `tsi-*` de `deploy/prod/build/files`, avec leurs `.meta.json` ;
  - `manifest.json` ;
  - `tsi-output-state.zip` : le contenu de `output/` sauf `vectors/`, `reports/` et les fichiers temporaires.

  Les YAML restent versionnés dans `deploy/prod/`. `deploy/prod/GUIDE.md` décrit la procédure par l'interface.
- **LLM compatible OpenAI, en option.** On ajoute des réglages dans `config/settings.yaml` :
  - `llm_provider` : `claude_cli` (valeur par défaut, comportement actuel) ou `openai` ;
  - `openai_base_url` : par exemple `https://api.tech.ec.europa.eu/ecgpt/v1`, ou vide pour OpenAI ;
  - `openai_api_key_env` : `OPENAI_API_KEY` par défaut ;
  - `openai_model` : `gpt-5.1` par défaut.

  Les 4 étapes à LLM de la mise à jour (extraction des pages, résumés des livrables, arbitrage du sitemap, liaison aux CSR) passent par une fabrique unique. Le JSON est obtenu en `response_format: json_object`, avec le schéma placé dans le prompt système, puis validé par les mêmes modèles pydantic. Au démarrage, la CLI charge `.env` sans écraser les variables déjà définies.
- **Validation de la qualité : `python -m tsi_agent llm-check [--n 3]`.** La commande refait l'extraction de N projets déjà traités, avec le fournisseur configuré, à partir du cache HTTP. Elle écrit `output/reports/llm_check.md`, qui met côte à côte l'existant et le nouveau résultat. Rien n'est écrit dans `output/projects`.
- **`deploy/updater/`** :
  - `README.md` : installation sur un PC Windows et usage ;
  - `update.ps1` (PowerShell 5.1) : enchaîne `run`, `deliverables`, `csr-fetch`, `csr-build`, `csr-link`, `anomalies` puis `prod-export`, et s'arrête à la première erreur ;
  - `restore-state.ps1` : télécharge la dernière release de données avec `gh` et décompresse l'état dans `output/` ;
  - `settings.updater.yaml` : exemple en mode `openai` ;
  - `.env.example`.
- **`deploy/frontend-local/`** :
  - `docker-compose.yml`, avec nginx servant `web/` et `config.local.js` monté à la place de `config.js` ;
  - `serve.ps1` : alternative sans Docker, avec le serveur HTTP de Python sur une copie de `web/` et la configuration locale ;
  - `config.local.example.js` ;
  - `README.md`, qui rappelle d'ajouter l'URL locale aux *Redirect URLs* de Supabase et de choisir `tsi-assistant` ou `haystack-proxy`.
- **README racine** : une section « Démarrer » avec les trois parcours et les liens vers chaque guide.

**Conventions :**
- branche `feature/deploy-kit`, créée depuis `main` ;
- commits en français, terminés par `Co-Authored-By: Claude Opus 5.5 (1M context) <noreply@anthropic.com>` ;
- aucun secret dans le dépôt ;
- tests pytest dans `tmp_path`, sans réseau et sans vrai LLM ;
- ne jamais toucher `output/` dans les tests ;
- scripts PowerShell compatibles avec Windows PowerShell 5.1 : pas de `&&` ni de `??`, et `$?` pour tester le succès.

---

### Task 1 : fabrique de générateurs et fournisseur OpenAI

**Fichiers :**
- Create : `src/tsi_agent/llm.py`
- Modify : `src/tsi_agent/config.py`, `src/tsi_agent/pipeline.py` (`default_chat_generator`, et le juge passé à `build_pipeline`), `src/tsi_agent/deliverables_pipeline.py` (`default_deliverables_generator`), `src/tsi_agent/components/merge_judge.py` (générateur par défaut inchangé, mais `build_pipeline` / `run_extraction` lui passe celui de la fabrique), `src/tsi_agent/__main__.py` (csr-link et chargement de `.env`), `src/tsi_agent/runner.py` si c'est lui qui construit les générateurs.
- Test : `tests/test_llm.py`

**Contenu :**
- **`Settings`** : ajouter les champs `llm_provider: str = "claude_cli"`, `openai_base_url: str | None = None`, `openai_api_key_env: str = "OPENAI_API_KEY"`, `openai_model: str = "gpt-5.1"` et `openai_timeout_s: int = 300`.
- **`OpenAIJsonChatGenerator`** : composant Haystack, sérialisable, avec la même interface `run(messages) -> {"replies": [ChatMessage]}`.
  - Il enveloppe `haystack.components.generators.chat.OpenAIChatGenerator`, avec `api_base_url`, `api_key=Secret.from_env_var(...)`, `model`, `timeout` et `generation_kwargs={"response_format": {"type": "json_object"}}`.
  - Il ajoute au message système la phrase « Answer with a single JSON object that conforms to this JSON schema: <schema> ».
  - Il recopie dans la réponse `meta.model` (sous la forme `openai/<model>`) et `meta.usage` (`prompt_tokens`, `completion_tokens`), pour que `LoggedChatGenerator` calcule les coûts avec `settings.pricing`.
  - Une erreur 429 ou de quota (le nom de l'exception contient `RateLimit`, ou statut 429) est convertie en `LLMQuotaExceeded`. Les autres erreurs deviennent des `LLMCallError`.
- **`make_json_generator(settings, schema, *, timeout_s, model=None)`** : renvoie un `ClaudeCliChatGenerator(model=model or settings.model, json_schema=schema, timeout_s=timeout_s)` quand le fournisseur est `claude_cli`, et un `OpenAIJsonChatGenerator(model=settings.openai_model, ...)` quand il vaut `openai`. Un fournisseur inconnu lève une `ValueError`. Le paramètre `model` d'origine Claude (`sonnet`, `opus`) est ignoré en mode `openai`.
- **`load_dotenv(path=Path(".env"))`** : lit `KEY=VALUE`, ignore les commentaires, retire les guillemets et n'écrase jamais une variable déjà présente dans `os.environ`. On peut réutiliser `supabase_sync._dotenv`, en le déplaçant ou en l'important. La fonction est appelée au début de `main()`.
- **Branchement** : les 4 étapes utilisent `make_json_generator`.
  - Extraction : schéma `ProjectContent.model_json_schema()`, délai `settings.llm_timeout_s`.
  - Livrables : schéma `DeliverableContent`, délai `deliverables_llm_timeout_s`.
  - Arbitrage du sitemap : `JUDGE_SCHEMA`, 300 s.
  - csr-link : `LINK_SCHEMA`, délai `llm_timeout_s`.

  Le comportement par défaut ne change pas : on le vérifie avec les tests existants.

**Tests :** aucun réseau. On remplace `OpenAIChatGenerator.run` par un faux, avec `monkeypatch`. Cas à couvrir :
- en mode `claude_cli`, la fabrique renvoie un `ClaudeCliChatGenerator` avec le schéma, et son modèle vaut `settings.model` ;
- en mode `openai`, elle renvoie un `OpenAIJsonChatGenerator` avec la bonne URL de base, le bon modèle et la bonne variable de clé ;
- un fournisseur inconnu lève une `ValueError` ;
- le système contient le schéma, `response_format` vaut `json_object`, et la réponse JSON est rendue telle quelle avec `meta.model` égal à `openai/gpt-5.1` et l'usage ;
- une erreur de type RateLimit devient `LLMQuotaExceeded` ;
- le générateur passe un aller-retour `to_dict`/`from_dict` ;
- `load_dotenv` n'écrase pas une variable existante.

**Commit :** `feat(llm): fournisseur compatible OpenAI (EC GPT) pour les etapes de mise a jour, fabrique unique`

### Task 2 : `llm-check`

**Fichiers :**
- Create : `src/tsi_agent/llm_check.py`
- Modify : `src/tsi_agent/__main__.py`
- Test : `tests/test_llm_check.py`

**Comportement :**
- Choix des projets : N projets avec page détail, dans un ordre déterministe (graine fixe), parmi ceux dont la page est **dans le cache HTTP** (`PoliteFetcher.cached(url)`), pour n'appeler aucun réseau.
- Pour chacun, refaire l'extraction comme le fait `ProjectExtractor`. Lire le composant pour réutiliser la construction du prompt et la validation `ProjectContent`, avec le générateur de `make_json_generator`.
- Écrire `output/reports/llm_check.md`. Pour chaque projet, le rapport donne :
  - le titre et l'URL ;
  - un tableau des champs comparés (résultats, nombre de sections, pays si extraits, longueur du résumé) ;
  - le résumé existant et le nouveau, l'un sous l'autre.

  Le coût est ajouté s'il est disponible. Rien n'est écrit dans `output/projects`.
- Sans page en cache : message clair et code de sortie 1.

**Tests :** un faux générateur, un cache simulé et un dossier `output/` factice dans `tmp_path`.

**Commit :** `feat(llm): commande llm-check (comparaison de l'extraction avec le fournisseur configure)`

### Task 3 : `release-data`

**Fichiers :**
- Create : `src/tsi_agent/release.py`
- Modify : `src/tsi_agent/__main__.py`
- Test : `tests/test_release.py`

**Comportement :**
- `build_release(output_dir, prod_build_dir, out_root, tag)` :
  - vérifie que `prod_build_dir/manifest.json` existe, que `meta_type_problems` est vide et que le nombre de fichiers `tsi-*.md` correspond à `manifest.files` ; sinon, lève une `ReleaseError` avec un message clair ;
  - crée `out_root/<tag>/` avec `tsi-prod-files.zip` (les fichiers `tsi-*` de `files/`, à plat), `manifest.json` et `tsi-output-state.zip` (le contenu de `output/` sauf `vectors/`, `reports/`, `*.tmp` et `*.part`, avec les chemins relatifs à `output/`) ;
  - écrit `NOTES.md` : date, chiffres du manifeste (fichiers et morceaux par type), procédure courte et lien vers `deploy/prod/GUIDE.md` ;
  - renvoie un résumé (chemins et tailles).
- `publish(tag, dir, repo=None)` : lance `gh release create <tag> <files...> --title "Données TSI <date>" --notes-file NOTES.md` par `subprocess`. Un tag déjà existant donne un message clair, sans écrasement.
- CLI : `python -m tsi_agent release-data [--tag data-YYYY-MM-DD] [--publish]`. `deploy/release/` est ajouté à `.gitignore`.

**Tests :**
- contenu des archives : noms, exclusions, chemins relatifs ;
- erreurs du manifeste ;
- `publish` avec un `subprocess.run` simulé : commande attendue, et erreur si le tag existe.

**Commit :** `feat(release): commande release-data (zips de prod et d'etat, publication GitHub)`

### Task 4 : `deploy/updater/` et `deploy/prod/GUIDE.md`

**Fichiers :**
- Create : `deploy/updater/README.md`, `deploy/updater/update.ps1`, `deploy/updater/restore-state.ps1`, `deploy/updater/settings.updater.yaml`, `deploy/updater/.env.example`
- Create : `deploy/prod/GUIDE.md`

**Contenu attendu :**
- **`README.md`**, en français, pour un utilisateur Windows non développeur, avec des étapes numérotées :
  1. prérequis : Python 3.12, Git, GitHub CLI (`gh auth login`) ;
  2. cloner le dépôt ;
  3. `python -m venv .venv`, puis `.venv\Scripts\pip install -e .` ;
  4. copier `settings.updater.yaml` vers `config/settings.yaml`, ou utiliser `--settings` ;
  5. copier `.env.example` vers `.env` et y mettre la clé EC GPT ou OpenAI ;
  6. `restore-state.ps1` pour récupérer l'état de la dernière release ;
  7. `llm-check` avant le premier gros lot ;
  8. `update.ps1` ;
  9. importer dans Haystack, soit par l'interface (voir `GUIDE.md`), soit par `deploy\prod\upload.ps1` ;
  10. facultatif : `release-data --publish`, si le collègue a les droits d'écriture sur le dépôt.

  Il ajoute une explication des coûts et des durées (politesse de 15 s entre deux requêtes, nombre de pages), un dépannage (quota, clé absente, timeout) et une liste de ce que fait chaque étape.
- **`settings.updater.yaml`** : copie de `config/settings.yaml` avec `llm_provider: openai`, `openai_base_url: https://api.tech.ec.europa.eu/ecgpt/v1` et `openai_model: gpt-5.1`, plus des commentaires.
- **`.env.example`** :
  - `OPENAI_API_KEY=` (clé EC GPT ou OpenAI) ;
  - `API_KEY=`, `API_URL=` et `DEFAULT_WORKSPACE_NAME=tsi-reform`, pour `upload.ps1` ;
  - un commentaire rappelant de ne jamais commiter ce fichier.
- **`update.ps1`** :
  - paramètres `-Settings` (par défaut `config\settings.yaml`), `-SkipDeliverables`, `-SkipCsr` et `-DetailOnly` (ce dernier est passé à `csr-link`) ;
  - lance chaque commande par `.venv\Scripts\python.exe -m tsi_agent ...` et s'arrête si `$LASTEXITCODE -ne 0`, sauf pour le code 1 de quota de `csr-link`, où il affiche « relancer plus tard » et passe à `prod-export` ;
  - affiche à la fin le chemin de `deploy/prod/build` et le manifeste.
- **`restore-state.ps1`** :
  - paramètre `-Tag` (par défaut, la dernière release) ;
  - commandes `gh release download <tag> -p tsi-output-state.zip -R MGE-Agilos/dg-reform-tsi -D <temp>` puis `Expand-Archive` vers `output\` ;
  - si `output\` existe et n'est pas vide, demande confirmation par un paramètre `-Force`, sans prompt interactif.
- **`deploy/prod/GUIDE.md`** : la procédure par l'interface, la plus utilisée lundi.
  1. Télécharger `tsi-prod-files.zip` et `manifest.json` depuis la release.
  2. Dans le workspace `tsi-reform`, *Files* : supprimer les fichiers `tsi-*`.
  3. Téléverser.
     - Attention, rappel du README existant : l'interface n'attache pas les `.meta.json`. Pour le chat complet (`list_projects`, `find_projects`, `csr`), il faut donc passer par le SDK (`upload.ps1`).
     - Sinon, utiliser `pipeline-tsi-chat-ui.yaml`.
  4. Index : contrôler les chiffres du manifeste.
  5. Pipelines : coller les YAML et republier dans la Gateway.
  6. Faire la recette, avec les questions du README de production.

  Le guide renvoie au `deploy/prod/README.md` existant pour le détail et ne le duplique pas : il donne un ordre d'opérations clair.
- **Vérification :** dans un dossier temporaire (scratchpad), créer un venv neuf, faire `pip install <repo>` et lancer `python -m tsi_agent --help`. Les commandes `run`, `deliverables`, `csr-link`, `prod-export` et `release-data` doivent s'importer sans les dépendances du chatbot (qdrant, sentence-transformers). Sinon, rendre ces imports paresseux dans `__main__.py` et le signaler.

**Commit :** `docs(deploy): kit de mise a jour sur PC (updater) et guide de deploiement PROD par l'interface`

### Task 5 : `deploy/frontend-local/` et README racine

**Fichiers :**
- Create : `deploy/frontend-local/docker-compose.yml`, `deploy/frontend-local/nginx.conf`, `deploy/frontend-local/serve.ps1`, `deploy/frontend-local/config.local.example.js`, `deploy/frontend-local/README.md`
- Modify : `README.md` (section « Démarrer » en tête), `.gitignore` (`deploy/frontend-local/config.local.js`)

**Contenu attendu :**
- **`docker-compose.yml`** :
  - `nginx:1.27-alpine` sur le port `8080:80` ;
  - montage de `../../web` en `/usr/share/nginx/html:ro` ;
  - montage de `./config.local.js` en `/usr/share/nginx/html/config.js:ro` ;
  - montage de `nginx.conf` : en-têtes de sécurité de base, pas de cache sur `config.js`, et `try_files`.
- **`serve.ps1`** :
  - copie `web\` vers `deploy\frontend-local\dist\`, en ignorant ce dossier dans Git ;
  - remplace `config.js` par `config.local.js` (erreur claire s'il manque) ;
  - lance `python -m http.server <Port>` (par défaut 8000) dans `dist`.
- **`config.local.example.js`** : les mêmes exports que `web/config.js` (`SUPABASE_URL`, `SUPABASE_ANON_KEY`, `PROXY_URL`), avec des commentaires sur le choix entre `tsi-assistant` et `haystack-proxy`. Lire `web/config.js` pour reprendre exactement ses exports.
- **`README.md`** du dossier :
  - les deux modes de lancement ;
  - ajouter `http://localhost:8080/**` ou `http://<serveur>:<port>/**` aux *Redirect URLs* de Supabase ;
  - pour un vrai serveur, mettre HTTPS devant (reverse proxy), sinon la connexion par lien magique et le presse-papiers sont limités ;
  - les testeurs restent gérés dans la table `testers`.
- **README racine** : une section « Démarrer » en tête, avec les trois parcours et leurs liens :
  - « Déployer en PROD sur Haystack Enterprise » : `deploy/prod/GUIDE.md` ;
  - « Mettre à jour les données (PC) » : `deploy/updater/README.md` ;
  - « Explorateur web : GitHub Pages ou serveur local » : URL Pages et `deploy/frontend-local/README.md`.

  Ajouter aussi une ligne « Dernières données : Releases du dépôt ».

**Vérification :**
- lancer `serve.ps1` sur le port 8011, avec un `config.local.js` temporaire copié de l'exemple : on doit obtenir HTTP 200 sur `/` et sur `/config.js` ;
- arrêter ensuite **son propre** processus ; ne jamais toucher le port 8765 ;
- la commande Docker n'est pas exécutée si Docker est absent : vérifier seulement la syntaxe YAML.

**Commit :** `docs(deploy): front end local (Docker nginx ou script) et parcours de demarrage`

### Task 6 : mise en service (contrôleur)

1. Lancer `prod-export`, puis `release-data --publish` avec le tag `data-2026-10-14`, ce qui publie la première release.
2. Fusionner dans `main`, pousser, puis vérifier que les liens du README racine et de la release fonctionnent.
