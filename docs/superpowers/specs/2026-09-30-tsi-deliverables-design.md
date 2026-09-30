# Étage « livrables » de l'agent d'extraction — Design

**Date :** 2026-09-30
**Statut :** validé en conversation, en relecture
**Contexte :** extension de l'agent d'extraction ([design](2026-09-30-tsi-extraction-agent-design.md)). Les pages
détail des projets renvoient souvent, dans la section « More about the project », à une **publication** : rapport
final, présentation ou fiche projet, sous forme de PDF téléchargeable. Ces livrables contiennent des informations
absentes des pages : objectifs détaillés, livrables clés, résultats, chiffres, prestataire. L'agent les ignore
aujourd'hui (`BOILERPLATE_HEADINGS` du parser détail).

## 1. Constats (mesurés le 2026-09-30)

- Sur 668 projets avec page détail, **483 renvoient vers au moins une publication**, soit **503 publications
  distinctes** (liens `/publications-0/<slug>_en`).
- Page de publication : liste de description (`dt`/`dd` : « Publication date », « Author », « Country », « Funding
  Programme », « Year », « Final deliverable »), puis un bloc « Files » avec, pour chaque fichier, sa langue, un lien
  « View » et un lien `Download (<taille> - PDF)` vers `/document/download/<uuid>_<lang>?filename=<nom>.pdf`.
- Échantillon de 20 publications (28 fichiers) :
  - 1 à 3 fichiers par publication, tous en PDF ;
  - de 6 à 166 pages, **~5 500 mots en médiane** (maximum 80 000) ;
  - souvent l'anglais plus une traduction nationale ;
  - texte extractible partout avec pdfplumber ;
  - mise en page parfois en colonnes, dont les lignes s'entremêlent à l'extraction.
- Extrapolation : ~700 PDF et ~8 millions de mots, soit 15 fois le corpus actuel.
- Le site renvoie des 429 et des pages d'erreur sous des requêtes rapprochées : on passe obligatoirement par le
  `PoliteFetcher`.

## 2. Objectifs et décisions

- Pour chaque publication d'un projet :
  - ses **métadonnées et liens** dans le fichier projet ;
  - un **résumé IA** des livrables dans le fichier projet, qui sera vectorisé ;
  - le **texte intégral** conservé à part dans `output/deliverables/`, non vectorisé (consultation, futur agent
    d'impact).
- **Texte conservé tel que publié**, données personnelles comprises (noms, e-mails) : ce sont des documents publics
  de la Commission (décision de l'utilisateur). Le résumé porte sur le contenu du projet.
- **Langue** : on garde les fichiers en anglais. Si aucun fichier n'est en anglais, on garde tous les fichiers de la
  publication. Le résumé est rédigé en anglais, comme ceux des pages.
- **Longs documents** : le résumé part du début du texte des fichiers retenus, dans l'ordre de la page, dans la
  limite de 15 000 mots par publication. Le texte intégral est conservé en entier.
- **Pas de cache pour les PDF** (~1,5 Go dans OneDrive) : téléchargement, extraction du texte, puis abandon du PDF.
  Pour l'incrémental, on ne regarde que la page de publication (en cache HTTP) et la liste de ses fichiers.
- **Étage séparé** : pipeline `deliverables`, lancé entre `run` et `index`. L'extraction reste rapide, et les appels
  LLM des livrables ont leur propre reprise sur limite de souscription.

Hors périmètre : OCR des PDF scannés, formats autres que PDF, vectorisation du texte intégral, masquage des données
personnelles.

## 3. Architecture

```
python -m tsi_agent run            (inchangé : pages détail, fichiers projet)
python -m tsi_agent deliverables   (nouveau)
python -m tsi_agent index          (inchangé, prend en compte la nouvelle section)
```

Pipeline Haystack `deliverables` :

```
PublicationDiscoverer ─▶ PublicationFetcher ─▶ DeliverableTextExtractor ─▶ DeliverableSummarizer ─▶ DeliverableWriter
```

### 3.1 `PublicationDiscoverer`

- Parcourt les projets avec page détail de `output/state/index.json` et lit leur page détail **dans le cache HTTP**,
  sans requête réseau (`PoliteFetcher.cached(url)`). Le run d'extraction vient de rafraîchir ce cache.
- Relève les liens `/publications-0/...` du `<main>` de la page (section « More about the project »).
- Sortie : une liste de `(project_id, publication_url)`. Une même publication liée à plusieurs projets apparaît une
  fois par projet.
- Page absente du cache : alerte, projet ignoré pour ce run.

### 3.2 `PublicationFetcher`

- `PoliteFetcher.get` (requête conditionnelle, cache HTTP) de chaque page de publication. Le parser en tire :
  - le titre (`h1`) et la date ;
  - les métadonnées `dt`/`dd` ;
  - les fichiers `[{url, filename, language, size_label}]`.
- `files_key` = hash de la liste triée des URL de fichiers (chaque URL porte un uuid).
- Comparaison avec `output/state/deliverables.json` :
  - si `files_key` est inchangé et que la publication a déjà été traitée, rien d'autre n'est fait ;
  - sinon, la publication part vers l'extraction de texte.
- `--full` force le retraitement. `--limit N` borne le nombre de publications envoyées au LLM, comme `run`.

### 3.3 `DeliverableTextExtractor`

- Sélection de la langue (§2).
- Téléchargement par `PoliteFetcher.get(url, cache=False)` : même politesse et mêmes reprises, sans écriture dans le
  cache.
- Extraction du texte avec `pdfplumber`, page par page, séparées par `--- page N ---`.
- Écrit `output/deliverables/<publication_slug>.md` :
  - un front matter : `publication_id`, `title`, `url`, `date`, `projects`, `files` (nom, langue, pages, mots,
    URL) ;
  - le texte intégral, par fichier : `## <nom du fichier>`.
- PDF illisible, trop gros (> 50 Mo) ou réponse non PDF : alerte. Le fichier est ignoré. Si aucun fichier n'est
  lisible, la publication n'est pas marquée comme traitée et sera retentée au run suivant.

### 3.4 `DeliverableSummarizer`

- Même chat generator que l'extraction (`ClaudeCliChatGenerator` en local, `OpenAIChatGenerator` dans deepset), avec
  une réponse JSON structurée (`DeliverableContent`) :
  - `summary` : environ 150 à 250 mots ;
  - `key_outputs` : liste courte ;
  - `results` : ce qui a été obtenu ou recommandé ;
  - `figures` : chiffres clés avec leur contexte.
- Entrée : titre du projet et de la publication, puis le texte limité à 15 000 mots.
- Consignes : ne rien inventer, ne pas citer de personnes, rester sur le projet.
- Parallélisme, nouvelle tentative, `LLMQuotaExceeded` (arrêt propre, reprise au run suivant), journal
  `llm_call` avec tokens et coût : même comportement que `ProjectExtractor`.

### 3.5 `DeliverableWriter` et section du fichier projet

- Met à jour `output/state/deliverables.json` : par publication, `files_key`, les fichiers, le résumé, la date de
  traitement et les projets liés.
- Pour chaque projet concerné, remplace ou insère dans son fichier la section suivante, **avant** « Listed in country
  factsheets as » / « Sources » :

```markdown
## Deliverables (AI-generated summary)

### <titre de la publication> (<date>)

Files: [Final report (EN, 52 p.)](<url>) · [Presentation (EN, 13 p.)](<url>)
Full text: output/deliverables/<slug>.md

<summary>

Key outputs:
- …

Results:
- …

Key figures:
- …
```

- Le front matter du projet reçoit `deliverables: <nombre de publications résumées>`.
- **L'extraction préserve la section** :
  - `replace_listing_tail` la garde déjà, puisqu'elle précède la partie listing ;
  - quand un fichier est réécrit entièrement après un changement de page, le writer d'extraction réinsère la
    section depuis `deliverables.json` (`render.deliverables_section(project_id)`).

### 3.6 Vectorisation

- `chunking.py` : la section `Deliverables (AI-generated summary)` est vectorisée avec `ai_generated: true`
  (`AI_SECTIONS`). `CHUNKER_VERSION` passe à 2, ce qui déclenche une revectorisation complète automatique (~5 min).
- Le texte intégral (`output/deliverables/`) n'est pas vectorisé.

## 4. Traçabilité et coûts

- Un run `deliverables` produit un journal `logs/runs/<run_id>.jsonl` et un résumé, avec une ligne par run dans
  `costs.csv`. On y trouve :
  - `discover` : nombre de publications ;
  - `fetch` : page de publication ;
  - `download` : taille, pages, mots, durée ;
  - `llm_call` : tokens, coût ;
  - `write`, `alert`.
- Estimation du premier run :
  - ~500 pages de publication et ~700 PDF, soit ~45 min de réseau au rythme poli ;
  - ~500 appels LLM avec de longues entrées : ~20 à 40 $ en équivalent API sur Sonnet, plus sur la souscription en
    plusieurs sessions (reprise automatique) ;
  - runs suivants : ~500 requêtes conditionnelles (~20 min) et seulement les nouveautés au LLM.

## 5. CLI et configuration

- `python -m tsi_agent deliverables [--full] [--limit N] [--model M]` avec les codes de sortie 0 (terminé),
  1 (limite de souscription, relancer) et 2 (erreur).
- `export-yaml --deliverables PATH`.
- `settings.yaml` :
  - `deliverables_max_words: 15000` ;
  - `deliverables_max_pdf_mb: 50` ;
  - `deliverables_dir: output/deliverables`.
- README : section « Livrables » et ordre des commandes pour la planification (`run`, puis `deliverables`, puis
  `index`).

## 6. Tests

- Parser de publication sur des fixtures réelles (publication Slovénie à 1 fichier, publication à 2 fichiers EN et
  langue nationale, publication à 3 fichiers).
- Découverte des liens sur de vraies pages détail en cache (fixture `detail_hems_croatia.html` et une page avec
  publication).
- Sélection de la langue ; `files_key` ; incrémental (inchangé, fichier ajouté, fichier remplacé) ; `--full` ;
  `--limit`.
- Extracteur : PDF de test (petit PDF réel en fixture), réponse non PDF, PDF trop gros, texte tronqué à la limite
  de mots pour le résumé.
- Résumeur avec un generator factice : JSON, nouvelle tentative, quota.
- Writer : insertion puis remplacement de la section, préservation par `replace_listing_tail`, réinsertion quand
  l'extraction réécrit le fichier, front matter `deliverables`.
- Chunking : la section est vectorisée avec `ai_generated`.
- Pipeline : aller-retour YAML.
- Manuel (réel) : 3 publications (dont la Slovénie), avec durée, coût et relecture des résumés.
