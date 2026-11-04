// Briefing for an occasion: pure model of the data of one country and period (optionally one policy area), the occasion
// helpers and the compact "facts" digest sent to the AI (tsi-assistant, service "briefing"). No DOM, no network: app.js fetches the
// rows, word-export.js builds the document, so Node can test this module.
// Several countries and themes: one model per country (briefingModel with `topics`), crossed by compareModel.

import { progressRank, PROGRESS_FROM } from "./progress.js";
import { groupOf, countriesLabel, joinNames } from "./scope.js";

export const COMMISSION_STAGE = "Commission recommendation";
export const FACTS_MAX = 25000; // characters of JSON.stringify(facts), checked again by the edge function (413)
const FACTS_TARGET = 24000;

/** Project policy area (slug) -> ECFIN CSR policy area labels (csrs.policy_areas, csr_parts.policy_areas).
 * The two classifications differ: projects use the 10 TSI policy areas, CSRs the ~60 ECFIN database labels.
 * A label may sit under two areas (pensions: labour market and public finances). Read off the real values on
 * 22 Oct 2026; a label that is in no list is never matched by the topic filter (see unmappedLabels). */
export const TOPIC_CSR_AREAS = {
  "competitiveness": ["Competition in services", "Single market, competition & state aid", "Research & innovation",
    "Business environment (incl. SME policies)", "Business environment", "Competition & regulatory framework",
    "Telecom, postal services & local public services", "Digitalisation of businesses"],
  "digital-transition": ["Digitalisation of businesses", "Digital connectivity, infrastructure & market functioning",
    "Digitalisation of public administration and public services"],
  "financial-sector-and-access-finance": ["Access to finance", "Access to finance & growth financing (incl. CMU)",
    "Financial services", "Financial services and financial stability", "Private sector debt & insolvency framework",
    "Insolvency framework", "Private indebtedness", "Anti-money laundering", "Housing market"],
  "green-transition": ["Renewable energy, energy infrastructure & networks", "Energy efficiency", "Climate adaptation",
    "Energy, resources & climate change", "Environmental policy & resource management", "Transport"],
  "health-and-long-term-care": ["Health & long-term care", "Healthcare", "Long-term care"],
  "labour-market-and-social-protection": ["Incentives to work, job creation, labour market participation",
    "Active labour market policies, incentives to work & labour market participation", "Active labour market policies",
    "Functioning of the labour market, including framework for labour contracts & undeclared work", "Wages & wage setting",
    "Employment protection legislation & framework for labour contracts", "Unemployment benefits",
    "Poverty reduction & social inclusion", "Poverty, social inclusion & social protection", "Pension systems and active ageing",
    "Long-term sustainability of public finances, inc. pensions", "Non-discrimination and equal opportunities", "Childcare",
    "Housing"],
  "migration-management": [],
  "public-administration-and-governance": ["Public administration", "Digitalisation of public administration and public services",
    "Justice system", "Civil justice", "Quality of law making", "Corruption", "Shadow economy & corruption",
    "Public procurement & concessions", "State-owned enterprises", "Regional development & local public services"],
  "revenue-administration-and-public-financial-management": ["Fiscal policy & fiscal governance",
    "Budgetary framework and fiscal governance", "Long-term sustainability of public finances, inc. pensions", "Taxation policy",
    "Reduce the tax burden on labour", "Broaden tax bases", "Tax administration, tax evasion & tax avoidance",
    "Fight against tax evasion, improve tax administration & tackle tax avoidance", "Reduce the debt bias"],
  "skills-education-and-training": ["Education", "Early childhood education & care",
    "Skills, Vocational Education and Training & adult learning", "Skills & life-long learning"],
  "recovery-and-resilience-plans": [],
};

/** Words looked for in the text of recommendations without any policy area (the 2026 Commission recommendations are
 * not classified yet): a weaker match, flagged as such in the document. */
export const TOPIC_KEYWORDS = {
  "competitiveness": /\b(competition|competitiveness|business environment|SMEs?|innovation|research|productivity|single market)\b/i,
  "digital-transition": /\b(digital\w*|e-government|broadband|connectivity)\b/i,
  "financial-sector-and-access-finance": /\b(access to finance|financial (?:sector|services|markets?|stability)|capital markets?|banking|insolvency|money laundering)\b/i,
  "green-transition": /\b(renewable\w*|energy|climate|decarboni\w+|emissions?|green transition|fossil fuels?|transport|environment\w*)\b/i,
  "health-and-long-term-care": /\b(health\w*|long-term care|primary care|hospital\w*)\b/i,
  "labour-market-and-social-protection": /\b(labour market|employment|unemploy\w+|social protection|social inclusion|poverty|pensions?|social services|social housing)\b/i,
  "migration-management": /\b(migra\w+|asylum)\b/i,
  "public-administration-and-governance": /\b(public administration|governance|justice|judicial|corruption|public procurement|state-owned)\b/i,
  "revenue-administration-and-public-financial-management": /\b(fiscal|tax\w*|budget\w*|expenditure|deficit|public finances)\b/i,
  "skills-education-and-training": /\b(education\w*|skills?|training|teach\w+|schools?|vocational)\b/i,
  "recovery-and-resilience-plans": /\b(recovery and resilience)\b/i,
};

const arr = (v) => (Array.isArray(v) ? v : []);
const num = (v) => { const n = Number(v); return Number.isFinite(n) ? n : 0; };
const https = (u) => (typeof u === "string" && u.startsWith("https://") ? u : null);

export function clip(text, max) {
  const t = String(text ?? "").replace(/\s+/g, " ").trim();
  return t.length > max ? t.slice(0, max - 1).replace(/\s+\S*$/, "") + "…" : t;
}

/** First sentence of a text (a period, question or exclamation mark followed by a capital, with or without the space
 * that the source sometimes drops: "growth.Address"), clipped. */
export function firstSentence(text, max = 220) {
  const t = String(text ?? "").replace(/\s+/g, " ").trim();
  const ends = [/^(.{20,}?[.!?])(?=\s+[A-Z(“"])/.exec(t), /^(.{20,}?[a-z)][.!?])(?=[A-Z][a-z])/.exec(t)].filter(Boolean).map((m) => m[1]);
  return clip(ends.length ? ends.sort((a, b) => a.length - b.length)[0] : t, max);
}

/** Labels of the CSR policy areas (all records) that the mapping never uses: they cannot match a topic. */
export function unmappedLabels(labels) {
  const known = new Set(Object.values(TOPIC_CSR_AREAS).flat());
  return [...new Set(arr(labels))].filter((l) => !known.has(l)).sort();
}

export function isCommission(stage) {
  return stage === COMMISSION_STAGE;
}

/** Same reading as the explorer: "achieved", "expected" or not stated. */
export function resultsKey(value) {
  const k = String(value ?? "").trim().toLowerCase();
  return k === "achieved" || k === "expected" ? k : "none";
}

/** Assessment as written in the ECFIN database ("Limited Progress") -> "Limited progress"; null if none. */
export function assessmentLabel(value) {
  const k = String(value ?? "").trim().toLowerCase();
  if (!k) return null;
  return k[0].toUpperCase() + k.slice(1);
}

export function weakAssessment(value) {
  const k = String(value ?? "").trim().toLowerCase();
  return k === "limited progress" || k === "no progress";
}

/** Formats of an occasion (value -> label): the same list as the edge function's OCCASION_FORMATS. */
export const OCCASION_FORMATS = { bilateral: "Bilateral meeting", panel: "Panel discussion", speech: "Speech",
  mission: "Country mission", conference: "Conference", other: "Other" };
export const OCCASION_MAX = { recipient: 120, title: 200, place: 120, context: 8000, background: 2000 };

/** YYYY-MM-DD, a real day, year 2010-2100 (same rule as the edge function). */
function validDay(s) {
  if (!/^\d{4}-\d{2}-\d{2}$/.test(s)) return false;
  const d = new Date(`${s}T00:00:00Z`);
  const y = Number(s.slice(0, 4));
  return !Number.isNaN(d.getTime()) && d.toISOString().slice(0, 10) === s && y >= 2010 && y <= 2100;
}

const isoDay = (d) => `${d.getFullYear()}-${String(d.getMonth() + 1).padStart(2, "0")}-${String(d.getDate()).padStart(2, "0")}`;

/** Form values -> the occasion sent to the AI and used by the document. */
export function cleanOccasion(raw = {}) {
  const line = (v) => String(v ?? "").replace(/\s+/g, " ").trim();
  const block = (v) => String(v ?? "").replace(/\r\n?/g, "\n").trim();
  const format = line(raw.format);
  return { recipient: line(raw.recipient), title: line(raw.title), date: line(raw.date), place: line(raw.place),
    format: Object.hasOwn(OCCASION_FORMATS, format) ? format : "other", context: block(raw.context), background: block(raw.background) };
}

/** Field -> message for an invalid occasion (the edge function checks the same rules); {} when valid. */
export function occasionErrors(o) {
  const e = {};
  const max = (k) => `At most ${OCCASION_MAX[k]} characters (${o[k].length} now).`;
  if (!o.recipient) e.recipient = "Enter who the briefing is for.";
  else if (o.recipient.length > OCCASION_MAX.recipient) e.recipient = max("recipient");
  if (!o.title) e.title = "Describe the occasion in one line.";
  else if (o.title.length > OCCASION_MAX.title) e.title = max("title");
  if (o.place.length > OCCASION_MAX.place) e.place = max("place");
  if (o.date && !validDay(o.date)) e.date = "Enter a valid date (2010 to 2100).";
  if (o.context.length > OCCASION_MAX.context) e.context = max("context");
  if (o.background.length > OCCASION_MAX.background) e.background = max("background");
  return e;
}

function fileName(subject, occasion = {}, today = new Date()) {
  const day = validDay(String(occasion?.date ?? "")) ? occasion.date : isoDay(today);
  const slug = String(occasion?.title ?? "").normalize("NFD").replace(/[\u0300-\u036f]/g, "").toLowerCase()
    .replace(/[^a-z0-9]+/g, "-").replace(/^-+/, "").slice(0, 60).replace(/-+$/, "");
  return `briefing-${subject}-${day}${slug ? `-${slug}` : ""}.docx`;
}

/** briefing-<ISO>-<occasion day or today>[-<occasion slug>].docx */
export function briefingFileName(country, occasion = {}, today = new Date()) {
  return fileName(String(country).replace(/[^A-Z]/gi, "").toUpperCase(), occasion, today);
}

/** briefing-<group key, or up to 5 ISO codes and "n-more">-<day>[-<slug>].docx */
export function compareFileName(countries, occasion = {}, today = new Date()) {
  const codes = [...new Set(countries)].map((c) => String(c).replace(/[^A-Z]/gi, "").toUpperCase()).filter(Boolean).sort();
  const g = groupOf(codes);
  const subject = g && codes.length > 1 ? g : `${codes.slice(0, 5).join("-")}${codes.length > 5 ? `-${codes.length - 5}-more` : ""}`;
  return fileName(subject, occasion, today);
}

/** Latest scored analysis per project (the latest one when none is scored). Rows must be sorted newest first. */
export function latestImpacts(rows) {
  const out = new Map();
  for (const r of arr(rows)) {
    if (!r || !r.project_id) continue;
    const had = out.get(r.project_id);
    if (!had || (!Number.isFinite(had.score) && Number.isFinite(r.score))) out.set(r.project_id, r);
  }
  return out;
}

/**
 * The briefing model.
 * raw: { projects (country, years in period), linked (projects linked to the country's CSRs, any year), docs
 * (csr_documents of the country), csrs (with csr_parts(part_id, policy_areas, project_csr_links(project_id, level, basis))),
 * impacts (impact_reports rows, newest first), anomalies (rows of the country), related (optional: csr_related_projects rows of the
 * country's recommendations with projects(title, programme, year, countries), sorted same country and high relevance first) }
 * opts: { country, countryName, from, to, topics (slugs; [] = all) or topic (one slug or ""), topicLabel, areaLabel(slug), date }
 * A project matches when its policy area is one of the topics; a recommendation when it matches one of them (rec.topics).
 */
export function briefingModel(raw, opts) {
  const { country, from, to } = opts;
  const topics = [...new Set(Array.isArray(opts.topics) && opts.topics.length ? opts.topics : (opts.topic ? [opts.topic] : []))];
  const topic = topics.length === 1 ? topics[0] : ""; // one theme: the single-country document names it
  const areaLabel = opts.areaLabel || ((s) => s || "");
  const inPeriod = (y) => num(y) >= from && num(y) <= to;
  const byId = new Map();
  for (const p of [...arr(raw.linked), ...arr(raw.projects)]) if (p?.project_id) byId.set(p.project_id, p);
  const countryProjects = arr(raw.projects).filter((p) => p && arr(p.countries).includes(country) && inPeriod(p.year));
  const projects = countryProjects.filter((p) => !topics.length || topics.includes(p.policy_area))
    .sort((a, b) => num(b.year) - num(a.year) || String(a.title).localeCompare(String(b.title)));
  const topicProjectIds = new Set(projects.map((p) => p.project_id));
  const impacts = latestImpacts(raw.impacts);

  // Documents of the period (csr_documents, or rebuilt from the recommendations).
  const docsById = new Map(arr(raw.docs).filter((d) => d && inPeriod(d.year)).map((d) => [d.doc_id, d]));
  const labelsOf = Object.fromEntries(topics.map((t) => [t, new Set(TOPIC_CSR_AREAS[t] || [])]));
  const labels = new Set(topics.flatMap((t) => TOPIC_CSR_AREAS[t] || []));
  const recs = [];
  for (const c of arr(raw.csrs)) {
    if (!c || !c.csr_id || !inPeriod(c.year)) continue;
    const docId = c.doc_id || `csrdoc-${country}-${c.year}`;
    if (!docsById.has(docId)) {
      docsById.set(docId, { doc_id: docId, country, year: num(c.year), title: `${opts.countryName || country} — ${c.year} country-specific recommendations`,
        stage: num(c.year) >= 2026 && !c.assessment ? COMMISSION_STAGE : "Council recommendation", document_url: https(c.source_url), derived: true });
    }
    const parts = arr(c.csr_parts);
    const areas = [...new Set([...arr(c.policy_areas), ...parts.flatMap((p) => arr(p?.policy_areas))])];
    const links = parts.flatMap((p) => arr(p?.project_csr_links).map((l) => ({ ...l, part_id: p.part_id })));
    const pairs = new Map(); // project id -> { level, parts }
    for (const l of links) {
      if (!l.project_id) continue;
      const had = pairs.get(l.project_id) || { level: "partial", parts: [] };
      if (l.level === "direct") had.level = "direct";
      had.parts.push(l.part_id);
      pairs.set(l.project_id, had);
    }
    // Per theme: its ECFIN labels, else a supporting project of that area, else (not classified) key words of the text.
    const kinds = topics.map((t) => {
      if (areas.some((a) => labelsOf[t].has(a))) return "areas";
      if ([...pairs.keys()].some((pid) => byId.get(pid)?.policy_area === t)) return "projects";
      if (!areas.length && TOPIC_KEYWORDS[t]?.test(String(c.text ?? ""))) return "text";
      return null;
    });
    const recTopics = topics.filter((_, i) => kinds[i]);
    const match = !topics.length ? "all" : (["areas", "projects", "text"].find((k) => kinds.includes(k)) || null);
    recs.push({
      id: c.csr_id, year: num(c.year), number: c.number ?? "", docId, text: String(c.text ?? ""), summary: firstSentence(c.text),
      areas, assessment: assessmentLabel(c.assessment), assessmentYear: c.assessment_year ?? null, match, topics: recTopics,
      subLinks: links.length,
      supporters: [...pairs.entries()].map(([pid, v]) => ({ id: pid, level: v.level, parts: [...new Set(v.parts)].sort() })),
    });
  }
  const numOrder = (a, b) => (parseFloat(a.number) - parseFloat(b.number)) || String(a.number).localeCompare(String(b.number));
  recs.sort((a, b) => b.year - a.year || numOrder(a, b));
  const shownRecs = recs.filter((r) => r.match);
  const docs = [...docsById.values()].sort((a, b) => num(b.year) - num(a.year)).map((d) => {
    const own = shownRecs.filter((r) => r.docId === d.doc_id);
    return { id: d.doc_id, year: num(d.year), title: d.title, stage: d.stage || "Council recommendation", commission: isCommission(d.stage),
      url: https(d.document_url), recs: own, total: recs.filter((r) => r.docId === d.doc_id).length };
  });
  const latest = docs.find((d) => d.recs.length) || null;
  const commissionDoc = new Set(docs.filter((d) => d.commission).map((d) => d.id));

  const projectInfo = (pid) => {
    const p = byId.get(pid) || { project_id: pid, title: pid };
    const imp = impacts.get(pid);
    return {
      id: pid, title: p.title || pid, programme: p.programme || "", year: p.year ?? "", policyArea: p.policy_area || "",
      results: resultsKey(p.results), deliverables: num(p.deliverables), url: https(p.url), detail: p.detail_page !== false,
      impact: imp ? { score: Number.isFinite(imp.score) ? imp.score : null, level: imp.level || "", confidence: imp.confidence || "",
        date: imp.created_at || "", verdict: firstSentence(imp.verdict || "", 260) } : null,
    };
  };

  // Lineage: recommendations of the period (topic filter) with their supporting projects.
  const lineage = shownRecs.filter((r) => r.supporters.length).map((r) => ({
    rec: r, projects: r.supporters.map((s) => ({ ...projectInfo(s.id), level: s.level, parts: s.parts }))
      .sort((a, b) => (a.level === b.level ? num(b.year) - num(a.year) : a.level === "direct" ? -1 : 1)),
  }));
  const pairs = lineage.flatMap((l) => l.projects);
  const assessedRecs = shownRecs.filter((r) => !commissionDoc.has(r.docId));
  const supported = assessedRecs.filter((r) => r.supporters.length);

  // Projects: the country's projects of the period (topic filter).
  const infos = projects.map((p) => projectInfo(p.project_id));
  const programmes = ["TSI", "SRSP", "SRSS"];
  const years = [...new Set(infos.map((p) => num(p.year)).filter(Boolean))].sort((a, b) => a - b);
  const byYear = years.map((y) => ({ year: y, ...Object.fromEntries(programmes.map((g) => [g, infos.filter((p) => num(p.year) === y && p.programme === g).length])),
    other: infos.filter((p) => num(p.year) === y && !programmes.includes(p.programme)).length, total: infos.filter((p) => num(p.year) === y).length }));
  const count = (list, key) => {
    const m = new Map();
    for (const x of list) m.set(key(x), (m.get(key(x)) || 0) + 1);
    return [...m.entries()].sort((a, b) => b[1] - a[1] || String(a[0]).localeCompare(String(b[0])));
  };
  // Impact analyses: the latest of each project of the period or of the lineage.
  const analysed = new Map();
  for (const p of [...infos, ...pairs]) if (p.impact && !analysed.has(p.id)) analysed.set(p.id, p);
  const impactList = [...analysed.values()].sort((a, b) => String(b.impact.date).localeCompare(String(a.impact.date)));
  const scored = impactList.filter((p) => Number.isFinite(p.impact.score));

  const anomalies = arr(raw.anomalies).filter((a) => a && String(a.countries ?? "").split(/\s*,\s*/).includes(country)
    && (a.year === null || a.year === undefined || inPeriod(a.year)) && (!topics.length || topicProjectIds.has(a.project_id)));
  const sevRank = { high: 0, medium: 1, low: 2, info: 3 };
  anomalies.sort((a, b) => (sevRank[a.severity] ?? 9) - (sevRank[b.severity] ?? 9) || num(b.year) - num(a.year) || String(a.title).localeCompare(String(b.title)));

  // Latest (2026) recommendations: past projects suggested by AI (csr_related_projects), top 3 each.
  const relBy = new Map();
  for (const x of arr(raw.related)) {
    if (!x?.csr_id || !x.project_id) continue;
    if (!relBy.has(x.csr_id)) relBy.set(x.csr_id, []);
    relBy.get(x.csr_id).push(x);
  }
  const related = latest && (latest.commission || latest.year >= 2026) ? latest.recs.map((r) => {
    const list = relBy.get(r.id) || [];
    return { rec: r, total: list.length, sameCountry: list.filter((x) => x.scope === "same_country").length,
      projects: list.slice(0, 3).map((x) => ({ id: x.project_id, title: x.projects?.title || x.project_id, year: x.projects?.year ?? "",
        programme: x.projects?.programme || "", scope: x.scope === "same_country" ? "same_country" : "other_country",
        relevance: x.relevance === "high" ? "high" : "medium", rationale: clip(x.rationale, 240) })) };
  }).filter((x) => x.projects.length) : [];

  const gaps = assessedRecs.filter((r) => !r.supporters.length)
    .sort((a, b) => Number(weakAssessment(b.assessment)) - Number(weakAssessment(a.assessment)) || b.year - a.year || numOrder(a, b));

  // Sources: the country PDFs (2026), the ECFIN database once, the project pages, and once each listing page from which
  // projects without a detail page are known (TSI country factsheet, SRSP PDF list).
  const sources = new Map();
  for (const d of docs) {
    if (!d.url || !d.recs.length || sources.has(d.url)) continue;
    sources.set(d.url, /\.pdf(?:$|[?#])/i.test(d.url) ? `CSR document: ${d.title}` : "CSR database: ECFIN country-specific recommendations database (texts, sub-parts, assessments)");
  }
  for (const p of [...infos, ...pairs]) {
    if (!p.url || sources.has(p.url)) continue;
    sources.set(p.url, p.detail ? `Project: ${p.title}` : "Listing: page listing projects without a detail page");
  }

  return {
    country, countryName: opts.countryName || country, from, to, topic, topics,
    topicLabel: topics.length ? (opts.topicLabel || joinNames(topics.map(areaLabel))) : "",
    date: opts.date || new Date(),
    projects: infos, byYear, programmes,
    byArea: count(infos, (p) => p.policyArea || "none").map(([k, n]) => ({ key: k, label: k === "none" ? "Not classified" : areaLabel(k), n })),
    results: { achieved: infos.filter((p) => p.results === "achieved").length, expected: infos.filter((p) => p.results === "expected").length,
      none: infos.filter((p) => p.results === "none").length },
    deliverables: infos.reduce((s, p) => s + p.deliverables, 0),
    links: { pairs: pairs.length, direct: pairs.filter((p) => p.level === "direct").length, subLinks: lineage.reduce((s, l) => s + l.rec.subLinks, 0),
      projects: new Set(pairs.map((p) => p.id)).size },
    coverage: { recs: assessedRecs.length, supported: supported.length },
    docs, latest, recs: shownRecs, allRecs: recs.length, lineage,
    impacts: impactList, impactMean: scored.length ? scored.reduce((s, p) => s + p.impact.score, 0) / scored.length : null,
    anomalies, anomalyCounts: count(anomalies, (a) => a.severity || "none").map(([k, n]) => ({ key: k, n }))
      .sort((a, b) => (sevRank[a.key] ?? 9) - (sevRank[b.key] ?? 9)),
    gaps, related, sources: [...sources.entries()].map(([url, label]) => ({ url, label })),
    topicMatch: topics.length ? { areas: shownRecs.filter((r) => r.match === "areas").length, projects: shownRecs.filter((r) => r.match === "projects").length,
      text: shownRecs.filter((r) => r.match === "text").length, excluded: recs.length - shownRecs.length, labels: [...labels] } : null,
  };
}

const pct = (a, b) => (b > 0 ? Math.round((100 * a) / b) : null);

/** The background figures of the briefing, built from the model (not by the AI): label and text. */
export function keyFigures(m) {
  const progs = m.programmes.map((g) => [g, m.projects.filter((p) => p.programme === g).length]).filter(([, k]) => k);
  const other = m.projects.length - progs.reduce((a, [, k]) => a + k, 0);
  const parts = [...progs.map(([g, k]) => `${g} ${k}`), ...(other > 0 ? [`other ${other}`] : [])];
  const out = [{ label: "Projects in the period", value: `${m.projects.length}${parts.length ? ` (${parts.join(", ")})` : ""}` }];
  if (m.coverage.recs) {
    out.push({ label: "Assessed recommendations supported by a project", value: `${m.coverage.supported} of ${m.coverage.recs} (${pct(m.coverage.supported, m.coverage.recs)}%)` });
  }
  if (m.impacts.length) {
    out.push({ label: "Impact analyses (AI-generated)",
      value: `${m.impacts.length}${m.impactMean === null ? "" : `, mean score ${(Math.round(m.impactMean * 10) / 10).toFixed(1)}/10`}` });
  }
  return out;
}

/** Compact digest of the model for the AI summary, under FACTS_TARGET characters (lists shortened step by step). */
export function briefingFacts(m, target = FACTS_TARGET) {
  const build = (lim) => ({
    country: { code: m.country, name: m.countryName },
    period: { from: m.from, to: m.to },
    topic: m.topicLabel || null,
    projects: {
      total: m.projects.length,
      by_programme: Object.fromEntries(m.programmes.map((g) => [g, m.projects.filter((p) => p.programme === g).length]).filter(([, n]) => n)),
      by_year: Object.fromEntries(m.byYear.map((y) => [y.year, y.total])),
      by_policy_area: Object.fromEntries(m.byArea.map((a) => [a.label, a.n])),
      results: { achieved: m.results.achieved, expected: m.results.expected, not_stated: m.results.none },
      deliverables: m.deliverables,
    },
    links: { pairs: m.links.pairs, direct_pct: pct(m.links.direct, m.links.pairs), projects: m.links.projects, note: "AI-assessed links" },
    coverage: { assessed_recommendations: m.coverage.recs, supported_by_a_project: m.coverage.supported, pct: pct(m.coverage.supported, m.coverage.recs) },
    latest_document: m.latest ? {
      id: m.latest.id, year: m.latest.year, stage: m.latest.stage, assessed: !m.latest.commission,
      recommendations: m.latest.recs.slice(0, 8).map((r) => ({ n: r.number, summary: clip(r.summary, lim.text), areas: r.areas.slice(0, 3),
        assessment: r.assessment, projects: r.supporters.length })),
    } : null,
    recommendations: m.recs.filter((r) => r.docId !== m.latest?.id).slice(0, lim.recs).map((r) => ({
      id: r.id, summary: clip(r.summary, lim.text), assessment: r.assessment, projects: r.supporters.length })),
    lineage: m.lineage.slice(0, lim.lineage).map((l) => ({ csr: l.rec.id, projects: l.projects.slice(0, 6).map((p) => `${clip(p.title, 80)} (${p.year}, ${p.level})`) })),
    top_projects: m.projects.filter((p) => p.results !== "none" || p.impact).slice(0, lim.projects).map((p) => ({
      title: clip(p.title, 110), year: p.year, programme: p.programme, results: p.results, deliverables: p.deliverables,
      impact: p.impact && Number.isFinite(p.impact.score) ? p.impact.score : null })),
    impact: { count: m.impacts.length, mean: m.impactMean === null ? null : Math.round(m.impactMean * 10) / 10,
      items: m.impacts.slice(0, lim.impacts).map((p) => ({ title: clip(p.title, 100), score: p.impact.score, level: p.impact.level,
        confidence: p.impact.confidence, verdict: clip(p.impact.verdict, lim.verdict) })) },
    gaps: m.gaps.slice(0, lim.gaps).map((r) => ({ id: r.id, summary: clip(r.summary, lim.text), assessment: r.assessment })),
    anomalies: { total: m.anomalies.length, by_severity: Object.fromEntries(m.anomalyCounts.map((a) => [a.key, a.n])) },
    related_past_projects: m.related?.length && lim.related ? {
      note: "AI-suggested past projects for the latest (2026) recommendations: leads to build on, not assessed links",
      items: m.related.slice(0, lim.related).map((x) => ({ csr: x.rec.id, projects: x.projects.map((p) => `${clip(p.title, lim.relTitle)} `
        + `(${p.year || "n.d."}, ${p.scope === "same_country" ? "same country" : "other country"}, ${p.relevance})`) })),
    } : undefined,
  });
  const steps = [
    { text: 200, recs: 40, lineage: 30, projects: 25, impacts: 10, verdict: 260, gaps: 25, related: 8, relTitle: 90 },
    { text: 140, recs: 30, lineage: 20, projects: 20, impacts: 8, verdict: 200, gaps: 20, related: 7, relTitle: 80 },
    { text: 100, recs: 20, lineage: 15, projects: 15, impacts: 6, verdict: 160, gaps: 15, related: 6, relTitle: 70 },
    { text: 70, recs: 12, lineage: 10, projects: 10, impacts: 5, verdict: 120, gaps: 10, related: 5, relTitle: 60 },
    { text: 50, recs: 6, lineage: 6, projects: 6, impacts: 3, verdict: 80, gaps: 6, related: 3, relTitle: 50 },
    { text: 50, recs: 6, lineage: 6, projects: 6, impacts: 3, verdict: 80, gaps: 6, related: 0, relTitle: 0 },
  ];
  let facts;
  for (const lim of steps) {
    facts = build(lim);
    if (JSON.stringify(facts).length <= target) return facts;
  }
  return facts;
}

// ---------- several countries and themes: the comparison ----------

/** Fields of a matrix cell, in the order sent to the AI (one array per cell, this legend once). */
export const CELL_FIELDS = ["projects", "recs", "assessed", "supported", "rated", "progressing", "weakUnsupported"];
const CELL_LEGEND = ["projects", "recommendations", "assessed recommendations (Council)", "assessed and supported by a project",
  "with a progress assessment", "progressing (some progress or better)", "Limited or No progress and no supporting project"];
export const HIGHLIGHT_MIN = 5; // assessed recommendations a cell needs to enter a highlight

const emptyCell = () => Object.fromEntries(CELL_FIELDS.map((k) => [k, 0]));

/** One cell: a country's projects and recommendations for one theme ("all": every theme of the scope). */
function matrixCell(m, key, commission) {
  const projects = m.projects.filter((p) => key === "all" || p.policyArea === key);
  const recs = m.recs.filter((r) => key === "all" || arr(r.topics).includes(key));
  const assessed = recs.filter((r) => !commission.has(r.docId));
  const ranks = assessed.map((r) => progressRank(r.assessment));
  return {
    projects: projects.length, recs: recs.length, assessed: assessed.length,
    supported: assessed.filter((r) => r.supporters.length).length,
    rated: ranks.filter((x) => x !== null).length,
    progressing: ranks.filter((x) => x !== null && x >= PROGRESS_FROM).length,
    weakUnsupported: assessed.filter((r) => weakAssessment(r.assessment) && !r.supporters.length).length,
  };
}

/**
 * Countries x themes. models: one briefingModel per country, built with the same topics and period.
 * opts: { topics (slugs, [] = all), topicLabels (slug -> label) }
 * -> { countries, names, topics, columns [{ key, label }], rows [{ country, name, cells: { key: cell } }], totals { key: cell },
 *      highlights (largest differences between countries), sharedGaps (themes with weak unsupported recommendations in 2+ countries),
 *      label, from, to, date, models }
 */
export function compareModel(models, { topics = [], topicLabels = {} } = {}) {
  const list = arr(models).slice().sort((a, b) => String(a.country).localeCompare(String(b.country)));
  const labelOf = (t) => topicLabels[t] || t;
  const columns = [...topics.map((t) => ({ key: t, label: labelOf(t) })), { key: "all", label: topics.length ? "All selected areas" : "All policy areas" }];
  const rows = list.map((m) => {
    const commission = new Set(m.docs.filter((d) => d.commission).map((d) => d.id));
    return { country: m.country, name: m.countryName, cells: Object.fromEntries(columns.map((c) => [c.key, matrixCell(m, c.key, commission)])) };
  });
  const totals = Object.fromEntries(columns.map((c) => {
    const t = emptyCell();
    for (const r of rows) for (const k of CELL_FIELDS) t[k] += r.cells[c.key][k];
    return [c.key, t];
  }));
  const share = (a, b) => Math.round((100 * a) / b);
  const highlights = [];
  for (const c of topics.length ? columns.filter((x) => x.key !== "all") : columns) {
    for (const [kind, part, whole] of [["supported", "supported", "assessed"], ["progressing", "progressing", "rated"]]) {
      const shares = rows.filter((r) => r.cells[c.key][whole] >= HIGHLIGHT_MIN)
        .map((r) => ({ country: r.country, name: r.name, pct: share(r.cells[c.key][part], r.cells[c.key][whole]), n: r.cells[c.key][whole] }))
        .sort((a, b) => b.pct - a.pct || a.country.localeCompare(b.country));
      if (shares.length < 2 || shares[0].pct === shares[shares.length - 1].pct) continue;
      highlights.push({ kind, topic: c.key, topicLabel: c.label, high: shares[0], low: shares[shares.length - 1],
        spread: shares[0].pct - shares[shares.length - 1].pct });
    }
  }
  highlights.sort((a, b) => b.spread - a.spread || a.topic.localeCompare(b.topic));
  const sharedGaps = columns.filter((c) => c.key !== "all" || !topics.length).map((c) => ({ topic: c.key, topicLabel: c.label,
    countries: rows.filter((r) => r.cells[c.key].weakUnsupported > 0).map((r) => ({ country: r.country, name: r.name, n: r.cells[c.key].weakUnsupported })) }))
    .filter((g) => g.countries.length >= 2);
  const names = Object.fromEntries(list.map((m) => [m.country, m.countryName]));
  const countries = list.map((m) => m.country);
  const label = [countriesLabel(countries, names), joinNames(topics.map(labelOf))].filter(Boolean).join(" · ");
  return { countries, names, topics, columns, rows, totals, highlights: highlights.slice(0, 5), sharedGaps, label,
    from: list[0]?.from ?? null, to: list[0]?.to ?? null, date: list[0]?.date || new Date(), models: list };
}

/**
 * Digest of the comparison for the AI, under FACTS_TARGET characters. Mode by number of countries, coarser when too large:
 * "detail" (up to 3: the matrix and each country's digest), "summary" (up to 8: the matrix and each country's 3 main gaps
 * and projects), "matrix" (the matrix, totals and highlights only).
 */
export function compareFacts(cmp) {
  const cells = (c) => CELL_FIELDS.map((k) => c[k]);
  const base = (lim) => ({
    countries: cmp.rows.map((r) => ({ code: r.country, name: r.name })),
    group: groupOf(cmp.countries),
    period: { from: cmp.from, to: cmp.to },
    topics: cmp.topics.length ? cmp.columns.filter((c) => c.key !== "all").map((c) => c.label) : null,
    cell_fields: CELL_LEGEND,
    columns: cmp.columns.map((c) => c.label),
    matrix: cmp.rows.map((r) => ({ country: r.country, cells: cmp.columns.map((c) => cells(r.cells[c.key])) })),
    totals: cmp.columns.map((c) => cells(cmp.totals[c.key])),
    highlights: cmp.highlights.map((h) => ({ measure: h.kind === "supported" ? "share of assessed recommendations supported" : "share progressing",
      area: h.topicLabel, highest: `${h.high.name} ${h.high.pct}% (of ${h.high.n})`, lowest: `${h.low.name} ${h.low.pct}% (of ${h.low.n})` })),
    shared_gaps: cmp.sharedGaps.slice(0, lim.gaps).map((g) => ({ area: g.topicLabel, countries: g.countries.map((c) => `${c.name} ${c.n}`) })),
    note: "Links between projects and recommendations are AI-assessed; 2026 recommendations are Commission-stage, not yet assessed.",
  });
  const summary = (m) => ({ country: m.country, projects: m.projects.length,
    coverage: { assessed: m.coverage.recs, supported: m.coverage.supported },
    gaps: m.gaps.slice(0, 3).map((r) => ({ id: r.id, summary: clip(r.summary, 90), assessment: r.assessment })),
    top_projects: m.projects.filter((p) => p.results !== "none" || p.impact).slice(0, 3).map((p) => ({ title: clip(p.title, 90), year: p.year, results: p.results })),
    latest_document: m.latest ? { id: m.latest.id, year: m.latest.year, stage: m.latest.stage,
      recommendations: m.latest.recs.slice(0, 6).map((r) => ({ n: r.number, summary: clip(r.summary, 80) })) } : null });
  const n = cmp.rows.length;
  const modes = ["detail", "summary", "matrix"].slice(n <= 3 ? 0 : n <= 8 ? 1 : 2);
  let facts;
  for (const mode of modes) {
    for (const lim of [{ gaps: 10 }, { gaps: 4 }]) {
      facts = { mode, ...base(lim) };
      const room = FACTS_TARGET - JSON.stringify(facts).length;
      if (mode === "detail") facts.countries_detail = cmp.models.map((m) => briefingFacts(m, Math.max(2000, Math.floor(room / n) - 200)));
      if (mode === "summary") facts.countries_detail = cmp.models.map(summary);
      if (JSON.stringify(facts).length <= FACTS_TARGET) return facts;
    }
  }
  // Still too large (27 countries with long names): fewer highlights, then no shared gaps.
  facts.highlights = facts.highlights.slice(0, 2);
  if (JSON.stringify(facts).length > FACTS_TARGET) facts.shared_gaps = [];
  return facts;
}

