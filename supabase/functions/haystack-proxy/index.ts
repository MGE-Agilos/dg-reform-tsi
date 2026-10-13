// Supabase Edge Function "haystack-proxy": relays the explorer's Chat and Impact requests to the
// Haystack Enterprise Gateway services tsi-chat / tsi-impact (OpenAI-compatible endpoints).
// Single self-contained file, no imports: paste it into Supabase (Edge Functions > Via Editor), keep "Verify JWT" on.
// Secrets (Edge Functions > Secrets): HAYSTACK_API_KEY, HAYSTACK_CHAT_URL, HAYSTACK_IMPACT_URL;
// optional HAYSTACK_CHAT_MODEL, HAYSTACK_IMPACT_MODEL, ALLOWED_ORIGINS. SUPABASE_URL and SUPABASE_ANON_KEY are provided
// (if SUPABASE_ANON_KEY is missing, the caller's apikey header, i.e. the public anon key, is used for is_tester).
// The pure logic is exported and tested with Node: node --experimental-strip-types --test supabase/functions/haystack-proxy/index.test.ts
// Never logs message contents nor keys: only service, status, duration, the is_tester status on failure, and the
// Gateway's own error excerpt (which holds no user content). The browser never receives that excerpt.

type Env = Record<string, string | undefined>;
type Message = { role: "user" | "assistant"; content: string };
type FetchImpl = (url: string, init: RequestInit) => Promise<Response>;
type ServiceName = "chat" | "impact";

export const SERVICES = {
  chat: { urlVar: "HAYSTACK_CHAT_URL", modelVar: "HAYSTACK_CHAT_MODEL", defaultModel: "tsi-chat" },
  impact: { urlVar: "HAYSTACK_IMPACT_URL", modelVar: "HAYSTACK_IMPACT_MODEL", defaultModel: "tsi-impact" },
};

export const MAX_MESSAGES = 20;
export const MAX_CONTENT = 8000; // per message
export const MAX_TOTAL = 40000; // all messages together
export const GATEWAY_TIMEOUT_MS = 130000; // below the page's 160 s and the Edge Function limit
const DEFAULT_ORIGINS = ["https://mge-agilos.github.io", "http://localhost:8000"];

export class HttpError extends Error {
  status: number;
  constructor(status: number, message: string) {
    super(message);
    this.status = status;
  }
}

export function allowedOrigins(env: Env): string[] {
  const raw = env.ALLOWED_ORIGINS;
  if (!raw || !raw.trim()) return [...DEFAULT_ORIGINS];
  return raw.split(",").map((s) => s.trim()).filter(Boolean);
}

export function corsHeaders(origin: string | null, allowed: string[]): Record<string, string> {
  const headers: Record<string, string> = {
    "Vary": "Origin",
    "Access-Control-Allow-Headers": "authorization, apikey, content-type, x-client-info",
    "Access-Control-Allow-Methods": "POST, OPTIONS",
  };
  if (origin && allowed.includes(origin)) headers["Access-Control-Allow-Origin"] = origin;
  return headers;
}

export function validate(body: unknown): { service: ServiceName; messages: Message[] } {
  if (!body || typeof body !== "object") throw new HttpError(400, "Invalid request body");
  const { service, messages } = body as { service?: unknown; messages?: unknown };
  if (service !== "chat" && service !== "impact") throw new HttpError(400, "Unknown service");
  if (!Array.isArray(messages) || messages.length < 1 || messages.length > MAX_MESSAGES) {
    throw new HttpError(400, `messages must contain 1 to ${MAX_MESSAGES} items`);
  }
  const clean: Message[] = messages.map((m, i) => {
    const { role, content } = (m ?? {}) as { role?: unknown; content?: unknown };
    if (role !== "user" && role !== "assistant") throw new HttpError(400, `messages[${i}].role must be user or assistant`);
    if (typeof content !== "string" || !content.trim()) throw new HttpError(400, `messages[${i}].content must be a non-empty string`);
    if (content.length > MAX_CONTENT) throw new HttpError(400, `messages[${i}].content exceeds ${MAX_CONTENT} characters`);
    return { role, content };
  });
  const total = clean.reduce((n, m) => n + m.content.length, 0);
  if (total > MAX_TOTAL) throw new HttpError(400, `messages exceed ${MAX_TOTAL} characters in total`);
  if (clean[clean.length - 1].role !== "user") throw new HttpError(400, "The last message must come from the user");
  return { service, messages: clean };
}

export function gatewayRequest(service: ServiceName, messages: Message[], env: Env): { url: string; init: RequestInit } {
  const cfg = SERVICES[service];
  const url = env[cfg.urlVar];
  if (!url) throw new HttpError(500, `${cfg.urlVar} not configured`);
  const key = env.HAYSTACK_API_KEY;
  if (!key) throw new HttpError(500, "HAYSTACK_API_KEY not configured");
  return {
    url,
    init: {
      method: "POST",
      headers: { "Authorization": `Bearer ${key}`, "Content-Type": "application/json" },
      body: JSON.stringify({ model: env[cfg.modelVar] || cfg.defaultModel, messages, stream: false }),
    },
  };
}

/** choices[0].message.content; no message object: unexpected response; non-string or blank content: empty response. */
export function extractAnswer(json: unknown): string {
  const message = (json as { choices?: { message?: { content?: unknown } }[] } | null)?.choices?.[0]?.message;
  if (!message || typeof message !== "object") throw new HttpError(502, "Unexpected response from the analysis service");
  const content = message.content;
  if (typeof content !== "string" || !content.trim()) throw new HttpError(502, "Empty response from the analysis service");
  return content;
}

function isTimeout(e: unknown): boolean {
  const name = (e as { name?: string } | null)?.name;
  return name === "TimeoutError" || name === "AbortError";
}

async function checkTester(authorization: string, apikey: string, env: Env, fetchImpl: FetchImpl): Promise<boolean> {
  try {
    const res = await fetchImpl(`${env.SUPABASE_URL}/rest/v1/rpc/is_tester`, {
      method: "POST",
      headers: { "apikey": apikey, "Authorization": authorization, "Content-Type": "application/json" },
      body: "{}",
    });
    if (!res.ok) {
      console.log(JSON.stringify({ rpcStatus: res.status }));
      return false;
    }
    return (await res.json()) === true;
  } catch {
    return false;
  }
}

export async function handle(req: Request, env: Env, fetchImpl: FetchImpl): Promise<Response> {
  const cors = corsHeaders(req.headers.get("Origin"), allowedOrigins(env));
  const reply = (status: number, body: unknown) =>
    new Response(JSON.stringify(body), { status, headers: { ...cors, "Content-Type": "application/json" } });

  if (req.method === "OPTIONS") {
    return new Response(null, { status: 204, headers: { ...cors, "Access-Control-Max-Age": "86400" } });
  }
  if (req.method !== "POST") return reply(405, { error: "Method not allowed" });

  const authorization = req.headers.get("Authorization") ?? "";
  if (!/^Bearer\s+\S+/i.test(authorization)) return reply(401, { error: "Missing bearer token" });

  const started = Date.now();
  let service = "?";
  let status = 500;
  try {
    if (!env.SUPABASE_URL) throw new HttpError(500, "SUPABASE_URL not configured");
    const apikey = env.SUPABASE_ANON_KEY || req.headers.get("apikey") || "";
    if (!apikey) throw new HttpError(500, "SUPABASE_ANON_KEY not configured");
    if (!(await checkTester(authorization, apikey, env, fetchImpl))) {
      status = 403;
      return reply(403, { error: "Access not authorised" });
    }
    let body: unknown;
    try {
      body = await req.json();
    } catch {
      throw new HttpError(400, "Invalid JSON body");
    }
    const valid = validate(body);
    service = valid.service;
    const { url, init } = gatewayRequest(valid.service, valid.messages, env);
    let res: Response;
    try {
      res = await fetchImpl(url, { ...init, signal: AbortSignal.timeout(GATEWAY_TIMEOUT_MS) });
    } catch (e) {
      if (isTimeout(e)) throw new HttpError(504, "The analysis service timed out");
      throw new HttpError(502, "The analysis service could not be reached");
    }
    if (!res.ok) {
      const text = await res.text().catch(() => "");
      console.log(JSON.stringify({ service, gatewayStatus: res.status, gatewayError: text.slice(0, 300) }));
      throw new HttpError(502, `Analysis service error ${res.status}`);
    }
    let json: unknown;
    try {
      json = await res.json();
    } catch (e) {
      if (isTimeout(e)) throw new HttpError(504, "The analysis service timed out");
      throw new HttpError(502, "Unexpected response from the analysis service");
    }
    const answer = extractAnswer(json);
    status = 200;
    return reply(200, { answer });
  } catch (e) {
    if (e instanceof HttpError) {
      status = e.status;
      return reply(e.status, { error: e.message });
    }
    status = 500;
    return reply(500, { error: "Internal error" });
  } finally {
    console.log(JSON.stringify({ service, status, ms: Date.now() - started }));
  }
}

declare const Deno: any;
if (typeof Deno !== "undefined") Deno.serve((req: Request) => handle(req, Deno.env.toObject(), fetch));
