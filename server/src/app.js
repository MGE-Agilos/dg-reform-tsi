// HTTP routes of the application service (all under /api; nginx serves the page and forwards /api here).
//   GET  /api/health              service, database (and assistant) reachable
//   GET  /api/auth/login          starts the sign-in (identity provider, or the local test page)
//   GET  /api/auth/callback       answer of the identity provider
//   POST /api/auth/dev            local test sign-in (AUTH_MODE=dev only)
//   GET  /api/auth/session        signed-in person and a short-lived token for the data requests (401 if none)
//   POST /api/auth/logout         ends the session
//   *    /api/rest/v1/...         data requests of the page, forwarded to PostgREST (row-level security applies)
//   POST /api/admin               administration of the users (administrators only)
//   POST /api/assistant           assistant (tsi-assistant on GPT@EC, when LLM_BASE_URL is set)
import { readFile, stat } from "node:fs/promises";
import { extname, join, normalize, sep } from "node:path";
import { seal, unseal, uuidV5, userToken, verifyJwt, serviceToken } from "./tokens.js";
import { devLoginPage, DEV_ISSUER } from "./auth.js";
import { HttpError, runAdmin, store, validate, normEmail } from "./admin.js";
import { verifyPassword, passwordProblem, hashPassword, DUMMY_HASH, MAX_FAILURES, LOCK_MINUTES, MAX_LENGTH } from "./passwords.js";

const SESSION_COOKIE = "tsi_session";
const PENDING_COOKIE = "tsi_signin";
const BODY_MAX = 2 * 1024 * 1024;
const REST_METHODS = new Set(["GET", "HEAD", "POST", "PATCH", "DELETE"]);
// Request headers forwarded to PostgREST (everything else, cookies included, is dropped). Not Accept-Profile /
// Content-Profile: the page always asks for the schema "public", while PostgREST serves the one schema of its
// db-schemas setting (deploy/onprem/README.md, step 5), which may have another name.
const REST_IN = ["authorization", "content-type", "accept", "prefer", "range", "range-unit"];
const REST_OUT = ["content-type", "content-range", "preference-applied", "location"];
const MIME = { ".html": "text/html; charset=utf-8", ".js": "text/javascript; charset=utf-8", ".css": "text/css; charset=utf-8",
  ".json": "application/json", ".svg": "image/svg+xml", ".png": "image/png", ".jpg": "image/jpeg", ".ico": "image/x-icon",
  ".woff2": "font/woff2", ".txt": "text/plain; charset=utf-8" };

const nowS = () => Math.floor(Date.now() / 1000);
const IP_MAX_FAILURES = 30; // failed password sign-ins per address and LOCK_MINUTES, all e-mail addresses together

/** Address of the visitor: the last X-Forwarded-For entry (added by nginx, not by the browser), else the socket. */
export function clientIp(req) {
  const fwd = String(req.headers["x-forwarded-for"] || "").split(",").map((x) => x.trim()).filter(Boolean);
  return fwd.at(-1) || req.socket?.remoteAddress || "unknown";
}

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

export function createApp(cfg, { fetchImpl = fetch, provider = null, log = console.log, assistant = null } = {}) {
  const db = store(cfg, fetchImpl);

  const session = (req) => unseal(parseCookies(req.headers.cookie)[SESSION_COOKIE], cfg.sessionSecret);
  const ipFailures = new Map(); // address -> { count, until }

  /** Session cookie: user id, e-mail, expiry and, with passwords, "must change the temporary password". */
  function sessionCookie(person, mustChange = false, exp = nowS() + cfg.sessionHours * 3600) {
    const user = { id: uuidV5(person.sub), email: person.email };
    return { user, value: cookie(cfg, SESSION_COOKIE, seal({ ...user, ...(mustChange ? { mc: true } : {}), exp }, cfg.sessionSecret),
      Math.max(0, exp - nowS())) };
  }

  function startSession(res, person, next) {
    const { user, value } = sessionCookie(person);
    res.writeHead(303, {
      "Location": `${cfg.publicUrl}/${safeNext(next)}`, "Cache-Control": "no-store",
      "Set-Cookie": [value, cookie(cfg, PENDING_COOKIE, "", 0)],
    });
    res.end();
    log(JSON.stringify({ event: "sign_in", user: user.id }));
  }

  const ipBlocked = (ip) => { const f = ipFailures.get(ip); return Boolean(f && f.until > Date.now() && f.count >= IP_MAX_FAILURES); };
  function ipFailed(ip) {
    const f = ipFailures.get(ip);
    if (!f || f.until <= Date.now()) ipFailures.set(ip, { count: 1, until: Date.now() + LOCK_MINUTES * 60000 });
    else f.count += 1;
    if (ipFailures.size > 10000) ipFailures.clear(); // bounded memory
  }

  async function jsonBody(req) {
    try { return JSON.parse((await readBody(req)).toString("utf8") || "null"); } catch { throw new HttpError(400, "Invalid JSON body"); }
  }

  /** E-mail + password (AUTH_MODE=password). Same answer for an unknown address and a wrong password. */
  async function passwordSignIn(req, res) {
    const ip = clientIp(req);
    const tooMany = { error: `Too many failed attempts: try again in ${LOCK_MINUTES} minutes, or ask an administrator to reset your password.` };
    if (ipBlocked(ip)) return send(res, 429, tooMany);
    const body = await jsonBody(req);
    const password = typeof body?.password === "string" ? body.password : "";
    let email = null;
    try { email = normEmail(body?.email); } catch { /* answered like a wrong password */ }
    const row = email && password && password.length <= MAX_LENGTH && (await db.tester(email)) ? await db.password(email) : null;
    if (row?.locked_until && Date.parse(row.locked_until) > Date.now()) {
      ipFailed(ip);
      log(JSON.stringify({ event: "sign_in_locked", email }));
      return send(res, 429, tooMany);
    }
    const ok = (await verifyPassword(password.slice(0, MAX_LENGTH), row?.hash ?? DUMMY_HASH)) && Boolean(row);
    if (!ok) {
      ipFailed(ip);
      if (row) {
        const n = (row.failed_count || 0) + 1;
        await db.signInResult(email, n >= MAX_FAILURES
          ? { failed_count: 0, locked_until: new Date(Date.now() + LOCK_MINUTES * 60000).toISOString() }
          : { failed_count: n });
      }
      log(JSON.stringify({ event: "sign_in_failed", email: email ?? "(invalid)", ip }));
      return send(res, 401, { error: "Invalid login credentials" });
    }
    if (row.failed_count || row.locked_until) await db.signInResult(email, { failed_count: 0, locked_until: null });
    const { user, value } = sessionCookie({ email, sub: `password|${email}` }, row.must_change === true);
    log(JSON.stringify({ event: "sign_in", user: user.id, email, ip }));
    return send(res, 200, { ok: true }, { "Set-Cookie": value });
  }

  /** New password chosen by the signed-in user (replaces a temporary one). */
  async function passwordChange(req, res) {
    const s = session(req);
    if (!s) return send(res, 401, { error: "Not signed in" });
    const body = await jsonBody(req);
    const problem = passwordProblem(body?.password);
    if (problem) return send(res, 400, { error: problem });
    if (!(await db.tester(s.email))) return send(res, 403, { error: "Not a user of the explorer" });
    await db.savePassword({ email: s.email, hash: await hashPassword(body.password), must_change: false });
    const { value } = sessionCookie({ email: s.email, sub: `password|${s.email}` }, false, s.exp);
    log(JSON.stringify({ event: "password_changed", email: s.email }));
    return send(res, 200, { ok: true }, { "Set-Cookie": value });
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

  async function runAssistant(req, res) {
    if (!assistant) return send(res, 503, { error: "The assistant is not available yet on this installation" });
    const out = await assistant(req, await readBody(req));
    send(res, out.status, Buffer.from(await out.arrayBuffer()), { "Content-Type": out.headers.get("content-type") || "application/json" });
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
        return send(res, database ? 200 : 503, { ok: database, database, assistant: Boolean(assistant), auth: cfg.authMode });
      }

      if (p === "/api/auth/config" && req.method === "GET") return send(res, 200, { mode: cfg.authMode });

      if (p === "/api/auth/password" && req.method === "POST") {
        if (cfg.authMode !== "password") return send(res, 404, { error: "Not found" });
        return await passwordSignIn(req, res);
      }

      if (p === "/api/auth/password/change" && req.method === "POST") {
        if (cfg.authMode !== "password") return send(res, 404, { error: "Not found" });
        return await passwordChange(req, res);
      }

      if (p === "/api/auth/login" && req.method === "GET") {
        const next = safeNext(url.searchParams.get("next"));
        if (cfg.authMode === "password") { // the sign-in form is on the page itself
          res.writeHead(303, { "Location": `${cfg.publicUrl}/${next}`, "Cache-Control": "no-store" });
          return res.end();
        }
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
          user: { ...user, user_metadata: { must_change_password: s.mc === true }, app_metadata: { provider: cfg.authMode } } });
      }

      if (p === "/api/auth/logout" && req.method === "POST") {
        res.writeHead(204, { "Cache-Control": "no-store", "Set-Cookie": cookie(cfg, SESSION_COOKIE, "", 0) });
        return res.end();
      }

      if (p.startsWith("/api/rest/v1/")) return await proxyRest(req, res, p.slice("/api/rest/v1".length) + url.search);

      if (p === "/api/admin" && req.method === "POST") {
        const me = await adminCaller(req);
        const v = validate(await jsonBody(req));
        const out = await runAdmin(db, me, v, cfg.authMode);
        log(JSON.stringify({ event: "admin", action: v.action }));
        return send(res, 200, out);
      }

      if (p === "/api/assistant" && req.method === "POST") return await runAssistant(req, res);

      return send(res, 404, { error: "Not found" });
    } catch (e) {
      if (e instanceof HttpError) return send(res, e.status, { error: e.message });
      log(JSON.stringify({ event: "error", path: p, error: String(e?.message || e).slice(0, 300) }));
      return send(res, 500, { error: "Internal error" });
    }
  };
}
