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
