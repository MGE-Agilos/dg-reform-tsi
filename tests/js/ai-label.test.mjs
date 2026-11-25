// Name of the AI service and web search flag shown by the page: defaults of the cloud configuration (web/config.js).
import { test } from "node:test";
import assert from "node:assert/strict";
import { readFileSync } from "node:fs";

test("ai-label: defaults are Claude with web search; the self-hosted config names GPT@EC without web search", async () => {
  const L = await import(new URL("../../web/ai-label.js", import.meta.url).href);
  assert.equal(L.AI_NAME, "Claude, Anthropic");
  assert.equal(L.AI_WEB_SEARCH, true);
  assert.equal(L.IMPACT_INPUTS, "the project data with a web search");
  const onprem = readFileSync(new URL("../../deploy/onprem/config.js", import.meta.url), "utf8");
  assert.match(onprem, /export const AI_NAME = "GPT@EC, European Commission";/);
  assert.match(onprem, /export const AI_WEB_SEARCH = false;/);
});

test("no hard-coded AI name left in the page modules", () => {
  for (const f of ["app.js", "reporting.js", "word-export.js", "index.html"]) {
    const text = readFileSync(new URL(`../../web/${f}`, import.meta.url), "utf8");
    const hits = text.split("\n").filter((l) => /Claude|Anthropic/.test(l) && !/^\s*\/\//.test(l) && !/data-ai-name/.test(l));
    assert.deepEqual(hits, [], `${f}: ${hits.join("\n")}`);
  }
});
