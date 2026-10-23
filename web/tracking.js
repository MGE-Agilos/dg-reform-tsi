// Usage monitoring (Admin > Usage): one row per finished view in public.page_views (migration 007).
// Pure logic (no DOM, no network: the caller passes the clock and the sender), so Node can test it.
// A view is a section of the app (hash route) plus its route parameters (an id, a sub-view, a country), never a text
// typed by the user (search words, chat questions). Only visible, active time counts: the clock stops while the page is
// hidden, and 5 minutes after the last user input (it restarts at the next input). Views shorter than 2 s are dropped.

export const IDLE_MS = 5 * 60 * 1000;
export const MIN_SECONDS = 2;
export const MAX_SECONDS = 86400;
export const VIEW_MAX = 40;
export const DETAIL_MAX = 200;
// Route parameters kept in the detail (filters and sub-views); anything else (q: free-text search, page) is dropped.
export const DETAIL_PARAMS = ["view", "country", "cc", "year", "from", "to", "level", "programme", "area", "severity", "type",
  "status", "linked", "detail", "unlinked", "sel"];

const clip = (s, max) => (s.length > max ? s.slice(0, max) : s);

/** { view, detail } of a location hash: view = first path segment (default projects), detail = the rest of the path
 * (an id) or else the kept parameters "k=v&k=v" (null when none). */
export function routeView(hash) {
  const raw = String(hash || "").replace(/^#/, "");
  const [path, query = ""] = raw.split("?");
  const [first, ...rest] = path.split("/");
  let view = (first || "projects").toLowerCase();
  if (!/^[a-z][a-z0-9_-]*$/.test(view)) view = "other";
  view = clip(view, VIEW_MAX);
  let id = rest.join("/");
  try { id = decodeURIComponent(id); } catch { /* keep raw */ }
  let detail = id;
  if (!detail) {
    const p = new URLSearchParams(query);
    detail = DETAIL_PARAMS.filter((k) => p.get(k)).map((k) => `${k}=${p.get(k)}`).join("&");
  }
  detail = clip(detail.replace(/[\u0000-\u001f]/g, " ").trim(), DETAIL_MAX);
  return { view, detail: detail || null };
}

/** Random session id (hex), kept per browser tab by the caller (sessionStorage). */
export function newSessionId(random = (n) => crypto.getRandomValues(new Uint8Array(n))) {
  return [...random(12)].map((b) => b.toString(16).padStart(2, "0")).join("");
}

/**
 * Tracker of one stream of views (the page, or the chat drawer). send(row, { keepalive }) receives
 * { session_id, view, detail, started_at, duration_s }.
 * start(view, detail): ends the current view (same view and detail: nothing happens) and starts this one.
 * end({ keepalive }): ends the current view (nothing current afterwards).
 * hide(): page hidden or left: ends the current view with keepalive and remembers it; show(): starts it again.
 * activity(): user input (restarts the clock after an idle pause).
 */
export function createTracker({ send, sessionId, now = Date.now }) {
  let cur = null; // { view, detail, startedAt, acc, segStart, lastInput }
  let hidden = null; // { view, detail } ended by hide()

  const counted = (c, t) => {
    if (c.segStart === null) return c.acc;
    const end = Math.min(t, c.lastInput + IDLE_MS);
    return c.acc + Math.max(0, end - c.segStart);
  };

  function end({ keepalive = false } = {}) {
    if (!cur) return null;
    const c = cur;
    cur = null;
    const seconds = Math.min(MAX_SECONDS, Math.round(counted(c, now()) / 1000));
    if (seconds < MIN_SECONDS) return null;
    const row = { session_id: sessionId(), view: c.view, detail: c.detail, started_at: new Date(c.startedAt).toISOString(), duration_s: seconds };
    try { send(row, { keepalive }); } catch { /* monitoring never breaks the app */ }
    return row;
  }

  function start(view, detail = null) {
    hidden = null;
    if (cur && cur.view === view && cur.detail === detail) return;
    end();
    const t = now();
    cur = { view, detail, startedAt: t, acc: 0, segStart: t, lastInput: t };
  }

  function activity() {
    if (!cur) return;
    const t = now();
    if (cur.segStart !== null && t > cur.lastInput + IDLE_MS) {
      cur.acc = counted(cur, t); // close the segment at the idle limit, open a new one now
      cur.segStart = t;
    }
    cur.lastInput = t;
  }

  function hide() {
    if (!cur) return;
    const { view, detail } = cur;
    end({ keepalive: true });
    hidden = { view, detail };
  }

  function show() {
    if (!hidden || cur) return;
    const h = hidden;
    start(h.view, h.detail);
  }

  return { start, end, hide, show, activity, current: () => (cur ? { view: cur.view, detail: cur.detail } : null) };
}
