// TSI x CSR Explorer: application service of the self-hosted installation (deploy/onprem/README.md, step 6).
import { createServer } from "node:http";
import { loadConfig, ConfigError } from "./src/config.js";
import { createApp } from "./src/app.js";
import { oidcProvider } from "./src/auth.js";
import { createAssistant } from "./src/assistant.js";

let cfg;
try {
  cfg = loadConfig();
} catch (e) {
  if (e instanceof ConfigError || e?.code === "ENOENT" || e?.code === "EACCES") {
    console.error(`Configuration error: ${e.message}`);
    process.exit(2);
  }
  throw e;
}

const provider = cfg.authMode === "oidc" ? oidcProvider(cfg.oidc, await import("openid-client")) : null;
let assistant = null;
if (cfg.llm) {
  try {
    assistant = await createAssistant(cfg);
  } catch (e) {
    console.error(e?.code === "ERR_UNKNOWN_FILE_EXTENSION"
      ? "Assistant: start the service with node --experimental-strip-types (Node 22.6 or later); npm start does it."
      : `Assistant: ${e?.message || e}`);
    process.exit(2);
  }
}
const handler = createApp(cfg, { provider, assistant });
createServer((req, res) => { handler(req, res); }).listen(cfg.port, cfg.host, () => {
  console.log(JSON.stringify({ event: "started", port: cfg.port, host: cfg.host, auth: cfg.authMode, static: Boolean(cfg.staticDir), assistant: Boolean(assistant) }));
  if (cfg.authMode === "dev") console.warn("AUTH_MODE=dev: local test sign-in, anyone can sign in with any address.");
});
