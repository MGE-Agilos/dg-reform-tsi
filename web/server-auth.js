// Sign-in through the application service of a self-hosted installation (config.js: AUTH_MODE = "server").
// Same methods and answers as the part of supabase-js `auth` used by the page (getSession, getUser, refreshSession,
// signOut, onAuthStateChange), backed by /api/auth/*: the session is an HttpOnly cookie, the page only receives a
// short-lived token for the data requests, renewed before it expires.

const RENEW_BEFORE_S = 60;

/** `base`: address of the service's API (e.g. https://tsi.example.eu/api). */
export function createServerAuth(base, { fetchImpl = (...a) => fetch(...a), now = () => Date.now() / 1000, timers = globalThis } = {}) {
  let session = null;
  let loading = null;
  let timer = null;
  const listeners = new Set();

  const emit = (event) => { for (const cb of listeners) { try { cb(event, session); } catch { /* listener error */ } } };

  function schedule() {
    if (timer) timers.clearTimeout(timer);
    timer = null;
    if (!session) return;
    const wait = Math.max(5, session.expires_at - RENEW_BEFORE_S - now());
    timer = timers.setTimeout(async () => {
      const before = session?.user?.id ?? null;
      await load();
      emit(session ? (session.user.id === before ? "TOKEN_REFRESHED" : "SIGNED_IN") : "SIGNED_OUT");
    }, wait * 1000);
  }

  /** Reads the session from the service: { session, error } (error only when the service cannot be reached). */
  function load() {
    loading ??= (async () => {
      try {
        const res = await fetchImpl(`${base}/auth/session`, { credentials: "same-origin", cache: "no-store" });
        if (res.status === 401) { session = null; return { session: null, error: null }; }
        if (!res.ok) return { session, error: { message: `The sign-in service answered ${res.status}` } };
        session = await res.json();
        return { session, error: null };
      } catch {
        return { session, error: { message: "The sign-in service cannot be reached" } };
      } finally {
        loading = null;
        schedule();
      }
    })();
    return loading;
  }

  const fresh = () => session && session.expires_at - RENEW_BEFORE_S > now();

  return {
    async getSession() {
      const out = fresh() ? { session, error: null } : await load();
      return { data: { session: out.session }, error: out.error };
    },
    /** Asks the service (detects a session ended elsewhere), like supabase-js getUser(). */
    async getUser() {
      const out = await load();
      if (out.error) return { data: { user: null }, error: out.error };
      return out.session ? { data: { user: out.session.user }, error: null }
        : { data: { user: null }, error: { name: "AuthSessionMissingError", message: "Not signed in" } };
    },
    async refreshSession() {
      const out = await load();
      return { data: { session: out.session }, error: out.error };
    },
    async signOut() {
      try { await fetchImpl(`${base}/auth/logout`, { method: "POST", credentials: "same-origin" }); } catch { /* still signed out here */ }
      session = null;
      schedule();
      emit("SIGNED_OUT");
      return { error: null };
    },
    onAuthStateChange(cb) {
      listeners.add(cb);
      Promise.resolve().then(() => cb("INITIAL_SESSION", session));
      return { data: { subscription: { unsubscribe: () => listeners.delete(cb) } } };
    },
    /** Address of the sign-in page, coming back to `next` (a route of the page, e.g. "#projects"). */
    loginUrl(next) {
      return `${base}/auth/login?next=${encodeURIComponent(next || "#dashboard")}`;
    },
    /** Current token, synchronously (for keepalive requests); null when signed out. */
    token: () => (fresh() ? session.access_token : null),
  };
}
