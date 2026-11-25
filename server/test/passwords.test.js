// Tests of the sign-in with e-mail + password (AUTH_MODE=password). PostgREST is faked (testers + user_passwords).
// Run: cd server && npm test
import { test } from "node:test";
import assert from "node:assert/strict";
import { createServer } from "node:http";
import { loadConfig, ConfigError } from "../src/config.js";
import { createApp, clientIp } from "../src/app.js";
import { runAdmin } from "../src/admin.js";
import { hashPassword, verifyPassword, tempPassword, passwordProblem, MAX_FAILURES } from "../src/passwords.js";
import { verifyJwt, userToken, uuidV5 } from "../src/tokens.js";

const SECRET = "s".repeat(40);
const ENV = { PUBLIC_URL: "http://localhost:8080", JWT_SECRET: SECRET, SESSION_SECRET: "k".repeat(40), AUTH_MODE: "password",
  POSTGREST_URL: "http://pgrst" };

function fakeDb() {
  const testers = [{ email: "admin@x.eu", role: "admin" }, { email: "user@x.eu", role: "user" }];
  const passwords = [];
  const json = (body, status = 200) => new Response(JSON.stringify(body), { status, headers: { "content-type": "application/json" } });
  const impl = async (url, init = {}) => {
    const u = new URL(url);
    const method = init.method || "GET";
    const email = u.searchParams.get("email")?.replace(/^eq\./, "");
    if (u.pathname === "/testers") {
      if (method === "GET") return json(email ? testers.filter((r) => r.email === email)
        : u.searchParams.get("role") === "eq.admin" ? testers.filter((r) => r.role === "admin") : testers);
      if (method === "POST") { testers.push(JSON.parse(init.body)); return new Response(null, { status: 201 }); }
      if (method === "DELETE") { // on delete cascade
        testers.splice(testers.findIndex((r) => r.email === email), 1);
        const i = passwords.findIndex((r) => r.email === email);
        if (i >= 0) passwords.splice(i, 1);
        return new Response(null, { status: 204 });
      }
    }
    if (u.pathname === "/user_passwords") {
      if (method === "GET") return json(email ? passwords.filter((r) => r.email === email) : passwords);
      if (method === "POST") {
        const row = JSON.parse(init.body);
        const i = passwords.findIndex((r) => r.email === row.email);
        if (i >= 0) passwords[i] = { ...passwords[i], ...row }; else passwords.push(row);
        return new Response(null, { status: 201 });
      }
      if (method === "PATCH") { Object.assign(passwords.find((r) => r.email === email), JSON.parse(init.body)); return new Response(null, { status: 204 }); }
    }
    if (u.pathname === "/page_views") return json([]);
    return json([]);
  };
  return { impl, testers, passwords };
}

async function serve(db, env = {}) {
  const logs = [];
  const app = createApp(loadConfig({ ...ENV, ...env }), { fetchImpl: db.impl, log: (l) => logs.push(JSON.parse(l)) });
  const server = createServer(app);
  await new Promise((r) => server.listen(0, "127.0.0.1", r));
  return { base: `http://127.0.0.1:${server.address().port}`, logs, close: () => new Promise((r) => server.close(r)) };
}

const post = (base, path, body, headers = {}) => fetch(`${base}${path}`, { method: "POST",
  headers: { "content-type": "application/json", ...headers }, body: JSON.stringify(body) });
const cookieOf = (res) => res.headers.getSetCookie().find((c) => c.startsWith("tsi_session="))?.split(";")[0];

test("passwords: scrypt hash verified, wrong password refused; temporary passwords; rules for a new one", async () => {
  const h = await hashPassword("correct horse battery");
  assert.match(h, /^scrypt\$16384\$8\$1\$/);
  assert.equal(await verifyPassword("correct horse battery", h), true);
  assert.equal(await verifyPassword("correct horse batterY", h), false);
  assert.equal(await verifyPassword("x", "garbage"), false);
  assert.notEqual(await hashPassword("same"), await hashPassword("same"), "salted");
  assert.match(tempPassword(), /^[a-zA-Z2-9]{4}(-[a-zA-Z2-9]{4}){3}$/);
  assert.equal(passwordProblem("short"), "The password must have at least 12 characters.");
  assert.equal(passwordProblem("aaaaaaaaaaaaaaa"), "The password is too simple.");
  assert.equal(passwordProblem("a good long password"), null);
});

test("config: password mode needs HTTPS outside localhost", () => {
  assert.equal(loadConfig(ENV).authMode, "password");
  assert.throws(() => loadConfig({ ...ENV, PUBLIC_URL: "http://tsi.example.eu" }), ConfigError);
  assert.equal(loadConfig({ ...ENV, PUBLIC_URL: "https://tsi.example.eu" }).secure, true);
});

test("clientIp: the address added by nginx (last X-Forwarded-For entry), not one sent by the browser", () => {
  assert.equal(clientIp({ headers: { "x-forwarded-for": "6.6.6.6, 10.0.0.5" }, socket: { remoteAddress: "172.18.0.2" } }), "10.0.0.5");
  assert.equal(clientIp({ headers: {}, socket: { remoteAddress: "172.18.0.2" } }), "172.18.0.2");
});

test("sign-in: temporary password, forced change, then the new password; same answer for unknown address and wrong password", async () => {
  const db = fakeDb();
  const s = await serve(db);
  try {
    assert.deepEqual(await (await fetch(`${s.base}/api/auth/config`)).json(), { mode: "password" });
    assert.equal((await post(s.base, "/api/auth/dev", {})).status, 404, "no test sign-in");
    // the administrator gives a temporary password
    const temp = (await runAdmin({ ...await import("../src/admin.js").then((m) => m.store(loadConfig(ENV), db.impl)) },
      "admin@x.eu", { action: "reset_password", email: "user@x.eu" }, "password")).password;
    assert.equal(db.passwords[0].must_change, true);
    for (const [email, password] of [["nobody@x.eu", temp], ["user@x.eu", "wrong password"], ["not an address", temp], ["admin@x.eu", temp]]) {
      const r = await post(s.base, "/api/auth/password", { email, password });
      assert.equal(r.status, 401, email);
      assert.deepEqual(await r.json(), { error: "Invalid login credentials" });
    }
    assert.equal(db.passwords[0].failed_count, 1);
    const ok = await post(s.base, "/api/auth/password", { email: " User@X.eu ", password: temp });
    assert.equal(ok.status, 200);
    let cookie = cookieOf(ok);
    assert.equal(db.passwords[0].failed_count, 0, "reset after a success");
    let session = await (await fetch(`${s.base}/api/auth/session`, { headers: { cookie } })).json();
    assert.equal(session.user.email, "user@x.eu");
    assert.equal(session.user.id, uuidV5("password|user@x.eu"));
    assert.equal(session.user.user_metadata.must_change_password, true);
    assert.equal(verifyJwt(session.access_token, SECRET).role, "authenticated");
    // forced change
    assert.equal((await post(s.base, "/api/auth/password/change", { password: "short" }, { cookie })).status, 400);
    assert.equal((await post(s.base, "/api/auth/password/change", { password: "my own long password" })).status, 401);
    const changed = await post(s.base, "/api/auth/password/change", { password: "my own long password" }, { cookie });
    assert.equal(changed.status, 200);
    cookie = cookieOf(changed);
    session = await (await fetch(`${s.base}/api/auth/session`, { headers: { cookie } })).json();
    assert.equal(session.user.user_metadata.must_change_password, false);
    assert.equal(db.passwords[0].must_change, false);
    assert.equal((await post(s.base, "/api/auth/password", { email: "user@x.eu", password: temp })).status, 401, "old one refused");
    assert.equal((await post(s.base, "/api/auth/password", { email: "user@x.eu", password: "my own long password" })).status, 200);
    assert.ok(s.logs.some((l) => l.event === "sign_in_failed" && l.email === "nobody@x.eu"));
    assert.ok(!JSON.stringify(s.logs).includes(temp), "no password in the logs");
  } finally { await s.close(); }
});

test("sign-in: account locked after repeated failures, even with the right password", async () => {
  const db = fakeDb();
  const s = await serve(db);
  try {
    db.passwords.push({ email: "user@x.eu", hash: await hashPassword("the right password"), must_change: false, failed_count: 0, locked_until: null });
    for (let i = 0; i < MAX_FAILURES; i++) assert.equal((await post(s.base, "/api/auth/password", { email: "user@x.eu", password: `bad ${i}` })).status, 401);
    assert.ok(Date.parse(db.passwords[0].locked_until) > Date.now());
    const locked = await post(s.base, "/api/auth/password", { email: "user@x.eu", password: "the right password" });
    assert.equal(locked.status, 429);
    assert.match((await locked.json()).error, /Too many failed attempts/);
    db.passwords[0].locked_until = new Date(Date.now() - 1000).toISOString(); // lock over
    assert.equal((await post(s.base, "/api/auth/password", { email: "user@x.eu", password: "the right password" })).status, 200);
  } finally { await s.close(); }
});

test("administration with passwords: invite gives a temporary password, list shows the status, revoke deletes the password", async () => {
  const db = fakeDb();
  const s = await serve(db);
  const token = userToken({ id: uuidV5("password|admin@x.eu"), email: "admin@x.eu" }, SECRET, 5).token;
  const admin = (body) => post(s.base, "/api/admin", body, { Authorization: `Bearer ${token}` });
  try {
    const inv = await (await admin({ action: "invite", email: "new@x.eu" })).json();
    assert.equal(inv.email, "new@x.eu");
    assert.match(inv.password, /^[a-zA-Z2-9]{4}(-[a-zA-Z2-9]{4}){3}$/);
    assert.equal(await verifyPassword(inv.password, db.passwords.find((r) => r.email === "new@x.eu").hash), true);
    const list = (await (await admin({ action: "list" })).json()).users;
    const byEmail = Object.fromEntries(list.map((u) => [u.email, u]));
    assert.equal(byEmail["new@x.eu"].has_password, true);
    assert.equal(byEmail["new@x.eu"].must_change_password, true);
    assert.equal(byEmail["user@x.eu"].has_password, false);
    assert.equal(byEmail["user@x.eu"].password_mode, true);
    const reset = await (await admin({ action: "reset_password", email: "user@x.eu" })).json();
    assert.equal(reset.email, "user@x.eu");
    assert.equal((await admin({ action: "reset_password", email: "ghost@x.eu" })).status, 404);
    await admin({ action: "revoke", email: "new@x.eu" });
    assert.equal(db.passwords.some((r) => r.email === "new@x.eu"), false);
  } finally { await s.close(); }
});
