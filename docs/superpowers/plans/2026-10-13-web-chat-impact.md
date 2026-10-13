# Chat et analyse d'impact dans l'explorateur web (lot 2) : plan d'implémentation

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Objectif :** ajouter les onglets Chat et Impact à `web/`, derrière une Edge Function Supabase qui relaie vers les services Gateway `tsi-chat` et `tsi-impact` de Haystack Enterprise.

**Architecture :** `supabase/functions/haystack-proxy/index.ts` est un seul fichier, sans import, pour être collé dans l'éditeur Supabase.
- **Logique pure exportée** : validation, CORS, requête vers la Gateway, extraction de la réponse. Elle est testable avec Node 22 (`--experimental-strip-types`).
- **`Deno.serve`** n'est appelé que si `Deno` existe.
- **Vérification du testeur** : par `fetch` vers `${SUPABASE_URL}/rest/v1/rpc/is_tester`, avec la clé anon et le jeton de l'utilisateur.

La page gagne deux vues. Le rendu Markdown passe par `marked` et `DOMPurify`, chargés depuis jsDelivr en version figée.

**Spec :** `docs/superpowers/specs/2026-10-13-web-chat-impact-design.md`.

**Conventions :**
- branche `feature/web-chat`, créée depuis `main` ;
- commits en français, terminés par `Co-Authored-By: Claude Opus 5.5 (1M context) <noreply@anthropic.com>` ;
- aucun secret dans un fichier suivi ;
- le push est fait par l'utilisateur.

---

### Task 1 : Edge Function `haystack-proxy` et ses tests

**Files :**
- Create : `supabase/functions/haystack-proxy/index.ts`
- Test : `supabase/functions/haystack-proxy/index.test.ts` (Node 22 : `node --experimental-strip-types --test supabase/functions/haystack-proxy/index.test.ts`)

Fonctions exportées :
- **`SERVICES`** : `{ chat: { urlVar: "HAYSTACK_CHAT_URL", modelVar: "HAYSTACK_CHAT_MODEL", defaultModel: "tsi-chat" }, impact: { ...IMPACT..., defaultModel: "tsi-impact" } }`.
- **`corsHeaders(origin, allowed)`** :
  - renvoie `Access-Control-Allow-Origin` égal à `origin` s'il figure dans `allowed`, sans en-tête sinon ;
  - ajoute `Vary: Origin`, `Access-Control-Allow-Headers: authorization, apikey, content-type, x-client-info` et `Access-Control-Allow-Methods: POST, OPTIONS`.
- **`allowedOrigins(env)`** : `env.ALLOWED_ORIGINS` découpé sur les virgules. Valeurs par défaut : `https://mge-agilos.github.io` et `http://localhost:8000`.
- **`validate(body)`** : renvoie `{ service, messages }` ou lève une `HttpError(400, msg)`. Règles :
  - `service` est `chat` ou `impact` ;
  - `messages` est un tableau de 1 à 20 éléments ;
  - chaque élément a un `role` (`user` ou `assistant`) et un `content` de type chaîne, non vide, d'au plus 8 000 caractères ;
  - le dernier message a le rôle `user`.
- **`gatewayRequest(service, messages, env)`** : renvoie `{ url, init }`.
  - Lève une `HttpError(500, "<VAR> not configured")` si l'URL ou `HAYSTACK_API_KEY` manque.
  - Corps : `{ model: env[modelVar] || defaultModel, messages, stream: false }`.
  - En-têtes : `Authorization: Bearer <key>` et `Content-Type: application/json`.
- **`extractAnswer(json)`** : renvoie `json.choices?.[0]?.message?.content`, ou lève une `HttpError(502, "Unexpected response from the analysis service")`.
- **`handle(req, env, fetchImpl)`** : la logique complète, testable avec un faux `fetch`.
  1. `OPTIONS` : renvoie 204 avec les en-têtes CORS.
  2. Toute méthode autre que `POST` : 405.
  3. Sans en-tête `Authorization: Bearer` : 401.
  4. Appel de `is_tester` : `fetchImpl(`${env.SUPABASE_URL}/rest/v1/rpc/is_tester`, POST)`, avec les en-têtes `apikey: env.SUPABASE_ANON_KEY`, `Authorization: <jeton de l'utilisateur>` et le corps `{}`. Si la réponse n'est pas ok, ou si son JSON n'est pas `true` : 403 `{error: "Access not authorised"}`.
  5. `validate`, puis appel de la Gateway avec `AbortSignal.timeout(140000)`.
     - Délai dépassé : 504 `{error: "The analysis service timed out"}`.
     - Statut de la Gateway différent de 2xx : 502 `{error: "Analysis service error <status>", detail: <texte de la réponse, 300 caractères au plus>}`.
  6. 200 `{answer}`.
  - Toutes les réponses portent les en-têtes CORS et `Content-Type: application/json`.
  - Aucun journal du contenu des messages ni des clés : on ne journalise que le service, le statut et la durée.
- **En fin de fichier** :

  ```ts
  declare const Deno: any;
  if (typeof Deno !== "undefined") Deno.serve((req: Request) => handle(req, Deno.env.toObject(), fetch));
  ```

- [ ] **Step 1 :** écrire les tests, avec `node:test` et `node:assert`. Chaque test appelle `handle` avec un faux `fetchImpl` qui enregistre ses appels. Cas à couvrir :
  - `OPTIONS` avec une origine autorisée → 204 et l'en-tête ACAO ;
  - origine non autorisée → pas d'en-tête ACAO ;
  - sans jeton → 401 ;
  - `is_tester` renvoie `false` → 403, et la Gateway n'est jamais appelée ;
  - corps invalide → 400 (service inconnu, 21 messages, dernier message `assistant`, `content` trop long) ;
  - cas nominal → 200 `{answer}`. La Gateway reçoit `model` `tsi-chat` et `stream: false`, l'en-tête `Bearer` porte la clé Haystack, et la clé ne figure pas dans le corps de la réponse ;
  - impact avec `HAYSTACK_IMPACT_MODEL` défini → ce modèle est utilisé ;
  - Gateway en 500 → 502 avec `detail` ;
  - le faux `fetch` lève une `DOMException` `TimeoutError` → 504 ;
  - l'URL de la Gateway manque → 500 `HAYSTACK_CHAT_URL not configured`.
- [ ] **Step 2 :** vérifier que les tests échouent, puis implémenter, puis vérifier qu'ils passent.
- [ ] **Step 3 :** commit `feat(web): Edge Function haystack-proxy (testeur verifie, relais vers les services Gateway)`.

### Task 2 : onglets Chat et Impact dans `web/`

**Files :** Modify : `web/index.html`, `web/app.js`, `web/style.css`

- **Navigation et configuration.**
  - Ajouter les onglets `#chat` et `#impact` (ou `#impact/<project_id>`).
  - Dans `config.js`, ajouter `export const PROXY_URL = SUPABASE_URL + "/functions/v1/haystack-proxy";`. Il n'y a pas de secret.
- **Appel du relais.** La fonction `callProxy(service, messages)` :
  - récupère la session (`sb.auth.getSession()`) ;
  - envoie un `fetch` en POST vers `PROXY_URL`, avec les en-têtes `Authorization: Bearer <access_token>`, `apikey: SUPABASE_ANON_KEY` et `Content-Type`, et un délai de 150 s via `AbortController` ;
  - renvoie `answer`, ou lève une erreur avec le message de la réponse (`error` et `detail`).
- **Rendu Markdown.**
  - Charger à la demande, depuis jsDelivr en version figée :
    - `https://cdn.jsdelivr.net/npm/marked@12.0.2/lib/marked.esm.js` ;
    - `https://cdn.jsdelivr.net/npm/dompurify@3.1.6/dist/purify.es.mjs`.
  - Fonction `md(text)` : `DOMPurify.sanitize(marked.parse(text))`. Les liens reçoivent `target="_blank"` et `rel="noopener noreferrer"`, par un hook DOMPurify `afterSanitizeAttributes`.
  - Si le CDN échoue, afficher le texte brut échappé.
- **Vue Chat.**
  - Historique conservé dans `sessionStorage` (clé `tsi-chat-history`, 20 messages au plus, les plus anciens supprimés en premier).
  - Bulles utilisateur et assistant. Le contenu de l'assistant est rendu par `md`, celui de l'utilisateur est échappé.
  - Zone de saisie : `textarea` avec `label`, envoi par Entrée, saut de ligne par Maj+Entrée, et bouton Send désactivé pendant l'attente.
  - Indicateur « Thinking… (up to 2 minutes) » avec `role=status`.
  - Bouton « New conversation ».
  - Quatre questions d'exemple, cliquables quand l'historique est vide :
    - « Which countries have projects on wildfires or forest fires? »
    - « What did the deliverables of the Spanish primary care project recommend? »
    - « What CSRs did Croatia receive on public administration since 2022, and how were they assessed? »
    - « Which projects support Croatia's 2022 country-specific recommendations? »
  - En cas d'erreur, un message est affiché dans la conversation, sans perte de la question.
- **Vue Impact.**
  - Champ de recherche de projet : `ilike` sur le titre, 10 résultats, avec le même échappement que l'explorateur.
  - `#impact/<id>` charge le projet.
  - Bouton « Run impact analysis ». Le message envoyé est : `Project: <title> (id <project_id>; <programme> <year>; countries <ISO list>)`.
  - Le rapport est rendu par `md`, avec un bouton « Copy report » (`navigator.clipboard`).
  - Pas de cache serveur. Le dernier rapport est gardé en `sessionStorage` par `project_id`.
  - Mention : « Takes 30 s to 2 min; uses web search ».
- **Fiche projet.** Ajouter un bouton « Impact analysis », qui mène à `#impact/<id>`.
- **Pied de page.** Ajouter « Chat and impact answers are generated by AI (gpt-5.1 via EC GPT): verify the cited sources. »
- **Styles.** Bulles, zone de saisie collée en bas sur mobile si c'est simple, rendu Markdown (titres, listes, tableaux qui défilent horizontalement, `code`), thème sombre et focus visible.
- [ ] `node --check web/app.js`.
- [ ] Commit `feat(web): onglets Chat et Impact (services Haystack via haystack-proxy, Markdown assaini)`.

### Task 3 : documentation et mise en service

- **README.** Ajouter une section « Chat et impact (lot 2) » :
  1. **Gateway.** Pour chaque service (`tsi-chat`, `tsi-impact`), copier l'URL de l'endpoint compatible OpenAI (`.../chat/completions`) et, si l'exemple de la page l'indique, la valeur `model`.
  2. **Fonction.** Dans Supabase, aller dans *Edge Functions > Deploy a new function > Via Editor*, nommer la fonction `haystack-proxy`, coller `supabase/functions/haystack-proxy/index.ts`, garder *Verify JWT* activé, puis *Deploy*.
  3. **Secrets.** Dans *Edge Functions > Secrets*, renseigner `HAYSTACK_API_KEY`, `HAYSTACK_CHAT_URL`, `HAYSTACK_IMPACT_URL`, et si besoin `HAYSTACK_CHAT_MODEL` et `HAYSTACK_IMPACT_MODEL`. `SUPABASE_URL` et `SUPABASE_ANON_KEY` sont fournies automatiquement.
  4. **Test.**

     ```bash
     curl -X POST <SUPABASE_URL>/functions/v1/haystack-proxy -H "Authorization: Bearer <jeton de session testeur>" -H "apikey: <anon>" -H "Content-Type: application/json" -d '{"service":"chat","messages":[{"role":"user","content":"How many TSI projects does Greece have?"}]}'
     ```

  5. **Préalable plateforme.** Supprimer les anciens fichiers `tsi-*`, téléverser `deploy/prod/build/files` (3 632 fichiers), puis mettre à jour et republier `tsi-chat` avec `pipeline-tsi-chat.yaml`.
- **Vérification.** Lancer `.venv/Scripts/python -m pytest -q -p no:cacheprovider` (la suite Python ne doit pas changer) et les tests Node de la fonction.
- [ ] Commit `docs(web): mise en service du chat et de l'analyse d'impact (Gateway, Edge Function, secrets)`.
