// Tests of the sign-in adapter of the self-hosted installation (web/server-auth.js), with a faked service.
// Run: node --test tests/js/server-auth.test.mjs
import { test } from "node:test";
import assert from "node:assert/strict";
import { createServerAuth } from "../../web/server-auth.js";

function fake(answers) {
  const calls = [];
  const impl = async (url, init = {}) => {
    calls.push({ url, method: init.method || "GET" });
    const next = answers.shift();
    if (next instanceof Error) throw next;
    return new Response(next.body ? JSON.stringify(next.body) : null, { status: next.status });
  };
  return { impl, calls };
}

const T = 1000;
const session = (id = "u1", exp = T + 900) => ({ access_token: `tok-${id}`, expires_at: exp, user: { id, email: `${id}@x.eu`, user_metadata: {} } });
const noTimers = { setTimeout: () => 1, clearTimeout: () => {} };

test("getSession: read once, kept while the token is fresh, null when not signed in", async () => {
  const f = fake([{ status: 200, body: session() }]);
  const auth = createServerAuth("/api", { fetchImpl: f.impl, now: () => T, timers: noTimers });
  const a = await auth.getSession();
  assert.equal(a.data.session.user.id, "u1");
  assert.equal(a.error, null);
  await auth.getSession();
  assert.equal(f.calls.length, 1, "cached");
  assert.equal(f.calls[0].url, "/api/auth/session");
  assert.equal(auth.token(), "tok-u1");
  const none = createServerAuth("/api", { fetchImpl: fake([{ status: 401 }]).impl, now: () => T, timers: noTimers });
  assert.deepEqual(await none.getSession(), { data: { session: null }, error: null });
  assert.equal(none.token(), null);
});

test("getUser always asks the service; ended session -> AuthSessionMissingError; unreachable -> error", async () => {
  const f = fake([{ status: 200, body: session() }, { status: 401 }, new TypeError("offline")]);
  const auth = createServerAuth("/api", { fetchImpl: f.impl, now: () => T, timers: noTimers });
  assert.equal((await auth.getUser()).data.user.email, "u1@x.eu");
  assert.equal((await auth.getUser()).error.name, "AuthSessionMissingError");
  assert.match((await auth.getUser()).error.message, /cannot be reached/);
});

test("an expiring token is read again; signOut calls the service and notifies", async () => {
  let now = T;
  const f = fake([{ status: 200, body: session("u1", T + 100) }, { status: 200, body: session("u1", T + 1000) }, { status: 204 }]);
  const auth = createServerAuth("/api", { fetchImpl: f.impl, now: () => now, timers: noTimers });
  const events = [];
  auth.onAuthStateChange((e, s) => events.push([e, s?.user?.id ?? null]));
  await auth.getSession();
  now = T + 50; // within 60 s of the expiry
  assert.equal((await auth.getSession()).data.session.expires_at, T + 1000);
  assert.equal(f.calls.length, 2);
  await auth.signOut();
  assert.deepEqual(f.calls[2], { url: "/api/auth/logout", method: "POST" });
  assert.deepEqual(events, [["INITIAL_SESSION", null], ["SIGNED_OUT", null]]);
  assert.equal(auth.loginUrl("#csrs?q=water"), "/api/auth/login?next=%23csrs%3Fq%3Dwater");
});

test("the renewal timer re-reads the session and emits TOKEN_REFRESHED", async () => {
  const pending = [];
  const timers = { setTimeout: (fn, ms) => { pending.push({ fn, ms }); return pending.length; }, clearTimeout: () => {} };
  const f = fake([{ status: 200, body: session("u1", T + 900) }, { status: 200, body: session("u1", T + 1800) }]);
  const auth = createServerAuth("/api", { fetchImpl: f.impl, now: () => T, timers });
  const events = [];
  auth.onAuthStateChange((e) => events.push(e));
  await auth.getSession();
  assert.equal(pending.at(-1).ms, (900 - 60) * 1000);
  await pending.at(-1).fn();
  assert.deepEqual(events, ["INITIAL_SESSION", "TOKEN_REFRESHED"]);
});
