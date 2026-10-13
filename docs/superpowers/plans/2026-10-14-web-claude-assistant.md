# Assistant Claude dans l'explorateur web (lot 2b) — design et plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Objectif :** faire fonctionner les onglets **Chat** et **Impact** de la page GitHub Pages sans l'environnement client. Une Edge Function Supabase `tsi-assistant` les sert, avec Claude (API Anthropic, secret `ANTHROPIC_API_KEY` déjà présent dans le projet). Ses outils interrogent les tables Supabase. L'impact s'appuie en plus sur l'outil web search d'Anthropic. Le relais `haystack-proxy` (lot 2) reste dans le dépôt pour plus tard.

**Design validé le 2026-10-14 :**
- **Contrat identique à `haystack-proxy`.** La requête `POST {service: "chat"|"impact", messages}` porte le jeton de session du testeur et renvoie `{answer}`. Les règles sont les mêmes :
  - CORS, avec la liste blanche `ALLOWED_ORIGINS` ;
  - jeton obligatoire (401 sinon) ;
  - testeur vérifié par `rpc/is_tester` (403 sinon) ;
  - validation : 20 messages au plus, 8 000 caractères par message et 40 000 au total, rôles `user` et `assistant` uniquement, dernier message de rôle `user`.

  La page change seulement `PROXY_URL` et les mentions sur l'IA.
- **Outils du chat.** Ils sont exécutés par la fonction via PostgREST, **avec le jeton du testeur**, si bien que le RLS s'applique :
  - `search_projects(query, countries?, programme?, year_from?, year_to?, limit≤20)` : RPC `search_projects`, plein texte sur le titre et le résumé ;
  - `list_projects(countries?, programme?, year_from?, year_to?, policy_area?, detail_only?, title_contains?, csr?, limit≤50)` : comptes exacts (total, par programme, par année, par pays, par domaine) et liste tronquée, avec le filtre `csr` par identifiant ou préfixe, sans faux préfixe et sans tenir compte de la casse ;
  - `get_project(project_id)` : la fiche du projet et ses liens CSR (niveau, justification, texte de la sous-partie, évaluation) ;
  - `search_csrs(query, countries?, year_from?, year_to?, limit≤20)` : RPC `search_csrs` ;
  - `get_csr(csr_id)` : la CSR, ses sous-parties et les projets liés.
- **Impact.** Outils `get_project` et `search_projects`, plus l'outil serveur d'Anthropic `{"type": "web_search_20250305", "name": "web_search", "max_uses": 4}`. Le prompt reprend la méthode, les règles, les sections et **la grille de score** de `impact_prompt` (`deploy/prod/build_pipelines.py`), avec un projet lu par `get_project` à la place de `search_projects`/`search_deliverables`.
- **Modèle et limites.**
  - `TSI_MODEL` vaut `claude-sonnet-5-5` par défaut ; `max_tokens` vaut 4 096 pour le chat et 6 000 pour l'impact.
  - 8 tours de boucle d'outils au maximum.
  - Budget de 115 s : au-delà, on fait un dernier appel sans outils (`tool_choice: {"type": "none"}`) pour obtenir une réponse.
  - L'appel à Anthropic est limité à 60 s.
  - Si la réponse arrive avec `stop_reason` égal à `pause_turn` (outil serveur long), on relance avec le contenu reçu.
- **Hors périmètre :** le texte intégral des livrables, qui n'est pas dans Supabase. Le prompt du chat le dit (« deliverable full texts are not available here »).

---

### Task 1 : recherche plein texte (migration `002_search.sql`)

**Fichier :** à créer, `supabase/migrations/002_search.sql`. Le script est idempotent.

```sql
-- Recherche plein texte pour l'assistant (lot 2b). Idempotent.
alter table public.projects add column if not exists fts tsvector
  generated always as (to_tsvector('english', coalesce(title, '') || ' ' || coalesce(summary, ''))) stored;
create index if not exists projects_fts_idx on public.projects using gin (fts);
alter table public.csrs add column if not exists fts tsvector
  generated always as (to_tsvector('english', coalesce(title, '') || ' ' || coalesce(text, ''))) stored;
create index if not exists csrs_fts_idx on public.csrs using gin (fts);

create or replace function public.search_projects(q text, p_countries text[] default null,
  p_programme text default null, p_year_from int default null, p_year_to int default null, lim int default 10)
returns table (project_id text, title text, programme text, year int, countries text[], policy_area text,
               detail_page boolean, url text, summary text, csrs text[], rank real)
language sql stable security invoker set search_path = public as $$
  select p.project_id, p.title, p.programme, p.year, p.countries, p.policy_area, p.detail_page, p.url,
         left(p.summary, 600), p.csrs, ts_rank(p.fts, websearch_to_tsquery('english', q))
  from public.projects p
  where (p.fts @@ websearch_to_tsquery('english', q) or p.title ilike '%' || q || '%')
    and (p_countries is null or p.countries && p_countries)
    and (p_programme is null or p.programme = upper(p_programme))
    and (p_year_from is null or p.year >= p_year_from)
    and (p_year_to is null or p.year <= p_year_to)
  order by 11 desc, p.year desc nulls last
  limit least(greatest(coalesce(lim, 10), 1), 20);
$$;

create or replace function public.search_csrs(q text, p_countries text[] default null,
  p_year_from int default null, p_year_to int default null, lim int default 10)
returns table (csr_id text, country text, year int, title text, text text, assessment text,
               assessment_year int, rank real)
language sql stable security invoker set search_path = public as $$
  select c.csr_id, c.country, c.year, c.title, left(c.text, 800), c.assessment, c.assessment_year,
         ts_rank(c.fts, websearch_to_tsquery('english', q))
  from public.csrs c
  where c.fts @@ websearch_to_tsquery('english', q)
    and (p_countries is null or c.country = any (p_countries))
    and (p_year_from is null or c.year >= p_year_from)
    and (p_year_to is null or c.year <= p_year_to)
  order by 8 desc, c.year desc
  limit least(greatest(coalesce(lim, 10), 1), 20);
$$;

revoke all on function public.search_projects(text, text[], text, int, int, int) from public, anon;
revoke all on function public.search_csrs(text, text[], int, int, int) from public, anon;
grant execute on function public.search_projects(text, text[], text, int, int, int) to authenticated, service_role;
grant execute on function public.search_csrs(text, text[], int, int, int) to authenticated, service_role;
```

Points à vérifier :
- **Synchronisation :** `supabase-sync` n'envoie jamais la colonne générée `fts`. On le contrôle en lisant `build_rows`.
- **RLS :** les fonctions sont en `security invoker`, donc le RLS des testeurs s'applique.
- **Commit :** `feat(web): recherche plein texte projets et CSR (fts, search_projects, search_csrs)`.

### Task 2 : Edge Function `tsi-assistant` et ses tests

**Fichiers :**
- `supabase/functions/tsi-assistant/index.ts` : un seul fichier, sans import, collable dans l'éditeur Supabase ou déployable par l'API ;
- `supabase/functions/tsi-assistant/index.test.ts` : à lancer avec `node --experimental-strip-types --test supabase/functions/tsi-assistant/index.test.ts`.

**À reprendre de `supabase/functions/haystack-proxy/index.ts` en adaptant :**
- `HttpError` ;
- `corsHeaders` (avec `Access-Control-Max-Age`) et `allowedOrigins` ;
- `validate` (avec `MAX_TOTAL`) ;
- le contrôle `is_tester` (clé anon de repli depuis l'en-tête `apikey`, journalisation de `rpcStatus` uniquement) ;
- la gestion des délais ;
- le journal `{service, status, ms, steps}` uniquement : aucun contenu, aucune clé.

**Exports :**
- `CHAT_SYSTEM` et `impactSystem()`, les prompts (en anglais). `CHAT_SYSTEM` reprend l'esprit du prompt de production (`CHAT_PROMPT` dans `deploy/prod/build_pipelines.py`), adapté aux outils :
  - répondre dans la langue de la question ;
  - n'utiliser que les résultats des outils ;
  - pour toute question de nombre ou de liste, appeler `list_projects` ;
  - pour un sujet libre, combiner `search_projects` (plusieurs formulations) et `list_projects` (`title_contains`) ;
  - citer chaque projet par un lien Markdown vers son URL, et chaque CSR par son identifiant ;
  - signaler que les liens projet → CSR et les résumés sont générés par une IA ;
  - préciser que les projets sans page détail ne sont connus que par leur titre ;
  - préciser que le texte intégral des livrables n'est pas disponible ici ;
  - rester concis (environ 250 mots) ;
  - écrire les requêtes des outils en anglais.

  Les valeurs des filtres sont : codes pays ISO de l'UE-27 (Grèce = `GR`), programmes `TSI`, `SRSP` et `SRSS`, et les 10 slugs de domaines (voir `web/app.js`).
- `TOOLS_CHAT` et `TOOLS_IMPACT` : les schémas JSON des outils. Pour l'impact, `TOOLS_IMPACT` contient `get_project`, `search_projects` et l'outil serveur `web_search_20250305` avec `max_uses: 4`.
- `runTool(name, input, ctx)` : exécute un outil client via PostgREST et renvoie un **texte** compact. Le contexte `ctx` contient `{supabaseUrl, apikey, userAuth, fetchImpl}`. Les entrées sont validées (limites bornées, types). Une erreur renvoie le texte `"Tool error: ..."`, sans lever d'exception.
  - `list_projects` récupère les colonnes légères (`project_id,title,programme,year,countries,policy_area,detail_page,url,csrs`) avec les filtres côté serveur, par pages de 1 000. Elle applique côté code les filtres `csr` et `title_contains`, puis calcule les répartitions.
- `agentLoop(service, messages, env, fetchImpl, now)` : la boucle Anthropic.
  - Appel : `POST https://api.anthropic.com/v1/messages`, avec les en-têtes `x-api-key: env.ANTHROPIC_API_KEY`, `anthropic-version: 2023-06-01` et `content-type`.
  - Corps : `{model, max_tokens, system, tools, messages}`.
  - Tant que `stop_reason === "tool_use"` (ou `"pause_turn"`) et qu'on n'a pas dépassé 8 tours : ajouter le contenu de l'assistant tel quel, y compris `server_tool_use` et `web_search_tool_result`, puis un message `user` avec les `tool_result` des outils **clients** seulement.
  - Si le budget de 115 s est dépassé, faire un dernier appel avec `tool_choice: {type: "none"}`.
  - Résultat : le texte des blocs `text` de la dernière réponse, joints par une ligne vide. S'il est vide, lever une `HttpError(502, "Empty response from the model")`.
  - Une erreur Anthropic donne une `HttpError(502, "Model service error <status>")`. Le détail est journalisé côté serveur, sans contenu utilisateur.
  - Si `ANTHROPIC_API_KEY` manque : `HttpError(500, "ANTHROPIC_API_KEY not configured")`.
- `handle(req, env, fetchImpl)` : le même déroulé que le relais, puis `agentLoop`.
- En fin de fichier : `declare const Deno: any; if (typeof Deno !== "undefined") Deno.serve(...)`.

**Tests**, avec un faux `fetch` qui répond selon l'URL (`/rpc/is_tester`, `/rest/v1/...` et `api.anthropic.com`). Ils couvrent :
- CORS, 401, 403 et 400, comme pour le relais ;
- une boucle avec un `tool_use` `list_projects`, puis une réponse finale : on vérifie le message `tool_result`, les en-têtes PostgREST (jeton de l'utilisateur et `apikey`) et le texte renvoyé ;
- le filtre `csr` sans faux préfixe : `csr-HR-2022-1` ne doit pas trouver `csr-HR-2022-10.1` ;
- pour l'impact, la présence de l'outil serveur `web_search` (avec `max_uses: 4`) ;
- la recopie des blocs `server_tool_use` à l'identique ;
- `pause_turn` ;
- l'arrêt au bout de 8 tours ;
- le budget de temps dépassé, qui déclenche l'appel avec `tool_choice: none` (via une horloge `now` injectée) ;
- une erreur Anthropic 500, qui donne un 502 sans détail ;
- une clé manquante, qui donne un 500 ;
- `runTool` avec un outil inconnu, qui renvoie `Tool error`.

**Commit :** `feat(web): Edge Function tsi-assistant (Claude + outils Supabase, recherche web pour l'impact)`.

### Task 3 : page

- `web/config.js` :

  ```js
  export const PROXY_URL = SUPABASE_URL + "/functions/v1/tsi-assistant";
  ```

  Garder un commentaire indiquant l'URL alternative `haystack-proxy`.
- Mentions du pied de page et des onglets : remplacer « gpt-5.1 via EC GPT » par « Claude (Anthropic) ».
  - Chat : « Answers use project records, AI summaries, CSRs and AI-assessed links; deliverable full texts are not searched here. »
  - Impact : « Takes 30 s to 2 min; uses web search (Anthropic) ».
- Le 4e exemple du chat reste « Which projects support Croatia's 2022 country-specific recommendations? ».
- Lancer `node --check web/app.js`.
- **Commit :** `feat(web): Chat et Impact servis par tsi-assistant (Claude)`.

### Task 4 : README

Dans la section du lot 2, ajouter une sous-section « Assistant Claude (lot 2b) » qui couvre :
- l'architecture ;
- le secret `ANTHROPIC_API_KEY`, partagé dans le projet Supabase, et la variable facultative `TSI_MODEL` ;
- le déploiement, en deux étapes :
  - la migration `002_search.sql` ;
  - la fonction, soit par l'éditeur Supabase, soit par l'API de gestion avec `SUPABASE_ACCESS_TOKEN` (`.env`) ;
- les limites : pas de livrables, 8 étapes, environ 2 minutes ;
- les coûts indicatifs ;
- le retour à `haystack-proxy` en changeant `PROXY_URL`.

**Commit :** `docs(web): assistant Claude (lot 2b)`.

### Task 5 : mise en service (contrôleur)

1. Appliquer `002_search.sql` par l'API de gestion : `POST https://api.supabase.com/v1/projects/vxwmhsgxoomcbiukeinp/database/query` avec `{"query": ...}`. Le jeton `SUPABASE_ACCESS_TOKEN` vient de `.env` et n'est jamais affiché.
2. Déployer la fonction : `POST /v1/projects/{ref}/functions/deploy?slug=tsi-assistant`, en multipart avec :
   - `metadata` : `{"entrypoint_path": "index.ts", "name": "tsi-assistant", "verify_jwt": true}` ;
   - `file` : `index.ts`.
3. Vérifier :
   - les fonctions listées ;
   - `OPTIONS` depuis l'origine Pages, qui doit répondre 204 ;
   - un `POST` sans jeton, qui doit répondre 401 ;
   - un `POST` avec la clé anon comme jeton, qui doit répondre 401 ou 403 ;
   - l'appel `rpc/search_projects` avec la clé `service_role`, qui doit renvoyer des résultats.
4. Pousser `main`, puis vérifier le workflow Pages. L'essai final dans la page est fait par l'utilisateur.
