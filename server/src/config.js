// Settings of the application service, from environment variables (deploy/onprem/README.md, step 6).
import { readFileSync } from "node:fs";

export class ConfigError extends Error {}

function secretFrom(env, name) {
  const file = env[`${name}_FILE`];
  const value = file ? readFileSync(file, "utf8").trim() : (env[name] || "").trim();
  if (value.length < 32) throw new ConfigError(`${name} (or ${name}_FILE) must hold at least 32 characters`);
  return value;
}

const LOOPBACK = new Set(["localhost", "127.0.0.1", "[::1]"]);

export function loadConfig(env = process.env) {
  const publicUrl = (env.PUBLIC_URL || "").replace(/\/+$/, "");
  let pub;
  try {
    pub = new URL(publicUrl);
  } catch {
    throw new ConfigError("PUBLIC_URL must be the address of the site, e.g. https://tsi-explorer.example.eu");
  }
  const authMode = (env.AUTH_MODE || "oidc").toLowerCase();
  if (!["oidc", "password", "dev"].includes(authMode)) throw new ConfigError("AUTH_MODE must be oidc, password or dev");
  // The test sign-in (any e-mail address, no password) only exists on a local address.
  if (authMode === "dev" && !LOOPBACK.has(pub.hostname)) {
    throw new ConfigError("AUTH_MODE=dev is only allowed when PUBLIC_URL is a local address (localhost)");
  }
  // Passwords never travel in clear: HTTPS, except for a local test.
  if (authMode === "password" && pub.protocol !== "https:" && !LOOPBACK.has(pub.hostname)) {
    throw new ConfigError("AUTH_MODE=password needs an https:// PUBLIC_URL");
  }
  const cfg = {
    port: Number(env.PORT || 3000),
    host: env.HOST || "127.0.0.1",
    publicUrl,
    secure: pub.protocol === "https:",
    postgrestUrl: (env.POSTGREST_URL || "http://127.0.0.1:3001").replace(/\/+$/, ""),
    jwtSecret: secretFrom(env, "JWT_SECRET"),
    sessionSecret: secretFrom(env, "SESSION_SECRET"),
    sessionHours: Number(env.SESSION_HOURS || 10),
    tokenMinutes: Number(env.TOKEN_MINUTES || 15),
    authMode,
    oidc: null,
    staticDir: env.STATIC_DIR || "",
    assistantUrl: (env.ASSISTANT_URL || "").replace(/\/+$/, ""),
  };
  if (authMode === "oidc") {
    for (const k of ["OIDC_ISSUER", "OIDC_CLIENT_ID"]) if (!env[k]) throw new ConfigError(`${k} is required (AUTH_MODE=oidc)`);
    cfg.oidc = {
      issuer: env.OIDC_ISSUER,
      clientId: env.OIDC_CLIENT_ID,
      clientSecret: env.OIDC_CLIENT_SECRET || "",
      scopes: env.OIDC_SCOPES || "openid email profile",
      emailClaim: env.OIDC_EMAIL_CLAIM || "email",
      redirectUri: `${publicUrl}/api/auth/callback`,
    };
  }
  return cfg;
}
