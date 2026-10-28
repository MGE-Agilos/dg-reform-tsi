// Admin tab: pure helpers (no DOM, no network) for the users list, the invitation form and the usage monitoring,
// so Node can test them. The page itself is built by app.js.

export const ROLES = { user: "User", admin: "Admin" };
export const PERIODS = { 7: "Last 7 days", 30: "Last 30 days", 90: "Last 90 days", custom: "Custom" };
export const MAX_PERIOD_DAYS = 400; // admin_usage refuses longer periods
export const LOG_PAGE = 50;
export const EXPORT_MAX = 20000;
export const VIEW_LABELS = {
  dashboard: "Dashboard", projects: "Projects", project: "Project page", csrs: "CSRs", csr: "Recommendation page",
  csrdoc: "CSR document page", lineage: "Lineage", progress: "CSR progress", anomalies: "Anomalies", impact: "Impact", guide: "Guide",
  reviews: "Links to review", admin: "Admin", chat: "Assistant (chat)",
};
export const LOG_COLS = ["email", "view", "section", "detail", "started_at", "duration_s", "session_id"];

const arr = (v) => (Array.isArray(v) ? v : []);
const num = (v) => { const n = Number(v); return Number.isFinite(n) ? n : 0; };
const EMAIL = /^[^\s@,;<>"'()]+@[^\s@,;<>"'()]+\.[^\s@,;<>"'()]+$/;

export function viewLabel(view) {
  return VIEW_LABELS[view] || String(view || "");
}

/** Lower-case e-mail, or null when invalid (same rule as tsi-admin). */
export function cleanEmail(value) {
  const e = String(value ?? "").trim().toLowerCase();
  return e && e.length <= 254 && EMAIL.test(e) ? e : null;
}

/** "45 s", "12 min", "3 h 05", "2 d 4 h" (seconds). */
export function fmtDuration(seconds) {
  const s = Math.max(0, Math.round(num(seconds)));
  if (s < 60) return `${s} s`;
  const m = Math.round(s / 60);
  if (m < 60) return `${m} min`;
  const h = Math.floor(s / 3600);
  if (h < 48) return `${h} h ${String(Math.floor((s % 3600) / 60)).padStart(2, "0")}`;
  return `${Math.floor(h / 24)} d ${h % 24} h`;
}

/** Status of a listed user: { key, label } — not confirmed, pending first password change, active, or no account. */
export function userStatus(u) {
  if (!u?.account) return { key: "none", label: "No account" };
  if (!u.email_confirmed_at) return { key: "unconfirmed", label: "Not confirmed" };
  if (u.must_change_password) return { key: "pending", label: "Pending password change" };
  return { key: "active", label: "Active" };
}

/** Users sorted: administrators first, then by e-mail. */
export function sortUsers(users) {
  return [...arr(users)].sort((a, b) => (a.role === b.role ? String(a.email).localeCompare(String(b.email)) : a.role === "admin" ? -1 : 1));
}

/** True when changing `email` to `role` would leave no administrator. */
export function wouldRemoveLastAdmin(users, email, role) {
  const admins = arr(users).filter((u) => u.role === "admin");
  return role !== "admin" && admins.length <= 1 && admins.some((u) => u.email === email);
}

const pad = (n) => String(n).padStart(2, "0");
/** YYYY-MM-DD of a local date. */
export function isoDate(d) {
  return `${d.getFullYear()}-${pad(d.getMonth() + 1)}-${pad(d.getDate())}`;
}

/** Period { from, to (exclusive, local midnights), days, label } from the hash parameters (period=7|30|90|custom,
 * from/to = YYYY-MM-DD). Invalid or too long custom ranges fall back to 30 days. */
export function periodOf(p, today = new Date()) {
  const midnight = (d) => new Date(d.getFullYear(), d.getMonth(), d.getDate());
  const end = new Date(midnight(today).getTime());
  end.setDate(end.getDate() + 1);
  const key = String(p?.period || "30");
  const parse = (s) => {
    const m = /^(\d{4})-(\d{2})-(\d{2})$/.exec(String(s || ""));
    if (!m) return null;
    const d = new Date(Number(m[1]), Number(m[2]) - 1, Number(m[3]));
    return d.getMonth() === Number(m[2]) - 1 ? d : null;
  };
  if (key === "custom") {
    const f = parse(p?.from);
    const t = parse(p?.to);
    if (f && t && t >= f) {
      const to = new Date(t.getTime());
      to.setDate(to.getDate() + 1);
      const days = Math.round((to - f) / 86400000);
      if (days <= MAX_PERIOD_DAYS) return { key, from: f, to, days, label: `${isoDate(f)} to ${isoDate(t)}` };
    }
  }
  const n = ["7", "30", "90"].includes(key) ? Number(key) : 30;
  const from = new Date(end.getTime());
  from.setDate(from.getDate() - n);
  return { key: String(n), from, to: end, days: n, label: PERIODS[n] };
}

/** Normalises admin_usage() into the page model (missing keys give empty lists); per_day filled with empty days. */
export function usageModel(raw, period) {
  const d = raw && typeof raw === "object" ? raw : {};
  const t = d.totals && typeof d.totals === "object" ? d.totals : {};
  const totals = { users: num(t.users), sessions: num(t.sessions), views: num(t.views), total_s: num(t.total_s) };
  totals.avg_session_s = totals.sessions ? Math.round(totals.total_s / totals.sessions) : 0;
  const perUser = arr(d.per_user).map((u) => ({ email: String(u.email ?? ""), last_seen: u.last_seen || null, sessions: num(u.sessions),
    views: num(u.views), total_s: num(u.total_s), top_view: u.top_view || null }));
  const perView = arr(d.per_view).map((v) => ({ view: String(v.view ?? ""), views: num(v.views), total_s: num(v.total_s), users: num(v.users) }))
    .sort((a, b) => b.total_s - a.total_s || a.view.localeCompare(b.view));
  const byDay = new Map(arr(d.per_day).map((x) => [String(x.day).slice(0, 10), x]));
  const perDay = [];
  if (period?.from && period?.to) {
    for (let day = new Date(period.from.getTime()); day < period.to; day.setDate(day.getDate() + 1)) {
      const k = isoDate(day);
      const x = byDay.get(k) || {};
      perDay.push({ day: k, users: num(x.users), sessions: num(x.sessions), views: num(x.views), total_s: num(x.total_s) });
    }
  } else {
    for (const [k, x] of [...byDay].sort()) perDay.push({ day: k, users: num(x.users), sessions: num(x.sessions), views: num(x.views), total_s: num(x.total_s) });
  }
  return { totals, perUser, perView, perDay };
}

/** Visit log rows for CSV / Excel (section label added, durations in seconds). */
export function logExportRows(rows) {
  return arr(rows).map((r) => ({ email: r.email, view: r.view, section: viewLabel(r.view), detail: r.detail ?? "",
    started_at: r.started_at, duration_s: num(r.duration_s), session_id: r.session_id }));
}

/** visits-YYYY-MM-DD.<ext> (local date). */
export function logFileName(ext, date = new Date()) {
  return `tsi-visits-${isoDate(date)}.${ext}`;
}
