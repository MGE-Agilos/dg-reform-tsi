// Country briefing: pure model of the data of one country and period (optionally one policy area) and the compact
// "facts" digest sent to the AI summary (tsi-assistant, service "briefing"). No DOM, no network: app.js fetches the
// rows, word-export.js builds the document, so Node can test this module.

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

/** briefing-<ISO>-<from>-<to>[-<topic>].docx */
export function briefingFileName(country, from, to, topic) {
  const t = topic ? `-${String(topic).replace(/[^a-z0-9-]+/gi, "-").replace(/^-+|-+$/g, "").slice(0, 60)}` : "";
  return `briefing-${String(country).replace(/[^A-Z]/gi, "").toUpperCase()}-${from}-${to}${t}.docx`;
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
 * impacts (impact_reports rows, newest first), anomalies (rows of the country) }
 * opts: { country, countryName, from, to, topic (slug or ""), topicLabel, areaLabel(slug), date }
 */
export function briefingModel(raw, opts) {
  const { country, from, to } = opts;
  const topic = opts.topic || "";
  const areaLabel = opts.areaLabel || ((s) => s || "");
  const inPeriod = (y) => num(y) >= from && num(y) <= to;
  const byId = new Map();
  for (const p of [...arr(raw.linked), ...arr(raw.projects)]) if (p?.project_id) byId.set(p.project_id, p);
  const countryProjects = arr(raw.projects).filter((p) => p && arr(p.countries).includes(country) && inPeriod(p.year));
  const projects = countryProjects.filter((p) => !topic || p.policy_area === topic)
    .sort((a, b) => num(b.year) - num(a.year) || String(a.title).localeCompare(String(b.title)));
  const topicProjectIds = new Set(projects.map((p) => p.project_id));
  const impacts = latestImpacts(raw.impacts);

  // Documents of the period (csr_documents, or rebuilt from the recommendations).
  const docsById = new Map(arr(raw.docs).filter((d) => d && inPeriod(d.year)).map((d) => [d.doc_id, d]));
  const labels = new Set(TOPIC_CSR_AREAS[topic] || []);
  const keyword = TOPIC_KEYWORDS[topic] || null;
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
    let match = "all";
    if (topic) {
      if (areas.some((a) => labels.has(a))) match = "areas";
      else if ([...pairs.keys()].some((pid) => byId.get(pid)?.policy_area === topic)) match = "projects";
      else if (!areas.length && keyword?.test(String(c.text ?? ""))) match = "text";
      else match = null;
    }
    recs.push({
      id: c.csr_id, year: num(c.year), number: c.number ?? "", docId, text: String(c.text ?? ""), summary: firstSentence(c.text),
      areas, assessment: assessmentLabel(c.assessment), assessmentYear: c.assessment_year ?? null, match,
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
    && (a.year === null || a.year === undefined || inPeriod(a.year)) && (!topic || topicProjectIds.has(a.project_id)));
  const sevRank = { high: 0, medium: 1, low: 2, info: 3 };
  anomalies.sort((a, b) => (sevRank[a.severity] ?? 9) - (sevRank[b.severity] ?? 9) || num(b.year) - num(a.year) || String(a.title).localeCompare(String(b.title)));

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
    country, countryName: opts.countryName || country, from, to, topic, topicLabel: topic ? (opts.topicLabel || areaLabel(topic)) : "",
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
    gaps, sources: [...sources.entries()].map(([url, label]) => ({ url, label })),
    topicMatch: topic ? { areas: shownRecs.filter((r) => r.match === "areas").length, projects: shownRecs.filter((r) => r.match === "projects").length,
      text: shownRecs.filter((r) => r.match === "text").length, excluded: recs.length - shownRecs.length, labels: [...labels] } : null,
  };
}

const pct = (a, b) => (b > 0 ? Math.round((100 * a) / b) : null);

/** Compact digest of the model for the AI summary, under FACTS_TARGET characters (lists shortened step by step). */
export function briefingFacts(m) {
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
  });
  const steps = [
    { text: 200, recs: 40, lineage: 30, projects: 25, impacts: 10, verdict: 260, gaps: 25 },
    { text: 140, recs: 30, lineage: 20, projects: 20, impacts: 8, verdict: 200, gaps: 20 },
    { text: 100, recs: 20, lineage: 15, projects: 15, impacts: 6, verdict: 160, gaps: 15 },
    { text: 70, recs: 12, lineage: 10, projects: 10, impacts: 5, verdict: 120, gaps: 10 },
    { text: 50, recs: 6, lineage: 6, projects: 6, impacts: 3, verdict: 80, gaps: 6 },
  ];
  let facts;
  for (const lim of steps) {
    facts = build(lim);
    if (JSON.stringify(facts).length <= FACTS_TARGET) return facts;
  }
  return facts;
}

