// The scope shared by the briefing, the Compare page, CSR progress and the briefing from the chat: several countries
// (or a ready-made group) x several themes (TSI policy areas) x a period. Pure module (no DOM), tested with Node.

export const EU_CODES = ["AT", "BE", "BG", "CY", "CZ", "DE", "DK", "EE", "ES", "FI", "FR", "GR", "HR", "HU", "IE", "IT",
  "LT", "LU", "LV", "MT", "NL", "PL", "PT", "RO", "SE", "SI", "SK"];
export const MAX_TOPICS = 4;

/** Ready-made groups (key -> { label, countries sorted }). Euro area: 21 members since Bulgaria joined on 1 Jan 2026;
 * cohesion countries: Cohesion Fund 2021-2027. */
export const COUNTRY_GROUPS = {
  eu27: { label: "EU-27", countries: EU_CODES },
  euro: { label: "Euro area", countries: ["AT", "BE", "BG", "CY", "DE", "EE", "ES", "FI", "FR", "GR", "HR", "IE", "IT", "LT", "LU", "LV", "MT", "NL", "PT", "SI", "SK"] },
  cohesion: { label: "Cohesion countries", countries: ["BG", "CY", "CZ", "EE", "GR", "HR", "HU", "LT", "LV", "MT", "PL", "PT", "RO", "SI", "SK"] },
  baltic: { label: "Baltic States", countries: ["EE", "LT", "LV"] },
  nordic: { label: "Nordic countries", countries: ["DK", "FI", "SE"] },
  benelux: { label: "Benelux", countries: ["BE", "LU", "NL"] },
  visegrad: { label: "Visegrád Four", countries: ["CZ", "HU", "PL", "SK"] },
  southern: { label: "Southern Europe", countries: ["CY", "ES", "GR", "IT", "MT", "PT"] },
};

/** Words that name a group in a question (accents removed, lower case). */
const GROUP_WORDS = {
  eu27: /\b(eu-?27|all (?:the )?member states|all (?:eu )?countries|tous les etats membres|tous les pays)\b/,
  euro: /\b(euro ?area|euro ?zone|zone euro)\b/,
  cohesion: /\bcohesion countries\b|\bpays de la cohesion\b/,
  baltic: /\b(baltic|baltes?|baltiques?|baltikum)\b/,
  nordic: /\b(nordic|nordiques?|scandinavi\w*)\b/,
  benelux: /\bbenelux\b/,
  visegrad: /\b(visegrad|v4)\b/,
  southern: /\b(southern europe|europe du sud|mediterranean countries|pays mediterraneens)\b/,
};

/** Other names of each country (accents removed, lower case): English demonym, French, German, Spanish, Italian. */
const ALIASES = {
  AT: ["austria", "austrian", "autriche", "osterreich", "oesterreich"],
  BE: ["belgium", "belgian", "belgique", "belgien", "belgica"],
  BG: ["bulgaria", "bulgarian", "bulgarie", "bulgarien"],
  CY: ["cyprus", "cypriot", "chypre", "zypern", "chipre", "cipro"],
  CZ: ["czechia", "czech republic", "czech", "tchequie", "republique tcheque", "tschechien", "chequia"],
  DE: ["germany", "german", "allemagne", "deutschland", "alemania", "germania"],
  DK: ["denmark", "danish", "danemark", "danemark", "dinamarca", "danimarca"],
  EE: ["estonia", "estonian", "estonie", "estland"],
  ES: ["spain", "spanish", "espagne", "spanien", "espana", "spagna"],
  FI: ["finland", "finnish", "finlande", "finnland", "finlandia"],
  FR: ["france", "french", "frankreich", "francia"],
  GR: ["greece", "greek", "grece", "griechenland", "grecia"],
  HR: ["croatia", "croatian", "croatie", "kroatien", "croacia", "croazia"],
  HU: ["hungary", "hungarian", "hongrie", "ungarn", "hungria", "ungheria"],
  IE: ["ireland", "irish", "irlande", "irland", "irlanda"],
  IT: ["italy", "italian", "italie", "italien", "italia"],
  LT: ["lithuania", "lithuanian", "lituanie", "litauen", "lituania"],
  LU: ["luxembourg", "luxembourgish", "luxemburg", "luxemburgo", "lussemburgo"],
  LV: ["latvia", "latvian", "lettonie", "lettland", "letonia", "lettonia"],
  MT: ["malta", "maltese", "malte"],
  NL: ["netherlands", "dutch", "pays-bas", "pays bas", "niederlande", "paises bajos", "paesi bassi"],
  PL: ["poland", "polish", "pologne", "polen", "polonia"],
  PT: ["portugal", "portuguese", "portogallo"],
  RO: ["romania", "romanian", "roumanie", "rumanien"],
  SE: ["sweden", "swedish", "suede", "schweden", "suecia", "svezia"],
  SI: ["slovenia", "slovenian", "slovene", "slovenie", "slowenien", "eslovenia"],
  SK: ["slovakia", "slovak", "slovaquie", "slowakei", "eslovaquia", "slovacchia"],
};

const plain = (s) => String(s ?? "").normalize("NFD").replace(/[̀-ͯ]/g, "").toLowerCase();
const escapeRe = (s) => s.replace(/[.*+?^${}()|[\]\\]/g, "\\$&");
const int = (v) => { const n = parseInt(v, 10); return Number.isInteger(n) ? n : null; };

/** Valid, sorted, unique countries; known themes in their order, at most MAX_TOPICS; years in order (null when absent). */
export function cleanScope(s = {}, { topics: validTopics = null } = {}) {
  const countries = [...new Set((Array.isArray(s.countries) ? s.countries : []).map((c) => String(c).trim().toUpperCase()))]
    .filter((c) => EU_CODES.includes(c)).sort();
  const topics = [...new Set((Array.isArray(s.topics) ? s.topics : []).map((t) => String(t).trim()))]
    .filter((t) => t && (!validTopics || validTopics.includes(t))).slice(0, MAX_TOPICS);
  let from = int(s.from);
  let to = int(s.to);
  if (from !== null && to !== null && from > to) [from, to] = [to, from];
  return { countries, topics, from, to };
}

/** Key of the group whose countries are exactly the selection, else null. */
export function groupOf(countries) {
  const key = [...new Set(countries || [])].sort().join(",");
  return Object.entries(COUNTRY_GROUPS).find(([, g]) => g.countries.join(",") === key)?.[0] || null;
}

/** "A", "A and B", "A, B and C". */
export function joinNames(list) {
  return list.length <= 1 ? (list[0] || "") : `${list.slice(0, -1).join(", ")} and ${list[list.length - 1]}`;
}

/** Countries part ("Baltic States", "Greece and Portugal", "5 countries"). */
export function countriesLabel(countries, names = {}) {
  const g = groupOf(countries);
  if (g && countries.length > 1) return COUNTRY_GROUPS[g].label;
  if (countries.length <= 3) return joinNames(countries.map((c) => names[c] || c));
  return `${countries.length} countries`;
}

/** "Baltic States · Digital transition and Green transition". */
export function scopeLabel(s, { names = {}, areas = {} } = {}) {
  const topics = joinNames((s.topics || []).map((t) => areas[t] || t));
  return [countriesLabel(s.countries || [], names), topics].filter(Boolean).join(" · ");
}

/** Scope of URL parameters: countries=EE,LV&topics=a,b&from=&to= (legacy country= and area= / topic= also read). */
export function parseScope(params, valid = {}) {
  const list = (v) => String(v ?? "").split(",").map((x) => x.trim()).filter(Boolean);
  const get = (k) => (typeof params.get === "function" ? params.get(k) : params[k]);
  const countries = get("countries") ? list(get("countries")) : list(get("country"));
  const topics = get("topics") ? list(get("topics")) : list(get("area") || get("topic"));
  return cleanScope({ countries, topics, from: get("from"), to: get("to") }, valid);
}

/** URL parameters of a scope (empty parts left out). */
export function scopeParams(s) {
  const out = {};
  if (s.countries?.length) out.countries = s.countries.join(",");
  if (s.topics?.length) out.topics = s.topics.join(",");
  if (s.from !== null && s.from !== undefined) out.from = String(s.from);
  if (s.to !== null && s.to !== undefined) out.to = String(s.to);
  return out;
}

/**
 * Scope named in a text (the questions put to the assistant). Deterministic, no AI: country names (several languages),
 * demonyms, codes inside CSR ids (csr-HR-2025-1, csrdoc-HR-2025), group names; themes by label or key words, in the order
 * of their first mention; years 2012-2030 as a period (min-max). Bare ISO codes are not read ("de", "it" are words).
 * opts: { names: code -> English name, areas: slug -> label, keywords: slug -> RegExp }
 */
export function scopeFromText(text, { names = {}, areas = {}, keywords = {} } = {}) {
  const raw = String(text ?? "");
  const t = plain(raw);
  const countries = new Set();
  for (const code of EU_CODES) {
    const words = [...new Set([plain(names[code] || ""), ...(ALIASES[code] || [])].filter(Boolean))];
    const re = new RegExp(`(?:^|[^a-z])(?:${words.map(escapeRe).join("|")})(?![a-z])`);
    if (re.test(t)) countries.add(code);
  }
  for (const m of raw.matchAll(/\bcsr(?:doc)?-([A-Z]{2})-\d{4}/gi)) {
    const c = m[1].toUpperCase();
    if (EU_CODES.includes(c)) countries.add(c);
  }
  for (const [key, re] of Object.entries(GROUP_WORDS)) if (re.test(t)) COUNTRY_GROUPS[key].countries.forEach((c) => countries.add(c));

  const found = [];
  for (const [slug, label] of Object.entries(areas)) {
    const i = t.indexOf(plain(label));
    const k = keywords[slug] ? raw.search(keywords[slug]) : -1;
    const at = [i, k].filter((x) => x >= 0);
    if (at.length) found.push([slug, Math.min(...at)]);
  }
  const topics = found.sort((a, b) => a[1] - b[1]).map(([slug]) => slug);

  const years = [...raw.matchAll(/\b(20[1-3]\d)\b/g)].map((m) => Number(m[1])).filter((y) => y >= 2012 && y <= 2030);
  return cleanScope({ countries: [...countries], topics, from: years.length ? Math.min(...years) : null, to: years.length ? Math.max(...years) : null });
}

/** The page scope completed by what a conversation names: each named part replaces the page's. */
export function mergeScope(page, extra) {
  const p = cleanScope(page);
  const e = cleanScope(extra);
  const years = e.from !== null ? { from: e.from, to: e.to } : { from: p.from, to: p.to };
  return { countries: e.countries.length ? e.countries : p.countries, topics: e.topics.length ? e.topics : p.topics, ...years };
}
