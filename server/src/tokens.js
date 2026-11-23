// Tokens of the application service: HS256 JWTs for PostgREST, sealed cookies for the browser session, stable user ids.
import { createHash, createHmac, timingSafeEqual, randomBytes } from "node:crypto";

export const b64url = (buf) => Buffer.from(buf).toString("base64url");
const json64 = (obj) => b64url(JSON.stringify(obj));
const nowS = () => Math.floor(Date.now() / 1000);

function sameBytes(a, b) {
  const x = Buffer.from(a), y = Buffer.from(b);
  return x.length === y.length && timingSafeEqual(x, y);
}

/** HS256 JWT with the given claims (iat added; exp must be given). */
export function signJwt(claims, secret) {
  const head = json64({ alg: "HS256", typ: "JWT" });
  const body = json64({ iat: nowS(), ...claims });
  const sig = createHmac("sha256", secret).update(`${head}.${body}`).digest("base64url");
  return `${head}.${body}.${sig}`;
}

/** Claims of a valid, unexpired HS256 JWT signed with `secret`, else null. */
export function verifyJwt(token, secret, now = nowS()) {
  const parts = String(token || "").split(".");
  if (parts.length !== 3) return null;
  try {
    const head = JSON.parse(Buffer.from(parts[0], "base64url").toString("utf8"));
    if (head?.alg !== "HS256") return null;
    const sig = createHmac("sha256", secret).update(`${parts[0]}.${parts[1]}`).digest("base64url");
    if (!sameBytes(sig, parts[2])) return null;
    const claims = JSON.parse(Buffer.from(parts[1], "base64url").toString("utf8"));
    if (typeof claims?.exp !== "number" || claims.exp <= now) return null;
    return claims;
  } catch {
    return null;
  }
}

/** Cookie value carrying `data` (JSON), signed with `secret`; `exp` (seconds) is checked when opened. */
export function seal(data, secret) {
  const body = json64(data);
  return `${body}.${createHmac("sha256", secret).update(body).digest("base64url")}`;
}

export function unseal(value, secret, now = nowS()) {
  const [body, sig, extra] = String(value || "").split(".");
  if (!body || !sig || extra !== undefined) return null;
  if (!sameBytes(createHmac("sha256", secret).update(body).digest("base64url"), sig)) return null;
  try {
    const data = JSON.parse(Buffer.from(body, "base64url").toString("utf8"));
    if (typeof data?.exp !== "number" || data.exp <= now) return null;
    return data;
  } catch {
    return null;
  }
}

// Namespace of the explorer's user ids (fixed: changing it would change every user's id).
const NAMESPACE = Buffer.from("6f1c2b7e9a4d4c3b8e5f0a1b2c3d4e5f", "hex");

/** RFC 4122 version 5 uuid of `name` (stable user id from the identity provider's issuer and subject). */
export function uuidV5(name) {
  const h = createHash("sha1").update(NAMESPACE).update(String(name), "utf8").digest();
  h[6] = (h[6] & 0x0f) | 0x50;
  h[8] = (h[8] & 0x3f) | 0x80;
  const x = h.subarray(0, 16).toString("hex");
  return `${x.slice(0, 8)}-${x.slice(8, 12)}-${x.slice(12, 16)}-${x.slice(16, 20)}-${x.slice(20, 32)}`;
}

export const randomToken = (n = 32) => b64url(randomBytes(n));

/** Short-lived token for PostgREST: the signed-in user (role authenticated). */
export function userToken(user, secret, minutes) {
  const exp = nowS() + minutes * 60;
  return { token: signJwt({ sub: user.id, email: user.email, role: "authenticated", aud: "authenticated", exp }, secret), exp };
}

/** Token of the service itself (role service_role: bypasses row-level security), for the administration actions. */
export function serviceToken(secret) {
  return signJwt({ role: "service_role", aud: "authenticated", exp: nowS() + 120 }, secret);
}
