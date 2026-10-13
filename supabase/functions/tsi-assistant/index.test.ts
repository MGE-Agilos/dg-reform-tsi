// Tests of the tsi-assistant logic, without any network access (fake fetch routed by URL).
// Run: node --experimental-strip-types --test supabase/functions/tsi-assistant/index.test.ts
import { test } from "node:test";
import assert from "node:assert/strict";
import {
  handle, agentLoop, runTool, validate, allowedOrigins, corsHeaders, answerText, csrMatches, normalizeCsr, canonicalCsrId,
  HttpError, CHAT_SYSTEM, impactSystem, TOOLS_CHAT, TOOLS_IMPACT, MAX_STEPS, BUDGET_MS, DEADLINE_MS, DEFAULT_MODEL,
  withCacheBreakpoint, emptyStats,
} from "./index.ts";

const ORIGIN = "https://mge-agilos.github.io";
const ENV: Record<string, string> = {
  SUPABASE_URL: "https://project.supabase.co",
  SUPABASE_ANON_KEY: "anon-test",
  ANTHROPIC_API_KEY: "sk-ant-secret-test",
};

type Call = { url: string; init: RequestInit };
type Reply = { status?: number; body: unknown };

const json = (body: unknown, status = 200) =>
  new Response(typeof body === "string" ? body : JSON.stringify(body), { status, headers: { "Content-Type": "application/json" } });

const textReply = (text: string): Reply => ({ body: { content: [{ type: "text", text }], stop_reason: "end_turn" } });
const toolReply = (name: string, input: unknown, id = "toolu_1"): Reply =>
  ({ body: { content: [{ type: "text", text: "Let me check." }, { type: "tool_use", id, name, input }], stop_reason: "tool_use" } });

const PROJECTS = [
  { project_id: "p1", title: "Health reform in Croatia", programme: "TSI", year: 2023, countries: ["HR"], policy_area: "health-and-long-term-care", detail_page: true, url: "https://reform.example/p1", csrs: ["csr-HR-2022-1.2"] },
  { project_id: "p2", title: "Tax administration", programme: "TSI", year: 2022, countries: ["HR", "SI"], policy_area: "revenue-administration-and-public-financial-management", detail_page: false, url: "https://reform.example/p2", csrs: ["csr-HR-2022-10.1"] },
  { project_id: "p3", title: "Pension reform", programme: "SRSP", year: 2019, countries: ["PT"], policy_area: null, detail_page: true, url: null, csrs: [] },
];

function fakeFetch({ tester = true as unknown, testerStatus = 200, model = [] as Reply[], projects = PROJECTS as unknown[], dbStatus = 200, rpc = {} as Record<string, unknown> } = {}) {
  const calls: Call[] = [];
  const queue = [...model];
  const impl = async (url: string, init: RequestInit) => {
    calls.push({ url, init });
    if (url.endsWith("/rest/v1/rpc/is_tester")) return json(tester, testerStatus);
    if (url.includes("/rest/v1/")) {
      if (dbStatus !== 200) return json({ message: "db error" }, dbStatus);
      const rpcName = /\/rpc\/([a-z_]+)/.exec(url)?.[1];
      if (rpcName) return json(rpc[rpcName] ?? []);
      if (url.includes("/rest/v1/projects?")) {
        const offset = Number(/offset=(\d+)/.exec(url)?.[1] ?? 0);
        return json(offset ? [] : projects);
      }
      return json([]);
    }
    if (url === "https://api.anthropic.com/v1/messages") {
      const next = queue.length > 1 ? queue.shift()! : queue[0];
      if (!next) return json({ content: [{ type: "text", text: "default answer" }], stop_reason: "end_turn" });
      return json(next.body, next.status ?? 200);
    }
    throw new Error("unexpected URL " + url);
  };
  return {
    impl, calls,
    modelCalls: () => calls.filter((c) => c.url.startsWith("https://api.anthropic.com")),
    dbCalls: () => calls.filter((c) => c.url.includes("/rest/v1/") && !c.url.endsWith("/rpc/is_tester")),
  };
}

const sent = (c: Call) => JSON.parse(String(c.init.body));

function post(body: unknown, { token = "user-jwt", origin = ORIGIN } = {}) {
  const headers: Record<string, string> = { "Content-Type": "application/json", Origin: origin };
  if (token) headers.Authorization = `Bearer ${token}`;
  return new Request("https://project.supabase.co/functions/v1/tsi-assistant", {
    method: "POST", headers, body: typeof body === "string" ? body : JSON.stringify(body),
  });
}

const chatBody = { service: "chat", messages: [{ role: "user", content: "How many TSI projects does Croatia have?" }] };
const impactBody = { service: "impact", messages: [{ role: "user", content: "Project: Health reform in Croatia (id p1; TSI 2023; countries HR)" }] };
const CTX = { supabaseUrl: ENV.SUPABASE_URL, apikey: "anon-test", userAuth: "Bearer user-jwt" };

// ---------- HTTP contract (same as haystack-proxy) ----------

test("OPTIONS from an allowed origin: 204 with CORS headers, nothing called", async () => {
  const f = fakeFetch();
  const res = await handle(new Request("https://x/tsi-assistant", { method: "OPTIONS", headers: { Origin: ORIGIN } }), ENV, f.impl);
  assert.equal(res.status, 204);
  assert.equal(res.headers.get("Access-Control-Allow-Origin"), ORIGIN);
  assert.match(res.headers.get("Access-Control-Allow-Headers") ?? "", /authorization/);
  assert.match(res.headers.get("Access-Control-Allow-Methods") ?? "", /POST/);
  assert.equal(res.headers.get("Access-Control-Max-Age"), "86400");
  assert.equal(f.calls.length, 0);
});

test("origin not allowed: no ACAO header; allowedOrigins from env", async () => {
  const res = await handle(new Request("https://x/tsi-assistant", { method: "OPTIONS", headers: { Origin: "https://evil.example" } }), ENV, fakeFetch().impl);
  assert.equal(res.headers.get("Access-Control-Allow-Origin"), null);
  assert.deepEqual(allowedOrigins({}), ["https://mge-agilos.github.io", "http://localhost:8000"]);
  assert.deepEqual(allowedOrigins({ ALLOWED_ORIGINS: "https://a.example, https://b.example" }), ["https://a.example", "https://b.example"]);
  assert.equal(corsHeaders("https://a.example", ["https://a.example"])["Access-Control-Allow-Origin"], "https://a.example");
});

test("method other than POST: 405", async () => {
  const res = await handle(new Request("https://x/tsi-assistant", { method: "GET", headers: { Origin: ORIGIN } }), ENV, fakeFetch().impl);
  assert.equal(res.status, 405);
});

test("no bearer token: 401, nothing called", async () => {
  const f = fakeFetch();
  const res = await handle(post(chatBody, { token: "" }), ENV, f.impl);
  assert.equal(res.status, 401);
  assert.equal(f.calls.length, 0);
});

test("is_tester false or failing: 403 and Anthropic is never called", async () => {
  for (const opts of [{ tester: false }, { tester: { message: "JWT expired" }, testerStatus: 401 }]) {
    const f = fakeFetch(opts);
    const res = await handle(post(chatBody), ENV, f.impl);
    assert.equal(res.status, 403);
    assert.deepEqual(await res.json(), { error: "Access not authorised" });
    assert.equal(f.modelCalls().length, 0);
    const h = new Headers(f.calls[0].init.headers);
    assert.equal(h.get("apikey"), "anon-test");
    assert.equal(h.get("Authorization"), "Bearer user-jwt");
  }
});

test("invalid bodies: 400, Anthropic never called", async () => {
  const many = Array.from({ length: 21 }, (_, i) => ({ role: i % 2 ? "assistant" : "user", content: "q" }));
  const long = Array.from({ length: 6 }, (_, i) => ({ role: i % 2 ? "assistant" : "user", content: "x".repeat(7000) }));
  const cases: unknown[] = [
    { service: "other", messages: chatBody.messages },
    { service: "chat", messages: many },
    { service: "chat", messages: [{ role: "user", content: "q" }, { role: "assistant", content: "a" }] },
    { service: "chat", messages: [{ role: "user", content: "x".repeat(8001) }] },
    { service: "chat", messages: [...long, { role: "user", content: "q" }] },
    { service: "chat", messages: [] },
    { service: "chat", messages: [{ role: "system", content: "q" }] },
    { service: "chat", messages: [{ role: "user", content: "" }] },
    "not json",
  ];
  for (const body of cases) {
    const f = fakeFetch();
    const res = await handle(post(body), ENV, f.impl);
    assert.equal(res.status, 400, JSON.stringify(body).slice(0, 80));
    assert.equal(f.modelCalls().length, 0);
  }
  assert.throws(() => validate(null), HttpError);
});

test("SUPABASE_ANON_KEY missing: the caller's apikey header is used, else 500", async () => {
  const env = { ...ENV };
  delete env.SUPABASE_ANON_KEY;
  const f = fakeFetch({ model: [textReply("ok")] });
  const req = post(chatBody);
  req.headers.set("apikey", "anon-from-page");
  assert.equal((await handle(req, env, f.impl)).status, 200);
  assert.equal(new Headers(f.calls[0].init.headers).get("apikey"), "anon-from-page");
  const f2 = fakeFetch();
  const res2 = await handle(post(chatBody), env, f2.impl);
  assert.equal(res2.status, 500);
  assert.equal(f2.calls.length, 0);
});

// ---------- agent loop ----------

test("chat loop: list_projects tool_use, tool_result sent back, PostgREST called with the user's token, final text", async () => {
  const f = fakeFetch({ model: [toolReply("list_projects", { countries: ["HR"], programme: "TSI" }), textReply("Croatia has **2** projects.")] });
  const res = await handle(post(chatBody), ENV, f.impl);
  assert.equal(res.status, 200);
  const text = await res.text();
  assert.deepEqual(JSON.parse(text), { answer: "Croatia has **2** projects." });
  assert.ok(!text.includes("sk-ant-secret-test"));

  const [first, second] = f.modelCalls();
  const h = new Headers(first.init.headers);
  assert.equal(h.get("x-api-key"), "sk-ant-secret-test");
  assert.equal(h.get("anthropic-version"), "2023-06-01");
  assert.equal(h.get("content-type"), "application/json");
  assert.ok(first.init.signal, "a timeout signal is set");
  const body1 = sent(first);
  assert.equal(body1.model, DEFAULT_MODEL);
  assert.equal(body1.max_tokens, 16000);
  assert.deepEqual(body1.output_config, { effort: "low" });
  assert.equal(body1.system[0].text, CHAT_SYSTEM);
  assert.deepEqual(body1.system[0].cache_control, { type: "ephemeral" });
  assert.deepEqual(body1.tools.map((t: { name: string }) => t.name), ["search_projects", "list_projects", "get_project", "search_csrs", "get_csr"]);
  assert.deepEqual(body1.messages, [{ role: "user", content: [{ type: "text", text: chatBody.messages[0].content, cache_control: { type: "ephemeral" } }] }]);
  assert.equal(body1.tool_choice, undefined);

  const body2 = sent(second);
  assert.equal(body2.messages.length, 3);
  assert.deepEqual(body2.messages[0], chatBody.messages[0], "earlier messages carry no breakpoint");
  assert.deepEqual(body2.messages[1], { role: "assistant", content: toolReply("list_projects", {}).body.content.map((b: any) => b.type === "tool_use" ? { ...b, input: { countries: ["HR"], programme: "TSI" } } : b) });
  const toolResult = body2.messages[2];
  assert.equal(toolResult.role, "user");
  assert.equal(toolResult.content[0].type, "tool_result");
  assert.equal(toolResult.content[0].tool_use_id, "toolu_1");
  assert.equal(toolResult.content[0].is_error, undefined);
  assert.deepEqual(toolResult.content[0].cache_control, { type: "ephemeral" });
  assert.match(toolResult.content[0].content, /Total matching projects: 3/);

  const [dbCall] = f.dbCalls();
  assert.match(dbCall.url, /^https:\/\/project\.supabase\.co\/rest\/v1\/projects\?select=project_id,title,programme,year,countries,policy_area,detail_page,url,csrs/);
  assert.match(dbCall.url, /countries=ov\.%7BHR%7D/);
  assert.match(dbCall.url, /programme=eq\.TSI/);
  const dh = new Headers(dbCall.init.headers);
  assert.equal(dh.get("Authorization"), "Bearer user-jwt");
  assert.equal(dh.get("apikey"), "anon-test");
});

test("TSI_MODEL overrides the model; impact: max_tokens 16000, effort medium, impact prompt, web_search server tool with max_uses 4", async () => {
  const f = fakeFetch({ model: [textReply("Report\n\nImpact score: 5/10 (moderate) — Confidence: medium")] });
  const res = await handle(post(impactBody), { ...ENV, TSI_MODEL: "claude-test-model" }, f.impl);
  assert.equal(res.status, 200);
  const body = sent(f.modelCalls()[0]);
  assert.equal(body.model, "claude-test-model");
  assert.equal(body.max_tokens, 16000);
  assert.deepEqual(body.output_config, { effort: "medium" });
  assert.equal(body.system[0].text, impactSystem());
  assert.match(body.system[0].text, /Impact score: X\/10/);
  assert.deepEqual(body.tools.find((t: { name: string }) => t.name === "web_search"), { type: "web_search_20260209", name: "web_search", max_uses: 4 });
  assert.deepEqual(TOOLS_IMPACT.map((t) => t.name), ["get_project", "search_projects", "web_search"]);
  assert.ok(!TOOLS_CHAT.some((t) => t.name === "web_search"));
});

test("server_tool_use and web_search_tool_result blocks are passed back unchanged and never answered", async () => {
  const content = [
    { type: "text", text: "I'll look." },
    { type: "server_tool_use", id: "srvtoolu_1", name: "web_search", input: { query: "Croatia health reform" } },
    { type: "web_search_tool_result", tool_use_id: "srvtoolu_1", content: [{ type: "web_search_result", url: "https://oecd.example", title: "OECD", encrypted_content: "abc" }] },
    { type: "tool_use", id: "toolu_9", name: "get_project", input: { project_id: "p1" } },
  ];
  const f = fakeFetch({ model: [{ body: { content, stop_reason: "tool_use" } }, textReply("Final report")] });
  const res = await handle(post(impactBody), ENV, f.impl);
  assert.equal(res.status, 200);
  const body2 = sent(f.modelCalls()[1]);
  assert.deepEqual(body2.messages[1], { role: "assistant", content });
  assert.equal(body2.messages[2].content.length, 1, "only the client tool is answered");
  assert.equal(body2.messages[2].content[0].tool_use_id, "toolu_9");
});

test("pause_turn: the conversation is resent with the paused content, without a tool_result message", async () => {
  const paused = [{ type: "server_tool_use", id: "srvtoolu_1", name: "web_search", input: { query: "q" } }];
  const f = fakeFetch({ model: [{ body: { content: paused, stop_reason: "pause_turn" } }, textReply("Done")] });
  const result = await agentLoop("impact", impactBody.messages as any, ENV, f.impl, Date.now, CTX);
  assert.equal(result.answer, "Done");
  assert.equal(result.steps, 1);
  assert.equal(result.stats.web_searches, 1);
  const body2 = sent(f.modelCalls()[1]);
  assert.equal(body2.messages.length, 2);
  assert.deepEqual(body2.messages[1], { role: "assistant", content: paused }, "assistant content sent back unchanged (no breakpoint)");
  assert.equal(body2.tool_choice, undefined);
});

test("stops after 8 tool rounds: the last call forbids tools", async () => {
  const tools = Array.from({ length: MAX_STEPS }, () => toolReply("search_projects", { query: "health" }));
  const f = fakeFetch({ model: [...tools, textReply("Final answer")] });
  const result = await agentLoop("chat", chatBody.messages as any, ENV, f.impl, Date.now, CTX);
  const calls = f.modelCalls();
  assert.equal(calls.length, MAX_STEPS + 1);
  assert.equal(result.steps, MAX_STEPS);
  assert.deepEqual(sent(calls[calls.length - 1]).tool_choice, { type: "none" });
  assert.ok(calls.slice(0, -1).every((c) => sent(c).tool_choice === undefined));
  assert.equal(result.answer, "Final answer");
});

test("last turn still mid tool use: the pre-tool preamble is never returned (502)", async () => {
  const f = fakeFetch({ model: [toolReply("search_projects", { query: "health" })] }); // tool_use forever, even on the final call
  const res = await handle(post(chatBody), ENV, f.impl);
  assert.equal(res.status, 502);
  assert.deepEqual(await res.json(), { error: "Empty response from the model" });
  assert.equal(f.modelCalls().length, MAX_STEPS + 1);
});

test("refusal: 422; max_tokens: text kept with a truncation note, or 502 without text", async () => {
  const refused = fakeFetch({ model: [{ body: { content: [{ type: "text", text: "partial" }], stop_reason: "refusal" } }] });
  const res = await handle(post(chatBody), ENV, refused.impl);
  assert.equal(res.status, 422);
  assert.deepEqual(await res.json(), { error: "The model declined this request" });
  const cut = fakeFetch({ model: [{ body: { content: [{ type: "text", text: "Long answer" }], stop_reason: "max_tokens" } }] });
  assert.equal((await agentLoop("chat", chatBody.messages as any, ENV, cut.impl, Date.now, CTX)).answer, "Long answer\n\n[answer truncated]");
  const cutEmpty = fakeFetch({ model: [{ body: { content: [{ type: "thinking", thinking: "" }], stop_reason: "max_tokens" } }] });
  const res2 = await handle(post(chatBody), ENV, cutEmpty.impl);
  assert.equal(res2.status, 502);
});

test("client tools of one turn run in parallel; results in block order; errors flagged is_error", async () => {
  const content = [
    { type: "tool_use", id: "a", name: "get_project", input: { project_id: "p1" } },
    { type: "tool_use", id: "b", name: "search_projects", input: {} },
    { type: "tool_use", id: "c", name: "nope", input: {} },
  ];
  let inFlight = 0;
  let maxInFlight = 0;
  const base = fakeFetch({ model: [{ body: { content, stop_reason: "tool_use" } }, textReply("ok")] });
  const impl = async (url: string, init: RequestInit) => {
    if (!url.includes("/rest/v1/projects?")) return base.impl(url, init);
    inFlight++;
    maxInFlight = Math.max(maxInFlight, inFlight);
    await new Promise((r) => setTimeout(r, 20));
    inFlight--;
    return json([{ ...PROJECTS[0], results: null, summary: null, deliverables: 0, project_csr_links: [] }]);
  };
  // two database tools in one turn: make the second one a database call too
  content[1] = { type: "tool_use", id: "b", name: "get_project", input: { project_id: "p2" } };
  await agentLoop("chat", chatBody.messages as any, ENV, impl, Date.now, CTX);
  assert.equal(maxInFlight, 2, "both database calls were in flight together");
  const results = sent(base.modelCalls()[1]).messages[2].content;
  assert.deepEqual(results.map((r: any) => r.tool_use_id), ["a", "b", "c"]);
  assert.equal(results[0].is_error, undefined);
  assert.equal(results[2].is_error, true);
  assert.match(results[2].content, /^Tool error: unknown tool nope/);
});

test("web search: once 4 searches are used in the run, the server tool is no longer offered (history kept)", async () => {
  const search = (id: string) => [{ type: "server_tool_use", id, name: "web_search", input: { query: "q" } },
    { type: "web_search_tool_result", tool_use_id: id, content: [] }];
  const f = fakeFetch({ model: [
    { body: { content: [...search("s1"), ...search("s2"), ...search("s3")], stop_reason: "pause_turn" } },
    { body: { content: [...search("s4"), { type: "tool_use", id: "t1", name: "get_project", input: { project_id: "p1" } }], stop_reason: "tool_use" } },
    textReply("Report"),
  ] });
  const result = await agentLoop("impact", impactBody.messages as any, ENV, f.impl, Date.now, CTX);
  assert.equal(result.stats.web_searches, 4);
  const calls = f.modelCalls().map(sent);
  assert.ok(calls[1].tools.some((t: any) => t.name === "web_search"), "3 searches: still offered");
  assert.ok(!calls[2].tools.some((t: any) => t.name === "web_search"), "4 searches: dropped");
  assert.deepEqual(calls[2].tools.map((t: any) => t.name), ["get_project", "search_projects"]);
  assert.equal(calls[2].messages.length, 4, "history intact");
});

test("usage is summed and logged without content", async () => {
  const usage = { input_tokens: 100, output_tokens: 20, cache_read_input_tokens: 50, cache_creation_input_tokens: 10 };
  const f = fakeFetch({ model: [{ body: { ...toolReply("list_projects", {}).body as object, usage } }, { body: { ...textReply("secret answer").body as object, usage } }] });
  const logs: string[] = [];
  const orig = console.log;
  console.log = (s: string) => { logs.push(s); };
  try {
    assert.equal((await handle(post(chatBody), ENV, f.impl)).status, 200);
  } finally {
    console.log = orig;
  }
  const last = JSON.parse(logs[logs.length - 1]);
  assert.deepEqual(last, { service: "chat", status: 200, ms: last.ms, steps: 1, input_tokens: 200, output_tokens: 40, cache_read_tokens: 100, cache_creation_tokens: 20, web_searches: 0 });
  assert.ok(!logs.join("\n").includes("secret answer") && !logs.join("\n").includes("Croatia"));
});

test("deadline: database calls bounded by the remaining time; list_projects stops paging with a partial note", async () => {
  let t = 0;
  const page = Array.from({ length: 1000 }, (_, i) => ({ ...PROJECTS[2], project_id: `x${i}` }));
  const calls: string[] = [];
  const impl = async (url: string) => {
    calls.push(url);
    t += 3000; // each page takes 3 s
    return json(page);
  };
  const out = await runTool("list_projects", {}, { ...CTX, fetchImpl: impl, now: () => t, deadline: 7000 });
  assert.equal(calls.length, 1, "second page not fetched with < 5 s left");
  assert.match(out, /^Partial: time limit/);
  assert.equal(await runTool("get_project", { project_id: "p1" }, { ...CTX, fetchImpl: impl, now: () => 10000, deadline: 7000 }), "Tool error: time limit reached");
  assert.equal(DEADLINE_MS, 140000);
  assert.ok(BUDGET_MS < DEADLINE_MS);
});

test("withCacheBreakpoint does not modify the conversation; emptyStats", () => {
  const conv: any[] = [{ role: "user", content: "q" }];
  const out = withCacheBreakpoint(conv);
  assert.deepEqual(conv, [{ role: "user", content: "q" }]);
  assert.deepEqual(out[0].content, [{ type: "text", text: "q", cache_control: { type: "ephemeral" } }]);
  assert.deepEqual(emptyStats().steps, 0);
});

test("time budget exceeded: next call is the last one, with tool_choice none", async () => {
  let t = 0;
  const now = () => t;
  const base = fakeFetch({ model: [toolReply("search_projects", { query: "health" }), textReply("Partial answer")] });
  const impl = async (url: string, init: RequestInit) => {
    if (url.startsWith("https://api.anthropic.com")) t += BUDGET_MS + 1000; // the first model call already exceeds the budget
    return base.impl(url, init);
  };
  const result = await agentLoop("chat", chatBody.messages as any, ENV, impl, now, CTX);
  const calls = base.modelCalls();
  assert.equal(calls.length, 2);
  assert.deepEqual(sent(calls[1]).tool_choice, { type: "none" });
  assert.equal(result.answer, "Partial answer");
});

test("Anthropic 500: 502 without the error detail", async () => {
  const f = fakeFetch({ model: [{ status: 500, body: { type: "error", error: { type: "api_error", message: "internal trace" } } }] });
  const res = await handle(post(chatBody), ENV, f.impl);
  assert.equal(res.status, 502);
  const text = await res.text();
  assert.deepEqual(JSON.parse(text), { error: "Model service error 500" });
  assert.ok(!text.includes("internal trace"));
});

test("Anthropic timeout: 504; empty final text: 502", async () => {
  const f = fakeFetch();
  const impl = async (url: string, init: RequestInit) => {
    if (url.startsWith("https://api.anthropic.com")) throw new DOMException("The operation timed out.", "TimeoutError");
    return f.impl(url, init);
  };
  assert.equal((await handle(post(chatBody), ENV, impl)).status, 504);
  const f2 = fakeFetch({ model: [{ body: { content: [{ type: "text", text: "  " }], stop_reason: "end_turn" } }] });
  const res = await handle(post(chatBody), ENV, f2.impl);
  assert.equal(res.status, 502);
  assert.deepEqual(await res.json(), { error: "Empty response from the model" });
});

test("ANTHROPIC_API_KEY missing: 500", async () => {
  const env = { ...ENV };
  delete env.ANTHROPIC_API_KEY;
  const f = fakeFetch();
  const res = await handle(post(chatBody), env, f.impl);
  assert.equal(res.status, 500);
  assert.deepEqual(await res.json(), { error: "ANTHROPIC_API_KEY not configured" });
  assert.equal(f.modelCalls().length, 0);
});

test("a tool not offered to the service is refused in the loop (impact cannot list_projects)", async () => {
  const f = fakeFetch({ model: [toolReply("list_projects", {}), textReply("ok")] });
  await agentLoop("impact", impactBody.messages as any, ENV, f.impl, Date.now, CTX);
  const result = sent(f.modelCalls()[1]).messages[2].content[0];
  assert.match(result.content, /^Tool error: unknown tool list_projects/);
  assert.equal(result.is_error, true);
  assert.equal(f.dbCalls().length, 0);
});

test("answerText: report after the last tool block, cited fragments joined as written, thinking ignored", () => {
  assert.equal(answerText([
    { type: "text", text: "I'll search." }, { type: "server_tool_use", id: "s", name: "web_search", input: {} },
    { type: "web_search_tool_result", tool_use_id: "s", content: [] },
    { type: "text", text: "The reform " }, { type: "text", text: "was adopted", citations: [] }, { type: "text", text: " in 2023." },
  ]), "The reform was adopted in 2023.");
  assert.equal(answerText([{ type: "text", text: "A" }, { type: "tool_use", id: "t", name: "x", input: {} }]), "A");
  assert.equal(answerText([{ type: "text", text: "A" }, { type: "tool_use", id: "t", name: "x", input: {} }], true), "");
  assert.equal(answerText([
    { type: "thinking", thinking: "" }, { type: "text", text: "Part one, " }, { type: "redacted_thinking", data: "x" },
    { type: "text", text: "part two." }, { type: "thinking", thinking: "" },
  ]), "Part one, part two.");
  assert.equal(answerText([]), "");
});

test("validate: the first message must come from the user", () => {
  assert.throws(() => validate({ service: "chat", messages: [{ role: "assistant", content: "a" }, { role: "user", content: "q" }] }),
    (e: unknown) => e instanceof HttpError && e.status === 400 && /first message/.test(e.message));
});

test("is_tester call has a timeout signal", async () => {
  const f = fakeFetch({ tester: false });
  await handle(post(chatBody), ENV, f.impl);
  assert.ok(f.calls[0].init.signal);
});

// ---------- tools ----------

test("runTool: unknown tool and invalid inputs give 'Tool error', never throw", async () => {
  const f = fakeFetch();
  const ctx = { ...CTX, fetchImpl: f.impl };
  assert.match(await runTool("drop_tables", {}, ctx), /^Tool error: unknown tool drop_tables/);
  assert.match(await runTool("toString", {}, ctx), /^Tool error: unknown tool/);
  assert.match(await runTool("search_projects", {}, ctx), /^Tool error: query is required/);
  assert.match(await runTool("list_projects", { countries: ["XX"] }, ctx), /^Tool error: unknown country code/);
  assert.match(await runTool("list_projects", { policy_area: "space" }, ctx), /^Tool error: unknown policy_area/);
  assert.match(await runTool("list_projects", { year_from: "soon" }, ctx), /^Tool error: year_from must be a year/);
  assert.match(await runTool("get_csr", { csr_id: "nonsense" }, ctx), /^Tool error: csr_id must look like/);
  assert.equal(f.dbCalls().length, 0);
  const failing = fakeFetch({ dbStatus: 500 });
  assert.equal(await runTool("get_project", { project_id: "p1" }, { ...CTX, fetchImpl: failing.impl }), "Tool error: database request failed (500)");
});

test("list_projects csr filter: no false prefix, case-insensitive, csr- prefix added", async () => {
  const f = fakeFetch();
  const ctx = { ...CTX, fetchImpl: f.impl };
  const exact = await runTool("list_projects", { csr: "csr-HR-2022-1" }, ctx);
  assert.match(exact, /Total matching projects: 1 /);
  assert.match(exact, /Health reform in Croatia/);
  assert.doesNotMatch(exact, /Tax administration/);
  assert.match(exact, /CSRs \(AI-assessed\): csr-HR-2022-1\.2/);
  const prefix = await runTool("list_projects", { csr: "hr-2022" }, ctx);
  assert.match(prefix, /Total matching projects: 2 /);
  assert.deepEqual(csrMatches(["csr-HR-2022-10.1", "csr-HR-2022-1"], normalizeCsr("CSR-hr-2022-1")), ["csr-HR-2022-1"]);
  assert.deepEqual(csrMatches(["csr-HR-2022-1.2"], normalizeCsr("csr-HR-2022-1.2")), ["csr-HR-2022-1.2"]);
  assert.equal(normalizeCsr("  "), null);
});

test("list_projects: exact breakdowns over all rows, truncated list, title filter, paging and server-side filters", async () => {
  const many = Array.from({ length: 1000 }, (_, i) => ({ ...PROJECTS[2], project_id: `x${i}`, title: `Project ${i}` }));
  const calls: string[] = [];
  const impl = async (url: string) => {
    calls.push(url);
    const offset = Number(/offset=(\d+)/.exec(url)?.[1] ?? 0);
    return json(offset === 0 ? many : offset === 1000 ? PROJECTS : []);
  };
  const out = await runTool("list_projects", { year_from: 2019, year_to: 2023, detail_only: true, policy_area: "health-and-long-term-care", limit: 999 }, { ...CTX, fetchImpl: impl });
  assert.equal(calls.length, 2);
  assert.match(calls[0], /year=gte\.2019&year=lte\.2023/);
  assert.match(calls[0], /policy_area=eq\.health-and-long-term-care/);
  assert.match(calls[0], /detail_page=is\.true/);
  assert.match(out, /^Total matching projects: 1003 \(by programme: SRSP 1001, TSI 2\)/);
  assert.match(out, /By country: PT 1001, HR 2, SI 1/);
  assert.doesNotMatch(out, /By policy area/);
  assert.match(out, /Showing 50 of 1003:/);
  const titled = await runTool("list_projects", { title_contains: "REFORM" }, { ...CTX, fetchImpl: fakeFetch().impl });
  assert.match(titled, /Total matching projects: 2 /);
  assert.match(titled, /By policy area: health-and-long-term-care 1, none 1/);
  assert.match(titled, /\[Health reform in Croatia\]\(https:\/\/reform\.example\/p1\)/);
});

test("search_projects and search_csrs call the RPCs with bounded limits and format results", async () => {
  const f = fakeFetch({ rpc: {
    tsi_search_projects: [{ ...PROJECTS[0], summary: "Supports the health reform.", rank: 0.5 }],
    tsi_search_csrs: [{ csr_id: "csr-HR-2022-1", country: "HR", year: 2022, title: "Health", text: "Improve health care.", assessment: "Some progress", assessment_year: 2023, rank: 0.3 }],
  } });
  const ctx = { ...CTX, fetchImpl: f.impl };
  const projects = await runTool("search_projects", { query: "health", countries: ["el"], programme: "tsi", limit: 500 }, ctx);
  assert.match(projects, /\[Health reform in Croatia\]\(https:\/\/reform\.example\/p1\) \(p1\)/);
  assert.match(projects, /Summary: Supports the health reform\./);
  const csrs = await runTool("search_csrs", { query: "health", year_from: 2022 }, ctx);
  assert.match(csrs, /csr-HR-2022-1 — HR 2022 — Health — Assessment \(2023\): Some progress/);
  const [p, c] = f.dbCalls();
  assert.equal(p.url, "https://project.supabase.co/rest/v1/rpc/tsi_search_projects");
  assert.deepEqual(sent(p), { q: "health", p_countries: ["GR"], p_programme: "TSI", p_year_from: null, p_year_to: null, lim: 20 });
  assert.equal(p.init.method, "POST");
  assert.deepEqual(sent(c), { q: "health", p_countries: null, p_year_from: 2022, p_year_to: null, lim: 10 });
  assert.match(await runTool("search_projects", { query: "nothing" }, { ...CTX, fetchImpl: fakeFetch().impl }), /^No project matches/);
});

test("get_project and get_csr: embedded links, canonical CSR id", async () => {
  const project = { ...PROJECTS[1], results: "Report delivered", summary: "AI summary.", deliverables: 2,
    project_csr_links: [{ level: "direct", rationale: "Same reform.", basis: "full_text", csr_parts: { part_id: "csr-HR-2022-10.1", csr_id: "csr-HR-2022-10", text: "Improve tax collection.", assessment: "Limited progress" } }] };
  const csr = { csr_id: "csr-HR-2022-1", country: "HR", year: 2022, number: "1", title: "Health", text: "Improve health.", assessment: "Some progress", assessment_year: 2023, source_url: "https://ec.example/csr",
    csr_parts: [{ part_id: "csr-HR-2022-1.2", text: "Hospitals.", assessment: null, project_csr_links: [{ level: "partial", rationale: "Related.", projects: PROJECTS[0] }] }] };
  const calls: string[] = [];
  const impl = async (url: string) => { calls.push(url); return json(url.includes("/csrs?") ? [csr] : [project]); };
  const ctx = { ...CTX, fetchImpl: impl };
  const p = await runTool("get_project", { project_id: "p2" }, ctx);
  assert.match(p, /Detail page: no \(known by its title, country and year only\)/);
  assert.match(p, /csr-HR-2022-10\.1 \(CSR csr-HR-2022-10\) — direct — rationale: Same reform\./);
  assert.match(calls[0], /\/rest\/v1\/projects\?select=.*&project_id=eq\.p2&limit=1$/);
  assert.ok(!decodeURIComponent(calls[0]).includes("fts"));
  const c = await runTool("get_csr", { csr_id: "hr-2022-1.2" }, ctx);
  assert.match(calls[1], /\/rest\/v1\/csrs\?select=.*&csr_id=eq\.csr-HR-2022-1&limit=1$/);
  assert.match(c, /Part csr-HR-2022-1\.2: Hospitals\./);
  assert.match(c, /\[Health reform in Croatia\]\(https:\/\/reform\.example\/p1\) \(p1\) — TSI 2023 — partial \(AI-assessed\)/);
  assert.equal(canonicalCsrId("CSR-hr-2022-3"), "csr-HR-2022-3");
  const none = await runTool("get_project", { project_id: "zz" }, { ...CTX, fetchImpl: async () => json([]) });
  assert.match(none, /^No project with id "zz"/);
});

test("prompts: chat rules and filter values, impact scoring rubric", () => {
  assert.match(CHAT_SYSTEM, /language of the user's question/);
  assert.match(CHAT_SYSTEM, /call list_projects/);
  assert.match(CHAT_SYSTEM, /Deliverable full texts .* are not available here/);
  assert.match(CHAT_SYSTEM, /GR Greece/);
  assert.match(CHAT_SYSTEM, /skills-education-and-training/);
  assert.match(impactSystem(), /Call get_project/);
  assert.match(impactSystem(), /At most 4 searches/);
  assert.match(impactSystem(), /9-10 \(strong\)/);
});
