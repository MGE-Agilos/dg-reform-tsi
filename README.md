# TSI Extraction Agent (DG REFORM)

Premier agent du pipeline Haystack pour la Commission européenne. Il crawle les sites publics du
Technical Support Instrument (TSI) et de son prédécesseur, le SRSP, et produit **un fichier Markdown
par projet** dans `output/projects/`, avec les métadonnées dans un front matter YAML. On peut le relancer
régulièrement : il ne traite que les projets nouveaux ou modifiés.

Design : [docs/superpowers/specs/2026-09-30-tsi-extraction-agent-design.md](docs/superpowers/specs/2026-09-30-tsi-extraction-agent-design.md)

## Installation

```powershell
python -m venv .venv
.venv\Scripts\python -m pip install -e ".[dev]"
```

Pour l'exécution locale, le CLI `claude` doit être installé et connecté à une souscription Claude
(`claude --version`). Aucune clé API n'est nécessaire.

## Utilisation

```powershell
# Pilote : Croatie seulement, 3 projets envoyés au LLM au maximum
.venv\Scripts\python -m tsi_agent run --countries HR --limit 3

# Voir ce qui serait extrait, sans LLM ni écriture (alias : --discover-only)
.venv\Scripts\python -m tsi_agent run --dry-run

# Run complet incrémental (le premier envoie environ 620 projets au LLM ; plusieurs heures, surtout
# à cause du débit limité imposé au site — voir le throttling adaptatif plus bas)
.venv\Scripts\python -m tsi_agent run

# Tout ré-extraire, même les pages inchangées
.venv\Scripts\python -m tsi_agent run --full

# Exporter le pipeline pour deepset Studio
.venv\Scripts\python -m tsi_agent export-yaml pipeline.yaml
```

Ordre recommandé pour un cycle complet : `run` (extraction des projets), puis `deliverables` (livrables des
projets), puis `index` (couche vectorisée) — chaque étage dépend des fichiers écrits par le précédent.

Un run interrompu (Ctrl-C, limite de souscription) reprend là où il s'était arrêté au run suivant.

`--limit` compte les projets réellement envoyés au LLM : les pages inchangées depuis le dernier run ne l'entament pas.

Codes de sortie : `0` run terminé, `1` arrêt sur la limite de la souscription (le run suivant reprend), `2` erreur inattendue (détail dans `logs/runs/<run_id>.summary.json`, champ `error`).

**`--dry-run`** (alias **`--discover-only`**) fait la découverte (sitemap compris) et la détection sans
appeler le LLM ni écrire de fichier projet, puis affiche :

- le bilan de la découverte : nombre de projets après fusion, URLs projet du sitemap (et combien manquent
  aux listings), fusions de pages du sitemap avec des projets thin (les 30 premières paires
  `slug sitemap -> id thin (score, règle)`, règle `title_key` ou `fuzzy`), nouveaux projets, doublons
  fusionnés (thin + page détail déjà connue). Le bilan complet est enregistré dans
  `logs/runs/<run_id>.discovery.json` : à relire avant le run réel ;
- trois compteurs : les pages détail candidates (`detail_candidates` — tous les projets qui ont une page
  détail ; leur hash n'est vérifié qu'au run réel, donc certains peuvent en fait être déjà à jour), les
  projets thin à écrire (`thin_to_write`) et les délistages (`delisted`).

Le premier `--dry-run` après l'ajout du sitemap récupère environ 170 pages détail absentes des listings
(au moins 2 s par requête, soit 6 à 10 minutes) ; elles restent ensuite en cache HTTP.

**Débit adaptatif :** le site répond par des HTTP 429 au-delà d'environ 1 requête/s soutenue. Après un 429,
l'agent ralentit durablement (l'intervalle minimum entre requêtes double, jusqu'à 15 s) et ne réaccélère que
progressivement, après 50 requêtes réussies d'affilée. C'est la première cause de durée d'un run complet.

## Sources

Déclarées dans `config/sources.yaml` :

| Source | Ce qu'elle apporte |
|---|---|
| 27 factsheets pays TSI (`factsheet_url`) | Projets TSI par pays et par année, avec ou sans lien vers une page détail |
| Page « Projects in the spotlight » (`spotlight_url`) | Pages détail mises en avant (TSI et SRSP) |
| 10 pages de domaine politique (`policy_areas`) | Pages détail liées depuis chaque domaine |
| PDF « List of approved technical support requests under the SRSP » (`srsp_pdf_url`) | Projets SRSP 2017-2020 par pays (projets thin), et en annexe les projets SRSS (voir plus bas) |
| Sitemap du site (`sitemap_url`) | Pages détail qu'aucun listing ne référence |
| Documents programme (`programme_docs`) | Textes programme (performance TSI) |

**Étape sitemap.** L'index `sitemap.xml` et ses pages (`sitemap.xml_en?page=N`) sont lus à chaque run
complet (pas dans les runs pilotes `--countries`, comme le PDF SRSP ; tout ou rien : un sitemap incomplet est
ignoré). Seules les URLs `.../technical-support-instrument-0/<domaine>/<slug>_en` sont retenues, hors rubriques
de `sitemap_excluded_areas` (pages de navigation `our-projects`, `our-projects-0`, `what-we-do`,
`other-eu-support-opportunities`, `our-partners`, et `flagship-technical-support-projects` : ce sont des
descriptions d'appels flagship, sans programme ni année, pas des pages projet). Chaque URL qui n'est la page
détail d'aucun projet déjà découvert est récupérée (cache HTTP) et lue comme une page détail : titre, programme
et année de la page, domaine de l'URL ; les pays restent vides (la page n'en donne pas : le LLM les lit dans
le texte, comme pour les pages des domaines politiques). Une page sans programme ni année est ignorée ; un
échec temporaire est reporté au run suivant (le projet n'est ni écrit ni dé-listé). Ces projets ont pour
source l'index du sitemap.

**Fusion avec les projets thin.** Une page du sitemap rejoint le projet thin correspondant (PDF SRSP,
mention de factsheet sans lien) par la règle existante (même programme, année et titre), sinon par une
règle floue, qui s'applique aussi entre les pages détail déjà connues et les projets thin (doublons) :

- titres normalisés (minuscules, sans ponctuation, sans mots vides ni noms ou adjectifs de pays) avec un
  ratio `difflib` ≥ 0,85, les mêmes marqueurs de phase (« phase II », « 2050 »…) et au moins la moitié des
  mots en commun (comparés sur leurs 5 premières lettres : « supporting » = « support », « waste » ≠ « water ») ;
- même programme, année égale ou à un an près (score ≥ 0,95 dans ce cas), pays communs (ou inconnus d'un côté) ;
- pour une page détail en cache, c'est son titre officiel qui est comparé, et son année et son programme
  qui sont contrôlés ;
- jamais deux pages détail ensemble, jamais une mention sans lien d'un titre *flagship* générique ; chaque
  page prend au plus un projet thin (le meilleur candidat), et rien n'est fusionné en cas d'égalité.

**Arbitrage LLM.** Une page du sitemap restée sans fusion est soumise à un LLM (CLI `claude`, sonnet, sortie
structurée, lots de 10 pages) avec au plus 3 projets thin candidats : même programme, année égale ou à un an
près, pays compatibles (pays de la page devinés du titre, sinon du début du texte), classés par proximité des
mots (Jaccard ≥ 0,25, mots sans mots vides ni pays). Le LLM reçoit le titre, le programme, l'année, les pays
devinés et le début du texte de la page, et pour chaque candidat son titre, programme, année, pays et sa source
(factsheet, PDF SRSP, page de domaine). Même projet = même demande d'assistance (même pays ou institution
bénéficiaire, même réforme), titre éventuellement reformulé ; une autre phase, un autre pays ou un projet
multi-pays face à une demande d'un seul pays = projets différents. Seules les réponses de confiance `high` ou
`medium` fusionnent, comme une fusion floue. Les décisions sont enregistrées dans
`output/state/merge_decisions.json` (clé : slug de la page + ids des candidats) : un nouveau run ne rappelle
pas le LLM. On peut les relire et les corriger : `"override": "<id thin>"` force la fusion, `"override":
"none"` l'interdit. Le bilan `--dry-run` liste les pages jugées, les fusions décidées par le LLM (avec la
confiance et la raison) et les refus ; chaque décision est journalisée (`merge_judge`). Le `--dry-run`
appelle donc le LLM (une dizaine d'appels au premier passage) et écrit ce fichier de décisions.

Le projet fusionné garde l'id de sa page détail ; l'id du projet thin passe dans ses `aliases`. À l'écriture
du projet détaillé, le fichier thin `output/projects/<id thin>.md` et son entrée d'état sont supprimés (comme
pour les autres fusions) ; sa date `first_seen` est conservée. Au `index` suivant, les morceaux vectorisés de
l'ancien fichier sont retirés et le nouveau fichier est vectorisé. Les livrables ne concernent que les
projets avec page détail : aucun n'est rattaché à un id thin.

**Programme SRSS.** L'annexe du PDF SRSP (« List of approved technical assistance projects covered by transfer
of funds from European Structural and Investment Funds », pages 36 à 39) liste 82 projets d'assistance
technique à la Grèce de 2012 à 2016, antérieurs au SRSP et exclus de ses chiffres officiels. Ils portent
`programme: SRSS` (Structural Reform Support Service) et gardent leur id `srsp-<année>-...`. Le chatbot
accepte le filtre `programme: "SRSS"`.

**Lacune connue : Malte.** Le PDF SRSP n'a pas de section Malte et aucune autre source utilisée ne publie la
liste des projets SRSP maltais (une trentaine selon les chiffres officiels) : seuls ceux qui ont une page
détail (trouvée par le sitemap ou un listing) sont présents.

## Ce qu'est un « projet »

Un projet correspond à une page détail du site de la Commission. Une même page peut couvrir plusieurs composantes nationales, listées dans les factsheets sous des titres différents (par exemple les 25 projets « PACE 2023 »). Ces titres sont conservés dans `listing_titles` et dans la section « Listed in country factsheets as ». Un titre générique de projet *flagship* qui pointe vers une page différente dans chaque pays donne un projet par page. Les mentions de ce titre sans lien forment un projet « thin » distinct. Un projet sans page détail (« thin ») est décrit à partir du listing seul.

Le corps du fichier reprend mot pour mot les sections officielles de la page (Context, Support delivered,
puis Results achieved ou Expected results selon le titre trouvé sur la page) ; seul le résumé est généré par
le LLM, sous l'étiquette « Summary (AI-generated) ». Le front matter porte `results: achieved` ou `expected`
en conséquence (`null` si la page n'a pas de section résultats reconnue).

## Sorties

| Chemin | Contenu |
|---|---|
| `output/projects/<project_id>.md` | Un fichier par projet : front matter (pays, année, programme, titres de listing nationaux, sources, hash, run, `results: achieved\|expected\|null`) + un résumé généré par IA (« Summary (AI-generated) ») suivi des sections officielles de la page recopiées mot pour mot (Context, Support delivered, Results achieved ou Expected results) |
| `output/programme/*.md` | Documents programme : chiffres par pays, domaines politiques, performance TSI |
| `output/state/index.json` | État utilisé pour l'incrémental. Ne pas éditer à la main. |
| `logs/runs/<run_id>.jsonl` | Journal : une action par ligne (fetch, classification, appel LLM avec tokens et coût, écriture…) |
| `logs/runs/<run_id>.summary.json` | Résumé du run, aussi affiché en fin de run |
| `logs/costs.csv` | Une ligne par run : appels LLM, tokens, coût |

Avec la souscription, `cost_usd` est un **coût équivalent API** (notionnel), calculé par le CLI.
Il sert à estimer le budget du passage à l'API.

## Livrables des projets

Un second pipeline, `deliverables`, parcourt les pages détail déjà en cache HTTP (celles rafraîchies par le
dernier `run`), relève leurs liens vers des pages de publication (`/publications-0/...`), télécharge les
documents qui y sont listés (rapport final, présentation, factsheet…) et en extrait le texte pour produire,
par publication, un résumé généré par IA inséré dans le fichier du ou des projets concernés.

```powershell
# Pilote : 3 publications envoyées au LLM au maximum
.venv\Scripts\python -m tsi_agent deliverables --limit 3

# Run complet incrémental (seules les publications nouvelles ou dont les fichiers ont changé sont retraitées)
.venv\Scripts\python -m tsi_agent deliverables

# Tout retraiter, même les publications inchangées
.venv\Scripts\python -m tsi_agent deliverables --full

# Recopier pays, année, programme et domaine des projets liés dans les textes intégraux existants (sans LLM ni réseau)
.venv\Scripts\python -m tsi_agent deliverables --refresh-metadata

# Exporter le pipeline pour deepset Studio
.venv\Scripts\python -m tsi_agent export-yaml --deliverables deliverables.yaml
```

Cet étage se place après `run` (il lit le cache HTTP des pages détail que `run` vient de rafraîchir) et avant
`index` (pour que la section générée soit vectorisée avec le reste du projet).

| Chemin | Contenu |
|---|---|
| `output/deliverables/<publication_id>.md` | Texte intégral des documents d'une publication, tel que publié (non vectorisé), avec un front matter (titre, URL, date, projets concernés, fichiers) |
| `output/state/deliverables.json` | État utilisé pour l'incrémental (une entrée par publication traitée, `files_key` pour détecter un fichier ajouté ou remplacé). Ne pas éditer à la main. |

Le fichier de chaque projet concerné reçoit une section « Deliverables (AI-generated summary) » (une
sous-section par publication, la plus récente d'abord, avec liens vers les fichiers et vers le texte intégral)
insérée juste avant les sections listing/sources, et le front matter gagne un champ `deliverables: N`. Une page
de publication peut lister une traduction nationale d'un document à côté de sa version anglaise : seule la
version anglaise est retenue si elle existe, sinon la version principale de la page.

Le texte envoyé au LLM pour le résumé est limité au début des documents (`deliverables_max_words`, 15 000 mots
par défaut) ; au-delà, le texte est tronqué et le prompt le signale. Ces prompts étant longs, le délai d'appel
au CLI `claude` a son propre réglage, plus large que celui de l'extraction (`deliverables_llm_timeout_s`,
600 s par défaut). Comme pour l'extraction, aucune clé API n'est nécessaire en local : le résumé passe par le
CLI `claude` de la souscription. Les fichiers lus sont les PDF (pdfplumber) et les documents Word `.docx`
(python-docx : paragraphes et tableaux, dans l'ordre du document) ; les autres formats sont ignorés avec une
alerte. Les fichiers ne passent jamais par le cache HTTP (ils ne changent pas d'URL quand on les retélécharge,
et un cache de plusieurs centaines de PDF n'apporterait rien) ; seules les pages HTML (détail, publication)
sont mises en cache.

Une publication traitée depuis moins de `deliverables_recheck_days` jours (7 par défaut) n'est pas revérifiée :
sa page de publication n'est même pas récupérée. Les relances rapprochées (reprise après une limite de
souscription) repartent donc directement sur les publications restantes. Un run hebdomadaire revérifie tout,
et `--full` force le retraitement.

Une publication qui n'est plus liée depuis aucune page détail (document retiré du site) est nettoyée : son
entrée disparaît de `output/state/deliverables.json`, et sa sous-section disparaît du fichier des projets qui
la citaient (le compteur `deliverables` suit). Son texte intégral reste dans `output/deliverables/` (fichier
orphelin, sans effet). Ce nettoyage ne se déclenche que lorsque la découverte a pu lire toutes les pages détail
en cache pendant le run ; s'il en manque une (page pas encore récupérée par `run`), rien n'est supprimé par
prudence, et le nettoyage sera retenté au run suivant.

Premier run complet (mesuré du 30 septembre au 2 octobre 2026) : 498 publications, 806 fichiers, 13,7 millions
de mots de texte intégral, 473 appels LLM pour 57 $ en équivalent API (Sonnet). Sur la souscription, il a fallu
une vingtaine de reprises après la limite d'usage, soit environ deux jours. Comme pour `run`, un arrêt sur la
limite est repris au run suivant sans retraiter ce qui a déjà été écrit. Pour un run de plusieurs heures,
lancer la commande dans un terminal qui reste ouvert, avec une boucle de reprise (par exemple : relancer après
30 minutes tant que le code de sortie vaut `1`).

Codes de sortie : identiques à `run` (`0` terminé, `1` arrêt sur la limite de la souscription, `2` erreur
inattendue).

**Passage à deepset :** comme pour `extractor`, le `chat_generator` du composant `summarizer` est remplacé par
un `OpenAIChatGenerator` dans le YAML exporté (voir la section dédiée plus bas).

## Couche vectorisée (préparation du RAG)

Un second pipeline, `index`, découpe les fichiers de `output/projects` et `output/programme` en morceaux (un par
section, redécoupé au-delà de 300 mots), les vectorise et les range dans une base Qdrant locale, `~/tsi-vectors` (réglage `vectors_dir`, hors OneDrive).
Les fichiers extraits ne sont pas modifiés : la couche vectorisée peut être supprimée et reconstruite à tout moment.

**Un seul processus à la fois sur la base locale** : Qdrant verrouille son dossier. Arrêter le chatbot pendant `index`
(et inversement) ; le message d'erreur le rappelle.

```powershell
# Une fois : embedder local (PyTorch CPU + modèle multilingual-e5-small, ~1 à 2 Go)
.venv\Scripts\python -m pip install -e ".[local]"

# Premier run : tout ; ensuite : seulement les fichiers nouveaux, modifiés ou supprimés
.venv\Scripts\python -m tsi_agent index
.venv\Scripts\python -m tsi_agent index --full

# Export pour un index deepset
.venv\Scripts\python -m tsi_agent export-yaml --index index.yaml
```

| Chemin | Contenu |
|---|---|
| `~/tsi-vectors/qdrant/` | Base Qdrant locale (collection `tsi`, similarité cosinus, `embedding_dim: 384`) : morceaux avec texte, métadonnées et vecteur. Plus de `store.json`. |
| `~/tsi-vectors/state.json` | Hash de chaque fichier indexé et identifiants de ses morceaux. Ne pas éditer à la main. |

Chaque morceau porte en métadonnées le projet (`doc_id`, titre, programme, année, pays, domaine, `detail_page`…),
la section et `ai_generated` (vrai pour le résumé IA). Aucun LLM n'intervient : seul l'embedder travaille.
Changer de modèle d'embedding ou de règles de découpage déclenche automatiquement un full. L'identifiant d'un
morceau est `<chemin relatif sans .md>#<n>` (ex. `projects/energy-regulation-academy#0`) ; le `doc_id` du projet
ou du document programme est conservé à part dans les métadonnées.

Environ 70 000 morceaux au total, dont environ 65 000 pour le texte intégral des livrables (`output/deliverables`, découpé
par page). Un full représente environ 1 h de vectorisation (embedder e5 sur CPU) plus environ 16 min d'écriture ; le
dossier Qdrant pèse environ 300 à 500 Mo dans `~/tsi-vectors`. Un run incrémental ne touche que les fichiers modifiés
(suppression des morceaux périmés, puis écriture). La base n'est pas synchronisée par OneDrive.

Un full vide la base en supprimant puis recréant la collection ; `state.json` est invalidé avant, si bien qu'un full
interrompu est refait en entier au run suivant. Si le dossier est déjà ouvert par un autre processus (chatbot), `index`
échoue tout de suite.

Dans deepset, l'embedder devient `OpenAIDocumentEmbedder` (même `meta_fields_to_embed`, sans préfixe), et on ajoute
son tarif pour journaliser le coût :

```yaml
embedder:
  type: haystack.components.embedders.openai_document_embedder.OpenAIDocumentEmbedder
  init_parameters:
    model: text-embedding-3-small
    meta_fields_to_embed: [context_header]
    api_key: {type: env_var, env_vars: [OPENAI_API_KEY], strict: true}
```

```yaml
# config/settings.yaml
pricing:
  text-embedding-3-small: {input: 0.02}
```

Le chargeur local (`ProjectFileLoader`) et le writer local (`VectorLayerWriter`) sont remplacés dans deepset par
l'entrée fichiers de l'index et un `DocumentWriter` vers le document store de l'espace de travail ; `ProjectChunker`
reste identique.

**À savoir pour deepset :**

- `loader` (`ProjectFileLoader`) et `vector_writer` (`VectorLayerWriter`) sont **retirés** du YAML dans deepset,
  pas édités : leurs paramètres d'init contiennent des chemins locaux et l'empreinte de l'embedder utilisé.
- La connexion `embedder.meta → vector_writer.embed_meta` n'existe que si l'embedder a une sortie `meta`
  (OpenAI : oui ; sentence-transformers : non). En exportant avec l'embedder local puis en passant à OpenAI dans
  deepset, ajouter cette connexion pour garder un comptage de tokens exact (sinon le coût est estimé à partir du
  nombre de mots). Dans l'autre sens (retour à un embedder local), la retirer, sinon le YAML ne se recharge pas.
- Le futur pipeline de requête doit utiliser le même modèle d'embedding (en local, avec le préfixe `query: `).

## Chatbot (agent RAG)

Un troisième pipeline, `chat`, répond aux questions sur les projets à partir de la couche vectorisée. C'est un
`Agent` Haystack qui dispose de quatre outils :

| Outil | Rôle |
|---|---|
| `search_projects` | Recherche sémantique dans les textes des projets, filtres pays, années, programme, domaine ; renvoie des passages numérotés [n] |
| `search_deliverables` | Recherche sémantique dans le texte intégral des livrables (rapports, présentations) : méthodes, chiffres, recommandations ; renvoie des passages [n] avec document et page |
| `list_projects` | Comptage et liste exacts des projets correspondant à des filtres, sur tout le corpus |
| `get_project` | Texte complet d'un projet |

```powershell
# Prérequis : la couche vectorisée (python -m tsi_agent index, chatbot arrêté) et l'extra local
.venv\Scripts\python -m tsi_agent chat            # ouvre http://127.0.0.1:8765
.venv\Scripts\python -m tsi_agent export-yaml --chat chat.yaml
```

Exemples de questions :
- « Que fait le projet Energy Regulation Academy ? »
- « Quels projets aident la Croatie à réformer son système de santé ? »
- « Combien de projets TSI pour la Grèce en 2023 ? »
- « Compare les projets santé de la Croatie et de la Slovénie. », puis « Et en Slovaquie ? »

Chaque réponse cite ses sources [n] vers les pages de la Commission et signale ce qui vient d'un résumé IA. La page
affiche les étapes de l'agent, la durée et le coût équivalent API. Chaque question est un run du journal
(`logs/runs/`, `mode: chat`). En local, une réponse demande 2 à 4 appels au CLI `claude`, soit 20 à 60 s.

Un seul processus à la fois sur la base locale : ne pas lancer `index` pendant que le chatbot tourne.

`export-yaml --chat` construit le pipeline comme la commande `chat` : il exige donc la couche vectorisée déjà
construite et l'extra `[local]` (embedder local pour `search_projects`).

**Passage à deepset :** le `chat_generator` de l'Agent reste un `LoggedChatGenerator` (il journalise coût et
tokens quel que soit le modèle appelé) ; seul le generator qu'il enveloppe change. Dans le YAML exporté,
remplacer :
- `agent.init_parameters.chat_generator.init_parameters.chat_generator` (le `ClaudeCliChatGenerator`) par un
  `OpenAIChatGenerator`, qui appelle les outils nativement ;
- le `text_embedder` des outils `search_projects` et `search_deliverables` par un `OpenAITextEmbedder` (`text-embedding-3-small`, sans
  préfixe), cohérent avec l'embedder de l'index ;
- le `LocalQdrantDocumentStore` (dans les outils et le retriever) et le `LocalQdrantEmbeddingRetriever` par le
  document store de l'espace de travail (OpenSearch) et son retriever (par exemple `OpenSearchEmbeddingRetriever`).

Les quatre outils, le prompt et l'Agent ne changent pas.

## Analyse d'impact

L'onglet « Impact analysis » de la page du chatbot (`python -m tsi_agent chat`) évalue l'impact d'un projet choisi
dans le catalogue (`output/projects/`). Un `Agent` Haystack lit le projet (`get_project`), ses livrables
(`search_deliverables`), cherche des preuves sur le web (`web_search`) et lit les pages ou documents trouvés
(`fetch_page`, HTML, PDF, Word). Il rend un rapport à sections fixes : présentation du projet, preuves d'impact
positif, preuves d'impact négatif ou limites, impact par niveau (pays, Europe, monde), évaluation par objectif et
sources. Les sources sont reconstruites par le code à partir des outils : toute citation [n] inconnue est retirée.

```powershell
.venv\Scripts\python -m tsi_agent chat                                # onglet Impact analysis
.venv\Scripts\python -m tsi_agent export-yaml --impact impact.yaml
```

Une analyse à la fois, en arrière-plan ; la page affiche les étapes en direct. Chaque analyse est un run du journal
(`logs/runs/`, `mode: impact`) et le rapport est enregistré dans `output/impact/<project_id>.md` (front matter :
verdict, confiance, coût, durée), en remplaçant le précédent. Une réponse finale vide ou illisible n'écrase jamais
un rapport existant.

Une fois le rapport écrit, un appel LLM séparé (`impact_score_model`) lit le rapport terminé et lui attribue un score
d'impact positif de 0 à 10 (0-3 Weak, 4-6 Moderate, 7-10 Strong) avec une justification ; la page l'affiche en
jauge. Pour recalculer le score des rapports existants sans relancer l'analyse :

```powershell
.venv\Scripts\python -m tsi_agent impact-rescore                    # tous les rapports de output/impact
.venv\Scripts\python -m tsi_agent impact-rescore <project_id> ...
```

Réglages (`config/settings.yaml`) : `impact_model`, `impact_score_model`, `impact_max_steps` (appels LLM par analyse),
`impact_max_searches` (recherches web par analyse), `impact_page_words` (texte d'une page transmis à l'agent),
`impact_page_max_mb` (taille maximale d'une page téléchargée), `impact_project_chars`, `impact_llm_timeout_s`,
ainsi que `web_search_model` et `web_search_timeout_s` pour la recherche web. `fetch_page` ne lit que des URL
http(s) publiques (adresses locales et privées refusées, y compris après redirection) et s'arrête au-delà de
`impact_page_max_mb` Mo ou de 20 s.

En local, la recherche web passe par le CLI `claude` (`ClaudeWebSearch`, outil natif WebSearch) et le LLM par
`ClaudeCliChatGenerator`.

**Passage à deepset :** le YAML exporté contient `ClaudeWebSearch` et `ClaudeCliChatGenerator` ; on les remplace à la
main :
- le `search_engine` de l'outil `web_search` (`ClaudeWebSearch`) par `SerperDevWebSearch` (package
  `serperdev-haystack`, clé API Serper), qui a la même interface (`query` → `documents`, `links`) ;
- le `chat_generator` enveloppé par le `LoggedChatGenerator` de l'Agent par un `OpenAIChatGenerator` ;
- comme pour le chatbot, l'embedder et le document store des outils `get_project` et `search_deliverables`.

Le lecteur de pages (`BoundedLinkFetcher`) peut rester tel quel ou être remplacé par le `LinkContentFetcher` de
Haystack (sans limite de taille ni filtrage des adresses).

## Banc de recherche

Mesure si la recherche du chatbot retrouve le bon passage et le bon fichier (spec :
`docs/superpowers/specs/2026-10-08-tsi-retrieval-bench-design.md`). **Arrêter le chatbot avant** (une seule
ouverture de la base Qdrant).

```powershell
.venv\Scripts\python -m tsi_agent eval-retrieval                      # questions réutilisées si elles existent
.venv\Scripts\python -m tsi_agent eval-retrieval --regenerate         # nouveau jeu de questions (Haiku)
.venv\Scripts\python -m tsi_agent eval-retrieval --skip-chunk-sizes   # sans la mesure des tailles (~30 min)
```

150 morceaux tirés au hasard (graine fixe : 60 projets, 60 livrables, 30 programme ou résumés IA ; options
`--n-project`, `--n-deliverable`, `--n-other`), une question par morceau rédigée par Haiku (moitié français, moitié
anglais), enregistrée dans `output/eval/questions.jsonl` et réutilisée ensuite. Quatre variantes sont mesurées sur
l'index actuel : A sémantique (actuel), B + filtre pays, C hybride sémantique + BM25 (RRF), D au plus 2 morceaux par
fichier ; puis trois tailles de morceaux (150 / 250 / 400 mots) sur les projets et le programme, revectorisés dans un
index temporaire en mémoire. Sorties : `output/eval/retrieval-<date>.md` (rapport) et `.csv` (rangs par question).

## Banc d'agrégation

Mesure si le chatbot retrouve **tous** les pays ou projets d'une question globale (« Quels pays ont des projets sur
la santé ? »). **Arrêter le chatbot avant.**

```powershell
.venv\Scripts\python -m tsi_agent eval-aggregation                   # vérité des thèmes réutilisée si elle existe
.venv\Scripts\python -m tsi_agent eval-aggregation --regenerate      # refaire la vérité des thèmes (juge Sonnet)
.venv\Scripts\python -m tsi_agent eval-aggregation --metadata-only   # ou --topics-only
```

12 questions sur les métadonnées (pays par domaine, projets d'un pays dans un domaine, comptages ; vérité exacte tirée
du catalogue des projets) et 16 sur 8 thèmes libres (cancer, hydrogène, feux de forêt, blanchiment, cybersécurité,
retraites, intégration des migrants, justice numérique ; vérité = candidats par mots-clés + 300 premiers morceaux
sémantiques, jugés par Sonnet, enregistrée dans `output/eval/aggregation_truth.json`). Chaque question passe par le
vrai chatbot ; rappel, précision et F1 des pays cités (noms anglais/français, codes ISO) et des projets cités
(sources, titres, identifiants). Sorties : `output/eval/aggregation-<date>.md` et `.csv`.

## Rapport d'anomalies

Contrôle de publication, sans LLM ni réseau, à partir de `output/` (spec :
`docs/superpowers/specs/2026-10-12-csr-linking-anomalies-design.md`).

```powershell
.venv\Scripts\python -m tsi_agent anomalies [--out DIR] [--year N]
```

Écrit `output/reports/anomalies.csv` (UTF-8 avec BOM, pour Excel) et `anomalies.md`. Types d'anomalies :
`tsi_not_published` (gravité selon l'âge, `--year` fixe l'année de référence), `srsp_title_only`,
`published_no_deliverable`, `missing_results`, `orphan_deliverable`, `possible_duplicate`.

## Explorateur web (Supabase + GitHub Pages)

Page statique, réservée aux testeurs connectés, pour parcourir les projets, les CSR (avec leurs sous-parties et les
projets qui les soutiennent), les liens projet → CSR évalués par IA et les anomalies de publication (spec :
`docs/superpowers/specs/2026-10-13-web-explorer-design.md`).

- **URL :** <https://mge-agilos.github.io/dg-reform-tsi/>
- **Code :** `web/` (HTML, CSS et JavaScript sans build ; `supabase-js` v2 figé, chargé depuis jsDelivr). La lecture
  passe par la clé `anon`, publique par nature, dans `web/config.js` ; les règles RLS ne laissent lire que les testeurs.

Mise en place (une fois) :

1. **Schéma :** coller `supabase/migrations/001_explorer.sql` dans *Supabase > SQL Editor* et l'exécuter (rejouable).
2. **Inscription sur invitation seulement :** dans *Authentication > Sign In / Providers*, désactiver
   « Allow new users to sign up » et garder « Confirm email » actif. La page demande le lien sans créer de compte
   (`shouldCreateUser: false`) : un e-mail non invité reçoit « This e-mail is not invited ».
3. **Testeurs :** pour chacun, *Authentication > Users > Invite user* (le testeur confirme son e-mail par le lien
   d'invitation), puis l'ajouter à `testers` (e-mail en minuscules) dans le *SQL Editor* :
   ```sql
   insert into public.testers (email) values ('prenom.nom@example.org') on conflict do nothing;
   ```
4. **Auth :** *Authentication > URL Configuration* : *Site URL* = l'URL de la page ci-dessus ; *Redirect URLs* =
   `https://mge-agilos.github.io/dg-reform-tsi/**` et, pour les essais en local, `http://localhost:8000/**`.
5. **Clé `anon` :** la reporter dans `web/config.js` (*Project Settings > API*). Sans elle, la page affiche
   « Configuration required ».
6. **Pages :** *Settings > Pages* du dépôt GitHub, source « GitHub Actions ». Le workflow
   `.github/workflows/pages.yml` publie `web/` à chaque push sur `main` qui touche `web/` (ou à la main, via
   *Run workflow*).

Mise à jour des données, après chaque mise à jour du corpus (`run`, `deliverables`, `csr-build`, `csr-link`…) :

```powershell
.venv\Scripts\python -m tsi_agent supabase-sync
```

La commande remplace le contenu des tables (sauf `testers`) et sauvegarde d'abord les lignes envoyées dans
`output/state/supabase_rows.json`. Elle lit `SUPABASE_URL` et `SUPABASE_SERVICE_KEY` (clé `service_role`) dans
l'environnement ou dans `.env`. **Cette clé ne doit jamais être commitée** (`.env` est dans `.gitignore`) ni copiée
dans `web/`.

Liste de contrôle après déploiement :

- un testeur reçoit le lien magique, se connecte et voit les onglets *Projects*, *CSRs* et *Anomalies* ;
- un e-mail non invité voit « This e-mail is not invited » ; un utilisateur invité mais absent de `testers` voit
  « Access not authorised for this e-mail » ;
- un appel REST anonyme ne renvoie aucune donnée : une erreur 401 / *permission denied* (droits retirés à `anon`)
  ou `[]`, les deux signifiant « pas de données » :
  ```powershell
  curl.exe "https://vxwmhsgxoomcbiukeinp.supabase.co/rest/v1/projects?select=project_id&limit=1" -H "apikey: <cle anon>"
  ```
- l'export CSV de l'onglet *Anomalies* s'ouvre dans Excel (séparateur `;`, UTF-8 avec BOM ; 5 000 lignes au plus).

Les résumés et les liens projet → CSR sont générés par IA : la page le signale et ils sont à vérifier avant usage.

### Chat et impact (lot 2)

Les onglets *Chat* et *Impact* de l'explorateur interrogent les services `tsi-chat` et `tsi-impact` de la Haystack
Enterprise Platform, par l'Edge Function Supabase `haystack-proxy` (spec :
`docs/superpowers/specs/2026-10-13-web-chat-impact-design.md`). La fonction vérifie que l'appelant est un testeur
(`is_tester()` appelée avec son jeton), puis relaie `{model, messages, stream: false}` vers l'endpoint compatible OpenAI
du service. La clé Haystack reste dans les secrets de la fonction : elle n'apparaît ni dans la page ni dans le dépôt.

Mise en service (une fois) :

1. **Gateway.** Pour chaque service (`tsi-chat`, `tsi-impact`), copier depuis sa page Gateway l'URL complète de
   l'endpoint compatible OpenAI (`.../chat/completions`) et, si l'exemple de la page l'indique, la valeur `model`.
2. **Fonction.** Dans Supabase, *Edge Functions > Deploy a new function > Via Editor* : nommer la fonction
   `haystack-proxy`, coller le contenu de `supabase/functions/haystack-proxy/index.ts` (fichier unique, sans import),
   garder *Verify JWT* activé, puis *Deploy*.
3. **Secrets.** Dans *Edge Functions > Secrets*, renseigner `HAYSTACK_API_KEY` (clé API du workspace `tsi-reform`),
   `HAYSTACK_CHAT_URL`, `HAYSTACK_IMPACT_URL`, et si besoin `HAYSTACK_CHAT_MODEL` et `HAYSTACK_IMPACT_MODEL` (par défaut
   `tsi-chat` et `tsi-impact`). `ALLOWED_ORIGINS` est facultatif (par défaut : `https://mge-agilos.github.io` et
   `http://localhost:8000`, séparés par des virgules). `SUPABASE_URL` et `SUPABASE_ANON_KEY` sont fournies
   automatiquement.
4. **Test.** Avec le jeton de session d'un testeur (par exemple `access_token` de la session, visible dans les outils de
   développement du navigateur une fois connecté) :

   ```bash
   curl -X POST <SUPABASE_URL>/functions/v1/haystack-proxy -H "Authorization: Bearer <jeton de session testeur>" -H "apikey: <anon>" -H "Content-Type: application/json" -d '{"service":"chat","messages":[{"role":"user","content":"How many TSI projects does Greece have?"}]}'
   ```

   Réponse attendue : `{"answer": "..."}`. Un utilisateur non testeur reçoit 403 `Access not authorised`.
5. **Préalable plateforme.** Pour que le chat connaisse les CSR et les liens : supprimer les anciens fichiers `tsi-*`
   du workspace, téléverser `deploy/prod/build/files` (3 632 fichiers), puis mettre à jour et republier `tsi-chat`
   avec `pipeline-tsi-chat.yaml`. Sans cette mise à jour, le chat fonctionne mais ignore les CSR.

Comportement de la fonction : 400 si la requête est invalide (service `chat` ou `impact`, 1 à 20 messages, rôles
`user`/`assistant`, 8 000 caractères au plus par message, dernier message de l'utilisateur), 401 sans jeton, 403 si
l'appelant n'est pas testeur, 502 si la Gateway répond en erreur (avec un extrait de 300 caractères de sa réponse),
504 au-delà de 140 s. Elle ne journalise que le service, le statut et la durée, jamais les questions ni les clés.

Côté page : la conversation est gardée dans l'onglet du navigateur (`sessionStorage`, 20 messages au plus) et effacée
à la déconnexion ; le dernier rapport d'impact de chaque projet aussi. Les réponses Markdown passent par `marked`
puis `DOMPurify` (versions figées, jsDelivr) ; si ces bibliothèques ne se chargent pas, le texte brut est affiché.

Liste de contrôle après déploiement :

- l'onglet *Chat* propose quatre questions d'exemple ; une question reçoit une réponse en moins de 2 minutes, avec
  des liens qui s'ouvrent dans un nouvel onglet ; *New conversation* vide l'historique ;
- une erreur (service arrêté, délai dépassé) s'affiche dans la conversation avec un bouton *Retry*, sans perdre la
  question ;
- depuis la fiche d'un projet, *Impact analysis* ouvre l'onglet *Impact* ; *Run impact analysis* produit un rapport
  qui se termine par le score d'impact, et *Copy report* le copie ;
- un utilisateur invité mais absent de `testers` reçoit « Access not authorised ».

Tests de la fonction (Node 22, sans réseau : `fetch` est simulé) :

```bash
node --experimental-strip-types --test supabase/functions/haystack-proxy/index.test.ts
```

## Planification (Windows)

À exécuter depuis le dossier du dépôt (les chemins sont résolus en absolu à partir du dossier courant) :

```powershell
$repo = (Get-Location).Path
$python = "$repo\.venv\Scripts\python.exe"
$chain = "& '$python' -m tsi_agent run; & '$python' -m tsi_agent deliverables; & '$python' -m tsi_agent index"
$action = New-ScheduledTaskAction -Execute "powershell.exe" -Argument "-NoProfile -Command `"$chain`"" -WorkingDirectory $repo
$trigger = New-ScheduledTaskTrigger -Weekly -DaysOfWeek Monday -At 7am
Register-ScheduledTask -TaskName "TSI Extraction Agent" -Action $action -Trigger $trigger
```

Les trois commandes s'enchaînent dans cet ordre (`run`, puis `deliverables`, puis `index`) : chacune dépend des
fichiers écrits par la précédente.

Le journal du run planifié est dans `logs/` (`logs/runs/<run_id>.jsonl` et `.summary.json`), comme pour un run
manuel. Code de sortie `1` : le run s'est arrêté sur la limite de la souscription, et le suivant reprendra.
Code `2` : erreur à examiner dans le journal.

## Passage à deepset Studio (API OpenAI)

Le pipeline est un pipeline Haystack standard (`export-yaml`). En production, le LLM passe par l'API
OpenAI : dans le YAML, on remplace le `chat_generator` de `extractor` par
`haystack.components.generators.chat.openai.OpenAIChatGenerator` :

```yaml
extractor:
  type: tsi_agent.components.extractor.ProjectExtractor
  init_parameters:
    chat_generator:
      type: haystack.components.generators.chat.openai.OpenAIChatGenerator
      init_parameters:
        model: gpt-4o-mini
        api_key: {type: env_var, env_vars: [OPENAI_API_KEY], strict: true}
        generation_kwargs:
          response_format: {type: json_object}
    pricing:
      gpt-4o-mini: {input: 0.15, output: 0.60}   # $ / million de tokens, pour journaliser le coût
```

Le reste ne change pas. Le composant de stockage local (`LocalProjectStore`) sera remplacé par une
implémentation qui écrit dans l'espace de travail deepset (hors périmètre de cette version).
Le package `tsi_agent` doit être installé côté deepset en tant que composant custom. Au rechargement
d'un YAML, Haystack 3 exige `allowed_modules=["tsi_agent"]` (voir `tsi_agent.pipeline.load_pipeline`).

### Ce qui ne passe pas par le YAML

Remplacer le `chat_generator` dans le YAML suffit pour le LLM (vérifié : `OpenAIChatGenerator` se recharge
bien). Ce n'est pas le cas de tout le reste :

- **La boucle par lots, le journal de run (JSONL, résumé, `costs.csv`) et la reprenabilité vivent dans
  `runner.py`**, en dehors du pipeline Haystack. Dans deepset, ce code n'est pas exécuté : les événements de
  `runlog` retombent alors sur le `logging` standard, et il n'y a ni fichier de run ni `costs.csv`.
- **`detector.run_id` et `writer.run_id` sont des entrées obligatoires du pipeline** (pas des paramètres
  d'init) : il faut les fournir explicitement à chaque exécution du pipeline dans deepset.
- **`output_dir`, `cache_dir` et `executable` sont des paramètres d'init locaux**, propres à l'exécution sur
  le poste de l'utilisateur.
- **Remplacer le stockage local par l'espace de travail deepset demande du code**, pas seulement du YAML :
  une nouvelle implémentation de `ProjectStore` (voir `store.py`), branchée dans `ChangeDetector`,
  `DetailPageFetcher` et `MarkdownProjectWriter`.

## Tests

```powershell
.venv\Scripts\python -m pytest -q
node --experimental-strip-types --test supabase/functions/haystack-proxy/index.test.ts
```

Les fixtures de `tests/fixtures/` sont des copies réelles des pages du site (septembre 2026).
