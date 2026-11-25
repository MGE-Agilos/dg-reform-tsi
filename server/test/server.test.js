// Tests of the application service (no network: PostgREST and the identity provider are faked).
// Run: cd server && npm test
import { test } from "node:test";
import assert from "node:assert/strict";
import { createServer } from "node:http";
import { mkdtempSync, writeFileSync, mkdirSync } from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";
import { signJwt, verifyJwt, seal, unseal, uuidV5, userToken } from "../src/tokens.js";
import { loadConfig, ConfigError } from "../src/config.js";
import { createApp, safeNext, parseCookies } from "../src/app.js";
import { oidcProvider } from "../src/auth.js";
import { runAdmin, validate, HttpError } from "../src/admin.js";
import { assistantEnv, routedFetch, createAssistant, proxyFetch } from "../src/assistant.js";
import { llmConfig } from "../src/config.js";
import { checkGateway } from "../assistant-check.js";
import { request as httpRequest } from "node:http";
import { connect as netConnect } from "node:net";

const SECRET = "s".repeat(40);
const SESSION = "k".repeat(40);
const BASE_ENV = { PUBLIC_URL: "http://localhost:8080", JWT_SECRET: SECRET, SESSION_SECRET: SESSION, AUTH_MODE: "dev", POSTGREST_URL: "http://pgrst" };

test("JWT: signed, verified, rejected when tampered, expired or signed with another secret", () => {
  const exp = Math.floor(Date.now() / 1000) + 60;
  const t = signJwt({ sub: "u", role: "authenticated", exp }, SECRET);
  assert.equal(verifyJwt(t, SECRET).sub, "u");
  assert.equal(verifyJwt(t, "x".repeat(40)), null);
  const [h, b, s] = t.split(".");
  const forged = Buffer.from(JSON.stringify({ sub: "u", role: "service_role", exp })).toString("base64url");
  assert.equal(verifyJwt(`${h}.${forged}.${s}`, SECRET), null);
  assert.equal(verifyJwt(signJwt({ exp: 1 }, SECRET), SECRET), null);
  assert.equal(verifyJwt(`${Buffer.from('{"alg":"none"}').toString("base64url")}.${b}.`, SECRET), null);
});

test("sealed cookie and stable uuid v5", () => {
  const v = seal({ email: "a@b.eu", exp: Math.floor(Date.now() / 1000) + 60 }, SESSION);
  assert.equal(unseal(v, SESSION).email, "a@b.eu");
  assert.equal(unseal(v + "x", SESSION), null);
  assert.equal(unseal(v, "z".repeat(40)), null);
  assert.equal(unseal(seal({ exp: 1 }, SESSION), SESSION), null);
  const id = uuidV5("https://idp|123");
  assert.match(id, /^[0-9a-f]{8}-[0-9a-f]{4}-5[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$/);
  assert.equal(uuidV5("https://idp|123"), id);
  assert.notEqual(uuidV5("https://idp|124"), id);
  const { token } = userToken({ id, email: "a@b.eu" }, SECRET, 15);
  assert.deepEqual(Object.keys(verifyJwt(token, SECRET)).sort(), ["aud", "email", "exp", "iat", "role", "sub"]);
});

test("config: secrets of 32 characters or more, test sign-in only on localhost, OIDC settings required", () => {
  assert.equal(loadConfig(BASE_ENV).authMode, "dev");
  assert.throws(() => loadConfig({ ...BASE_ENV, JWT_SECRET: "short" }), ConfigError);
  assert.throws(() => loadConfig({ ...BASE_ENV, PUBLIC_URL: "https://tsi.example.eu" }), /only allowed/);
  assert.throws(() => loadConfig({ ...BASE_ENV, AUTH_MODE: "oidc", PUBLIC_URL: "https://tsi.example.eu" }), /OIDC_ISSUER/);
  const c = loadConfig({ ...BASE_ENV, AUTH_MODE: "oidc", PUBLIC_URL: "https://tsi.example.eu/", OIDC_ISSUER: "https://idp", OIDC_CLIENT_ID: "c" });
  assert.equal(c.oidc.redirectUri, "https://tsi.example.eu/api/auth/callback");
  assert.equal(c.secure, true);
});

test("safeNext keeps page routes only", () => {
  assert.equal(safeNext("#projects?q=water&country=PT"), "#projects?q=water&country=PT");
  assert.equal(safeNext("https://evil.example"), "#dashboard");
  assert.equal(safeNext("#x\"><script>"), "#dashboard");
  assert.equal(safeNext(null), "#dashboard");
});

// ---------- HTTP tests on a real server, with PostgREST faked ----------

function fakePostgrest(testers = [{ email: "admin@x.eu", role: "admin" }, { email: "user@x.eu", role: "user" }]) {
  const calls = [];
  const rows = testers.map((t) => ({ invited_at: null, invited_by: null, ...t }));
  const impl = async (url, init = {}) => {
    const u = new URL(url);
    calls.push({ url: u, init });
    const json = (body, status = 200, headers = {}) => new Response(JSON.stringify(body), { status, headers: { "content-type": "application/json", ...headers } });
    if (u.pathname === "/testers") {
      const email = u.searchParams.get("email")?.replace(/^eq\./, "");
      if ((init.method || "GET") === "GET") {
        let out = rows;
        if (email) out = rows.filter((r) => r.email === email);
        if (u.searchParams.get("role") === "eq.admin") out = rows.filter((r) => r.role === "admin");
        return json(out);
      }
      if (init.method === "POST") { rows.push(JSON.parse(init.body)); return new Response(null, { status: 201 }); }
      if (init.method === "PATCH") { rows.find((r) => r.email === email).role = JSON.parse(init.body).role; return new Response(null, { status: 204 }); }
      if (init.method === "DELETE") { rows.splice(rows.findIndex((r) => r.email === email), 1); return new Response(null, { status: 204 }); }
    }
    if (u.pathname === "/page_views") return json([{ started_at: "2026-11-20T10:00:00Z" }]);
    return json([{ project_id: "p1" }], 200, { "content-range": "0-0/1", "set-cookie": "leak=1" });
  };
  return { impl, calls, rows };
}

async function serve(env, deps) {
  const app = createApp(loadConfig({ ...BASE_ENV, ...env }), { log: () => {}, ...deps });
  const server = createServer(app);
  await new Promise((r) => server.listen(0, "127.0.0.1", r));
  const base = `http://127.0.0.1:${server.address().port}`;
  return { base, close: () => new Promise((r) => server.close(r)) };
}

async function devSignIn(base, email) {
  const res = await fetch(`${base}/api/auth/dev`, { method: "POST", redirect: "manual",
    headers: { "content-type": "application/x-www-form-urlencoded" }, body: new URLSearchParams({ email, next: "#projects" }) });
  assert.equal(res.status, 303);
  assert.equal(res.headers.get("location"), "http://localhost:8080/#projects");
  const set = res.headers.getSetCookie().find((c) => c.startsWith("tsi_session="));
  assert.match(set, /HttpOnly; SameSite=Lax/);
  return set.split(";")[0];
}

test("test sign-in, session token for the data requests, sign-out", async () => {
  const pg = fakePostgrest();
  const s = await serve({}, { fetchImpl: pg.impl });
  try {
    assert.equal((await fetch(`${s.base}/api/auth/session`)).status, 401);
    assert.match(await (await fetch(`${s.base}/api/auth/login?next=%23csrs`)).text(), /Local test mode/);
    const cookie = await devSignIn(s.base, "User@X.eu");
    const session = await (await fetch(`${s.base}/api/auth/session`, { headers: { cookie } })).json();
    assert.equal(session.user.email, "user@x.eu");
    assert.equal(session.user.id, uuidV5("dev-local|user@x.eu"));
    const claims = verifyJwt(session.access_token, SECRET);
    assert.equal(claims.role, "authenticated");
    assert.equal(claims.sub, session.user.id);
    const out = await fetch(`${s.base}/api/auth/logout`, { method: "POST", headers: { cookie } });
    assert.equal(out.status, 204);
    assert.match(out.headers.get("set-cookie"), /tsi_session=; .*Max-Age=0/);
  } finally { await s.close(); }
});

test("data requests: forwarded to PostgREST with allowed headers only (no cookie, no apikey), answer headers filtered", async () => {
  const pg = fakePostgrest();
  const s = await serve({}, { fetchImpl: pg.impl });
  try {
    const res = await fetch(`${s.base}/api/rest/v1/projects?select=project_id&limit=1`, {
      headers: { Authorization: "Bearer abc", apikey: "server", cookie: "tsi_session=x", Prefer: "count=exact",
        "Accept-Profile": "public" } }); // schema chosen by PostgREST's db-schemas, not by the page
    assert.equal(res.status, 200);
    assert.equal(res.headers.get("content-range"), "0-0/1");
    assert.equal(res.headers.get("set-cookie"), null);
    const call = pg.calls.at(-1);
    assert.equal(call.url.href, "http://pgrst/projects?select=project_id&limit=1");
    assert.deepEqual(Object.keys(call.init.headers).sort(), ["accept", "authorization", "prefer"]); // accept: added by fetch, allowed
    assert.equal((await fetch(`${s.base}/api/rest/v1/projects`, { method: "PUT" })).status, 405);
  } finally { await s.close(); }
});

test("administration: administrators only; invite, roles and revoke with the last-administrator rules", async () => {
  const pg = fakePostgrest();
  const s = await serve({}, { fetchImpl: pg.impl });
  const token = (email) => userToken({ id: uuidV5(email), email }, SECRET, 5).token;
  const admin = (body, email = "admin@x.eu") => fetch(`${s.base}/api/admin`, { method: "POST",
    headers: { "content-type": "application/json", Authorization: `Bearer ${token(email)}` }, body: JSON.stringify(body) });
  try {
    assert.equal((await fetch(`${s.base}/api/admin`, { method: "POST", body: "{}" })).status, 401);
    assert.equal((await admin({ action: "list" }, "user@x.eu")).status, 403);
    const list = await (await admin({ action: "list" })).json();
    assert.equal(list.users.length, 2);
    assert.equal(list.users[0].sso, true);
    assert.equal(list.users[0].last_sign_in_at, "2026-11-20T10:00:00Z");
    assert.deepEqual(await (await admin({ action: "invite", email: "New@X.eu" })).json(), { email: "new@x.eu", role: "user", existing: false });
    assert.equal(pg.rows.at(-1).invited_by, "admin@x.eu");
    assert.equal((await admin({ action: "invite", email: "new@x.eu" })).status, 409);
    assert.equal((await admin({ action: "set_role", email: "admin@x.eu", role: "user" })).status, 409, "last administrator");
    assert.equal((await admin({ action: "revoke", email: "admin@x.eu" })).status, 409, "own access");
    assert.equal((await admin({ action: "reset_password", email: "user@x.eu" })).status, 400);
    assert.deepEqual(await (await admin({ action: "revoke", email: "user@x.eu" })).json(), { email: "user@x.eu", revoked: true });
    assert.equal((await admin({ action: "nope" })).status, 400);
  } finally { await s.close(); }
});

test("admin rules without HTTP", async () => {
  assert.throws(() => validate({ action: "invite", email: "bad" }), HttpError);
  const db = { tester: async () => ({ email: "b@x.eu", role: "admin" }), adminCount: async () => 2, remove: async () => null };
  assert.deepEqual(await runAdmin(db, "a@x.eu", { action: "revoke", email: "b@x.eu" }), { email: "b@x.eu", revoked: true });
});

test("health, assistant not configured, static files without leaving the folder", async () => {
  const dir = mkdtempSync(join(tmpdir(), "tsi-static-"));
  mkdirSync(join(dir, "site"));
  writeFileSync(join(dir, "site", "index.html"), "<h1>ok</h1>");
  writeFileSync(join(dir, "secret.txt"), "no");
  const pg = fakePostgrest();
  const s = await serve({ STATIC_DIR: join(dir, "site") }, { fetchImpl: pg.impl });
  try {
    assert.deepEqual(await (await fetch(`${s.base}/api/health`)).json(), { ok: true, database: true, assistant: false, auth: "dev" });
    assert.equal((await fetch(`${s.base}/api/assistant`, { method: "POST", body: "{}" })).status, 503);
    assert.equal(await (await fetch(`${s.base}/`)).text(), "<h1>ok</h1>");
    assert.equal((await fetch(`${s.base}/%2e%2e/secret.txt`)).status, 404);
    assert.equal((await fetch(`${s.base}/..%2fsecret.txt`)).status, 404);
  } finally { await s.close(); }
});

test("OpenID Connect: redirect with PKCE, state checked at the callback, session from the ID token", async () => {
  const seen = {};
  const lib = {
    discovery: async () => ({ issuer: "https://idp" }),
    randomPKCECodeVerifier: () => "verifier", randomState: () => "state1", randomNonce: () => "nonce1",
    calculatePKCECodeChallenge: async () => "challenge",
    buildAuthorizationUrl: (_c, p) => { seen.params = p; return new URL(`https://idp/authorize?state=${p.state}`); },
    authorizationCodeGrant: async (_c, url, checks) => {
      seen.checks = checks;
      if (url.searchParams.get("state") !== checks.expectedState) throw new Error("state mismatch");
      return { claims: () => ({ iss: "https://idp", sub: "42", email: "Person@EC.europa.eu", email_verified: true }) };
    },
  };
  const env = { AUTH_MODE: "oidc", OIDC_ISSUER: "https://idp", OIDC_CLIENT_ID: "tsi" };
  const cfg = loadConfig({ ...BASE_ENV, ...env });
  const s = await serve(env, { fetchImpl: fakePostgrest().impl, provider: oidcProvider(cfg.oidc, lib) });
  try {
    const start = await fetch(`${s.base}/api/auth/login?next=%23impact`, { redirect: "manual" });
    assert.equal(start.status, 303);
    assert.equal(start.headers.get("location"), "https://idp/authorize?state=state1");
    assert.equal(seen.params.code_challenge_method, "S256");
    assert.equal(seen.params.redirect_uri, "http://localhost:8080/api/auth/callback");
    const pending = start.headers.getSetCookie()[0].split(";")[0];
    assert.equal((await fetch(`${s.base}/api/auth/callback?code=c&state=state1`)).status, 400, "no pending cookie");
    assert.equal((await fetch(`${s.base}/api/auth/callback?code=c&state=other`, { headers: { cookie: pending } })).status, 401);
    const back = await fetch(`${s.base}/api/auth/callback?code=c&state=state1`, { redirect: "manual", headers: { cookie: pending } });
    assert.equal(back.status, 303);
    assert.equal(back.headers.get("location"), "http://localhost:8080/#impact");
    assert.deepEqual(seen.checks, { pkceCodeVerifier: "verifier", expectedState: "state1", expectedNonce: "nonce1" });
    const cookie = back.headers.getSetCookie().find((c) => c.startsWith("tsi_session=")).split(";")[0];
    const session = await (await fetch(`${s.base}/api/auth/session`, { headers: { cookie } })).json();
    assert.equal(session.user.email, "person@ec.europa.eu");
    assert.equal(session.user.id, uuidV5("https://idp|42"));
    assert.equal((await fetch(`${s.base}/api/auth/dev`, { method: "POST", body: "email=a@b.eu" })).status, 404, "no test sign-in");
  } finally { await s.close(); }
});

test("parseCookies", () => {
  assert.deepEqual(parseCookies("a=1; tsi_session=x%3Dy"), { a: "1", tsi_session: "x=y" });
});

// ---------- assistant on an OpenAI-compatible gateway (GPT@EC) ----------

const LLM_ENV = { LLM_BASE_URL: "https://gateway.example/v1/", LLM_API_KEY: "gw-key" };

test("assistant settings: off without LLM_BASE_URL, key required, key file, defaults, reasoning on/off", () => {
  assert.equal(loadConfig(BASE_ENV).llm, null);
  assert.throws(() => loadConfig({ ...BASE_ENV, LLM_BASE_URL: "https://gateway.example/v1" }), /LLM_API_KEY/);
  assert.throws(() => loadConfig({ ...BASE_ENV, ...LLM_ENV, LLM_BASE_URL: "gateway" }), /LLM_BASE_URL/);
  assert.throws(() => loadConfig({ ...BASE_ENV, ...LLM_ENV, LLM_REASONING: "maybe" }), /LLM_REASONING/);
  assert.deepEqual(loadConfig({ ...BASE_ENV, ...LLM_ENV }).llm,
    { baseUrl: "https://gateway.example/v1", apiKey: "gw-key", model: "gpt-5.1", expandModel: "gpt-5.1", reasoning: true });
  const dir = mkdtempSync(join(tmpdir(), "tsi-llm-"));
  writeFileSync(join(dir, "key"), "file-key\n");
  assert.deepEqual(llmConfig({ LLM_BASE_URL: "https://g/v1", LLM_API_KEY_FILE: join(dir, "key"), LLM_MODEL: "m", LLM_EXPAND_MODEL: "s", LLM_REASONING: "OFF" }),
    { baseUrl: "https://g/v1", apiKey: "file-key", model: "m", expandModel: "s", reasoning: false });
});

test("assistant environment and routing: gateway calls through the proxy-aware fetch, the others direct", async () => {
  const cfg = loadConfig({ ...BASE_ENV, ...LLM_ENV });
  const env = assistantEnv(cfg);
  assert.equal(env.TSI_LLM_PROVIDER, "openai");
  assert.equal(env.TSI_OPENAI_BASE_URL, "https://gateway.example/v1");
  assert.equal(env.TSI_OPENAI_API_KEY, "gw-key");
  assert.equal(env.TSI_MODEL, "gpt-5.1");
  assert.equal(env.TSI_OPENAI_REASONING, "on");
  assert.equal(env.TSI_REST_URL, "http://pgrst");
  const claims = verifyJwt(env.SUPABASE_SERVICE_ROLE_KEY, SECRET);
  assert.equal(claims.role, "service_role");
  assert.ok(claims.exp - Math.floor(Date.now() / 1000) >= 290);
  const seen = [];
  const route = routedFetch("https://gateway.example/v1", async (u) => { seen.push("llm"); return new Response("{}"); },
    async (u) => { seen.push("direct"); return new Response("{}"); });
  await route("https://gateway.example/v1/chat/completions", {});
  await route("http://pgrst/projects?x", {});
  await route("https://gateway.example/v10/other", {});
  assert.deepEqual(seen, ["llm", "direct", "direct"]);
});

test("assistant in the service: user token to PostgREST, gateway key, impact saved with a service token; health", async () => {
  const dbCalls = [];
  const db = async (url, init = {}) => {
    const u = new URL(url);
    dbCalls.push({ u, init });
    if (u.pathname === "/rpc/is_tester") return Response.json(true);
    if (u.pathname === "/projects") return Response.json([{ project_id: "p1", title: "Health reform in Croatia", url: "https://reform.example/p1" }]);
    if (u.pathname === "/impact_reports") return new Response(null, { status: 201 });
    return Response.json([]);
  };
  const REPORT_TEXT = "Report\n\nImpact score: 6/10 (moderate) — Confidence: low";
  const replies = [
    { choices: [{ message: { content: null, tool_calls: [{ id: "c1", type: "function", function: { name: "get_project", arguments: "{\"project_id\":\"p1\"}" } }] }, finish_reason: "tool_calls" }] },
    { choices: [{ message: { content: REPORT_TEXT }, finish_reason: "stop" }] },
  ];
  const llmCalls = [];
  const llm = async (url, init) => { llmCalls.push({ url, init }); return Response.json(replies.shift()); };
  const assistant = await createAssistant(loadConfig({ ...BASE_ENV, ...LLM_ENV }), { fetchImpl: db, llmFetch: llm });
  const s = await serve(LLM_ENV, { fetchImpl: db, assistant });
  try {
    const cookie = await devSignIn(s.base, "user@x.eu");
    const { access_token } = await (await fetch(`${s.base}/api/auth/session`, { headers: { cookie } })).json();
    const res = await fetch(`${s.base}/api/assistant`, { method: "POST",
      headers: { authorization: `Bearer ${access_token}`, "content-type": "application/json" },
      body: JSON.stringify({ service: "impact", messages: [{ role: "user", content: "Project: Health reform in Croatia (id p1; TSI 2023; countries HR)" }] }) });
    assert.equal(res.status, 200);
    assert.deepEqual(await res.json(), { answer: REPORT_TEXT, saved: true });
    assert.equal(llmCalls.length, 2);
    assert.equal(llmCalls[0].url, "https://gateway.example/v1/chat/completions");
    assert.equal(new Headers(llmCalls[0].init.headers).get("authorization"), "Bearer gw-key");
    const tester = dbCalls.find((c) => c.u.pathname === "/rpc/is_tester");
    assert.equal(new Headers(tester.init.headers).get("authorization"), `Bearer ${access_token}`);
    const save = dbCalls.find((c) => c.u.pathname === "/impact_reports");
    assert.equal(verifyJwt(new Headers(save.init.headers).get("authorization").slice(7), SECRET).role, "service_role");
    assert.equal(JSON.parse(save.init.body).model, "gpt-5.1");
    assert.deepEqual(await (await fetch(`${s.base}/api/health`)).json(), { ok: true, database: true, assistant: true, auth: "dev" });
    assert.equal((await fetch(`${s.base}/api/assistant`, { method: "POST", body: "{}" })).status, 401);
  } finally { await s.close(); }
});

test("assistant-check: tool and JSON calls; gateway errors shown with status and excerpt", async () => {
  const llm = { baseUrl: "https://g/v1", apiKey: "k", model: "m", expandModel: "m", reasoning: false };
  const bodies = [];
  const good = async (url, init) => {
    assert.equal(url, "https://g/v1/chat/completions");
    const b = JSON.parse(init.body);
    bodies.push(b);
    return Response.json(b.tools
      ? { choices: [{ message: { tool_calls: [{ id: "1", type: "function", function: { name: "ping", arguments: "{\"ok\":true}" } }] } }] }
      : { choices: [{ message: { content: "{\"ok\": true}" } }] });
  };
  assert.deepEqual((await checkGateway(llm, good)).map((r) => r.ok), [true, true]);
  assert.ok(bodies.every((b) => b.model === "m" && !("reasoning_effort" in b)));
  assert.deepEqual(bodies[1].response_format, { type: "json_object" });
  const bad = async () => new Response("{\"error\":{\"message\":\"Unrecognized request argument supplied: reasoning_effort\"}}", { status: 400 });
  const r = await checkGateway({ ...llm, reasoning: true }, bad);
  assert.deepEqual(r.map((x) => x.ok), [false, false]);
  assert.match(r[0].detail, /^HTTP 400: .*reasoning_effort/);
  const noTool = async () => Response.json({ choices: [{ message: { content: "pong" } }] });
  assert.match((await checkGateway(llm, noTool))[0].detail, /without calling the tool/);
  const down = async () => { throw Object.assign(new TypeError("fetch failed"), { cause: { code: "ECONNREFUSED" } }); };
  assert.match((await checkGateway(llm, down))[0].detail, /no answer \(ECONNREFUSED\)/);
});

// ---------- hardening of the assistant ----------

test("proxyFetch: goes through HTTP_PROXY / HTTPS_PROXY, direct when NO_PROXY matches", async () => {
  const viaProxy = [];
  const targetSeen = [];
  const target = createServer((req, res) => { targetSeen.push(req.headers.authorization || ""); res.setHeader("content-type", "application/json"); res.end("{\"ok\":true}"); });
  await new Promise((r) => target.listen(0, "127.0.0.1", r));
  const targetPort = target.address().port;
  const proxy = createServer((req, res) => { // absolute-form request (plain http target)
    viaProxy.push(`GET ${req.url}`);
    const up = httpRequest(req.url, { method: req.method, headers: req.headers }, (r) => { res.writeHead(r.statusCode, r.headers); r.pipe(res); });
    req.pipe(up);
  });
  proxy.on("connect", (req, socket, head) => { // tunnel (CONNECT)
    viaProxy.push(`CONNECT ${req.url}`);
    const [host, port] = req.url.split(":");
    const up = netConnect(Number(port), host, () => { socket.write("HTTP/1.1 200 Connection Established\r\n\r\n"); up.write(head); up.pipe(socket); socket.pipe(up); });
    up.on("error", () => socket.destroy()); socket.on("error", () => up.destroy());
  });
  await new Promise((r) => proxy.listen(0, "127.0.0.1", r));
  const saved = {};
  const names = ["HTTP_PROXY", "http_proxy", "HTTPS_PROXY", "https_proxy", "NO_PROXY", "no_proxy"];
  for (const k of names) { saved[k] = process.env[k]; delete process.env[k]; }
  const url = `http://127.0.0.1:${targetPort}/v1/ping`;
  try {
    process.env.HTTP_PROXY = `http://127.0.0.1:${proxy.address().port}`;
    process.env.HTTPS_PROXY = process.env.HTTP_PROXY;
    const viaFetch = await proxyFetch();
    const res = await viaFetch(url, { headers: { authorization: "Bearer gw-key" } });
    assert.deepEqual(await res.json(), { ok: true });
    assert.equal(viaProxy.length, 1);
    assert.ok(viaProxy[0].includes(`127.0.0.1:${targetPort}`));
    assert.equal(targetSeen[0], "Bearer gw-key");
    process.env.NO_PROXY = "127.0.0.1";
    const direct = await proxyFetch();
    assert.deepEqual(await (await direct(url, {})).json(), { ok: true });
    assert.equal(viaProxy.length, 1, "NO_PROXY: the request must not go through the proxy");
    assert.equal(targetSeen.length, 2);
  } finally {
    for (const k of names) { if (saved[k] === undefined) delete process.env[k]; else process.env[k] = saved[k]; }
    await new Promise((r) => proxy.close(r)); await new Promise((r) => target.close(r));
  }
});

test("assistant route: the token is verified in the service before anything is called", async () => {
  const pg = fakePostgrest();
  const assistant = async () => Response.json({ answer: "x" });
  const s = await serve(LLM_ENV, { fetchImpl: pg.impl, assistant });
  try {
    const post = (headers) => fetch(`${s.base}/api/assistant`, { method: "POST", headers: { "content-type": "application/json", ...headers }, body: "{}" });
    const bad = await post({ authorization: "Bearer garbage" });
    assert.equal(bad.status, 401);
    assert.deepEqual(await bad.json(), { error: "Invalid or expired session" });
    assert.equal((await post({})).status, 401);
    assert.equal((await post({ authorization: `Bearer ${signJwt({ role: "service_role", exp: Math.floor(Date.now() / 1000) + 60 }, SECRET)}` })).status, 401);
    assert.equal(pg.calls.length, 0);
  } finally { await s.close(); }
});

test("config: POSTGREST_URL must not be under LLM_BASE_URL", () => {
  assert.throws(() => loadConfig({ ...BASE_ENV, ...LLM_ENV, LLM_BASE_URL: "http://pgrst/v1", POSTGREST_URL: "http://pgrst/v1/db" }), /POSTGREST_URL must not be under LLM_BASE_URL/);
  assert.ok(loadConfig({ ...BASE_ENV, ...LLM_ENV, LLM_BASE_URL: "http://pgrst/v1", POSTGREST_URL: "http://pgrst/v10" }));
});

test("assistant-check: error excerpts never show the key or token-like strings", async () => {
  const llm = { baseUrl: "https://g/v1", apiKey: "sk-secret-key", model: "m", expandModel: "m", reasoning: false };
  const long = "Ab3".repeat(12) + "-" + "x9".repeat(5);
  const leak = async () => new Response(`bad key sk-secret-key, token ${long}, see reasoning, `
    + `max_completion_tokens_is_unsupported at api.tech.ec.europa.eu.example`, { status: 401 });
  const r = await checkGateway(llm, leak);
  for (const x of r) {
    assert.ok(!x.detail.includes("sk-secret-key"));
    assert.ok(!x.detail.includes("Ab3Ab3Ab3"));
    assert.match(x.detail, /^HTTP 401: bad key …, token …, see reasoning/);
    assert.match(x.detail, /max_completion_tokens_is_unsupported at api\.tech\.ec\.europa\.eu\.example/, "words stay readable");
  }
});
