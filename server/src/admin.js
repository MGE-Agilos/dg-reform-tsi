// Administration of the explorer's users (testers table), same contract as the cloud function tsi-admin without
// passwords: people sign in with their organisation account, so an invitation only grants access.
import { serviceToken } from "./tokens.js";

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
    remove: (email) => call(`/testers?email=${eq(email)}`, { method: "DELETE", headers: { "Prefer": "return=minimal" } }, "Revoking the access"),
  };
}

/** Runs one action for the administrator `me` (already checked). */
export async function runAdmin(db, me, v) {
  if (v.action === "list") {
    const rows = await db.testers();
    const seen = await Promise.all(rows.map((t) => db.lastSeen(t.email).catch(() => null)));
    // sso: accounts of the identity provider; "last_sign_in_at" is the last page opened in the explorer.
    return { users: rows.map((t, i) => ({ email: t.email, role: t.role, invited_at: t.invited_at ?? null, invited_by: t.invited_by ?? null,
      account: true, sso: true, email_confirmed_at: seen[i], last_sign_in_at: seen[i], must_change_password: false })) };
  }
  if (v.action === "invite") {
    if (await db.tester(v.email)) throw new HttpError(409, "This e-mail already has access to the explorer");
    await db.upsert({ email: v.email, role: v.role, invited_by: me, invited_at: new Date().toISOString() });
    return { email: v.email, role: v.role, existing: false };
  }
  if (v.action === "reset_password") {
    throw new HttpError(400, "Not available: users sign in with their organisation account (no password in the explorer)");
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
