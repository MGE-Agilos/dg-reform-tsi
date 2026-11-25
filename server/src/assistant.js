// Assistant of the self-hosted installation: the explorer's tsi-assistant (supabase/functions/tsi-assistant/index.ts) run in
// this process with its OpenAI-compatible connector (GPT@EC). Loaded only when LLM_BASE_URL is set; Node must strip the
// types (node --experimental-strip-types, Node 22.12 or later). Calls to the gateway honour HTTPS_PROXY / NO_PROXY; the
// database calls go straight to PostgREST with the user's token (row-level security applies).
import { serviceToken } from "./tokens.js";

export const ASSISTANT_MODULE = new URL("../../supabase/functions/tsi-assistant/index.ts", import.meta.url);
const SAVE_TOKEN_SECONDS = 300; // the impact save happens at most ~150 s after the request starts

/** Environment of tsi-assistant for one request (a fresh service token for the impact save). */
export function assistantEnv(cfg) {
  return {
    TSI_LLM_PROVIDER: "openai",
    TSI_OPENAI_BASE_URL: cfg.llm.baseUrl,
    TSI_OPENAI_API_KEY: cfg.llm.apiKey,
    TSI_MODEL: cfg.llm.model,
    TSI_EXPAND_MODEL: cfg.llm.expandModel,
    TSI_OPENAI_REASONING: cfg.llm.reasoning ? "on" : "off",
    TSI_REST_URL: cfg.postgrestUrl,
    SUPABASE_ANON_KEY: "server", // PostgREST ignores it; tsi-assistant only requires a value
    SUPABASE_SERVICE_ROLE_KEY: serviceToken(cfg.jwtSecret, SAVE_TOKEN_SECONDS),
    ALLOWED_ORIGINS: cfg.publicUrl,
  };
}

/** fetch for tsi-assistant: requests to the gateway through `llmFetch`, every other request through `fetchImpl`. */
export function routedFetch(baseUrl, llmFetch, fetchImpl) {
  const prefix = baseUrl.replace(/\/+$/, "") + "/";
  return (url, init) => (String(url).startsWith(prefix) ? llmFetch(url, init) : fetchImpl(url, init));
}

/** fetch honouring HTTPS_PROXY, HTTP_PROXY and NO_PROXY (Node's own fetch ignores them). */
export async function proxyFetch() {
  const { fetch: undiciFetch, EnvHttpProxyAgent } = await import("undici");
  const dispatcher = new EnvHttpProxyAgent();
  return (url, init) => undiciFetch(url, { ...init, dispatcher });
}

/** (req, body) -> Response of tsi-assistant, same JSON contract as the cloud function. */
export async function createAssistant(cfg, { fetchImpl = fetch, llmFetch = null, loadModule = () => import(ASSISTANT_MODULE.href) } = {}) {
  const { handle } = await loadModule();
  const route = routedFetch(cfg.llm.baseUrl, llmFetch || await proxyFetch(), fetchImpl);
  return (req, body) => handle(new Request(`${cfg.publicUrl}/api/assistant`, {
    method: "POST", body,
    headers: { "content-type": "application/json", authorization: String(req.headers.authorization || "") },
  }), assistantEnv(cfg), route);
}
