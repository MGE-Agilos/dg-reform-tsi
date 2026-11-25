// Administration of the explorer's users (testers table), same contract as the cloud function tsi-admin.
// AUTH_MODE=oidc: people sign in with their organisation account, so an invitation only grants access.
// AUTH_MODE=password: an invitation (or a reset) also gives a temporary password, shown once to the administrator.
import { serviceToken } from "./tokens.js";
import { hashPassword, tempPassword } from "./passwords.js";

export class HttpError extends Error {
  constructor(status, message) { super(message); this.status = status; }
}

const ACTIONS = ["list", "invite", "set_role", "revoke", "reset_password"];
const EMAIL = /^[^\s@]+@[^\s@]+\.[^\s@]+$/;
const MAX_USERS = 500;

export function normEmail(value) {
  const email = typeof value === "string" ? value.trim().toLowerCase() : "";
  if (!EMAIL.test(email) || email.length > 254) throw new HttpError(400, "A valid e-mail address is required");
  return email;
}

export function normRole(value) {
  if (value === "user" || value === "admin") return value;
  throw new HttpError(400, "role must be user or admin");
}

export function validate(body) {
  if (!body || typeof body !== "object" || Array.isArray(body)) throw new HttpError(400, "Invalid request body");
  const { action } = body;
  if (!ACTIONS.includes(action)) throw new HttpError(400, "Unknown action");
  if (action === "list") return { action };
  const email = normEmail(body.email);
  if (action === "invite") return { action, email, role: body.role === undefined ? "user" : normRole(body.role) };
  if (action === "set_role") return { action, email, role: normRole(body.role) };
  return { action, email };
}

/** Database calls with the service token (PostgREST). */
export function store(cfg, fetchImpl) {
  const call = async (path, init = {}, what = "Database") => {
    let res;
    try {
      res = await fetchImpl(`${cfg.postgrestUrl}${path}`, {
        ...init,
        headers: { "Authorization": `Bearer ${serviceToken(cfg.jwtSecret)}`, "Content-Type": "application/json", ...(init.headers || {}) },
        signal: AbortSignal.timeout(20000),
      });
    } catch {
      throw new HttpError(502, "The database service is unreachable");
    }
    if (!res.ok) throw new HttpError(502, `${what} failed (${res.status})`);
    const text = await res.text();
    return text ? JSON.parse(text) : null;
  };
  const eq = (v) => `eq.${encodeURIComponent(v)}`;
  return {
    testers: () => call(`/testers?select=email,role,invited_at,invited_by&order=email&limit=${MAX_USERS}`, {}, "Reading users"),
    tester: async (email) => (await call(`/testers?select=email,role&email=${eq(email)}`, {}, "Reading users"))?.[0] ?? null,
    adminCount: async () => (await call("/testers?select=email&role=eq.admin", {}, "Reading users")).length,
    lastSeen: async (email) => (await call(`/page_views?select=started_at&email=${eq(email)}&order=started_at.desc&limit=1`, {}, "Reading usage"))?.[0]?.started_at ?? null,
    upsert: (row) => call("/testers?on_conflict=email", {
      method: "POST", headers: { "Prefer": "resolution=merge-duplicates,return=minimal" }, body: JSON.stringify(row),
    }, "Saving the user"),
    setRole: (email, role) => call(`/testers?email=${eq(email)}`, {
      method: "PATCH", headers: { "Prefer": "return=minimal" }, body: JSON.stringify({ role }),
    }, "Saving the role"),
    passwords: async () => (await call(`/user_passwords?select=email,must_change&limit=${MAX_USERS}`, {}, "Reading passwords")) ?? [],
    password: async (email) => (await call(`/user_passwords?select=email,hash,must_change,failed_count,locked_until&email=${eq(email)}`,
      {}, "Reading the password"))?.[0] ?? null,
    savePassword: (row) => call("/user_passwords?on_conflict=email", {
      method: "POST", headers: { "Prefer": "resolution=merge-duplicates,return=minimal" },
      body: JSON.stringify({ failed_count: 0, locked_until: null, ...row, updated_at: new Date().toISOString() }),
    }, "Saving the password"),
    signInResult: (email, fields) => call(`/user_passwords?email=${eq(email)}`, {
      method: "PATCH", headers: { "Prefer": "return=minimal" }, body: JSON.stringify(fields),
    }, "Saving the sign-in"),
    remove: (email) => call(`/testers?email=${eq(email)}`, { method: "DELETE", headers: { "Prefer": "return=minimal" } }, "Revoking the access"),
  };
}

/** New temporary password for `email` (must be changed at the next sign-in); returns it in clear, once. */
export async function giveTempPassword(db, email) {
  const password = tempPassword();
  await db.savePassword({ email, hash: await hashPassword(password), must_change: true });
  return password;
}

/** Runs one action for the administrator `me` (already checked). `mode`: AUTH_MODE of the installation. */
export async function runAdmin(db, me, v, mode = "oidc") {
  const withPasswords = mode === "password";
  if (v.action === "list") {
    const rows = await db.testers();
    const seen = await Promise.all(rows.map((t) => db.lastSeen(t.email).catch(() => null)));
    if (withPasswords) {
      const pw = new Map((await db.passwords()).map((r) => [r.email, r]));
      return { users: rows.map((t, i) => ({ email: t.email, role: t.role, invited_at: t.invited_at ?? null, invited_by: t.invited_by ?? null,
        account: true, password_mode: true, has_password: pw.has(t.email), must_change_password: pw.get(t.email)?.must_change === true,
        email_confirmed_at: seen[i], last_sign_in_at: seen[i] })) };
    }
    // sso: accounts of the identity provider; "last_sign_in_at" is the last page opened in the explorer.
    return { users: rows.map((t, i) => ({ email: t.email, role: t.role, invited_at: t.invited_at ?? null, invited_by: t.invited_by ?? null,
      account: true, sso: true, email_confirmed_at: seen[i], last_sign_in_at: seen[i], must_change_password: false })) };
  }
  if (v.action === "invite") {
    if (await db.tester(v.email)) throw new HttpError(409, "This e-mail already has access to the explorer");
    await db.upsert({ email: v.email, role: v.role, invited_by: me, invited_at: new Date().toISOString() });
    if (withPasswords) return { email: v.email, role: v.role, existing: false, password: await giveTempPassword(db, v.email) };
    return { email: v.email, role: v.role, existing: false };
  }
  if (v.action === "reset_password") {
    if (!withPasswords) throw new HttpError(400, "Not available: users sign in with their organisation account (no password in the explorer)");
    if (!(await db.tester(v.email))) throw new HttpError(404, "Not a user of the explorer");
    return { email: v.email, password: await giveTempPassword(db, v.email) };
  }
  const row = await db.tester(v.email);
  if (!row) throw new HttpError(404, "Not a user of the explorer");
  if (v.action === "set_role") {
    if (row.role === v.role) return { email: v.email, role: v.role };
    if (row.role === "admin" && (await db.adminCount()) <= 1) throw new HttpError(409, "The last administrator cannot be demoted");
    await db.setRole(v.email, v.role);
    return { email: v.email, role: v.role };
  }
  // revoke
  if (v.email === me) throw new HttpError(409, "You cannot revoke your own access");
  if (row.role === "admin" && (await db.adminCount()) <= 1) throw new HttpError(409, "The last administrator cannot be revoked");
  await db.remove(v.email);
  return { email: v.email, revoked: true };
}
