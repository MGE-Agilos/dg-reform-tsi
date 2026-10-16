// Supabase Edge Function "tsi-assistant": serves the explorer's Chat and Impact tabs with Claude (Anthropic Messages API).
// Same contract as haystack-proxy: POST {service: "chat"|"impact", messages} with the tester's session token -> {answer, saved}.
// Claude's client tools query the Supabase tables through PostgREST WITH THE TESTER'S TOKEN (RLS applies); the impact
// analysis also uses Anthropic's server web search tool.
// Single self-contained file, no imports: paste it into Supabase (Edge Functions > Via Editor), keep "Verify JWT" on.
// Secrets (Edge Functions > Secrets): TSI_ANTHROPIC_API_KEY (preferred) or ANTHROPIC_API_KEY; optional TSI_MODEL (default claude-sonnet-5-5), ALLOWED_ORIGINS.
// SUPABASE_URL and SUPABASE_ANON_KEY are provided (if SUPABASE_ANON_KEY is missing, the caller's apikey header, i.e. the
// public anon key, is used). Needs supabase/migrations/002_search.sql (tsi_search_projects, tsi_search_csrs).
// Impact analyses are kept (best effort) in public.impact_reports (003_dashboard.sql) with SUPABASE_SERVICE_ROLE_KEY
// (provided by Supabase); the response is {answer, saved}.
// The pure logic is exported and tested with Node: node --experimental-strip-types --test supabase/functions/tsi-assistant/index.test.ts
// Never logs message contents, tool inputs nor keys: only service, status, duration, steps, token usage, the is_tester
// status on failure and the Anthropic error status/type.

type Env = Record<string, string | undefined>;
type Message = { role: "user" | "assistant"; content: string };
type FetchImpl = (url: string, init: RequestInit) => Promise<Response>;
type ServiceName = "chat" | "impact";
type Block = { type: string; [key: string]: unknown };
type ApiMessage = { role: "user" | "assistant"; content: string | Block[] };
type Usage = { input_tokens?: number; output_tokens?: number; cache_read_input_tokens?: number; cache_creation_input_tokens?: number };
type ModelResponse = { content: Block[]; stop_reason: string | null; usage?: Usage };
/** deadline: absolute time (same clock as now) after which no database call may run. */
export type ToolContext = { supabaseUrl: string; apikey: string; userAuth: string; fetchImpl: FetchImpl; deadline?: number; now?: () => number };
/** Run statistics, filled in place (also when the run fails) and logged without any content. */
export type RunStats = { steps: number; input_tokens: number; output_tokens: number; cache_read_tokens: number; cache_creation_tokens: number; web_searches: number };

export const MAX_MESSAGES = 20;
export const MAX_CONTENT = 8000; // per message
export const MAX_TOTAL = 40000; // all messages together
export const ANTHROPIC_URL = "https://api.anthropic.com/v1/messages";
export const ANTHROPIC_VERSION = "2023-06-01";
export const DEFAULT_MODEL = "claude-sonnet-5-5";
export const MAX_TOKENS: Record<ServiceName, number> = { chat: 16000, impact: 16000 };
export const EFFORT: Record<ServiceName, string> = { chat: "low", impact: "medium" };
export const MAX_STEPS = 8; // tool rounds; then one last call without tools
export const MODEL_TIMEOUT_MS = 60000; // per Anthropic call
export const DEADLINE_MS = 140000; // nothing runs past it (the page gives up at 160 s)
export const BUDGET_MS = DEADLINE_MS - 30000; // beyond it, one last call without tools (with at least ~30 s left)
export const DB_TIMEOUT_MS = 20000; // per PostgREST call
export const LIST_MIN_REMAINING_MS = 5000; // list_projects stops paging below this
export const TESTER_TIMEOUT_MS = 10000;
export const WEB_SEARCH_MAX_USES = 4;
const TOOL_OUTPUT_MAX = 15000; // characters returned to the model per tool call
const DEFAULT_ORIGINS = ["https://mge-agilos.github.io", "http://localhost:8000"];

export const COUNTRIES = ["AT", "BE", "BG", "CY", "CZ", "DE", "DK", "EE", "ES", "FI", "FR", "GR", "HR", "HU", "IE", "IT",
  "LT", "LU", "LV", "MT", "NL", "PL", "PT", "RO", "SE", "SI", "SK"];
export const PROGRAMMES = ["TSI", "SRSP", "SRSS"];
export const POLICY_AREAS = ["competitiveness", "digital-transition", "financial-sector-and-access-finance",
  "green-transition", "health-and-long-term-care", "labour-market-and-social-protection", "migration-management",
  "public-administration-and-governance", "revenue-administration-and-public-financial-management",
  "skills-education-and-training"];

export class HttpError extends Error {
  status: number;
  constructor(status: number, message: string) {
    super(message);
    this.status = status;
  }
}

class ToolError extends Error {}

// ---------- prompts ----------

const FILTER_VALUES = `Filter values: countries are ISO codes (AT Austria, BE Belgium, BG Bulgaria, HR Croatia, CY Cyprus, CZ Czechia,
DK Denmark, EE Estonia, FI Finland, FR France, DE Germany, GR Greece, HU Hungary, IE Ireland, IT Italy, LV Latvia,
LT Lithuania, LU Luxembourg, MT Malta, NL Netherlands, PL Poland, PT Portugal, RO Romania, SK Slovakia, SI Slovenia,
ES Spain, SE Sweden; Greece is GR, never EL). Programmes: TSI, SRSP, SRSS. Policy areas: ${POLICY_AREAS.join(", ")}.`;

export const CHAT_SYSTEM = `You answer questions about technical support projects of the European Commission (DG REFORM): the
Technical Support Instrument (TSI, 2021 onwards) and its predecessor, the Structural Reform Support
Programme (SRSP, 2017-2020). Some older projects (SRSS, 2012-2016) are pre-SRSP technical assistance to
Greece. You only know what the tools return.

Tools:
- search_projects: full-text search over project titles and summaries, with filters; returns the best matches with
  their id, programme, year, countries, policy area, URL, summary and linked CSR ids.
- list_projects: exact counts and lists of projects over the whole corpus, with filters (countries, years,
  programme, policy area, detail page, title words, csr).
- get_project: one project's record and its links to CSRs (level, rationale, CSR part text, assessment).
- search_csrs: full-text search over EU country-specific recommendations (CSRs, European Semester 2016-2025).
- get_csr: one CSR, its sub-parts, their assessments and the projects linked to them.

Rules:
1. Answer in the language of the user's question.
2. Use only information returned by the tools in this conversation. If it is not there, say so plainly.
3. Cite every project with a Markdown link to its URL, e.g. [Project title](https://...), and every CSR with its id
   (e.g. csr-FR-2023-1). Never invent a project, a URL, a figure or a CSR.
4. Project summaries are AI-generated and project-CSR links are assessed by an AI model: say so when you use them.
   CSR assessments come from the Commission's CSR database.
5. For any number, count or list of projects by country, policy area, programme or year, call list_projects and
   report its figures; never estimate. For "which countries", "how many per country" or "which policy areas"
   questions, read its "By country" / "By policy area" lines: they are exact over the whole corpus. Do not infer
   countries or areas from the listed projects: that list is truncated to \`limit\` (raise \`limit\` when the user
   wants the full list). When you announce a number of projects, it must match what you report.
6. For a free topic (not an official policy area), combine search_projects with 2 to 3 different phrasings
   (synonyms, acronyms) and list_projects with title_contains on key words; keep only projects clearly about the
   topic, and say that the list comes from a search and may not be exhaustive.
7. For a comparison, search each compared item separately (for example one search per country).
8. Projects without a detail page ("detail page: no") are only known by their title, country and year: say so.
9. Deliverable full texts (reports, studies) are not available here: for details from the deliverables, say that
   they cannot be searched in this assistant and point to the project page.
10. For follow-up questions, reuse the context of the conversation (countries, topics, projects cited).
11. You have a limited number of steps: after a few tool calls, answer with what you found.
12. Be concise: at most about 250 words unless the user asks for details or a full list; use a short table only
    for comparisons. Use headings sparingly (at most one level).
13. Always write tool queries in English, naming the country, the institution and the topic, whatever the language
    of the question.
14. For CSR questions, call search_csrs or get_csr. Do not state that a project implements a CSR unless a tool result
    says so. To list the projects linked to a CSR, call list_projects with csr (a CSR id or a prefix such as
    csr-HR-2022) or get_csr.

${FILTER_VALUES}`;

export function impactSystem(maxSearches: number = WEB_SEARCH_MAX_USES): string {
  return `You are an impact analyst for the European Commission's DG REFORM. You assess the impact of one Technical
Support Instrument (TSI) or SRSP project on its country, on Europe and on the world, against the project's own
objectives. The user gives a project title or id.

Method:
1. Call get_project with the project id to read the project: identify its objectives and the actions carried out.
   If you only have a title, call search_projects first to find the id. The summary is AI-generated and the
   deliverables' full texts are not available.
2. Search the web with web_search for evidence of impact: adoption and implementation of the reform (laws, decrees,
   action plans), indicators before and after, evaluations, OECD / World Bank / European Commission documents
   (European Semester country reports, recovery and resilience plans), government sites, press articles, criticism.
   Use precise English queries naming the country, the institution and the reform. At most ${maxSearches} searches.

Rules:
- Cite the project with a Markdown link to its URL; cite web sources with a Markdown link to the source URL given by
  web_search, e.g. ([OECD 2024](https://...)). Never invent a source, a URL, a figure or an impact.
- Distinguish correlation from causation: say how directly the evidence can be attributed to the project (causal
  link established, plausible, or unclear).
- When evidence is missing or too recent, say so: a short, honest report is better than a speculative one.
- Write in English, concise and factual. Do not write any text before the report (no "I will search...").

Answer with a Markdown report with these sections:
**Project overview** (objectives and actions), **Evidence of positive impact**, **Evidence of negative impact or
limitations**, **Impact by level** (country, Europe, world), **Objectives** (a table: objective | verdict:
achieved, partly achieved, not achieved or insufficient evidence | rationale), **Overall verdict** (one or two
sentences).
End with exactly one line: "Impact score: X/10 (strong | moderate | weak) — Confidence: high | medium | low".
The score rates the POSITIVE impact documented in your report, on outcomes rather than outputs:
- 0: no evidence of positive impact (outputs not used, reform abandoned, contradicted or reversed).
- 1-3 (weak): outputs delivered (reports, recommendations, trainings) but little or no evidence of uptake; a reform
  adopted outside the project or going against its objectives counts as no uptake.
- 4-6 (moderate): the supported reform was adopted or partly implemented, attribution to the project plausible but
  partial or early.
- 7-8 (strong): documented results or outcomes plausibly attributable to the project.
- 9-10 (strong): well-documented, sustained outcomes clearly attributable to the project.
An objective whose intended reform did not happen, or went the other way, is "not achieved", not "insufficient
evidence".`;
}

// ---------- tool schemas ----------

const countriesProp = { type: "array", items: { type: "string", enum: COUNTRIES }, description: "ISO country codes, e.g. [\"GR\"]; a project matches if it involves any of them." };
const programmeProp = { type: "string", enum: PROGRAMMES, description: "TSI, SRSP or SRSS (pre-2017 technical assistance to Greece)." };
const yearFromProp = { type: "integer", description: "From this year on." };
const yearToProp = { type: "integer", description: "Up to this year." };

const SEARCH_PROJECTS = {
  name: "search_projects",
  description: "Full-text search (English) over project titles and AI-generated summaries. Returns the best matches with id, programme, year, countries, policy area, detail page, URL, summary excerpt and linked CSR ids.",
  input_schema: {
    type: "object",
    properties: {
      query: { type: "string", description: "English search words (topic, institution, country)." },
      countries: countriesProp, programme: programmeProp, year_from: yearFromProp, year_to: yearToProp,
      limit: { type: "integer", minimum: 1, maximum: 20, description: "Maximum results (default 10)." },
    },
    required: ["query"],
  },
};
const LIST_PROJECTS = {
  name: "list_projects",
  description: "Count and list distinct projects matching filters, over the whole corpus. Use it for any number or list. The result gives the total and exact counts by programme, year, country and (without a policy_area filter) policy area; the project list itself is truncated to `limit`.",
  input_schema: {
    type: "object",
    properties: {
      countries: countriesProp, programme: programmeProp, year_from: yearFromProp, year_to: yearToProp,
      policy_area: { type: "string", enum: POLICY_AREAS, description: "Policy area slug." },
      detail_only: { type: "boolean", description: "Only projects with a detail page." },
      title_contains: { type: "string", description: "Case-insensitive word or phrase that must appear in the project title." },
      csr: { type: "string", description: "CSR id or prefix (e.g. \"csr-HR-2022-1\", \"csr-HR-2022-1.2\" or \"csr-HR-2022\" for all 2022 CSRs of Croatia): only projects linked to it (AI-assessed links)." },
      limit: { type: "integer", minimum: 1, maximum: 50, description: "Maximum number of projects to list (default 20; the total is always given)." },
    },
  },
};
const GET_PROJECT = {
  name: "get_project",
  description: "One project's full record (programme, year, countries, policy area, results, URL, AI-generated summary, number of deliverables) and its AI-assessed links to CSR parts (level, rationale, CSR part text and assessment).",
  input_schema: {
    type: "object",
    properties: { project_id: { type: "string", description: "Project id, as returned by search_projects or list_projects." } },
    required: ["project_id"],
  },
};
const SEARCH_CSRS = {
  name: "search_csrs",
  description: "Full-text search (English) over EU country-specific recommendations (European Semester 2016-2025). Returns CSR ids, country, year, title, text excerpt and latest assessment.",
  input_schema: {
    type: "object",
    properties: {
      query: { type: "string", description: "English search words." },
      countries: { ...countriesProp, description: "ISO country codes, e.g. [\"HR\"]." },
      year_from: yearFromProp, year_to: yearToProp,
      limit: { type: "integer", minimum: 1, maximum: 20, description: "Maximum results (default 10)." },
    },
    required: ["query"],
  },
};
const GET_CSR = {
  name: "get_csr",
  description: "One CSR (e.g. csr-HR-2022-1): its text, assessment, sub-parts with their assessments, and the projects linked to each sub-part (AI-assessed links).",
  input_schema: {
    type: "object",
    properties: { csr_id: { type: "string", description: "CSR id such as csr-HR-2022-1 (a sub-part id such as csr-HR-2022-1.2 is accepted)." } },
    required: ["csr_id"],
  },
};
export const WEB_SEARCH_TOOL = { type: "web_search_20260209", name: "web_search", max_uses: WEB_SEARCH_MAX_USES };

export const TOOLS_CHAT: Record<string, unknown>[] = [SEARCH_PROJECTS, LIST_PROJECTS, GET_PROJECT, SEARCH_CSRS, GET_CSR];
export const TOOLS_IMPACT: Record<string, unknown>[] = [GET_PROJECT, SEARCH_PROJECTS, WEB_SEARCH_TOOL];

const SERVICES: Record<ServiceName, { system: () => string; tools: Record<string, unknown>[]; clientTools: string[] }> = {
  chat: { system: () => CHAT_SYSTEM, tools: TOOLS_CHAT, clientTools: ["search_projects", "list_projects", "get_project", "search_csrs", "get_csr"] },
  impact: { system: () => impactSystem(), tools: TOOLS_IMPACT, clientTools: ["get_project", "search_projects"] },
};

// ---------- tool input validation ----------

type Input = Record<string, unknown>;

function optString(input: Input, key: string, max = 300): string | null {
  const v = input[key];
  if (v === undefined || v === null || v === "") return null;
  if (typeof v !== "string") throw new ToolError(`${key} must be a string`);
  const s = v.trim();
  if (s.length > max) throw new ToolError(`${key} exceeds ${max} characters`);
  return s || null;
}

function reqString(input: Input, key: string, max = 300): string {
  const s = optString(input, key, max);
  if (!s) throw new ToolError(`${key} is required`);
  return s;
}

function optCountries(input: Input): string[] | null {
  const v = input.countries;
  if (v === undefined || v === null) return null;
  const list = Array.isArray(v) ? v : [v];
  const out: string[] = [];
  for (const c of list) {
    if (typeof c !== "string") throw new ToolError("countries must be ISO codes");
    const code = c.trim().toUpperCase() === "EL" ? "GR" : c.trim().toUpperCase();
    if (!COUNTRIES.includes(code)) throw new ToolError(`unknown country code ${c.slice(0, 10)}`);
    if (!out.includes(code)) out.push(code);
  }
  return out.length ? out : null;
}

function optYear(input: Input, key: string): number | null {
  const v = input[key];
  if (v === undefined || v === null || v === "") return null;
  const n = typeof v === "string" ? Number(v) : v;
  if (typeof n !== "number" || !Number.isInteger(n) || n < 1990 || n > 2100) throw new ToolError(`${key} must be a year`);
  return n;
}

function optProgramme(input: Input): string | null {
  const s = optString(input, "programme", 10);
  if (!s) return null;
  if (!PROGRAMMES.includes(s.toUpperCase())) throw new ToolError("programme must be TSI, SRSP or SRSS");
  return s.toUpperCase();
}

function limitOf(input: Input, def: number, max: number): number {
  const v = input.limit;
  const n = typeof v === "string" ? Number(v) : v;
  if (typeof n !== "number" || !Number.isFinite(n)) return def;
  return Math.min(Math.max(Math.trunc(n), 1), max);
}

/** CSR filter value: trimmed, lower case, "csr-" prefix added if missing; empty -> no filter. */
export function normalizeCsr(value: unknown): string | null {
  const wanted = String(value ?? "").trim().toLowerCase();
  if (!wanted) return null;
  return wanted.startsWith("csr-") ? wanted : "csr-" + wanted;
}

/** Ids of `csrs` matching `wanted` (CSR or sub-part id, or country-year prefix; case-insensitive), without false
 * prefixes: csr-HR-2022-1 does not match csr-HR-2022-10.1. */
export function csrMatches(csrs: unknown, wanted: string | null): string[] {
  if (!wanted || !Array.isArray(csrs)) return [];
  return csrs.filter((c): c is string => typeof c === "string"
    && (c.toLowerCase() === wanted || c.toLowerCase().startsWith(wanted + ".") || c.toLowerCase().startsWith(wanted + "-")));
}

/** Canonical CSR id (csr-CC-YYYY-N) from a CSR or sub-part id, with or without the prefix, in any case. */
export function canonicalCsrId(value: string): string | null {
  const m = /^(?:csr-)?([a-z]{2})-(\d{4})-(\d+)(?:\.\d+)?$/i.exec(value.trim());
  return m ? `csr-${m[1].toUpperCase()}-${m[2]}-${m[3]}` : null;
}

// ---------- tool execution (PostgREST with the tester's token) ----------

function remainingMs(ctx: ToolContext): number {
  return ctx.deadline === undefined ? Infinity : ctx.deadline - (ctx.now ?? Date.now)();
}

async function db(ctx: ToolContext, path: string, body?: unknown): Promise<unknown> {
  const headers: Record<string, string> = { "apikey": ctx.apikey, "Authorization": ctx.userAuth, "Accept": "application/json" };
  if (body !== undefined) headers["Content-Type"] = "application/json";
  const remaining = remainingMs(ctx);
  if (remaining <= 0) throw new ToolError("time limit reached");
  let res: Response;
  try {
    res = await ctx.fetchImpl(`${ctx.supabaseUrl}/rest/v1/${path}`, {
      method: body === undefined ? "GET" : "POST",
      headers,
      body: body === undefined ? undefined : JSON.stringify(body),
      signal: AbortSignal.timeout(Math.max(1, Math.min(DB_TIMEOUT_MS, remaining))),
    });
  } catch (e) {
    throw new ToolError(isTimeout(e) ? "database timed out" : "database unreachable");
  }
  if (!res.ok) throw new ToolError(`database request failed (${res.status})`);
  try {
    return await res.json();
  } catch {
    throw new ToolError("unexpected database response");
  }
}

const eq = (v: string) => "eq." + encodeURIComponent(v);
const yesNo = (b: unknown) => (b ? "yes" : "no");
const clip = (s: unknown, n: number) => {
  const t = String(s ?? "").replace(/\s+/g, " ").trim();
  return t.length > n ? t.slice(0, n - 1) + "…" : t;
};
const link = (title: unknown, url: unknown) =>
  typeof url === "string" && url ? `[${String(title ?? "").replace(/[[\]]/g, "")}](${url})` : String(title ?? "");

function breakdown(values: unknown[]): string {
  const counts = new Map<string, number>();
  let none = 0;
  for (const v of values) {
    if (v === null || v === undefined || v === "") none++;
    else counts.set(String(v), (counts.get(String(v)) ?? 0) + 1);
  }
  const parts = [...counts.entries()].sort((a, b) => b[1] - a[1] || a[0].localeCompare(b[0])).map(([k, n]) => `${k} ${n}`);
  if (none) parts.push(`none ${none}`);
  return parts.join(", ") || "none";
}

type ProjectRow = {
  project_id: string; title: string; programme: string | null; year: number | null; countries: string[] | null;
  policy_area: string | null; detail_page: boolean; url: string | null; csrs: string[] | null; summary?: string | null;
};

function projectLine(p: ProjectRow): string {
  return `${link(p.title, p.url)} (${p.project_id}) — ${p.programme ?? "?"} ${p.year ?? "?"} — `
    + `${(p.countries || []).join(", ") || "no country"} — ${p.policy_area || "no policy area"} — detail page: ${yesNo(p.detail_page)}`;
}

async function searchProjects(input: Input, ctx: ToolContext): Promise<string> {
  const q = reqString(input, "query");
  const rows = await db(ctx, "rpc/tsi_search_projects", {
    q, p_countries: optCountries(input), p_programme: optProgramme(input),
    p_year_from: optYear(input, "year_from"), p_year_to: optYear(input, "year_to"), lim: limitOf(input, 10, 20),
  }) as ProjectRow[];
  if (!Array.isArray(rows) || !rows.length) return `No project matches "${q}" with these filters.`;
  const lines = [`${rows.length} projects (best matches first; summaries are AI-generated):`];
  rows.forEach((p, i) => {
    lines.push(`${i + 1}. ${projectLine(p)}`);
    if (p.csrs?.length) lines.push(`   CSRs (AI-assessed links): ${p.csrs.join(", ")}`);
    lines.push(p.summary ? `   Summary: ${clip(p.summary, 600)}` : "   No summary (known by its title only).");
  });
  return lines.join("\n");
}

const LIST_COLUMNS = "project_id,title,programme,year,countries,policy_area,detail_page,url,csrs";
const LIST_PAGE = 1000;
const LIST_MAX_PAGES = 20;

async function listProjects(input: Input, ctx: ToolContext): Promise<string> {
  const countries = optCountries(input);
  const programme = optProgramme(input);
  const yearFrom = optYear(input, "year_from");
  const yearTo = optYear(input, "year_to");
  const area = optString(input, "policy_area", 80);
  if (area && !POLICY_AREAS.includes(area)) throw new ToolError(`unknown policy_area ${clip(area, 60)}`);
  const detailOnly = input.detail_only === true || input.detail_only === "true";
  const titleContains = optString(input, "title_contains", 200)?.toLowerCase() ?? null;
  const csr = normalizeCsr(optString(input, "csr", 60));
  const limit = limitOf(input, 20, 50);

  const filters = [`select=${LIST_COLUMNS}`, "order=project_id.asc"];
  if (countries) filters.push(`countries=ov.${encodeURIComponent("{" + countries.join(",") + "}")}`);
  if (programme) filters.push(`programme=${eq(programme)}`);
  if (yearFrom !== null) filters.push(`year=gte.${yearFrom}`);
  if (yearTo !== null) filters.push(`year=lte.${yearTo}`);
  if (area) filters.push(`policy_area=${eq(area)}`);
  if (detailOnly) filters.push("detail_page=is.true");
  let rows: ProjectRow[] = [];
  let partial = false;
  for (let page = 0; page < LIST_MAX_PAGES; page++) {
    if (page > 0 && remainingMs(ctx) < LIST_MIN_REMAINING_MS) {
      partial = true;
      break;
    }
    const batch = await db(ctx, `projects?${filters.join("&")}&limit=${LIST_PAGE}&offset=${page * LIST_PAGE}`) as ProjectRow[];
    if (!Array.isArray(batch)) throw new ToolError("unexpected database response");
    rows = rows.concat(batch);
    if (batch.length < LIST_PAGE) break;
  }
  if (titleContains) rows = rows.filter((p) => String(p.title ?? "").toLowerCase().includes(titleContains));
  if (csr) rows = rows.filter((p) => csrMatches(p.csrs, csr).length > 0);
  rows.sort((a, b) => (b.year ?? 0) - (a.year ?? 0) || String(a.title).localeCompare(String(b.title)));

  const byProgramme = breakdown(rows.map((p) => p.programme));
  const years = new Map<string, number>();
  for (const p of rows) years.set(p.year === null ? "none" : String(p.year), (years.get(p.year === null ? "none" : String(p.year)) ?? 0) + 1);
  const byYear = [...years.entries()].sort((a, b) => (a[0] === "none" ? 1 : b[0] === "none" ? -1 : Number(b[0]) - Number(a[0])))
    .map(([y, n]) => `${y}: ${n}`).join(", ");
  const lines = [
    `Total matching projects: ${rows.length}${rows.length ? ` (by programme: ${byProgramme})` : ""}`,
  ];
  if (partial) lines.unshift(`Partial: time limit — only the first ${rows.length} projects read were counted; the figures are not exact.`);
  if (rows.length) {
    lines.push(`By year: ${byYear}`);
    lines.push(`By country: ${breakdown(rows.flatMap((p) => (p.countries?.length ? p.countries : [null])))}`);
    if (!area) lines.push(`By policy area: ${breakdown(rows.map((p) => p.policy_area))}`);
    lines.push(`Showing ${Math.min(limit, rows.length)} of ${rows.length}:`);
    for (const p of rows.slice(0, limit)) {
      lines.push(`- ${projectLine(p)}${csr ? ` — CSRs (AI-assessed): ${csrMatches(p.csrs, csr).join(", ")}` : ""}`);
    }
  }
  return lines.join("\n");
}

type LinkRow = { level: string; rationale: string; basis: string; csr_parts: { part_id: string; csr_id: string; text: string; assessment: string | null } | null };

async function getProject(input: Input, ctx: ToolContext): Promise<string> {
  const id = reqString(input, "project_id", 200);
  const select = "project_id,title,programme,year,countries,policy_area,results,detail_page,url,summary,deliverables,csrs,"
    + "project_csr_links(level,rationale,basis,csr_parts(part_id,csr_id,text,assessment))";
  const rows = await db(ctx, `projects?select=${encodeURIComponent(select)}&project_id=${eq(id)}&limit=1`) as
    (ProjectRow & { results: string | null; deliverables: number; project_csr_links: LinkRow[] | null })[];
  if (!Array.isArray(rows) || !rows.length) return `No project with id "${clip(id, 100)}". Use search_projects to find the id.`;
  const p = rows[0];
  const lines = [
    `Project: ${p.title} (${p.project_id})`,
    `URL: ${p.url || "none"}`,
    `Programme and year: ${p.programme ?? "?"} ${p.year ?? "?"} — Countries: ${(p.countries || []).join(", ") || "none"} — Policy area: ${p.policy_area || "none"}`,
    `Detail page: ${yesNo(p.detail_page)}${p.detail_page ? "" : " (known by its title, country and year only)"} — Deliverables: ${p.deliverables ?? 0} (full texts not available here)`,
    `Results: ${p.results || "not stated"}`,
    p.summary ? `Summary (AI-generated): ${clip(p.summary, 1500)}` : "Summary: none",
  ];
  const links = (p.project_csr_links || []).filter((l) => l.csr_parts);
  if (links.length) {
    lines.push(`Links to CSR parts (assessed by an AI model): ${links.length}`);
    for (const l of links.sort((a, b) => a.csr_parts!.part_id.localeCompare(b.csr_parts!.part_id, "en", { numeric: true }))) {
      const part = l.csr_parts!;
      lines.push(`- ${part.part_id} (CSR ${part.csr_id}) — ${l.level} — rationale: ${clip(l.rationale, 400)}`);
      lines.push(`  CSR part text: ${clip(part.text, 400)}${part.assessment ? ` — Assessment: ${clip(part.assessment, 120)}` : ""}`);
    }
  } else {
    lines.push("Links to CSR parts: none");
  }
  return lines.join("\n");
}

type CsrRow = { csr_id: string; country: string; year: number; title: string; text: string; assessment: string | null; assessment_year: number | null };

async function searchCsrs(input: Input, ctx: ToolContext): Promise<string> {
  const q = reqString(input, "query");
  const rows = await db(ctx, "rpc/tsi_search_csrs", {
    q, p_countries: optCountries(input), p_year_from: optYear(input, "year_from"), p_year_to: optYear(input, "year_to"),
    lim: limitOf(input, 10, 20),
  }) as CsrRow[];
  if (!Array.isArray(rows) || !rows.length) return `No CSR matches "${q}" with these filters.`;
  const lines = [`${rows.length} CSRs (best matches first; assessments from the Commission's CSR database):`];
  rows.forEach((c, i) => {
    lines.push(`${i + 1}. ${c.csr_id} — ${c.country} ${c.year} — ${clip(c.title, 200)}`
      + (c.assessment ? ` — Assessment${c.assessment_year ? ` (${c.assessment_year})` : ""}: ${clip(c.assessment, 120)}` : ""));
    lines.push(`   ${clip(c.text, 800)}`);
  });
  return lines.join("\n");
}

type PartRow = { part_id: string; text: string; assessment: string | null; project_csr_links: { level: string; rationale: string; projects: ProjectRow | null }[] | null };

async function getCsr(input: Input, ctx: ToolContext): Promise<string> {
  const raw = reqString(input, "csr_id", 60);
  const id = canonicalCsrId(raw);
  if (!id) throw new ToolError("csr_id must look like csr-HR-2022-1");
  const select = "csr_id,country,year,number,title,text,assessment,assessment_year,source_url,"
    + "csr_parts(part_id,text,assessment,project_csr_links(level,rationale,projects(project_id,title,programme,year,url)))";
  const rows = await db(ctx, `csrs?select=${encodeURIComponent(select)}&csr_id=${eq(id)}&limit=1`) as
    (CsrRow & { source_url: string | null; csr_parts: PartRow[] | null })[];
  if (!Array.isArray(rows) || !rows.length) return `No CSR with id "${id}". Use search_csrs to find it.`;
  const c = rows[0];
  const lines = [
    `CSR ${c.csr_id} — ${c.country} ${c.year} — ${c.title}`,
    `Assessment${c.assessment_year ? ` (${c.assessment_year})` : ""}: ${c.assessment || "none"} (Commission's CSR database${c.source_url ? `: ${c.source_url}` : ""})`,
    `Text: ${clip(c.text, 2500)}`,
  ];
  const parts = (c.csr_parts || []).sort((a, b) => a.part_id.localeCompare(b.part_id, "en", { numeric: true }));
  for (const part of parts) {
    lines.push(`Part ${part.part_id}${part.assessment ? ` — Assessment: ${clip(part.assessment, 120)}` : ""}: ${clip(part.text, 500)}`);
    const linked = (part.project_csr_links || []).filter((l) => l.projects);
    if (!linked.length) lines.push("  Linked projects: none");
    for (const l of linked) {
      const p = l.projects!;
      lines.push(`  - ${link(p.title, p.url)} (${p.project_id}) — ${p.programme ?? "?"} ${p.year ?? "?"} — ${l.level} (AI-assessed): ${clip(l.rationale, 300)}`);
    }
  }
  return lines.join("\n");
}

const TOOL_IMPLS: Record<string, (input: Input, ctx: ToolContext) => Promise<string>> = {
  search_projects: searchProjects, list_projects: listProjects, get_project: getProject,
  search_csrs: searchCsrs, get_csr: getCsr,
};

/** Runs a client tool and returns compact text for the model; never throws ("Tool error: ..." instead). */
export async function runTool(name: string, input: unknown, ctx: ToolContext): Promise<string> {
  const impl = Object.hasOwn(TOOL_IMPLS, name) ? TOOL_IMPLS[name] : undefined;
  if (!impl) return `Tool error: unknown tool ${String(name).slice(0, 60)}`;
  try {
    const text = await impl(input && typeof input === "object" && !Array.isArray(input) ? input as Input : {}, ctx);
    return text.length > TOOL_OUTPUT_MAX ? text.slice(0, TOOL_OUTPUT_MAX) + "\n[output truncated: narrow the filters or lower the limit]" : text;
  } catch (e) {
    return `Tool error: ${e instanceof ToolError ? e.message : "unexpected failure"}`;
  }
}

// ---------- Anthropic loop ----------

function isTimeout(e: unknown): boolean {
  const name = (e as { name?: string } | null)?.name;
  return name === "TimeoutError" || name === "AbortError";
}

const NOT_ANSWER_BLOCKS = new Set(["thinking", "redacted_thinking"]); // ignored: neither text nor a tool boundary

/** Final text, joined as written (Claude splits cited text into several adjacent blocks). If the content ends with text,
 * only the text after the last tool block (the report, without "I'll search..." preambles); with trailingOnly (turn
 * stopped mid tool use), that trailing text or nothing; otherwise all text groups. Thinking blocks are skipped. */
export function answerText(content: Block[], trailingOnly = false): string {
  const blocks = (Array.isArray(content) ? content : []).filter((b) => b && !NOT_ANSWER_BLOCKS.has(b.type));
  const groups: string[][] = [[]];
  for (const b of blocks) {
    if (b.type === "text" && typeof b.text === "string") groups[groups.length - 1].push(b.text);
    else if (groups[groups.length - 1].length) groups.push([]);
  }
  const endsWithText = blocks.length > 0 && blocks[blocks.length - 1].type === "text";
  const trailing = endsWithText ? groups[groups.length - 1].join("").trim() : "";
  if (trailing || trailingOnly) return trailing;
  return groups.map((g) => g.join("").trim()).filter(Boolean).join("\n\n");
}

export function emptyStats(): RunStats {
  return { steps: 0, input_tokens: 0, output_tokens: 0, cache_read_tokens: 0, cache_creation_tokens: 0, web_searches: 0 };
}

/** Copy of the conversation whose last user message carries a cache breakpoint on its last block (caches the loop
 * prefix); an assistant last message (pause_turn) is sent back unchanged. The conversation itself is not modified. */
export function withCacheBreakpoint(conversation: ApiMessage[]): ApiMessage[] {
  const out = conversation.slice();
  const last = out[out.length - 1];
  if (!last || last.role !== "user") return out;
  const blocks: Block[] = typeof last.content === "string" ? [{ type: "text", text: last.content }] : last.content.slice();
  if (!blocks.length) return out;
  blocks[blocks.length - 1] = { ...blocks[blocks.length - 1], cache_control: { type: "ephemeral" } };
  out[out.length - 1] = { role: "user", content: blocks };
  return out;
}

export async function agentLoop(
  service: ServiceName, messages: Message[], env: Env, fetchImpl: FetchImpl, now: () => number = Date.now,
  ctx?: { supabaseUrl: string; apikey: string; userAuth: string }, stats: RunStats = emptyStats(),
): Promise<{ answer: string; steps: number; stats: RunStats }> {
  // Cle propre au projet TSI d'abord (le secret ANTHROPIC_API_KEY est partage avec une autre application).
  const key = env.TSI_ANTHROPIC_API_KEY || env.ANTHROPIC_API_KEY;
  if (!key) throw new HttpError(500, "ANTHROPIC_API_KEY not configured");
  const cfg = SERVICES[service];
  const started = now();
  const deadline = started + DEADLINE_MS;
  const toolCtx: ToolContext = {
    supabaseUrl: ctx?.supabaseUrl ?? env.SUPABASE_URL ?? "", apikey: ctx?.apikey ?? "", userAuth: ctx?.userAuth ?? "",
    fetchImpl, deadline, now,
  };
  const model = env.TSI_MODEL || DEFAULT_MODEL;
  const system = [{ type: "text", text: cfg.system(), cache_control: { type: "ephemeral" } }];
  const conversation: ApiMessage[] = messages.map((m) => ({ role: m.role, content: m.content }));

  const call = async (final: boolean): Promise<ModelResponse> => {
    // Once the web searches allowed for the run are used up, the server tool is no longer offered (history unchanged).
    const tools = stats.web_searches >= WEB_SEARCH_MAX_USES ? cfg.tools.filter((t) => t !== WEB_SEARCH_TOOL) : cfg.tools;
    const body: Record<string, unknown> = {
      model, max_tokens: MAX_TOKENS[service], system, tools, messages: withCacheBreakpoint(conversation),
      output_config: { effort: EFFORT[service] },
    };
    if (final) body.tool_choice = { type: "none" };
    const timeout = Math.max(1000, Math.min(MODEL_TIMEOUT_MS, deadline - now()));
    let res: Response;
    try {
      res = await fetchImpl(ANTHROPIC_URL, {
        method: "POST",
        headers: { "x-api-key": key, "anthropic-version": ANTHROPIC_VERSION, "content-type": "application/json" },
        body: JSON.stringify(body),
        signal: AbortSignal.timeout(timeout),
      });
    } catch (e) {
      if (isTimeout(e)) throw new HttpError(504, "The model service timed out");
      throw new HttpError(502, "The model service could not be reached");
    }
    if (!res.ok) {
      const err = await res.json().catch(() => null) as { error?: { type?: unknown } } | null;
      console.log(JSON.stringify({ service, anthropicStatus: res.status, anthropicError: typeof err?.error?.type === "string" ? err.error.type : null, requestId: res.headers.get("request-id") }));
      throw new HttpError(502, `Model service error ${res.status}`);
    }
    let json: ModelResponse;
    try {
      json = await res.json() as ModelResponse;
    } catch (e) {
      if (isTimeout(e)) throw new HttpError(504, "The model service timed out");
      throw new HttpError(502, "Unexpected response from the model service");
    }
    if (!json || !Array.isArray(json.content)) throw new HttpError(502, "Unexpected response from the model service");
    const u = json.usage ?? {};
    stats.input_tokens += u.input_tokens ?? 0;
    stats.output_tokens += u.output_tokens ?? 0;
    stats.cache_read_tokens += u.cache_read_input_tokens ?? 0;
    stats.cache_creation_tokens += u.cache_creation_input_tokens ?? 0;
    stats.web_searches += json.content.filter((b) => b?.type === "server_tool_use" && b.name === "web_search").length;
    return json;
  };

  let res = await call(false);
  let final = false;
  while (!final && (res.stop_reason === "tool_use" || res.stop_reason === "pause_turn")) {
    stats.steps++;
    conversation.push({ role: "assistant", content: res.content }); // as received, server tool blocks included
    if (res.stop_reason === "tool_use") {
      // Client tools of one turn run in parallel; server_tool_use is run by Anthropic, never answered here.
      const uses = res.content.filter((b) => b?.type === "tool_use");
      const outputs = await Promise.all(uses.map((b) => {
        const name = String(b.name ?? "");
        return cfg.clientTools.includes(name) ? runTool(name, b.input, toolCtx) : Promise.resolve(`Tool error: unknown tool ${name.slice(0, 60)}`);
      }));
      const results: Block[] = uses.map((b, i) => {
        const result: Block = { type: "tool_result", tool_use_id: b.id, content: outputs[i] };
        if (outputs[i].startsWith("Tool error:")) result.is_error = true;
        return result;
      });
      if (results.length) conversation.push({ role: "user", content: results });
    }
    final = stats.steps >= MAX_STEPS || now() - started > BUDGET_MS;
    res = await call(final);
  }
  if (res.stop_reason === "refusal") throw new HttpError(422, "The model declined this request");
  const midTool = res.stop_reason === "tool_use" || res.stop_reason === "pause_turn";
  let answer = answerText(res.content, midTool);
  if (!answer) throw new HttpError(502, "Empty response from the model");
  if (res.stop_reason === "max_tokens") answer += "\n\n[answer truncated]";
  return { answer, steps: stats.steps, stats };
}

// ---------- impact reports kept in public.impact_reports (best effort) ----------

export type ImpactScore = { score: number; level: "strong" | "moderate" | "weak"; confidence: "high" | "medium" | "low" };

const IMPACT_LINE = /impact\s*score\s*:\s*(\d{1,2})\s*\/\s*10\s*\(\s*(strong|moderate|weak)\s*\)\s*[—–-]\s*confidence\s*:\s*(high|medium|low)\b/i;

/** Score line "Impact score: X/10 (strong|moderate|weak) — Confidence: high|medium|low" (the last one in the answer;
 * "-" or "–" accepted for "—", any case, extra spaces, Markdown emphasis ignored), or null. */
export function parseImpact(answer: string): ImpactScore | null {
  const lines = String(answer ?? "").split(/\r?\n/);
  for (let i = lines.length - 1; i >= 0; i--) {
    const m = IMPACT_LINE.exec(lines[i].replace(/[*_`]/g, ""));
    if (!m) continue;
    const score = Number(m[1]);
    if (score > 10) return null;
    return { score, level: m[2].toLowerCase() as ImpactScore["level"], confidence: m[3].toLowerCase() as ImpactScore["confidence"] };
  }
  return null;
}

const VERDICT_LINE = /^\s*(?:#{1,6}\s*)?[*_]*\s*overall verdict\s*[*_]*\s*:?\s*[*_]*\s*(.*)$/i;
const VERDICT_MAX = 2000;

/** Text of the "Overall verdict" section (inline after the label, or the following paragraph), or null. */
export function extractVerdict(answer: string): string | null {
  const lines = String(answer ?? "").split(/\r?\n/);
  for (let i = 0; i < lines.length; i++) {
    const m = VERDICT_LINE.exec(lines[i]);
    if (!m) continue;
    const parts: string[] = [];
    if (m[1].trim()) parts.push(m[1].trim());
    else {
      for (let j = i + 1; j < lines.length; j++) {
        const line = lines[j].trim();
        if (!line) {
          if (parts.length) break;
          continue;
        }
        if (line.startsWith("#") || IMPACT_LINE.test(line.replace(/[*_`]/g, ""))) break;
        parts.push(line);
      }
    }
    const text = parts.join(" ").replace(/\s+/g, " ").trim();
    return text ? text.slice(0, VERDICT_MAX) : null;
  }
  return null;
}

/** Project analysed, from the page's message "Project: <title> (id <project_id>; ...)"; null without an id. */
export function extractProject(messages: Message[]): { project_id: string; title: string } | null {
  for (const m of messages) {
    if (m.role !== "user") continue;
    const match = /Project:\s*(.+?)\s*\(id\s+([^;)\s]+)\s*[;)]/.exec(m.content);
    if (match) return { project_id: match[2].slice(0, 200), title: match[1].trim().slice(0, 500) };
  }
  return null;
}

const UUID = /^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/i;

/** `sub` (uuid) of the user's JWT, for information only: the payload is decoded WITHOUT verifying the signature (the
 * token was already checked by the gateway and by is_tester). Null if absent or not a uuid. */
export function jwtSub(authorization: string): string | null {
  try {
    const parts = authorization.replace(/^Bearer\s+/i, "").trim().split(".");
    if (parts.length !== 3) return null;
    const b64 = parts[1].replace(/-/g, "+").replace(/_/g, "/");
    const payload = JSON.parse(atob(b64 + "=".repeat((4 - (b64.length % 4)) % 4)));
    const sub = payload && typeof payload === "object" ? payload.sub : null;
    return typeof sub === "string" && UUID.test(sub) ? sub : null;
  } catch {
    return null;
  }
}

export const SAVE_TIMEOUT_MS = 5000; // per call (project check, insert)
export const SAVE_DEADLINE_MS = 145000; // from the start of the request: the page gives up at 160 s

/** Inserts the impact report with the service_role key (RLS: no insert for testers), only when the answer has a score
 * line and the project exists for the user (checked with the user's token; the stored title is the database's).
 * Each call is bounded by min(SAVE_TIMEOUT_MS, time left before handlerStart + SAVE_DEADLINE_MS), at least 1 s.
 * Never throws; failures are logged without content. Returns whether the row was saved. */
async function saveImpact(answer: string, messages: Message[], env: Env, fetchImpl: FetchImpl, authorization: string,
  apikey: string, handlerStart: number, now: () => number): Promise<boolean> {
  const key = env.SUPABASE_SERVICE_ROLE_KEY;
  const project = extractProject(messages);
  const impact = parseImpact(answer);
  if (!key || !env.SUPABASE_URL || !project || !impact) return false;
  const timeout = () => Math.max(1000, Math.min(SAVE_TIMEOUT_MS, handlerStart + SAVE_DEADLINE_MS - now()));
  try {
    const check = await fetchImpl(`${env.SUPABASE_URL}/rest/v1/projects?project_id=${eq(project.project_id)}&select=title`, {
      method: "GET", headers: { "apikey": apikey, "Authorization": authorization, "Accept": "application/json" },
      signal: AbortSignal.timeout(timeout()),
    });
    if (!check.ok) {
      console.log(JSON.stringify({ impactSave: "project check failed", status: check.status }));
      return false;
    }
    const found = await check.json() as { title?: unknown }[];
    if (!Array.isArray(found) || !found.length) {
      console.log(JSON.stringify({ impactSave: "project not found" }));
      return false;
    }
    if (typeof found[0]?.title === "string" && found[0].title) project.title = found[0].title;
  } catch (e) {
    console.log(JSON.stringify({ impactSave: isTimeout(e) ? "timeout" : "unreachable" }));
    return false;
  }
  const headers: Record<string, string> = { "apikey": key, "Content-Type": "application/json", "Prefer": "return=minimal" };
  if (!key.startsWith("sb_secret_")) headers["Authorization"] = `Bearer ${key}`; // sb_secret_ keys: apikey only
  const row = {
    project_id: project.project_id, title: project.title, score: impact.score, level: impact.level,
    confidence: impact.confidence, verdict: extractVerdict(answer), report: answer,
    model: env.TSI_MODEL || DEFAULT_MODEL, source: "web", created_by: jwtSub(authorization),
  };
  try {
    const res = await fetchImpl(`${env.SUPABASE_URL}/rest/v1/impact_reports`, {
      method: "POST", headers, body: JSON.stringify(row), signal: AbortSignal.timeout(timeout()),
    });
    if (!res.ok) {
      console.log(JSON.stringify({ impactSave: res.status }));
      return false;
    }
    return true;
  } catch (e) {
    console.log(JSON.stringify({ impactSave: isTimeout(e) ? "timeout" : "unreachable" }));
    return false;
  }
}

// ---------- HTTP handler (same contract and checks as haystack-proxy) ----------

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
  if (clean[0].role !== "user") throw new HttpError(400, "The first message must come from the user");
  if (clean[clean.length - 1].role !== "user") throw new HttpError(400, "The last message must come from the user");
  return { service, messages: clean };
}

async function checkTester(authorization: string, apikey: string, env: Env, fetchImpl: FetchImpl): Promise<boolean> {
  try {
    const res = await fetchImpl(`${env.SUPABASE_URL}/rest/v1/rpc/is_tester`, {
      method: "POST",
      headers: { "apikey": apikey, "Authorization": authorization, "Content-Type": "application/json" },
      body: "{}",
      signal: AbortSignal.timeout(TESTER_TIMEOUT_MS),
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

export async function handle(req: Request, env: Env, fetchImpl: FetchImpl, now: () => number = Date.now): Promise<Response> {
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
  const handlerStart = now();
  let service = "?";
  let status = 500;
  const stats = emptyStats();
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
    const result = await agentLoop(valid.service, valid.messages, env, fetchImpl, now,
      { supabaseUrl: env.SUPABASE_URL, apikey, userAuth: authorization }, stats);
    const saved = valid.service === "impact"
      ? await saveImpact(result.answer, valid.messages, env, fetchImpl, authorization, apikey, handlerStart, now)
      : false;
    status = 200;
    return reply(200, { answer: result.answer, saved });
  } catch (e) {
    if (e instanceof HttpError) {
      status = e.status;
      return reply(e.status, { error: e.message });
    }
    status = 500;
    return reply(500, { error: "Internal error" });
  } finally {
    console.log(JSON.stringify({ service, status, ms: Date.now() - started, ...stats }));
  }
}

declare const Deno: any;
if (typeof Deno !== "undefined") Deno.serve((req: Request) => handle(req, Deno.env.toObject(), fetch));
