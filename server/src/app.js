// HTTP routes of the application service (all under /api; nginx serves the page and forwards /api here).
//   GET  /api/health              service, database (and assistant) reachable
//   GET  /api/auth/login          starts the sign-in (identity provider, or the local test page)
//   GET  /api/auth/callback       answer of the identity provider
//   POST /api/auth/dev            local test sign-in (AUTH_MODE=dev only)
//   GET  /api/auth/session        signed-in person and a short-lived token for the data requests (401 if none)
//   POST /api/auth/logout         ends the session
//   *    /api/rest/v1/...         data requests of the page, forwarded to PostgREST (row-level security applies)
//   POST /api/admin               administration of the users (administrators only)
//   POST /api/assistant           assistant (forwarded to ASSISTANT_URL when configured)
import { readFile, stat } from "node:fs/promises";
import { extname, join, normalize, sep } from "node:path";
import { seal, unseal, uuidV5, userToken, verifyJwt, serviceToken } from "./tokens.js";
import { devLoginPage, DEV_ISSUER } from "./auth.js";
import { HttpError, runAdmin, store, validate, normEmail } from "./admin.js";

const SESSION_COOKIE = "tsi_session";
const PENDING_COOKIE = "tsi_signin";
const BODY_MAX = 2 * 1024 * 1024;
const REST_METHODS = new Set(["GET", "HEAD", "POST", "PATCH", "DELETE"]);
// Request headers forwarded to PostgREST (everything else, cookies included, is dropped).
const REST_IN = ["authorization", "content-type", "accept", "prefer", "range", "range-unit", "accept-profile", "content-profile"];
const REST_OUT = ["content-type", "content-range", "preference-applied", "location"];
const MIME = { ".html": "text/html; charset=utf-8", ".js": "text/javascript; charset=utf-8", ".css": "text/css; charset=utf-8",
  ".json": "application/json", ".svg": "image/svg+xml", ".png": "image/png", ".jpg": "image/jpeg", ".ico": "image/x-icon",
  ".woff2": "font/woff2", ".txt": "text/plain; charset=utf-8" };

const nowS = () => Math.floor(Date.now() / 1000);

export function parseCookies(header) {
  const out = {};
  for (const part of String(header || "").split(";")) {
    const i = part.indexOf("=");
    if (i > 0) out[part.slice(0, i).trim()] = decodeURIComponent(part.slice(i + 1).trim());
  }
  return out;
}

function cookie(cfg, name, value, maxAge) {
  return `${name}=${encodeURIComponent(value)}; Path=/api; HttpOnly; SameSite=Lax; Max-Age=${maxAge}${cfg.secure ? "; Secure" : ""}`;
}

/** Route of the page to come back to after signing in: a hash such as "#projects?q=water", nothing else. */
export function safeNext(value) {
  const s = String(value || "");
  return /^#[\w\-/?=&%.:,+~@]*$/.test(s) && s.length <= 500 ? s : "#dashboard";
}

async function readBody(req) {
  const chunks = [];
  let size = 0;
  for await (const chunk of req) {
    size += chunk.length;
    if (size > BODY_MAX) throw new HttpError(413, "Request too large");
    chunks.push(chunk);
  }
  return Buffer.concat(chunks);
}

function send(res, status, body, headers = {}) {
  const isText = typeof body === "string" || Buffer.isBuffer(body);
  res.writeHead(status, {
    "Cache-Control": "no-store", "X-Content-Type-Options": "nosniff",
    ...(isText ? {} : { "Content-Type": "application/json" }), ...headers,
  });
  res.end(isText ? body : JSON.stringify(body));
}

export function createApp(cfg, { fetchImpl = fetch, provider = null, log = console.log } = {}) {
  const db = store(cfg, fetchImpl);

  const session = (req) => unseal(parseCookies(req.headers.cookie)[SESSION_COOKIE], cfg.sessionSecret);

  function startSession(res, person, next) {
    const user = { id: uuidV5(person.sub), email: person.email };
    const exp = nowS() + cfg.sessionHours * 3600;
    res.writeHead(303, {
      "Location": `${cfg.publicUrl}/${safeNext(next)}`, "Cache-Control": "no-store",
      "Set-Cookie": [cookie(cfg, SESSION_COOKIE, seal({ ...user, exp }, cfg.sessionSecret), cfg.sessionHours * 3600),
        cookie(cfg, PENDING_COOKIE, "", 0)],
    });
    res.end();
    log(JSON.stringify({ event: "sign_in", user: user.id }));
  }

  /** Administrator calling /api/admin: valid token, e-mail in testers with the role admin. */
  async function adminCaller(req) {
    const m = /^Bearer\s+(\S+)$/i.exec(req.headers.authorization || "");
    const claims = m && verifyJwt(m[1], cfg.jwtSecret);
    if (!claims?.email || claims.role !== "authenticated") throw new HttpError(401, "Invalid or expired session");
    const row = await db.tester(String(claims.email).toLowerCase());
    if (row?.role !== "admin") throw new HttpError(403, "Administrators only");
    return row.email;
  }

  async function proxyRest(req, res, path) {
    if (!REST_METHODS.has(req.method)) return send(res, 405, { error: "Method not allowed" });
    const headers = {};
    for (const h of REST_IN) if (req.headers[h]) headers[h] = req.headers[h];
    const body = req.method === "GET" || req.method === "HEAD" ? undefined : await readBody(req);
    let up;
    try {
      up = await fetchImpl(`${cfg.postgrestUrl}${path}`, { method: req.method, headers, body, signal: AbortSignal.timeout(60000) });
    } catch {
      return send(res, 502, { error: "The database service is unreachable" });
    }
    const out = { "Cache-Control": "no-store" };
    for (const h of REST_OUT) { const v = up.headers.get(h); if (v) out[h] = v; }
    res.writeHead(up.status, out);
    res.end(req.method === "HEAD" ? undefined : Buffer.from(await up.arrayBuffer()));
  }

  async function forwardAssistant(req, res) {
    if (!cfg.assistantUrl) return send(res, 503, { error: "The assistant is not available yet on this installation" });
    const body = await readBody(req);
    let up;
    try {
      up = await fetchImpl(cfg.assistantUrl, { method: "POST", body, signal: AbortSignal.timeout(170000),
        headers: { "content-type": "application/json", authorization: req.headers.authorization || "" } });
    } catch {
      return send(res, 502, { error: "The assistant service is unreachable" });
    }
    send(res, up.status, Buffer.from(await up.arrayBuffer()), { "Content-Type": up.headers.get("content-type") || "application/json" });
  }

  async function serveStatic(req, res, pathname) {
    if (!cfg.staticDir || (req.method !== "GET" && req.method !== "HEAD")) return send(res, 404, { error: "Not found" });
    const root = normalize(cfg.staticDir);
    let file = normalize(join(root, decodeURIComponent(pathname)));
    if (file !== root && !file.startsWith(root + sep)) return send(res, 404, { error: "Not found" });
    try {
      if ((await stat(file)).isDirectory()) file = join(file, "index.html");
      const data = await readFile(file);
      res.writeHead(200, { "Content-Type": MIME[extname(file)] || "application/octet-stream", "Cache-Control": "no-cache",
        "X-Content-Type-Options": "nosniff" });
      res.end(req.method === "HEAD" ? undefined : data);
    } catch {
      send(res, 404, "Not found", { "Content-Type": "text/plain" });
    }
  }

  return async function handle(req, res) {
    const url = new URL(req.url, cfg.publicUrl);
    const p = url.pathname;
    try {
      if (!p.startsWith("/api/")) return await serveStatic(req, res, p);

      if (p === "/api/health" && req.method === "GET") {
        let database = false;
        try {
          const r = await fetchImpl(`${cfg.postgrestUrl}/csr_documents?select=doc_id&limit=1`,
            { headers: { Authorization: `Bearer ${serviceToken(cfg.jwtSecret)}` }, signal: AbortSignal.timeout(5000) });
          database = r.ok;
        } catch { /* unreachable */ }
        return send(res, database ? 200 : 503, { ok: database, database, assistant: Boolean(cfg.assistantUrl), auth: cfg.authMode });
      }

      if (p === "/api/auth/login" && req.method === "GET") {
        const next = safeNext(url.searchParams.get("next"));
        if (cfg.authMode === "dev") return send(res, 200, devLoginPage(next), { "Content-Type": "text/html; charset=utf-8" });
        const { url: to, pending } = await provider.start();
        res.writeHead(303, { "Location": to, "Cache-Control": "no-store",
          "Set-Cookie": cookie(cfg, PENDING_COOKIE, seal({ ...pending, next, exp: nowS() + 600 }, cfg.sessionSecret), 600) });
        return res.end();
      }

      if (p === "/api/auth/callback" && req.method === "GET") {
        if (cfg.authMode !== "oidc") return send(res, 404, { error: "Not found" });
        const pending = unseal(parseCookies(req.headers.cookie)[PENDING_COOKIE], cfg.sessionSecret);
        if (!pending) return send(res, 400, "Sign-in expired or started in another browser: open the site again.", { "Content-Type": "text/plain; charset=utf-8" });
        let person;
        try {
          person = await provider.finish(`${cfg.publicUrl}${p}${url.search}`, pending);
        } catch (e) {
          log(JSON.stringify({ event: "sign_in_failed", reason: String(e?.message || e).slice(0, 200) }));
          return send(res, 401, "Sign-in failed. Open the site again to retry.", { "Content-Type": "text/plain; charset=utf-8" });
        }
        return startSession(res, person, pending.next);
      }

      if (p === "/api/auth/dev" && req.method === "POST") {
        if (cfg.authMode !== "dev") return send(res, 404, { error: "Not found" });
        const form = new URLSearchParams((await readBody(req)).toString("utf8"));
        let email;
        try { email = normEmail(form.get("email")); } catch {
          return send(res, 400, devLoginPage(safeNext(form.get("next")), "Enter a valid e-mail address."), { "Content-Type": "text/html; charset=utf-8" });
        }
        return startSession(res, { email, sub: `${DEV_ISSUER}|${email}` }, form.get("next"));
      }

      if (p === "/api/auth/session" && req.method === "GET") {
        const s = session(req);
        if (!s) return send(res, 401, { error: "Not signed in" });
        const user = { id: s.id, email: s.email };
        const { token, exp } = userToken(user, cfg.jwtSecret, cfg.tokenMinutes);
        return send(res, 200, { access_token: token, token_type: "bearer", expires_at: Math.min(exp, s.exp),
          user: { ...user, user_metadata: {}, app_metadata: { provider: cfg.authMode } } });
      }

      if (p === "/api/auth/logout" && req.method === "POST") {
        res.writeHead(204, { "Cache-Control": "no-store", "Set-Cookie": cookie(cfg, SESSION_COOKIE, "", 0) });
        return res.end();
      }

      if (p.startsWith("/api/rest/v1/")) return await proxyRest(req, res, p.slice("/api/rest/v1".length) + url.search);

      if (p === "/api/admin" && req.method === "POST") {
        const me = await adminCaller(req);
        let body;
        try { body = JSON.parse((await readBody(req)).toString("utf8") || "null"); } catch { throw new HttpError(400, "Invalid JSON body"); }
        const v = validate(body);
        const out = await runAdmin(db, me, v);
        log(JSON.stringify({ event: "admin", action: v.action }));
        return send(res, 200, out);
      }

      if (p === "/api/assistant" && req.method === "POST") return await forwardAssistant(req, res);

      return send(res, 404, { error: "Not found" });
    } catch (e) {
      if (e instanceof HttpError) return send(res, e.status, { error: e.message });
      log(JSON.stringify({ event: "error", path: p, error: String(e?.message || e).slice(0, 300) }));
      return send(res, 500, { error: "Internal error" });
    }
  };
}
