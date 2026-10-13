// Tests of the haystack-proxy logic, without any network access (fake fetch).
// Run: node --experimental-strip-types --test supabase/functions/haystack-proxy/index.test.ts
import { test } from "node:test";
import assert from "node:assert/strict";
import { handle, validate, corsHeaders, allowedOrigins, gatewayRequest, extractAnswer, HttpError } from "./index.ts";

const ORIGIN = "https://mge-agilos.github.io";
const ENV: Record<string, string> = {
  SUPABASE_URL: "https://project.supabase.co",
  SUPABASE_ANON_KEY: "anon-test",
  HAYSTACK_API_KEY: "hk-secret-test",
  HAYSTACK_CHAT_URL: "https://gateway.example/tsi-chat/chat/completions",
  HAYSTACK_IMPACT_URL: "https://gateway.example/tsi-impact/chat/completions",
};

type Call = { url: string; init: RequestInit };

function fakeFetch({ tester = true as unknown, testerStatus = 200, gatewayStatus = 200, gatewayBody = null as unknown, gatewayThrows = null as unknown } = {}) {
  const calls: Call[] = [];
  const impl = async (url: string, init: RequestInit) => {
    calls.push({ url, init });
    if (url.endsWith("/rest/v1/rpc/is_tester")) {
      return new Response(JSON.stringify(tester), { status: testerStatus, headers: { "Content-Type": "application/json" } });
    }
    if (gatewayThrows) throw gatewayThrows;
    const body = gatewayBody ?? { choices: [{ message: { role: "assistant", content: "Answer **ok**" } }] };
    return new Response(typeof body === "string" ? body : JSON.stringify(body), { status: gatewayStatus });
  };
  return { impl, calls, gatewayCalls: () => calls.filter((c) => !c.url.includes("/rest/v1/")) };
}

function post(body: unknown, { token = "user-jwt", origin = ORIGIN } = {}) {
  const headers: Record<string, string> = { "Content-Type": "application/json", Origin: origin };
  if (token) headers.Authorization = `Bearer ${token}`;
  return new Request("https://project.supabase.co/functions/v1/haystack-proxy", {
    method: "POST", headers, body: typeof body === "string" ? body : JSON.stringify(body),
  });
}

const chatBody = { service: "chat", messages: [{ role: "user", content: "How many TSI projects does Greece have?" }] };

test("OPTIONS from an allowed origin: 204 with CORS headers", async () => {
  const f = fakeFetch();
  const res = await handle(new Request("https://x/haystack-proxy", { method: "OPTIONS", headers: { Origin: ORIGIN } }), ENV, f.impl);
  assert.equal(res.status, 204);
  assert.equal(res.headers.get("Access-Control-Allow-Origin"), ORIGIN);
  assert.match(res.headers.get("Access-Control-Allow-Headers") ?? "", /authorization/);
  assert.match(res.headers.get("Access-Control-Allow-Methods") ?? "", /POST/);
  assert.equal(res.headers.get("Access-Control-Max-Age"), "86400");
  assert.equal(f.calls.length, 0);
});

test("origin not allowed: no ACAO header", async () => {
  const res = await handle(new Request("https://x/haystack-proxy", { method: "OPTIONS", headers: { Origin: "https://evil.example" } }), ENV, fakeFetch().impl);
  assert.equal(res.headers.get("Access-Control-Allow-Origin"), null);
  assert.equal(res.headers.get("Vary"), "Origin");
});

test("allowedOrigins: defaults and env list", () => {
  assert.deepEqual(allowedOrigins({}), ["https://mge-agilos.github.io", "http://localhost:8000"]);
  assert.deepEqual(allowedOrigins({ ALLOWED_ORIGINS: "https://a.example, https://b.example" }), ["https://a.example", "https://b.example"]);
  assert.equal(corsHeaders("https://a.example", ["https://a.example"])["Access-Control-Allow-Origin"], "https://a.example");
});

test("method other than POST: 405", async () => {
  const res = await handle(new Request("https://x/haystack-proxy", { method: "GET", headers: { Origin: ORIGIN } }), ENV, fakeFetch().impl);
  assert.equal(res.status, 405);
});

test("no bearer token: 401", async () => {
  const f = fakeFetch();
  const res = await handle(post(chatBody, { token: "" }), ENV, f.impl);
  assert.equal(res.status, 401);
  assert.equal(f.calls.length, 0);
});

test("is_tester false: 403 and the Gateway is never called", async () => {
  const f = fakeFetch({ tester: false });
  const res = await handle(post(chatBody), ENV, f.impl);
  assert.equal(res.status, 403);
  assert.deepEqual(await res.json(), { error: "Access not authorised" });
  assert.equal(f.gatewayCalls().length, 0);
  const rpc = f.calls[0];
  assert.equal(rpc.url, "https://project.supabase.co/rest/v1/rpc/is_tester");
  const h = new Headers(rpc.init.headers);
  assert.equal(h.get("apikey"), "anon-test");
  assert.equal(h.get("Authorization"), "Bearer user-jwt");
});

test("is_tester error status: 403", async () => {
  const f = fakeFetch({ tester: { message: "JWT expired" }, testerStatus: 401 });
  const res = await handle(post(chatBody), ENV, f.impl);
  assert.equal(res.status, 403);
  assert.equal(f.gatewayCalls().length, 0);
});

test("invalid bodies: 400", async () => {
  const many = Array.from({ length: 21 }, (_, i) => ({ role: i % 2 ? "assistant" : "user", content: "q" }));
  const cases: unknown[] = [
    { service: "other", messages: chatBody.messages },
    { service: "chat", messages: many },
    { service: "chat", messages: [{ role: "user", content: "q" }, { role: "assistant", content: "a" }] },
    { service: "chat", messages: [{ role: "user", content: "x".repeat(8001) }] },
    { service: "chat", messages: [] },
    { service: "chat", messages: [{ role: "system", content: "q" }] },
    { service: "chat", messages: [{ role: "user", content: "" }] },
    "not json",
  ];
  for (const body of cases) {
    const f = fakeFetch();
    const res = await handle(post(body), ENV, f.impl);
    assert.equal(res.status, 400, JSON.stringify(body));
    assert.equal(f.gatewayCalls().length, 0);
  }
  assert.throws(() => validate(null), HttpError);
});

test("nominal chat: 200 {answer}, model tsi-chat, stream false, Bearer key, no key in the response", async () => {
  const f = fakeFetch();
  const res = await handle(post(chatBody), ENV, f.impl);
  assert.equal(res.status, 200);
  assert.equal(res.headers.get("Content-Type"), "application/json");
  assert.equal(res.headers.get("Access-Control-Allow-Origin"), ORIGIN);
  const text = await res.text();
  assert.deepEqual(JSON.parse(text), { answer: "Answer **ok**" });
  assert.ok(!text.includes("hk-secret-test"));
  const [gw] = f.gatewayCalls();
  assert.equal(gw.url, ENV.HAYSTACK_CHAT_URL);
  const sent = JSON.parse(String(gw.init.body));
  assert.equal(sent.model, "tsi-chat");
  assert.equal(sent.stream, false);
  assert.deepEqual(sent.messages, chatBody.messages);
  const h = new Headers(gw.init.headers);
  assert.equal(h.get("Authorization"), "Bearer hk-secret-test");
  assert.equal(h.get("Content-Type"), "application/json");
  assert.ok(gw.init.signal, "a timeout signal is set");
});

test("impact with HAYSTACK_IMPACT_MODEL: that model is used", async () => {
  const f = fakeFetch();
  const env = { ...ENV, HAYSTACK_IMPACT_MODEL: "custom-impact" };
  const res = await handle(post({ service: "impact", messages: [{ role: "user", content: "Project: X" }] }), env, f.impl);
  assert.equal(res.status, 200);
  const [gw] = f.gatewayCalls();
  assert.equal(gw.url, ENV.HAYSTACK_IMPACT_URL);
  assert.equal(JSON.parse(String(gw.init.body)).model, "custom-impact");
});

test("Gateway 500: 502 without the Gateway's text (kept in the server log only)", async () => {
  const f = fakeFetch({ gatewayStatus: 500, gatewayBody: "boom internal trace" });
  const res = await handle(post(chatBody), ENV, f.impl);
  assert.equal(res.status, 502);
  assert.deepEqual(await res.json(), { error: "Analysis service error 500" });
});

test("Gateway 200 with empty or blank content: 502 Empty response", async () => {
  for (const content of ["", "   \n", null]) {
    const f = fakeFetch({ gatewayBody: { choices: [{ message: { role: "assistant", content } }] } });
    const res = await handle(post(chatBody), ENV, f.impl);
    assert.equal(res.status, 502);
    assert.deepEqual(await res.json(), { error: "Empty response from the analysis service" });
  }
});

test("total content above 40 000 characters: 400", async () => {
  const messages = Array.from({ length: 6 }, (_, i) => ({ role: i % 2 ? "assistant" : "user", content: "x".repeat(7000) }));
  messages.push({ role: "user", content: "q" });
  const f = fakeFetch();
  const res = await handle(post({ service: "chat", messages }), ENV, f.impl);
  assert.equal(res.status, 400);
  assert.match((await res.json()).error, /40000 characters in total/);
  assert.equal(f.gatewayCalls().length, 0);
});

test("SUPABASE_ANON_KEY missing: the caller's apikey header is used, else 500", async () => {
  const env = { ...ENV };
  delete env.SUPABASE_ANON_KEY;
  const f = fakeFetch();
  const req = post(chatBody);
  req.headers.set("apikey", "anon-from-page");
  const res = await handle(req, env, f.impl);
  assert.equal(res.status, 200);
  assert.equal(new Headers(f.calls[0].init.headers).get("apikey"), "anon-from-page");

  const f2 = fakeFetch();
  const res2 = await handle(post(chatBody), env, f2.impl);
  assert.equal(res2.status, 500);
  assert.deepEqual(await res2.json(), { error: "SUPABASE_ANON_KEY not configured" });
  assert.equal(f2.calls.length, 0);
});

test("Gateway 200 with an unexpected shape: 502", async () => {
  const f = fakeFetch({ gatewayBody: { foo: 1 } });
  const res = await handle(post(chatBody), ENV, f.impl);
  assert.equal(res.status, 502);
  assert.equal((await res.json()).error, "Unexpected response from the analysis service");
});

test("timeout: 504", async () => {
  const f = fakeFetch({ gatewayThrows: new DOMException("The operation timed out.", "TimeoutError") });
  const res = await handle(post(chatBody), ENV, f.impl);
  assert.equal(res.status, 504);
  assert.deepEqual(await res.json(), { error: "The analysis service timed out" });
});

test("missing Gateway URL: 500 HAYSTACK_CHAT_URL not configured", async () => {
  const env = { ...ENV };
  delete env.HAYSTACK_CHAT_URL;
  const res = await handle(post(chatBody), env, fakeFetch().impl);
  assert.equal(res.status, 500);
  assert.equal((await res.json()).error, "HAYSTACK_CHAT_URL not configured");
});

test("gatewayRequest and extractAnswer", () => {
  assert.throws(() => gatewayRequest("chat", [], { HAYSTACK_CHAT_URL: "https://x" }), /HAYSTACK_API_KEY not configured/);
  const { url, init } = gatewayRequest("impact", [{ role: "user", content: "q" }], ENV);
  assert.equal(url, ENV.HAYSTACK_IMPACT_URL);
  assert.equal(JSON.parse(String(init.body)).model, "tsi-impact");
  assert.equal(extractAnswer({ choices: [{ message: { content: "hi" } }] }), "hi");
  assert.throws(() => extractAnswer({ choices: [] }), (e: unknown) => e instanceof HttpError && e.status === 502);
  assert.throws(() => extractAnswer({ choices: [{ message: { content: " " } }] }), /Empty response/);
});
