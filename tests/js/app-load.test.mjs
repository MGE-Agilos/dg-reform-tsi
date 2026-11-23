// web/app.js must load: a top-level constant read before its declaration (temporal dead zone) or a typo leaves the
// page blank in the browser, which the other tests (pure modules) do not see. Browser globals are permissive stubs.
import { test } from "node:test";
import assert from "node:assert/strict";

const handler = {
  get: (t, k) => (k === Symbol.toPrimitive ? () => "" : k === "then" ? undefined : k in t ? t[k] : stub()),
  apply: () => stub(), construct: () => stub(),
};
function stub() { return new Proxy(function () {}, handler); }

test("web/app.js loads without a ReferenceError or SyntaxError", async () => {
  for (const name of ["document", "window", "localStorage", "sessionStorage", "location", "history", "navigator",
    "supabase", "matchMedia", "addEventListener", "HTMLElement", "customElements", "requestAnimationFrame"]) {
    try { if (!(name in globalThis) || name === "navigator") globalThis[name] = stub(); } catch { /* read-only global */ }
  }
  try {
    await import(new URL("../../web/app.js", import.meta.url).href);
  } catch (e) {
    assert.ok(!(e instanceof ReferenceError || e instanceof SyntaxError), `${e.name}: ${e.message}`);
  }
});
