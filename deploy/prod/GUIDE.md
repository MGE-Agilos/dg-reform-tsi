# Mettre à jour la PROD Haystack Enterprise par l'interface

Ordre des opérations pour remplacer le corpus du workspace `tsi-reform` par une nouvelle release de données, puis
republier les pipelines. Le détail (rôle de chaque fichier, isolation du workspace, secrets, recette complète) est dans
[README.md](README.md) : ce guide ne le répète pas, il donne l'ordre à suivre.

Première installation (workspace, secret `GPT_EC`, SearchApi, clé API, création de l'index) : suivre d'abord les
étapes 1 et 3 du [README](README.md#étapes).

## Avant de commencer

- Accès au workspace **`tsi-reform`** de la plateforme du client.
- La release de données à déployer : page *Releases* du dépôt
  (<https://github.com/MGE-Agilos/dg-reform-tsi/releases>), tag `data-AAAA-MM-JJ`. Sa description (`NOTES.md`) donne
  les chiffres attendus.
- Choisir la méthode de téléversement (étape 3) : **SDK** (recommandé, chatbot complet) ou **interface** (sans
  installation, chatbot réduit).

### Pour le SDK seulement : poste préparé (une fois)

Python 3.12 et Git installés. Dans **PowerShell** (la console normale, pas PowerShell ISE) :

```powershell
# Scripts du dépôt (upload.ps1, obsolete-files.ps1) et YAML des pipelines
cd C:\
git clone https://github.com/MGE-Agilos/dg-reform-tsi.git tsi
# SDK Haystack Enterprise, dans son propre environnement Python
python -m venv C:\tsi-sdk
C:\tsi-sdk\Scripts\pip install haystack-enterprise-sdk
```

À chaque session, avant d'utiliser le SDK :

```powershell
Set-ExecutionPolicy -Scope Process Bypass      # autorise les scripts .ps1 dans cette fenêtre seulement
C:\tsi-sdk\Scripts\Activate.ps1                # active l'environnement du SDK (commande haystack-enterprise)
cd C:\tsi
git pull
```

Si Windows refuse encore un script téléchargé (dépôt récupéré en ZIP plutôt que par `git clone`) :
`Get-ChildItem C:\tsi -Recurse -Filter *.ps1 | Unblock-File`. Autre façon de lancer un script sans changer la
politique : `powershell -ExecutionPolicy Bypass -File .\deploy\prod\upload.ps1 ...`.

## 1. Télécharger

Depuis la release, télécharger **`tsi-prod-files.zip`** et **`manifest.json`**, puis décompresser le zip dans un
**dossier vide**, par exemple `C:\tsi-release\files` (et `manifest.json` dans `C:\tsi-release`). Le zip contient les
fichiers `tsi-*.md` et leurs `.meta.json`, à plat. Un dossier qui contient déjà d'anciens fichiers fausse les contrôles.

En ligne de commande (facultatif, GitHub CLI connecté) :

```powershell
gh release download data-AAAA-MM-JJ -p tsi-prod-files.zip -p manifest.json -R MGE-Agilos/dg-reform-tsi -D C:\tsi-release
Expand-Archive C:\tsi-release\tsi-prod-files.zip -DestinationPath C:\tsi-release\files
```

## 2. Préparer le workspace

Pendant l'indexation (étapes 3 et 4), le chatbot et l'analyse d'impact répondent avec un corpus partiel, ou sans
résultat juste après une suppression : prévenir les testeurs, ou faire la mise à jour en dehors des heures d'usage.

- **SDK : ne pas tout supprimer.** Le téléversement en `OVERWRITE` remplace les fichiers modifiés et la plateforme ne
  réindexe que ceux-là. Il reste à supprimer les documents qui ont disparu du corpus. Pour obtenir leurs noms :
  ```powershell
  # Dossier des fichiers de la release précédente (celle en ligne), décompressée à part :
  .\deploy\prod\obsolete-files.ps1 -Current C:\tsi-release-precedente\files -New C:\tsi-release\files
  # ou liste des noms copiée depuis Files (un nom par ligne) :
  .\deploy\prod\obsolete-files.ps1 -Current noms-workspace.txt -New C:\tsi-release\files -Out a-supprimer.txt
  ```
  Supprimer ces fichiers dans *Files* (avant ou après le téléversement). Le script ne supprime rien lui-même.
- **Interface : supprimer d'abord tous les fichiers `tsi-*`.** Dans *Files*, filtrer sur `tsi-` et tout supprimer.
  Ne pas toucher aux fichiers qui ne commencent pas par `tsi-` : ils appartiennent à d'autres corpus.

## 3. Téléverser

Les deux méthodes ne donnent pas le même chatbot :

| Méthode | Métadonnées | Pipeline de chat à utiliser |
|---|---|---|
| **SDK** : `deploy\prod\upload.ps1` (recommandé) | complètes : chaque `.meta.json` est rattaché à son fichier (pays, programme, année, CSR liés…) | `pipeline-tsi-chat.yaml` (avec `list_projects`, `find_projects` et le filtre `csr`) |
| **Interface** : *Files > Upload*, en 4 lots | communes à chaque lot seulement (`Topic`, `corpus`, `doc_type`) | `pipeline-tsi-chat-ui.yaml` (recherche seulement : ni `list_projects`, ni `find_projects`, ni filtre `csr`) |

### Par le SDK

Dans la session préparée plus haut (environnement du SDK activé, dossier `C:\tsi`) :

```powershell
$s = Read-Host -AsSecureString "Clé API du workspace"
$env:API_KEY = [Runtime.InteropServices.Marshal]::PtrToStringAuto([Runtime.InteropServices.Marshal]::SecureStringToBSTR($s))
$env:API_URL = "<URL de l'API de la plateforme du client>"
$env:DEFAULT_WORKSPACE_NAME = "tsi-reform"
.\deploy\prod\upload.ps1 -Files C:\tsi-release\files -Manifest C:\tsi-release\manifest.json
```

Le script vérifie les variables, le manifeste et le nombre de fichiers, affiche l'URL et le workspace visés, puis
téléverse. Sans `-Files` ni `-Manifest`, il prend `deploy\prod\build\files` et `deploy\prod\build\manifest.json`
(sortie de `prod-export` sur le PC de mise à jour) : ce dossier doit alors ne contenir que l'export courant.

### Par l'interface

L'interface n'attache pas les `.meta.json` : ils deviendraient des documents ordinaires. On téléverse donc **seulement
les `.md`**, en **4 lots**, chacun avec ses métadonnées communes saisies dans la fenêtre de téléversement :

| Lot | Fichiers (filtre du nom dans l'explorateur Windows) | Métadonnées du lot |
|---|---|---|
| 1 | `tsi-project-*.md` | `Topic` = `TSI-BDG`, `corpus` = `dg-reform-tsi`, `doc_type` = `project` |
| 2 | `tsi-programme-*.md` | `Topic` = `TSI-BDG`, `corpus` = `dg-reform-tsi`, `doc_type` = `programme` |
| 3 | `tsi-deliverable-*.md` | `Topic` = `TSI-BDG`, `corpus` = `dg-reform-tsi`, `doc_type` = `deliverable` |
| 4 | `tsi-csr-*.md` | `Topic` = `TSI-BDG`, `corpus` = `dg-reform-tsi`, `doc_type` = `csr` |

Dans l'explorateur Windows, taper le filtre (par exemple `tsi-project-*.md`) dans la zone de recherche du dossier,
tout sélectionner (Ctrl+A) et glisser dans la fenêtre de téléversement. Vérifier qu'aucun `.meta.json` n'est
sélectionné. `Topic` et `corpus` sont indispensables : sans eux, le routeur et les filtres de l'index écartent les
fichiers. `doc_type` sert aux outils `search_projects`, `search_deliverables` et `search_csrs`.

Contrôle dans *Files* : autant de fichiers `tsi-*.md` que `files` dans `manifest.json` (détail par type dans
`files_by_type`), et les métadonnées visibles sur un fichier de chaque lot.

## 4. Contrôler l'index

*Indexes > tsi-corpus* : la plateforme indexe les nouveaux fichiers. Quand l'état est *Indexed* :

- *Indexed files* = `files` du manifeste ;
- *Documents in index* = `chunks` du manifeste (le détail par type est dans `chunks_by_type`) ;
- *Failed files* = 0.

Si l'index n'existe pas encore, le créer avec `index-tsi-corpus.yaml` et ses filtres (préfixe `tsi-`, `corpus` =
`dg-reform-tsi`) avant *Enable* : étape 3 du [README](README.md#3-index).

## 5. Pipelines

Prendre les YAML **au tag de la release**, pour qu'ils correspondent aux données :
`https://github.com/MGE-Agilos/dg-reform-tsi/tree/data-AAAA-MM-JJ/deploy/prod` (ou, dans le dépôt cloné,
`git checkout data-AAAA-MM-JJ`, puis `git checkout main` après).

- `tsi-chat` : `pipeline-tsi-chat.yaml` (téléversement par le SDK) ou `pipeline-tsi-chat-ui.yaml` (par l'interface) ;
- `tsi-impact` : `pipeline-tsi-impact.yaml`.

Pour chacun : ouvrir le pipeline, vue YAML, remplacer tout le contenu par le fichier, *Save*, puis **Publish >
Publish through Gateway** sur le service existant (ou *Publish to new service* la première fois, voir l'étape 4 du
[README](README.md#4-pipelines)). Si les YAML n'ont pas changé depuis le dernier déploiement et que la méthode de
téléversement est la même, une nouvelle publication n'est pas nécessaire : l'index suffit.

## 6. Recette

Dans le Playground de chaque service, une nouvelle conversation par question : reprendre les questions de la section
[Recette du README](README.md#5-recette) (chat, agrégation, CSR, analyse d'impact) et comparer avec les réponses
attendues. Avec l'interface (`pipeline-tsi-chat-ui.yaml`), les questions d'agrégation et de CSR liés donnent des
listes non exhaustives : c'est attendu. Relever la durée et les tokens dans *Analytics*.

## En cas de problème

- *Indexed files* = 0 (ou bien moins que prévu) : métadonnées manquantes sur le lot (`Topic` = `TSI-BDG`, `corpus` =
  `dg-reform-tsi`) ; supprimer ces fichiers et refaire le lot avec ses métadonnées. Vérifier aussi les filtres de
  l'index (préfixe `tsi-` et `corpus`).
- *Failed files* > 0 : ouvrir le fichier en échec dans *Files* et le téléverser de nouveau.
- Des `.meta.json` apparaissent comme documents dans *Files* : ils ont été téléversés par l'interface ; les supprimer.
- `upload.ps1` : « variable d'environnement … absente » : définir `API_KEY`, `API_URL` et `DEFAULT_WORKSPACE_NAME`
  dans la même fenêtre PowerShell ; « haystack-enterprise n'est pas reconnu » : activer l'environnement du SDK.
- « Model (custom) is not connected » : le YAML collé n'est pas la version du dépôt (voir le README, *À fournir ou
  vérifier côté client*).
