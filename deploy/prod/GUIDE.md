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

## 1. Télécharger

Depuis la release, télécharger **`tsi-prod-files.zip`** et **`manifest.json`**, puis décompresser le zip dans un
dossier vide. Il contient les fichiers `tsi-*.md` et leurs `.meta.json`, à plat.

En ligne de commande (facultatif) :

```powershell
gh release download data-AAAA-MM-JJ -p tsi-prod-files.zip -p manifest.json -R MGE-Agilos/dg-reform-tsi -D $env:TEMP\tsi-release
```

## 2. Vider les anciens fichiers

Dans le workspace `tsi-reform`, *Files* : filtrer sur `tsi-` et supprimer tous les fichiers `tsi-*`. Un document
supprimé depuis la dernière release ne doit pas rester dans l'index. Ne pas toucher aux fichiers qui ne commencent pas
par `tsi-` : ils appartiennent à d'autres corpus.

## 3. Téléverser

Deux façons, qui ne donnent pas le même chatbot :

| Méthode | Métadonnées par fichier | Pipeline de chat à utiliser |
|---|---|---|
| **SDK** : `deploy\prod\upload.ps1` (recommandé) | oui, chaque `.meta.json` est rattaché à son fichier | `pipeline-tsi-chat.yaml` (avec `list_projects`, `find_projects` et le filtre `csr`) |
| **Interface** : glisser-déposer dans *Files* | non : l'interface n'attache pas les `.meta.json`, qui deviennent des fichiers ordinaires | `pipeline-tsi-chat-ui.yaml` (recherche seulement) |

- **SDK.** Placer le contenu du zip dans `deploy\prod\build\files\` et `manifest.json` dans `deploy\prod\build\`, puis
  suivre l'étape 2 du [README](README.md#2-générer-et-téléverser-le-corpus) à partir de
  `pip install haystack-enterprise-sdk` (clé API du workspace saisie masquée, `.\deploy\prod\upload.ps1`).
  Le script contrôle le manifeste et le nombre de fichiers avant le téléversement.
- **Interface.** Téléverser seulement les fichiers `tsi-*.md` (pas les `.meta.json`). Avec ce mode, les comptes
  exacts (« combien de projets… », « quels pays… ») et les questions sur les CSR liés à un projet ne sont pas
  disponibles.

Contrôle dans *Files* : autant de fichiers `tsi-*.md` que `files` dans `manifest.json`, et, avec le SDK, les
métadonnées visibles sur l'un d'eux (`Topic` = `TSI-BDG`, `corpus` = `dg-reform-tsi`, `doc_type`…).

## 4. Contrôler l'index

*Indexes > tsi-corpus* : la plateforme indexe les nouveaux fichiers. Quand l'état est *Indexed* :

- *Indexed files* = `files` du manifeste ;
- *Documents in index* = `chunks` du manifeste (le détail par type est dans `chunks_by_type`) ;
- *Failed files* = 0.

Si l'index n'existe pas encore, le créer avec `index-tsi-corpus.yaml` et ses filtres (préfixe `tsi-`, `corpus` =
`dg-reform-tsi`) avant *Enable* : étape 3 du [README](README.md#3-index).

## 5. Pipelines

Les YAML sont versionnés dans ce dossier (`deploy/prod/`), à prendre sur la branche `main` du dépôt :

- `tsi-chat` : `pipeline-tsi-chat.yaml` (téléversement par le SDK) ou `pipeline-tsi-chat-ui.yaml` (par l'interface) ;
- `tsi-impact` : `pipeline-tsi-impact.yaml`.

Pour chacun : ouvrir le pipeline, vue YAML, remplacer tout le contenu par le fichier, *Save*, puis **Publish >
Publish through Gateway** sur le service existant (ou *Publish to new service* la première fois, voir l'étape 4 du
[README](README.md#4-pipelines)). Les YAML ne changent que si le dépôt a été modifié depuis le dernier déploiement :
sans changement, une nouvelle publication n'est pas nécessaire, l'index suffit.

## 6. Recette

Dans le Playground de chaque service, une nouvelle conversation par question : reprendre les questions de la section
[Recette du README](README.md#5-recette) (chat, agrégation, CSR, analyse d'impact) et comparer avec les réponses
attendues. Relever la durée et les tokens dans *Analytics*.

## En cas de problème

- *Failed files* > 0 : ouvrir le fichier en échec dans *Files* et le téléverser de nouveau.
- Moins de documents que prévu : vérifier les filtres de l'index (préfixe `tsi-` et `corpus`), et, avec l'interface,
  que les `.meta.json` n'ont pas été téléversés comme documents.
- « Model (custom) is not connected » : le YAML collé n'est pas la version actuelle du dépôt (voir le README, *À
  fournir ou vérifier côté client*).
