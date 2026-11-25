// Passwords of the users (AUTH_MODE=password): scrypt hashes, temporary passwords, rules for a new password.
import { randomBytes, randomInt, scrypt, timingSafeEqual } from "node:crypto";

const N = 16384, R = 8, P = 1, KEYLEN = 32; // scrypt cost: about 50 ms and 16 MB per hash
export const MIN_LENGTH = 12;
export const MAX_LENGTH = 200;
export const MAX_FAILURES = 5;         // failed sign-ins in a row before the account is locked
export const LOCK_MINUTES = 15;

const derive = (password, salt, n, r, p) => new Promise((resolve, reject) => {
  scrypt(String(password).normalize("NFC"), salt, KEYLEN, { N: n, r, p, maxmem: 64 * 1024 * 1024 },
    (err, key) => (err ? reject(err) : resolve(key)));
});

/** "scrypt$N$r$p$salt$hash" (base64url). */
export async function hashPassword(password) {
  const salt = randomBytes(16);
  const key = await derive(password, salt, N, R, P);
  return `scrypt$${N}$${R}$${P}$${salt.toString("base64url")}$${key.toString("base64url")}`;
}

export async function verifyPassword(password, stored) {
  const parts = String(stored || "").split("$");
  if (parts.length !== 6 || parts[0] !== "scrypt") return false;
  const [, n, r, p, salt, hash] = parts;
  const expected = Buffer.from(hash, "base64url");
  const key = await derive(password, Buffer.from(salt, "base64url"), Number(n), Number(r), Number(p));
  return key.length === expected.length && timingSafeEqual(key, expected);
}

/** A hash of nothing, to spend the same time when the e-mail address is unknown (no account enumeration). */
export const DUMMY_HASH = await hashPassword(randomBytes(16).toString("hex"));

// No 0/O, 1/l/I: read aloud or copied by hand without mistakes.
const ALPHABET = "abcdefghijkmnpqrstuvwxyzABCDEFGHJKLMNPQRSTUVWXYZ23456789";

/** Temporary password given by an administrator: 4 groups of 4 characters (about 93 bits). */
export function tempPassword() {
  const group = () => Array.from({ length: 4 }, () => ALPHABET[randomInt(ALPHABET.length)]).join("");
  return [group(), group(), group(), group()].join("-");
}

/** Error message for a new password chosen by the user, or null when it is acceptable. */
export function passwordProblem(password) {
  if (typeof password !== "string" || password.length < MIN_LENGTH) return `The password must have at least ${MIN_LENGTH} characters.`;
  if (password.length > MAX_LENGTH) return `The password must have at most ${MAX_LENGTH} characters.`;
  if (new Set(password).size < 5) return "The password is too simple.";
  return null;
}
