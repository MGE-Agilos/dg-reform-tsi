// Tests of tsi-admin without any network access (fake Auth admin API and PostgREST routed by URL).
// Run: node --experimental-strip-types --test supabase/functions/tsi-admin/index.test.ts
import { test } from "node:test";
import assert from "node:assert/strict";
import { handle, validate, tempPassword, allowedOrigins, corsHeaders, serviceHeaders, normEmail, PASSWORD_LENGTH, PER_PAGE, DEFAULT_SITE } from "./index.ts";

const ORIGIN = "https://mge-agilos.github.io";
const ENV: Record<string, string> = {
  SUPABASE_URL: "https://project.supabase.co",
  SUPABASE_ANON_KEY: "anon-test",
  SUPABASE_SERVICE_ROLE_KEY: "service-secret-test",
};
const ME = "admin@agilos.com";

type Call = { url: string; method: string; headers: Headers; body: unknown };
type Tester = { email: string; role: string; invited_at: string | null; invited_by: string | null };
type User = { id: string; email: string; email_confirmed_at: string | null; last_sign_in_at?: string | null; user_metadata?: Record<string, unknown> };

const json = (body: unknown, status = 200) => new Response(JSON.stringify(body), { status, headers: { "Content-Type": "application/json" } });

/** In-memory Supabase: testers table, auth users (incl. users of another app), caller per token. */
function fakeSupabase({ testers = [] as Tester[], users = [] as User[], callerEmail = ME, callerConfirmed = true, userStatus = 200, createStatus = 200, otpStatus = 200 } = {}) {
  const db = { testers: testers.map((t) => ({ ...t })), users: users.map((u) => ({ ...u })) };
  const calls: Call[] = [];
  const impl = async (url: string, init: RequestInit) => {
    const u = new URL(url);
    const method = (init.method || "GET").toUpperCase();
    const body = init.body ? JSON.parse(String(init.body)) : undefined;
    calls.push({ url, method, headers: new Headers(init.headers), body });
    const p = u.pathname;
    if (p === "/auth/v1/user") {
      if (userStatus !== 200) return json({ msg: "bad jwt" }, userStatus);
      return json({ id: "me-id", email: callerEmail, email_confirmed_at: callerConfirmed ? "2026-01-01T00:00:00Z" : null });
    }
    if (p === "/auth/v1/admin/users" && method === "GET") {
      const page = Number(u.searchParams.get("page"));
      const per = Number(u.searchParams.get("per_page"));
      return json({ users: db.users.slice((page - 1) * per, page * per), aud: "authenticated" });
    }
    if (p === "/auth/v1/admin/users" && method === "POST") {
      if (createStatus !== 200) return json({ error_code: "email_exists" }, createStatus);
      const user = { id: `id-${db.users.length + 1}`, email: body.email, email_confirmed_at: body.email_confirm ? "2026-10-23T00:00:00Z" : null, user_metadata: body.user_metadata };
      db.users.push(user);
      return json(user);
    }
    if (p.startsWith("/auth/v1/admin/users/") && method === "PUT") {
      const id = decodeURIComponent(p.split("/").pop()!);
      const user = db.users.find((x) => x.id === id);
      if (!user) return json({}, 404);
      user.user_metadata = { ...(user.user_metadata || {}), ...body.user_metadata };
      return json(user);
    }
    if (p === "/auth/v1/otp") return json({}, otpStatus);
    if (p === "/rest/v1/testers") {
      const emailEq = u.searchParams.get("email")?.replace(/^eq\./, "");
      const roleEq = u.searchParams.get("role")?.replace(/^eq\./, "");
      const match = (t: Tester) => (!emailEq || t.email === emailEq) && (!roleEq || t.role === roleEq);
      if (method === "GET") return json(db.testers.filter(match));
      if (method === "POST") {
        const i = db.testers.findIndex((t) => t.email === body.email);
        if (i >= 0) db.testers[i] = { ...db.testers[i], ...body }; else db.testers.push(body);
        return new Response(null, { status: 201 });
      }
      if (method === "PATCH") { db.testers.filter(match).forEach((t) => Object.assign(t, body)); return new Response(null, { status: 204 }); }
      if (method === "DELETE") { db.testers = db.testers.filter((t) => !match(t)); return new Response(null, { status: 204 }); }
    }
    throw new Error(`unexpected ${method} ${url}`);
  };
  return { impl, calls, db };
}

function post(body: unknown, { token = "user-jwt", origin = ORIGIN } = {}) {
  const headers: Record<string, string> = { "Content-Type": "application/json", Origin: origin };
  if (token) headers.Authorization = `Bearer ${token}`;
  return new Request("https://project.supabase.co/functions/v1/tsi-admin", { method: "POST", headers, body: typeof body === "string" ? body : JSON.stringify(body) });
}

const T = (email: string, role = "user"): Tester => ({ email, role, invited_at: null, invited_by: null });
const U = (email: string, extra: Partial<User> = {}): User => ({ id: `id-${email}`, email, email_confirmed_at: "2026-01-01T00:00:00Z", ...extra });
const base = () => ({ testers: [T(ME, "admin"), T("user@example.org")], users: [U(ME), U("user@example.org", { last_sign_in_at: "2026-10-20T10:00:00Z" }), U("europanel@other.org")] });
const run = async (f: ReturnType<typeof fakeSupabase>, body: unknown, opts = {}) => {
  const res = await handle(post(body, opts), ENV, f.impl, () => new Uint8Array(32).map((_, i) => (i * 37 + 11) % 256));
  return { res, body: await res.json() };
};

// ---------- HTTP contract ----------

test("OPTIONS: 204 with CORS for an allowed origin, none otherwise; same allow-list as tsi-assistant", async () => {
  const f = fakeSupabase();
  const res = await handle(new Request("https://x/tsi-admin", { method: "OPTIONS", headers: { Origin: ORIGIN } }), ENV, f.impl);
  assert.equal(res.status, 204);
  assert.equal(res.headers.get("Access-Control-Allow-Origin"), ORIGIN);
  assert.equal(f.calls.length, 0);
  const bad = await handle(new Request("https://x/tsi-admin", { method: "OPTIONS", headers: { Origin: "https://evil.example" } }), ENV, f.impl);
  assert.equal(bad.headers.get("Access-Control-Allow-Origin"), null);
  assert.deepEqual(allowedOrigins({}), ["https://mge-agilos.github.io", "http://localhost:8000"]);
  assert.deepEqual(allowedOrigins({ ALLOWED_ORIGINS: "https://a.example, https://b.example" }), ["https://a.example", "https://b.example"]);
  assert.equal(corsHeaders("https://a.example", ["https://a.example"])["Access-Control-Allow-Origin"], "https://a.example");
});

test("GET: 405; no bearer: 401 without any call", async () => {
  const f = fakeSupabase(base());
  assert.equal((await handle(new Request("https://x/tsi-admin", { method: "GET" }), ENV, f.impl)).status, 405);
  const { res } = await run(f, { action: "list" }, { token: "" });
  assert.equal(res.status, 401);
  assert.equal(f.calls.length, 0);
});

test("token rejected by Auth: 401; the token is checked with the anon key, the role with the service key", async () => {
  const f = fakeSupabase({ ...base(), userStatus: 401 });
  const { res, body } = await run(f, { action: "list" });
  assert.equal(res.status, 401);
  assert.deepEqual(body, { error: "Invalid or expired session" });
  assert.equal(f.calls[0].headers.get("apikey"), "anon-test");
  assert.equal(f.calls[0].headers.get("Authorization"), "Bearer user-jwt");
  const g = fakeSupabase(base());
  await run(g, { action: "list" });
  const roleCall = g.calls.find((c) => c.url.includes("/rest/v1/testers?") && c.url.includes(encodeURIComponent(ME)))!;
  assert.equal(roleCall.headers.get("apikey"), "service-secret-test");
  assert.equal(roleCall.headers.get("Authorization"), "Bearer service-secret-test");
});

test("not an administrator (user role, not a tester, unconfirmed e-mail): 403 and nothing else is read", async () => {
  for (const opts of [{ callerEmail: "user@example.org" }, { callerEmail: "stranger@example.org" }, { callerConfirmed: false }]) {
    const f = fakeSupabase({ ...base(), ...opts });
    const { res, body } = await run(f, { action: "list" });
    assert.equal(res.status, 403, JSON.stringify(opts));
    assert.deepEqual(body, { error: "Administrators only" });
    assert.ok(!f.calls.some((c) => c.url.includes("/auth/v1/admin/")));
  }
});

test("missing configuration: 500", async () => {
  const f = fakeSupabase(base());
  const res = await handle(post({ action: "list" }), { SUPABASE_URL: ENV.SUPABASE_URL }, f.impl);
  assert.equal(res.status, 500);
});

test("validation: action, e-mail, role, send_link", () => {
  assert.throws(() => validate(null), /Invalid request body/);
  assert.throws(() => validate({ action: "drop" }), /Unknown action/);
  assert.throws(() => validate({ action: "invite", email: "not-an-email" }), /Invalid e-mail/);
  assert.throws(() => validate({ action: "invite", email: "a@b.c", role: "owner" }), /role must be/);
  assert.throws(() => validate({ action: "invite", email: "a@b.c", send_link: "yes" }), /send_link/);
  assert.throws(() => validate({ action: "set_role", email: "a@b.c" }), /role must be/);
  assert.deepEqual(validate({ action: "invite", email: "  New.User@Example.ORG " }), { action: "invite", email: "new.user@example.org", role: "user", send_link: false });
  assert.deepEqual(validate({ action: "list", email: 3 }), { action: "list" });
  assert.equal(normEmail("A@B.CO"), "a@b.co");
  assert.throws(() => normEmail("a@b.c, x@y.z"), /Invalid/);
});

test("invalid body after the role check: 400", async () => {
  const f = fakeSupabase(base());
  assert.equal((await run(f, "{oops")).res.status, 400);
  assert.equal((await run(f, { action: "invite", email: "bad" })).res.status, 400);
});

// ---------- list ----------

test("list: testers joined with their accounts; users of other apps are never returned", async () => {
  const f = fakeSupabase({ ...base(), testers: [...base().testers, T("pending@example.org")] });
  f.db.users.push(U("pending@example.org", { email_confirmed_at: null, user_metadata: { must_change_password: true } }));
  const { res, body } = await run(f, { action: "list" });
  assert.equal(res.status, 200);
  assert.deepEqual(body.users.map((u: { email: string }) => u.email), [ME, "user@example.org", "pending@example.org"]);
  const text = JSON.stringify(body);
  assert.ok(!text.includes("europanel"));
  assert.ok(!text.includes("id-"), "account ids are not returned");
  const pending = body.users[2];
  assert.equal(pending.account, true);
  assert.equal(pending.email_confirmed_at, null);
  assert.equal(pending.must_change_password, true);
  assert.equal(body.users[1].last_sign_in_at, "2026-10-20T10:00:00Z");
});

test("list: accounts are paged (per_page 1000) until every tester is found or the last page", async () => {
  const many = Array.from({ length: PER_PAGE + 5 }, (_, i) => U(`other${i}@other.org`));
  const f = fakeSupabase({ testers: [T(ME, "admin"), T("late@example.org")], users: [U(ME), ...many, U("late@example.org")] });
  const { body } = await run(f, { action: "list" });
  assert.equal(body.users[1].account, true);
  assert.equal(f.calls.filter((c) => c.url.includes("/auth/v1/admin/users?")).length, 2);
  assert.ok(!JSON.stringify(body).includes("other.org"));
});

// ---------- invite ----------

test("invite a new e-mail: confirmed account with a temporary password and must_change_password; tester row; password returned once", async () => {
  const f = fakeSupabase(base());
  const { res, body } = await run(f, { action: "invite", email: "New@Example.org", role: "admin" });
  assert.equal(res.status, 200);
  assert.equal(body.existing, false);
  assert.equal(body.email, "new@example.org");
  assert.equal(body.password.length, PASSWORD_LENGTH);
  const create = f.calls.find((c) => c.method === "POST" && c.url.endsWith("/auth/v1/admin/users"))!;
  assert.deepEqual(create.body, { email: "new@example.org", password: body.password, email_confirm: true, user_metadata: { must_change_password: true } });
  const row = f.db.testers.find((t) => t.email === "new@example.org")!;
  assert.equal(row.role, "admin");
  assert.equal(row.invited_by, ME);
  assert.ok(!("link_sent" in body));
  assert.equal(res.headers.get("Cache-Control"), "no-store");
});

test("invite an existing account (another app): password untouched, tester row added, existing: true", async () => {
  const f = fakeSupabase(base());
  const { res, body } = await run(f, { action: "invite", email: "europanel@other.org" });
  assert.equal(res.status, 200);
  assert.deepEqual(body, { email: "europanel@other.org", role: "user", existing: true });
  assert.ok(!f.calls.some((c) => c.method === "POST" && c.url.endsWith("/auth/v1/admin/users")));
  assert.ok(!f.calls.some((c) => c.method === "PUT"));
  assert.equal(f.db.testers.find((t) => t.email === "europanel@other.org")?.role, "user");
});

test("invite: account created meanwhile (422 email_exists) is handled as existing", async () => {
  const f = fakeSupabase({ ...base(), createStatus: 422 });
  const { body } = await run(f, { action: "invite", email: "race@example.org" });
  assert.equal(body.existing, true);
  assert.ok(!("password" in body));
});

test("invite an e-mail that already has access: 409", async () => {
  const f = fakeSupabase(base());
  const { res } = await run(f, { action: "invite", email: "user@example.org" });
  assert.equal(res.status, 409);
});

test("invite with send_link: OTP request like the page's sign-in link (anon key, no account creation, site redirect)", async () => {
  const f = fakeSupabase(base());
  const { body } = await run(f, { action: "invite", email: "new@example.org", send_link: true });
  assert.equal(body.link_sent, true);
  const otp = f.calls.find((c) => c.url.includes("/auth/v1/otp"))!;
  assert.deepEqual(otp.body, { email: "new@example.org", create_user: false });
  assert.equal(new URL(otp.url).searchParams.get("redirect_to"), DEFAULT_SITE);
  assert.equal(otp.headers.get("apikey"), "anon-test");
  assert.equal(otp.headers.get("Authorization"), null);
  const g = fakeSupabase({ ...base(), otpStatus: 429 });
  const r2 = await run(g, { action: "invite", email: "europanel@other.org", send_link: true });
  assert.equal(r2.res.status, 200);
  assert.equal(r2.body.link_sent, false);
});

// ---------- roles, revoke, reset ----------

test("set_role: promote and demote; the last administrator cannot be demoted", async () => {
  const f = fakeSupabase(base());
  assert.deepEqual((await run(f, { action: "set_role", email: "user@example.org", role: "admin" })).body, { email: "user@example.org", role: "admin" });
  assert.equal(f.db.testers.find((t) => t.email === "user@example.org")?.role, "admin");
  // two admins now: demoting yourself is allowed
  assert.equal((await run(f, { action: "set_role", email: ME, role: "user" })).res.status, 200);
  const g = fakeSupabase(base());
  const last = await run(g, { action: "set_role", email: ME, role: "user" });
  assert.equal(last.res.status, 409);
  assert.match(last.body.error, /last administrator/);
  assert.equal(g.db.testers.find((t) => t.email === ME)?.role, "admin");
  assert.equal((await run(g, { action: "set_role", email: "nobody@example.org", role: "user" })).res.status, 404);
});

test("revoke: testers row deleted, auth account kept; yourself: 409; unknown: 404", async () => {
  const f = fakeSupabase(base());
  const { res, body } = await run(f, { action: "revoke", email: "user@example.org" });
  assert.equal(res.status, 200);
  assert.deepEqual(body, { email: "user@example.org", revoked: true });
  assert.ok(!f.db.testers.some((t) => t.email === "user@example.org"));
  assert.ok(f.db.users.some((u) => u.email === "user@example.org"));
  assert.ok(!f.calls.some((c) => c.method === "DELETE" && c.url.includes("/auth/")));
  assert.equal((await run(f, { action: "revoke", email: ME })).res.status, 409);
  assert.equal((await run(f, { action: "revoke", email: "europanel@other.org" })).res.status, 404);
});

test("reset_password: testers only; new temporary password and must_change_password; other metadata kept", async () => {
  const f = fakeSupabase(base());
  f.db.users[1].user_metadata = { name: "Kept" };
  const { res, body } = await run(f, { action: "reset_password", email: "user@example.org" });
  assert.equal(res.status, 200);
  assert.equal(body.password.length, PASSWORD_LENGTH);
  const put = f.calls.find((c) => c.method === "PUT")!;
  assert.ok(put.url.endsWith("/auth/v1/admin/users/id-user%40example.org"));
  assert.deepEqual(put.body, { password: body.password, user_metadata: { must_change_password: true } });
  assert.deepEqual(f.db.users[1].user_metadata, { name: "Kept", must_change_password: true });
  assert.equal((await run(f, { action: "reset_password", email: "europanel@other.org" })).res.status, 404);
});

test("upstream failure: 502 with a short message; logs carry no e-mail, password nor token", async () => {
  const logs: string[] = [];
  const orig = console.log;
  console.log = (s: string) => logs.push(String(s));
  try {
    const f = fakeSupabase(base());
    const impl = async (url: string, init: RequestInit) => (url.includes("/auth/v1/admin/users") && init.method === "POST" ? json({}, 500) : f.impl(url, init));
    const res = await handle(post({ action: "invite", email: "new@example.org" }), ENV, impl);
    assert.equal(res.status, 502);
    assert.deepEqual(await res.json(), { error: "Creating the account failed (500)" });
    const g = fakeSupabase(base());
    const ok = await run(g, { action: "reset_password", email: "user@example.org" });
    const text = logs.join("\n");
    assert.ok(!text.includes("example.org") && !text.includes(ME) && !text.includes("user-jwt") && !text.includes(ok.body.password) && !text.includes("service-secret"));
    assert.match(text, /"action":"reset_password","status":200/);
  } finally {
    console.log = orig;
  }
});

test("tempPassword: length, classes, alphabet; real randomness differs; sb_secret_ keys sent as apikey only", () => {
  for (let i = 0; i < 50; i++) {
    const p = tempPassword();
    assert.equal(p.length, PASSWORD_LENGTH);
    assert.match(p, /[a-z]/);
    assert.match(p, /[A-Z]/);
    assert.match(p, /[0-9]/);
    assert.match(p, /^[a-km-zA-HJ-NP-Z2-9]+$/);
  }
  assert.notEqual(tempPassword(), tempPassword());
  assert.equal(serviceHeaders("sb_secret_x").Authorization, undefined);
  assert.equal(serviceHeaders("eyJ.x.y").Authorization, "Bearer eyJ.x.y");
});
