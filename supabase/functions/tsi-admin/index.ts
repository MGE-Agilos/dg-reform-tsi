// Supabase Edge Function "tsi-admin": administration of the explorer's users (Admin tab), for administrators only.
// POST JSON {action, ...} with the caller's session token:
//   list                                   -> {users: [{email, role, invited_at, invited_by, account, email_confirmed_at,
//                                              last_sign_in_at, must_change_password}]}  (testers only, never other users)
//   invite {email, role, send_link?}       -> new account: {email, role, existing: false, password, link_sent?}
//                                             (temporary password returned ONCE, never stored nor logged);
//                                             existing account (e.g. another app of the shared project): {existing: true}
//                                             (its password is not touched); already a tester: 409
//   set_role {email, role}                 -> {email, role}   (the last administrator cannot be demoted: 409)
//   revoke {email}                         -> {email, revoked: true}  (testers row only; the auth account is kept: it may
//                                             belong to another app; revoking yourself: 409)
//   reset_password {email}                 -> {email, password}  (new temporary password, must_change_password = true)
// Errors: {error} with 400 (invalid input), 401 (no or invalid token), 403 (not an administrator), 404, 409, 502.
// Checks: the caller's token is verified by Auth (GET /auth/v1/user with the anon key), then its confirmed e-mail must
// be in public.testers with role 'admin' (read with the service_role key). Supabase project SHARED with other apps: this
// function only reads auth users whose e-mail is in testers, creates accounts only for invitations, never deletes an
// account and never changes project settings.
// Single self-contained file, no imports: paste it into Supabase (Edge Functions > Via Editor), keep "Verify JWT" on.
// Env: SUPABASE_URL, SUPABASE_ANON_KEY and SUPABASE_SERVICE_ROLE_KEY (provided by Supabase); optional ALLOWED_ORIGINS
// (same default as tsi-assistant) and TSI_SITE_URL (redirect of the sign-in link, default the GitHub Pages site).
// Needs supabase/migrations/007_admin_monitoring.sql (testers.role, invited_at, invited_by).
// Tests: node --experimental-strip-types --test supabase/functions/tsi-admin/index.test.ts
// Never logs passwords, tokens, keys nor e-mail addresses: only action, status and duration.

type Env = Record<string, string | undefined>;
type FetchImpl = (url: string, init: RequestInit) => Promise<Response>;
type Role = "user" | "admin";
type TesterRow = { email: string; role: Role; invited_at: string | null; invited_by: string | null };
type AuthUser = { id: string; email?: string; email_confirmed_at?: string | null; last_sign_in_at?: string | null;
  created_at?: string | null; user_metadata?: Record<string, unknown> | null };
export type Ctx = { env: Env; fetchImpl: FetchImpl; url: string; service: Record<string, string>; anon: string;
  random: (n: number) => Uint8Array };

export const ROLES: Role[] = ["user", "admin"];
export const ACTIONS = ["list", "invite", "set_role", "revoke", "reset_password"] as const;
export const PASSWORD_LENGTH = 16;
export const PER_PAGE = 1000; // auth admin users per page
export const MAX_PAGES = 50; // 50,000 accounts at most are scanned
export const CALL_TIMEOUT_MS = 10000;
export const DEFAULT_SITE = "https://mge-agilos.github.io/dg-reform-tsi/";
const DEFAULT_ORIGINS = ["https://mge-agilos.github.io", "http://localhost:8000"];
const ALPHABET = "abcdefghijkmnopqrstuvwxyzABCDEFGHJKLMNPQRSTUVWXYZ23456789"; // no 0/O, 1/l/I
const EMAIL = /^[^\s@,;<>"'()]+@[^\s@,;<>"'()]+\.[^\s@,;<>"'()]+$/;

export class HttpError extends Error {
  status: number;
  constructor(status: number, message: string) {
    super(message);
    this.status = status;
  }
}

// ---------- validation ----------

export function normEmail(value: unknown): string {
  if (typeof value !== "string") throw new HttpError(400, "email is required");
  const email = value.trim().toLowerCase();
  if (!email || email.length > 254 || !EMAIL.test(email)) throw new HttpError(400, "Invalid e-mail address");
  return email;
}

export function normRole(value: unknown): Role {
  if (value === "user" || value === "admin") return value;
  throw new HttpError(400, "role must be user or admin");
}

export function validate(body: unknown): { action: (typeof ACTIONS)[number]; email?: string; role?: Role; send_link?: boolean } {
  if (!body || typeof body !== "object" || Array.isArray(body)) throw new HttpError(400, "Invalid request body");
  const b = body as Record<string, unknown>;
  const action = b.action;
  if (typeof action !== "string" || !(ACTIONS as readonly string[]).includes(action)) throw new HttpError(400, "Unknown action");
  const a = action as (typeof ACTIONS)[number];
  if (a === "list") return { action: a };
  const email = normEmail(b.email);
  if (a === "invite") {
    if (b.send_link !== undefined && typeof b.send_link !== "boolean") throw new HttpError(400, "send_link must be a boolean");
    return { action: a, email, role: b.role === undefined ? "user" : normRole(b.role), send_link: b.send_link === true };
  }
  if (a === "set_role") return { action: a, email, role: normRole(b.role) };
  return { action: a, email };
}

/** Temporary password: PASSWORD_LENGTH characters without look-alikes, with a lower case letter, an upper case letter
 * and a digit (rejection sampling: no modulo bias). */
export function tempPassword(random: (n: number) => Uint8Array = (n) => crypto.getRandomValues(new Uint8Array(n))): string {
  const limit = 256 - (256 % ALPHABET.length);
  for (let attempt = 0; attempt < 100; attempt++) {
    let out = "";
    while (out.length < PASSWORD_LENGTH) {
      for (const b of random(PASSWORD_LENGTH * 2)) {
        if (b < limit && out.length < PASSWORD_LENGTH) out += ALPHABET[b % ALPHABET.length];
      }
    }
    if (/[a-z]/.test(out) && /[A-Z]/.test(out) && /[0-9]/.test(out)) return out;
  }
  throw new HttpError(500, "Could not generate a password");
}

// ---------- HTTP helpers ----------

export function allowedOrigins(env: Env): string[] {
  const raw = env.ALLOWED_ORIGINS;
  if (!raw || !raw.trim()) return [...DEFAULT_ORIGINS];
  return raw.split(",").map((s) => s.trim()).filter(Boolean);
}

export function corsHeaders(origin: string | null, allowed: string[]): Record<string, string> {
  const headers: Record<string, string> = {
    "Vary": "Origin",
    "Access-Control-Allow-Headers": "authorization, apikey, content-type, x-client-info",
    "Access-Control-Allow-Methods": "POST, OPTIONS",
  };
  if (origin && allowed.includes(origin)) headers["Access-Control-Allow-Origin"] = origin;
  return headers;
}

/** Headers of the service_role key (sb_secret_ keys: apikey only, as in tsi-assistant). */
export function serviceHeaders(key: string): Record<string, string> {
  const h: Record<string, string> = { "apikey": key, "Content-Type": "application/json", "Accept": "application/json" };
  if (!key.startsWith("sb_secret_")) h["Authorization"] = `Bearer ${key}`;
  return h;
}

async function call(ctx: Ctx, path: string, init: RequestInit & { headers?: Record<string, string> } = {}): Promise<Response> {
  try {
    return await ctx.fetchImpl(`${ctx.url}${path}`, { ...init, headers: { ...ctx.service, ...(init.headers || {}) },
      signal: AbortSignal.timeout(CALL_TIMEOUT_MS) });
  } catch {
    throw new HttpError(502, "Supabase is unreachable");
  }
}

async function ok(res: Response, what: string): Promise<unknown> {
  if (!res.ok) throw new HttpError(502, `${what} failed (${res.status})`);
  const text = await res.text();
  return text ? JSON.parse(text) : null;
}

const eq = (v: string) => `eq.${encodeURIComponent(v)}`;

// ---------- data access ----------

async function testers(ctx: Ctx): Promise<TesterRow[]> {
  const rows = await ok(await call(ctx, "/rest/v1/testers?select=email,role,invited_at,invited_by&order=email"), "Reading testers");
  return Array.isArray(rows) ? rows as TesterRow[] : [];
}

async function tester(ctx: Ctx, email: string): Promise<TesterRow | null> {
  const rows = await ok(await call(ctx, `/rest/v1/testers?select=email,role,invited_at,invited_by&email=${eq(email)}`), "Reading testers");
  return Array.isArray(rows) && rows.length ? rows[0] as TesterRow : null;
}

/** Auth accounts whose e-mail is in `wanted` (scans the admin list page by page; other accounts are dropped at once). */
export async function authUsers(ctx: Ctx, wanted: Set<string>): Promise<Map<string, AuthUser>> {
  const found = new Map<string, AuthUser>();
  if (!wanted.size) return found;
  for (let page = 1; page <= MAX_PAGES; page++) {
    const body = await ok(await call(ctx, `/auth/v1/admin/users?page=${page}&per_page=${PER_PAGE}`), "Listing accounts") as { users?: AuthUser[] } | null;
    const users = Array.isArray(body?.users) ? body!.users! : [];
    for (const u of users) {
      const email = typeof u?.email === "string" ? u.email.toLowerCase() : "";
      if (email && wanted.has(email)) found.set(email, u);
    }
    if (found.size === wanted.size || users.length < PER_PAGE) break;
  }
  return found;
}

async function authUser(ctx: Ctx, email: string): Promise<AuthUser | null> {
  return (await authUsers(ctx, new Set([email]))).get(email) ?? null;
}

async function adminCount(ctx: Ctx): Promise<number> {
  const rows = await ok(await call(ctx, "/rest/v1/testers?select=email&role=eq.admin"), "Reading testers");
  return Array.isArray(rows) ? rows.length : 0;
}

/** The caller: token verified by Auth, confirmed e-mail in testers with role admin; else 401 / 403. */
async function caller(ctx: Ctx, authorization: string): Promise<string> {
  let res: Response;
  try {
    res = await ctx.fetchImpl(`${ctx.url}/auth/v1/user`, {
      method: "GET", headers: { "apikey": ctx.anon, "Authorization": authorization },
      signal: AbortSignal.timeout(CALL_TIMEOUT_MS),
    });
  } catch {
    throw new HttpError(502, "Supabase is unreachable");
  }
  if (res.status === 401 || res.status === 403) throw new HttpError(401, "Invalid or expired session");
  const user = await ok(res, "Checking the session") as AuthUser | null;
  const email = typeof user?.email === "string" ? user.email.toLowerCase() : "";
  if (!email || !user?.email_confirmed_at) throw new HttpError(403, "Administrators only");
  const row = await tester(ctx, email);
  if (row?.role !== "admin") throw new HttpError(403, "Administrators only");
  return email;
}

// ---------- actions ----------

function userView(t: TesterRow, u: AuthUser | undefined) {
  return {
    email: t.email, role: t.role, invited_at: t.invited_at ?? null, invited_by: t.invited_by ?? null,
    account: Boolean(u), email_confirmed_at: u?.email_confirmed_at ?? null, last_sign_in_at: u?.last_sign_in_at ?? null,
    must_change_password: u?.user_metadata?.must_change_password === true,
  };
}

async function list(ctx: Ctx) {
  const rows = await testers(ctx);
  const users = await authUsers(ctx, new Set(rows.map((t) => t.email)));
  return { users: rows.map((t) => userView(t, users.get(t.email))) };
}

async function sendLink(ctx: Ctx, email: string): Promise<boolean> {
  // Same request as the page's "Send me a sign-in link" (signInWithOtp, shouldCreateUser false), with the anon key.
  const site = ctx.env.TSI_SITE_URL || DEFAULT_SITE;
  try {
    const res = await ctx.fetchImpl(`${ctx.url}/auth/v1/otp?redirect_to=${encodeURIComponent(site)}`, {
      method: "POST", headers: { "apikey": ctx.anon, "Content-Type": "application/json" },
      body: JSON.stringify({ email, create_user: false }), signal: AbortSignal.timeout(CALL_TIMEOUT_MS),
    });
    return res.ok;
  } catch {
    return false;
  }
}

async function upsertTester(ctx: Ctx, email: string, role: Role, by: string) {
  await ok(await call(ctx, "/rest/v1/testers?on_conflict=email", {
    method: "POST", headers: { "Prefer": "resolution=merge-duplicates,return=minimal" },
    body: JSON.stringify({ email, role, invited_by: by, invited_at: new Date().toISOString() }),
  }), "Saving the tester");
}

async function invite(ctx: Ctx, me: string, email: string, role: Role, link: boolean) {
  if (await tester(ctx, email)) throw new HttpError(409, "This e-mail already has access to the explorer");
  const existing = await authUser(ctx, email);
  if (existing) {
    // An account of the shared project (another app, or a former tester): its password is never changed here.
    await upsertTester(ctx, email, role, me);
    const out: Record<string, unknown> = { email, role, existing: true };
    if (link) out.link_sent = await sendLink(ctx, email);
    return out;
  }
  const password = tempPassword(ctx.random);
  const res = await call(ctx, "/auth/v1/admin/users", {
    method: "POST",
    body: JSON.stringify({ email, password, email_confirm: true, user_metadata: { must_change_password: true } }),
  });
  if (res.status === 422 || res.status === 409) {
    // Created meanwhile (or listed beyond the scanned pages): treat it as an existing account.
    await upsertTester(ctx, email, role, me);
    return { email, role, existing: true, ...(link ? { link_sent: await sendLink(ctx, email) } : {}) };
  }
  await ok(res, "Creating the account");
  await upsertTester(ctx, email, role, me);
  const out: Record<string, unknown> = { email, role, existing: false, password };
  if (link) out.link_sent = await sendLink(ctx, email);
  return out;
}

async function setRole(ctx: Ctx, email: string, role: Role) {
  const row = await tester(ctx, email);
  if (!row) throw new HttpError(404, "Not a user of the explorer");
  if (row.role === role) return { email, role };
  if (row.role === "admin" && role !== "admin" && (await adminCount(ctx)) <= 1) {
    throw new HttpError(409, "The last administrator cannot be demoted");
  }
  await ok(await call(ctx, `/rest/v1/testers?email=${eq(email)}`, {
    method: "PATCH", headers: { "Prefer": "return=minimal" }, body: JSON.stringify({ role }),
  }), "Saving the role");
  return { email, role };
}

async function revoke(ctx: Ctx, me: string, email: string) {
  if (email === me) throw new HttpError(409, "You cannot revoke your own access");
  const row = await tester(ctx, email);
  if (!row) throw new HttpError(404, "Not a user of the explorer");
  if (row.role === "admin" && (await adminCount(ctx)) <= 1) throw new HttpError(409, "The last administrator cannot be revoked");
  await ok(await call(ctx, `/rest/v1/testers?email=${eq(email)}`, { method: "DELETE", headers: { "Prefer": "return=minimal" } }),
    "Revoking the access");
  return { email, revoked: true };
}

async function resetPassword(ctx: Ctx, email: string) {
  if (!(await tester(ctx, email))) throw new HttpError(404, "Not a user of the explorer");
  const user = await authUser(ctx, email);
  if (!user?.id) throw new HttpError(404, "No account for this e-mail");
  const password = tempPassword(ctx.random);
  await ok(await call(ctx, `/auth/v1/admin/users/${encodeURIComponent(user.id)}`, {
    method: "PUT", body: JSON.stringify({ password, user_metadata: { must_change_password: true } }),
  }), "Resetting the password");
  return { email, password };
}

// ---------- handler ----------

export async function handle(req: Request, env: Env, fetchImpl: FetchImpl,
  random: (n: number) => Uint8Array = (n) => crypto.getRandomValues(new Uint8Array(n))): Promise<Response> {
  const cors = corsHeaders(req.headers.get("Origin"), allowedOrigins(env));
  const reply = (status: number, body: unknown) =>
    new Response(JSON.stringify(body), { status, headers: { ...cors, "Content-Type": "application/json", "Cache-Control": "no-store" } });
  if (req.method === "OPTIONS") return new Response(null, { status: 204, headers: { ...cors, "Access-Control-Max-Age": "86400" } });
  if (req.method !== "POST") return reply(405, { error: "Method not allowed" });
  const authorization = req.headers.get("Authorization") ?? "";
  if (!/^Bearer\s+\S+/i.test(authorization)) return reply(401, { error: "Missing bearer token" });

  const started = Date.now();
  let action = "?";
  let status = 500;
  try {
    const url = (env.SUPABASE_URL || "").replace(/\/$/, "");
    const key = env.SUPABASE_SERVICE_ROLE_KEY || "";
    const anon = env.SUPABASE_ANON_KEY || req.headers.get("apikey") || "";
    if (!url || !key || !anon) throw new HttpError(500, "Function not configured");
    const ctx: Ctx = { env, fetchImpl, url, service: serviceHeaders(key), anon, random };
    const me = await caller(ctx, authorization);
    let body: unknown;
    try {
      body = await req.json();
    } catch {
      throw new HttpError(400, "Invalid JSON body");
    }
    const v = validate(body);
    action = v.action;
    let out: unknown;
    if (v.action === "list") out = await list(ctx);
    else if (v.action === "invite") out = await invite(ctx, me, v.email!, v.role!, v.send_link === true);
    else if (v.action === "set_role") out = await setRole(ctx, v.email!, v.role!);
    else if (v.action === "revoke") out = await revoke(ctx, me, v.email!);
    else out = await resetPassword(ctx, v.email!);
    status = 200;
    return reply(200, out);
  } catch (e) {
    status = e instanceof HttpError ? e.status : 500;
    return reply(status, { error: e instanceof HttpError ? e.message : "Internal error" });
  } finally {
    console.log(JSON.stringify({ action, status, ms: Date.now() - started }));
  }
}

declare const Deno: any;
if (typeof Deno !== "undefined") Deno.serve((req: Request) => handle(req, Deno.env.toObject(), fetch));
