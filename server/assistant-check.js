// Checks the GPT@EC gateway before users do: one call with a tool, one in JSON mode. Reads the LLM_* settings and
// HTTPS_PROXY / NO_PROXY like the service. Run on the server, from server/:
//   set -a; . /etc/tsi-explorer/server.env; set +a; npm run assistant-check
import { pathToFileURL } from "node:url";
import { llmConfig } from "./src/config.js";
import { proxyFetch } from "./src/assistant.js";

const PING_TOOL = { type: "function", function: { name: "ping", description: "Health check: call it with ok set to true.",
  parameters: { type: "object", properties: { ok: { type: "boolean" } }, required: ["ok"] } } };

/** Error excerpt without the key and without long token-like strings. */
function mask(text, apiKey) {
  let s = text;
  if (apiKey) s = s.split(apiKey).join("…");
  return s.replace(/[A-Za-z0-9_\-\.]{20,}/g, "…");
}

/** [{ name, ok, detail }] for the tool call and the JSON mode; never throws. */
export async function checkGateway(llm, fetchImpl) {
  const call = async (body) => {
    let res;
    try {
      res = await fetchImpl(`${llm.baseUrl}/chat/completions`, {
        method: "POST",
        headers: { authorization: `Bearer ${llm.apiKey}`, "content-type": "application/json" },
        body: JSON.stringify({ model: llm.model, max_completion_tokens: 2000, ...(llm.reasoning ? { reasoning_effort: "low" } : {}), ...body }),
        signal: AbortSignal.timeout(60000),
      });
    } catch (e) {
      return { error: `no answer (${e?.cause?.code || e?.name || "network error"})` };
    }
    const text = await res.text();
    if (!res.ok) return { error: `HTTP ${res.status}: ${mask(text.slice(0, 300), llm.apiKey)}` };
    try {
      return { message: JSON.parse(text)?.choices?.[0]?.message ?? null };
    } catch {
      return { error: `the answer is not JSON: ${mask(text.slice(0, 200), llm.apiKey)}` };
    }
  };
  const tool = await call({ messages: [{ role: "user", content: "Call the ping tool with ok set to true." }], tools: [PING_TOOL] });
  const called = tool.message?.tool_calls?.[0]?.function?.name === "ping";
  const json = await call({ messages: [{ role: "system", content: "Answer with a single JSON object." },
    { role: "user", content: "Return the JSON object {\"ok\": true}." }], response_format: { type: "json_object" } });
  let parsed = null;
  try { parsed = JSON.parse(json.message?.content ?? ""); } catch { /* reported below */ }
  return [
    { name: "tools (chat, impact analysis)", ok: called,
      detail: tool.error || (called ? "the model called the tool" : "the model answered without calling the tool") },
    { name: "JSON mode (briefing, related terms)", ok: parsed?.ok === true,
      detail: json.error || (parsed?.ok === true ? "valid JSON object" : "the answer is not the expected JSON object") },
  ];
}

async function main() {
  let llm;
  try { llm = llmConfig(process.env); } catch (e) { console.error(`Configuration error: ${e.message}`); return 2; }
  if (!llm) {
    console.error("LLM_BASE_URL is not set: load the service settings first (set -a; . /etc/tsi-explorer/server.env; set +a)");
    return 2;
  }
  const shown = new URL(llm.baseUrl);
  shown.username = ""; shown.password = "";
  const env = process.env;
  const proxySet = Boolean(env.HTTPS_PROXY || env.https_proxy || env.HTTP_PROXY || env.http_proxy);
  console.log(`Gateway ${shown.href.replace(/\/+$/, "")}, model ${llm.model}, reasoning_effort ${llm.reasoning ? "sent" : "not sent"}, `
    + `proxy: HTTPS_PROXY ${proxySet ? "set" : "unset"}, NO_PROXY ${env.NO_PROXY || env.no_proxy || "unset"}`);
  const results = await checkGateway(llm, await proxyFetch());
  for (const r of results) console.log(`${r.ok ? "OK  " : "FAIL"} ${r.name}: ${r.detail}`);
  if (results.some((r) => /reasoning/i.test(r.detail))) console.log("Hint: the gateway refuses reasoning_effort: set LLM_REASONING=off");
  return results.every((r) => r.ok) ? 0 : 1;
}

if (process.argv[1] && import.meta.url === pathToFileURL(process.argv[1]).href) process.exitCode = await main();
