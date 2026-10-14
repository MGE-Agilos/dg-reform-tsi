# Mettre à jour les données TSI sur un PC Windows

Ce guide installe sur un PC la recherche des nouveaux documents publiés par la Commission (projets TSI et SRSP,
publications des projets, recommandations par pays (CSR) et liens projet → CSR), avec un LLM compatible OpenAI :
la passerelle **EC GPT** de la Commission ou l'API **OpenAI**. Le résultat est un dossier de fichiers prêt à importer
dans Haystack Enterprise.

Aucune connaissance de Python n'est nécessaire : il suffit de copier les commandes dans **PowerShell**, depuis le
dossier du dépôt.

## Installation (une fois)

1. **Prérequis.** Installer :
   - **Python 3.12** (<https://www.python.org/downloads/windows/>), en cochant « Add python.exe to PATH » ;
   - **Git** (<https://git-scm.com/download/win>) ;
   - **GitHub CLI** (<https://cli.github.com>), puis se connecter une fois :
     ```powershell
     gh auth login
     ```
     (choisir *GitHub.com*, *HTTPS*, puis la connexion par le navigateur).

2. **Cloner le dépôt** dans un dossier de travail, de préférence hors OneDrive (les mises à jour écrivent des
   milliers de petits fichiers) :
   ```powershell
   cd C:\
   git clone https://github.com/MGE-Agilos/dg-reform-tsi.git tsi
   cd C:\tsi
   ```
   Toutes les commandes suivantes se lancent depuis ce dossier.

3. **Créer l'environnement Python** :
   ```powershell
   python -m venv .venv
   .venv\Scripts\pip install -e .
   ```
   Les commandes de mise à jour n'ont pas besoin des dépendances du chatbot local (`.[local]` : PyTorch, Qdrant).

4. **Réglages.** Copier les réglages prévus pour ce mode :
   ```powershell
   Copy-Item deploy\updater\settings.updater.yaml config\settings.yaml
   ```
   Ils choisissent `llm_provider: openai`, la passerelle EC GPT (`openai_base_url`) et le modèle `gpt-5.1`. Pour
   l'API OpenAI publique, vider la ligne `openai_base_url:`. Variante sans copie : ajouter
   `--settings deploy\updater\settings.updater.yaml` à chaque commande, et `-Settings ...` à `update.ps1`.
   Deux réglages facultatifs : `openai_max_retries` (nouvelles tentatives du client en cas d'erreur réseau, de 429
   ou de 5xx, 2 par défaut) et `openai_max_completion_tokens` (plafond des tokens de sortie, raisonnement compris ;
   vide par défaut, sans plafond).

5. **Clé du LLM.** Copier le modèle de fichier de clés, puis l'ouvrir dans le Bloc-notes :
   ```powershell
   Copy-Item deploy\updater\.env.example .env
   notepad .env
   ```
   Renseigner `OPENAI_API_KEY=` avec la clé EC GPT (ou la clé OpenAI). **Ne jamais commiter `.env`** (il est
   ignoré par Git) et ne jamais copier la clé dans un autre fichier.

6. **Récupérer l'état actuel des données** (projets déjà extraits, publications, CSR, liens) depuis la dernière
   release du dépôt, pour ne pas tout refaire :
   ```powershell
   .\deploy\updater\restore-state.ps1
   ```
   Option `-Tag data-AAAA-MM-JJ` pour une release précise. Si `output\` existe déjà, le script refuse de continuer
   sans `-Force` (les fichiers du même nom sont alors remplacés, les autres gardés).

7. **Contrôler la qualité avec ce LLM** avant le premier gros lot. `llm-check` refait l'extraction de quelques
   projets déjà traités et met côte à côte l'ancien et le nouveau résultat :
   ```powershell
   .venv\Scripts\python.exe -m tsi_agent run --limit 3
   .venv\Scripts\python.exe -m tsi_agent llm-check --n 3
   ```
   `llm-check` lit les pages dans le cache HTTP (`.cache\http`), qui n'est pas dans la release : sur un PC neuf il
   répond « aucune page detail … dans le cache » (code 1) tant qu'un `run` n'a pas rechargé des pages. Le premier
   `run` recharge **toutes** les pages détail du site (voir *Durées*) : on peut l'interrompre avec Ctrl+C après
   quelques minutes, `llm-check` a alors assez de pages, et le run reprend plus tard où il s'était arrêté. Lire
   ensuite `output\reports\llm_check.md` : résumés, sections et résultats doivent être comparables.

## Mise à jour (à chaque fois)

8. **Lancer la mise à jour complète** :
   ```powershell
   .\deploy\updater\update.ps1
   ```
   Le script vérifie les réglages et la clé, enchaîne les sept étapes ci-dessous et s'arrête à la première erreur.
   À la fin, il affiche le dossier à importer (`deploy\prod\build\files`) et les chiffres du manifeste
   (`deploy\prod\build\manifest.json`).

   Options :
   - `-SkipDeliverables` : ne pas traiter les publications (le plus long et le plus cher) ;
   - `-SkipCsr` : ne pas mettre à jour les CSR ni les liens projet → CSR ;
   - `-DetailOnly` : `csr-link` n'envoie au LLM que les projets avec page détail (les autres plus tard) ;
   - `-Settings <fichier>` : autres réglages que `config\settings.yaml`.

   Les étapes peuvent aussi être lancées une par une, dans le même ordre, avec
   `.venv\Scripts\python.exe -m tsi_agent <commande>`.

9. **Importer dans Haystack Enterprise**, au choix :
   - par l'interface : suivre [deploy/prod/GUIDE.md](../prod/GUIDE.md), avec les fichiers de
     `deploy\prod\build\files` à la place du zip de la release ;
   - par le SDK (recommandé : garde les métadonnées de chaque fichier) : `deploy\prod\upload.ps1`. Il lit
     `API_KEY`, `API_URL` et `DEFAULT_WORKSPACE_NAME` dans la session PowerShell. Pour les prendre dans `.env` :
     ```powershell
     Get-Content .env | Where-Object { $_ -match '^\s*(API_KEY|API_URL|DEFAULT_WORKSPACE_NAME)\s*=\s*(.+)$' } | ForEach-Object { Set-Item "env:$($Matches[1])" $Matches[2].Trim() }
     .\deploy\prod\upload.ps1
     ```
     (`pip install haystack-enterprise-sdk` au préalable, de préférence dans un autre environnement Python ; voir
     [deploy/prod/README.md](../prod/README.md), étape 2.)

10. **Facultatif : publier une release de données**, si vous avez les droits d'écriture sur le dépôt. Les collègues
    récupèrent alors cet état avec `restore-state.ps1`, et le déploiement par l'interface part de cette release :
    ```powershell
    .venv\Scripts\python.exe -m tsi_agent release-data --publish
    ```
    Tag par défaut `data-AAAA-MM-JJ` (option `--tag` si une release du jour existe déjà ; rien n'est écrasé).

## Ce que fait chaque étape

| Étape | Commande | Ce qu'elle fait | LLM |
|---|---|---|---|
| 1 | `run` | relit les pages pays, les domaines, le PDF SRSP et le sitemap ; extrait les projets nouveaux ou modifiés dans `output\projects` | oui, un appel par page modifiée (et un arbitrage du sitemap si besoin) |
| 2 | `deliverables` | télécharge les publications des projets (PDF), extrait leur texte et en fait un résumé dans `output\deliverables` | oui, un appel par nouvelle publication |
| 3 | `csr-fetch` | télécharge la base des CSR d'ECFIN (export complet, `output\csr\csr_full.xlsx`) | non |
| 4 | `csr-build` | écrit une fiche par CSR (2016-2025) dans `output\csr` | non |
| 5 | `csr-link` | évalue quels CSR chaque projet soutient (`output\csr_links`), avec cache : seuls les projets nouveaux ou modifiés sont envoyés | oui |
| 6 | `anomalies` | rapport des anomalies de publication (`output\reports\anomalies.csv` et `.md`) | non |
| 7 | `prod-export` | écrit un fichier par projet, programme, publication et CSR, avec ses métadonnées, dans `deploy\prod\build\files`, et le manifeste | non |

Codes de sortie : `0` terminé ; `1` quota du LLM atteint (le run suivant reprend où il s'était arrêté) ; `2` erreur
fatale (détail dans le message `ERREUR` et dans `logs\runs\`).

**Vérification du LLM avant tout travail.** `run`, `deliverables`, `csr-link` et `llm-check` vérifient d'abord le
fournisseur : réglage `llm_provider` connu, clé présente et, en mode `openai`, un tout petit appel de test. Une clé,
une URL (`openai_base_url`) ou un modèle (`openai_model`) refusés arrêtent donc la commande **tout de suite**, avec
le message `ERREUR (fournisseur LLM) : ...` et le code `2`, sans rien traiter ; `update.ps1` s'arrête alors et
rappelle quoi corriger. Si la clé ou le modèle sont refusés en cours de route (401, 403, 404), le lot s'arrête
comme sur un quota (code `1`) et le journal précise qu'il s'agit d'une erreur de configuration. Pour `csr-link` seulement, le script continue sur un quota (« relancer plus
tard ») et va jusqu'à `prod-export` : les liens déjà faits sont gardés.

## Durées et coûts

- **Politesse envers le site de la Commission.** Au moins 2 s entre deux requêtes ; après une réponse « trop de
  requêtes » (HTTP 429), l'agent ralentit jusqu'à 15 s entre deux requêtes et ne réaccélère que progressivement.
  C'est la première cause de durée.
- **Premier `run` sur un PC** : le cache HTTP est vide (il n'est pas dans la release), donc toutes les pages détail
  sont rechargées, environ **2 heures**. Les runs suivants ne rechargent que les listings et les pages modifiées :
  quelques minutes à une demi-heure.
- **Publications** : téléchargement des PDF (jusqu'à 50 Mo) et un appel LLM long par publication ; compter plusieurs
  minutes par dizaine de nouvelles publications.
- **Coûts** (ordre de grandeur, prix public OpenAI de gpt-5.1 : 1,25 $ par million de tokens en entrée, 10 $ en
  sortie) : environ 1 centime par projet extrait, 5 à 10 centimes par publication résumée, 4 centimes par projet
  lié aux CSR. Une mise à jour hebdomadaire courante coûte moins de quelques dollars ; tout refaire (`run --full`,
  toutes les publications, tous les liens) de l'ordre de 100 $. Avec EC GPT, la facturation est interne à la
  Commission. Le coût de chaque run est dans `logs\runs\<run>.summary.json` (`cost_usd`), calculé avec la section
  `pricing` des réglages.

## Dépannage

| Message | Cause et solution |
|---|---|
| `cle OPENAI_API_KEY absente` (update.ps1) | `.env` absent de la racine du dépôt, ou ligne `OPENAI_API_KEY=` vide : étape 5. |
| `ERREUR (fournisseur LLM) : cle API absente` (code 2) | `.env` absent de la racine du dépôt, ou ligne `OPENAI_API_KEY=` vide : étape 5. |
| `ERREUR (fournisseur LLM) : erreur de configuration ...` (code 2), ou 401, 403, 404 dans le journal | clé invalide ou expirée, clé OpenAI utilisée avec l'URL EC GPT (et inversement), URL ou nom de modèle erronés : vérifier `.env`, `openai_base_url` et `openai_model`. |
| `reponse tronquee (finish_reason=length)` | la réponse a atteint `openai_max_completion_tokens` : augmenter ce réglage (par exemple 16000) ou le vider. |
| Arrêt avec le code 1, « quota » | limite de débit ou de budget de la clé atteinte : relancer plus tard, la reprise est automatique (`openai_max_retries` règle les nouvelles tentatives immédiates). |
| Délai dépassé (timeout) sur des publications | passerelle lente : augmenter `openai_timeout_s` (ou `deliverables_llm_timeout_s`) dans les réglages, puis relancer. |
| HTTP 429 du site de la Commission | normal : l'agent ralentit tout seul. Ne pas lancer deux mises à jour en même temps. |
| `csr-fetch a echoue` | site ECFIN indisponible : relancer plus tard, ou `update.ps1 -SkipCsr`. |
| `prod-export a echoue` | types de métadonnées incohérents ou fichier de plus de 10 Mo : le JSON affiché donne les fichiers en cause. |
| `llm-check` : « aucune page detail … dans le cache » | lancer d'abord un `run` (étape 7). |
| `restore-state.ps1` : `gh` introuvable ou non connecté | installer GitHub CLI et lancer `gh auth login` (étape 1). |
| `Python du projet introuvable` | l'environnement `.venv` n'est pas créé dans le dossier du dépôt : étape 3. |
| Scripts bloqués (« l'exécution de scripts est désactivée ») | lancer une fois `Set-ExecutionPolicy -Scope CurrentUser RemoteSigned`, ou `powershell -ExecutionPolicy Bypass -File .\deploy\updater\update.ps1`. |

Ne jamais lancer deux commandes de mise à jour en même temps sur le même dossier (`run`, `deliverables` et
`csr-link` écrivent dans `output\`).
