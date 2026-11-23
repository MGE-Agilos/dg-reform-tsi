// TSI x CSR Explorer: application service of the self-hosted installation (deploy/onprem/README.md, step 6).
import { createServer } from "node:http";
import { loadConfig, ConfigError } from "./src/config.js";
import { createApp } from "./src/app.js";
import { oidcProvider } from "./src/auth.js";

let cfg;
try {
  cfg = loadConfig();
} catch (e) {
  if (e instanceof ConfigError || e?.code === "ENOENT") {
    console.error(`Configuration error: ${e.message}`);
    process.exit(2);
  }
  throw e;
}

const provider = cfg.authMode === "oidc" ? oidcProvider(cfg.oidc, await import("openid-client")) : null;
const handler = createApp(cfg, { provider });
createServer((req, res) => { handler(req, res); }).listen(cfg.port, cfg.host, () => {
  console.log(JSON.stringify({ event: "started", port: cfg.port, host: cfg.host, auth: cfg.authMode, static: Boolean(cfg.staticDir) }));
  if (cfg.authMode === "dev") console.warn("AUTH_MODE=dev: local test sign-in, anyone can sign in with any address.");
});
