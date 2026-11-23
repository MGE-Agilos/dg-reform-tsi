// Sign-in: OpenID Connect with the organisation's identity provider (authorization code + PKCE), or the local test
// sign-in (AUTH_MODE=dev, localhost only). Either way the result is { email, sub } of the signed-in person.

/** Identity provider (OpenID Connect). `lib` is the openid-client module (injected in tests). */
export function oidcProvider(oidc, lib) {
  let configPromise = null;
  const config = () => {
    configPromise ??= lib.discovery(new URL(oidc.issuer), oidc.clientId, oidc.clientSecret || undefined)
      .catch((e) => { configPromise = null; throw e; });
    return configPromise;
  };
  return {
    /** URL of the provider's sign-in page and the values to keep until the callback (sealed cookie). */
    async start() {
      const cfg = await config();
      const verifier = lib.randomPKCECodeVerifier();
      const state = lib.randomState();
      const nonce = lib.randomNonce();
      const url = lib.buildAuthorizationUrl(cfg, {
        redirect_uri: oidc.redirectUri, scope: oidc.scopes, state, nonce,
        code_challenge: await lib.calculatePKCECodeChallenge(verifier), code_challenge_method: "S256",
      });
      return { url: url.href, pending: { verifier, state, nonce } };
    },
    /** Checks the provider's answer (state, PKCE, nonce, ID token) and returns the person; throws on any failure. */
    async finish(currentUrl, pending) {
      const cfg = await config();
      const tokens = await lib.authorizationCodeGrant(cfg, new URL(currentUrl), {
        pkceCodeVerifier: pending.verifier, expectedState: pending.state, expectedNonce: pending.nonce,
      });
      const claims = tokens.claims() || {};
      const email = String(claims[oidc.emailClaim] || "").trim().toLowerCase();
      if (!email || !email.includes("@")) throw new Error(`the identity provider gave no e-mail address (claim ${oidc.emailClaim})`);
      if (claims.email_verified === false) throw new Error("the identity provider says this e-mail address is not verified");
      return { email, sub: `${claims.iss}|${claims.sub}` };
    },
  };
}

export const DEV_ISSUER = "dev-local";

const esc = (s) => String(s).replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));

/** Sign-in page of the local test mode: any e-mail address, no password (refused outside localhost by the config). */
export function devLoginPage(next, message = "") {
  return `<!doctype html><html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>Test sign-in</title><style>body{font:16px system-ui,sans-serif;max-width:32rem;margin:4rem auto;padding:0 1rem}
input,button{font:inherit;padding:.5rem;margin:.25rem 0}input{width:100%;box-sizing:border-box}.warn{background:#fff4ce;padding:.75rem}</style>
</head><body><h1>Test sign-in</h1>
<p class="warn"><strong>Local test mode (AUTH_MODE=dev).</strong> Anyone can sign in with any address: never use it on a server.</p>
${message ? `<p role="alert">${esc(message)}</p>` : ""}
<form method="post" action="/api/auth/dev"><label for="email">E-mail address</label>
<input id="email" name="email" type="email" required autofocus><input type="hidden" name="next" value="${esc(next)}">
<button type="submit">Sign in</button></form></body></html>`;
}
