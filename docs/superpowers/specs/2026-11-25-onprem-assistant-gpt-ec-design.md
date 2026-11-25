# Assistant of the self-hosted installation on GPT@EC — design

Date: 2026-11-25 · Branch: `feature/onprem-assistant` (from `feature/country-reports` at 53482f4)

## Goal

On the client's server, the explorer's assistant (questions, impact analyses, briefings, related search terms) runs
entirely on that server and on the Commission's GPT@EC gateway. Nothing goes to Supabase or Anthropic.

Today the assistant only exists as the Supabase Edge Function `tsi-assistant` (Anthropic Messages API). On the server,
the application service relays `POST /api/assistant` to an `ASSISTANT_URL` that does not exist, and the page shows
"not available yet".

## Decisions

| Question | Decision |
|---|---|
| Where the assistant runs | Inside the application service (`server/`), same process, no new service |
| Code | The same `tsi-assistant/index.ts`, with a second model connector (OpenAI-compatible) next to the Anthropic one |
| Cloud version | Unchanged: Anthropic stays the default; Supabase keeps running the same file |
| Data access | The tools query the local PostgREST with the user's own token (row-level security applies, as in the cloud) |
| Structured output (briefing, related terms) | `response_format: json_object` with the schema in the system prompt: the mode already verified against GPT@EC by the data update (`llm.py`) |
| Impact analysis | No web search (Anthropic server tools have no GPT@EC equivalent): it relies on the explorer's data |
| Proxy | Calls to GPT@EC go through `HTTPS_PROXY` (with `NO_PROXY`); database calls stay direct |
| Page texts | Two optional `config.js` settings name the AI service and say whether web search exists |

## 1. Model connector in `tsi-assistant/index.ts`

`TSI_LLM_PROVIDER` selects the connector: `anthropic` (default, today's behaviour) or `openai`.

Settings of the `openai` connector (environment of the function):

| Variable | Meaning |
|---|---|
| `TSI_OPENAI_BASE_URL` | Gateway base URL up to `/v1`, e.g. `https://api.tech.ec.europa.eu/ecgpt/v1` (required) |
| `TSI_OPENAI_API_KEY` | Gateway key (required) |
| `TSI_MODEL` | Model, default `gpt-5.1` with this connector (already used for Anthropic) |
| `TSI_EXPAND_MODEL` | Model of the related terms, default `TSI_MODEL` with this connector |
| `TSI_OPENAI_REASONING` | `on` (default): `reasoning_effort` sent from the existing `EFFORT` table; `off`: not sent, for a gateway that refuses it |

The agent loop (`agentLoop`) keeps its logic (steps, time budget, last call without tools, parallel client tools,
limits, statistics). What depends on the API moves behind a small connector interface:

- **Request.** Anthropic: as today. OpenAI: `POST {base}/chat/completions` with `Authorization: Bearer <key>`,
  `model`, the system prompt as the first message, `tools` as `{type: "function", function: {name, description,
  parameters}}` built from the same tool schemas, `tool_choice: "none"` on the last call, `max_completion_tokens`
  from `OPENAI_MAX_TOKENS` (larger than `MAX_TOKENS`: they include the reasoning tokens), and `reasoning_effort` unless turned off. No cache markers (the gateway caches on its own).
- **Reading the answer.** OpenAI `finish_reason`: `tool_calls` is a tool round, `stop` is the final answer, `length`
  is truncation (same note as `max_tokens`; during a tool round the content is empty, so the answer is
  the usual "Empty response" 502), `content_filter` is a refusal (422). Tool arguments are parsed from JSON;
  unreadable arguments give a `Tool error:` result to the model, never an exception.
- **Conversation.** The assistant message is sent back as received (`content`, `tool_calls`); each tool call gets one
  `{role: "tool", tool_call_id, content}` message, unknown tools included (the API refuses a call left unanswered).
- **Usage.** `prompt_tokens`, `completion_tokens` and `prompt_tokens_details.cached_tokens` fill the same statistics.
- **Errors.** Same mapping as today: timeout 504, network 502, HTTP error 502 with only the status and the error
  type or code logged, never the content or the key.

Briefing and related terms: one call with `response_format: {type: "json_object"}` and the instruction
"Answer with a single JSON object that conforms to this JSON schema: …" appended to the system prompt; the existing
`parseBriefing` (fences stripped) and `cleanTerms` read the result.

**Database address.** New variable `TSI_REST_URL`, default `${SUPABASE_URL}/rest/v1`. The tools, the `is_tester`
check and the impact save use it. On the server it is the local PostgREST (`http://127.0.0.1:3001`, no `/rest/v1`).
`SUPABASE_URL` is no longer required when `TSI_REST_URL` is set.

**Impact analysis without web search** (when the connector has no web tools, i.e. `openai`):

- tools: `get_project`, `search_projects`, `get_csr`, `search_csrs`, `search_recitals`, `search_country_reports`;
- prompt: a variant of `impactSystem` whose method reads the project, its linked CSRs and their Commission
  assessments, the 2026 recitals and country reports of the country; the report says that no web search was made
  and that the confidence is limited to the explorer's data; same sections, same score line, same rubric;
- the saved row keeps `source: 'web'` ("run from this site", the only values allowed are `web` and `local`) and
  `model` is the gateway model.

## 2. Application service (`server/`)

New settings (`/etc/tsi-explorer/server.env`), replacing `ASSISTANT_URL`:

```ini
LLM_BASE_URL=https://api.tech.ec.europa.eu/ecgpt/v1
LLM_API_KEY_FILE=/etc/tsi-explorer/llm-key      # or LLM_API_KEY=...
LLM_MODEL=gpt-5.1                               # optional
# LLM_EXPAND_MODEL=                             # optional, default LLM_MODEL
# LLM_REASONING=off                             # if the gateway refuses reasoning_effort
HTTPS_PROXY=http://<proxy>:<port>               # if any
NO_PROXY=localhost,127.0.0.1
```

- The assistant is on when `LLM_BASE_URL` is set; the key is then required (configuration error at start-up
  otherwise). Off: `POST /api/assistant` answers 503 "not available yet", as today.
- New `src/assistant.js`: loads `tsi-assistant/index.ts` once (only when the assistant is on), turns the Node request
  into a `Request`, builds the function's environment for each request (connector `openai`, `TSI_REST_URL` =
  `POSTGREST_URL`, a fresh service token for the impact save valid 5 minutes, model and key) and writes the
  `Response` back. Same JSON contract as the cloud, so the page does not change its calls.
- **Proxy.** Node's `fetch` ignores `HTTPS_PROXY`. Calls to `LLM_BASE_URL` go through `undici`'s `fetch` with
  `EnvHttpProxyAgent` (reads `HTTPS_PROXY`, `HTTP_PROXY`, `NO_PROXY`); every other call keeps Node's `fetch`.
  `undici` is added as the service's second dependency, pinned. A proxy certificate authority is given with Node's
  own `NODE_EXTRA_CA_CERTS`.
- **TypeScript.** The service starts with `node --experimental-strip-types index.js` (Node 22.6 or later): Node removes
  the types of `index.ts`, nothing is compiled. `npm start`, the systemd unit and the README use it.
- `GET /api/health`: `assistant: true` when on.
- **Gateway check.** `npm run assistant-check` reads the same settings and makes two short calls to the gateway: one
  with a tool (the model must call it) and one in JSON mode. It prints OK or the HTTP status and the gateway's error
  excerpt for each, without touching the database. It is the first thing to run on the server with the real key.

## 3. Page texts

`config.js` gets two optional settings; `deploy/onprem/config.js` sets them:

```js
export const AI_NAME = "GPT@EC, European Commission";   // default "Claude, Anthropic"
export const AI_WEB_SEARCH = false;                      // default true
```

The page and the Word export use them wherever they name the AI or mention web search today (about 15 texts: chat
note, impact tab and guide, impact statistics, briefing note, Word "Model" lines). Analyses saved before the switch
keep their own text in their report.

## 4. Documentation

`deploy/onprem/README.md`: status table (assistant ready), step 6 (settings, key file, proxy, start command, systemd
unit), new check `npm run assistant-check`, step 12 (assistant question), operations (logs: `journalctl -u
tsi-explorer` holds one line per assistant request, without content). `REQUIREMENTS.md`: Node 22.6 or later.

## 5. Tests

- `tsi-assistant/index.test.ts`, existing 69 tests unchanged (Anthropic). New tests with a fake gateway answering in the
  OpenAI format: chat loop with `tool_calls` and tool messages, parallel calls in order, unknown tool and invalid
  arguments, last call with `tool_choice: "none"`, `length` and `content_filter`, error mapping without content,
  usage, `reasoning_effort` on and off, briefing and related terms in JSON mode, impact with data tools only and its
  prompt, impact saved through `TSI_REST_URL`, `TSI_REST_URL` used by every database call.
- `server/test`: settings (on/off, key required, key file), `/api/assistant` in the service end to end with a fake
  gateway and a fake PostgREST (user token forwarded, service token for the save), 503 when off, gateway calls routed
  through the proxy-aware `fetch` and the others not, health.
- Not testable here: the real gateway's support of tools, `reasoning_effort` and JSON mode. `assistant-check` covers it
  on the server.

## Out of scope

- Web search for impact analyses on the server.
- The Python chatbot (`tsi_agent chat`) and Haystack Enterprise.
- Sign-in (other branch) and the Docker/podman files of the client's installation (not in the repository).
