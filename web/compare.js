// Compare page: one measure crossed over countries x policy areas, or policy areas x years (for the selected countries).
// Pure functions, no DOM: app.js renders the heatmap. Same tables as CSR progress (recommendations, sub-parts, links,
// projects); recommendations match a policy area through its ECFIN labels (TOPIC_CSR_AREAS), projects through their area.

import { progressRank, PROGRESS_FROM } from "./progress.js";

/** Measures: label, kind (count, share 0-100, diverging in points), sentence for the legend. */
export const METRICS = {
  projects: { label: "Projects", kind: "count", sub: "Projects whose year is in the period (a multi-country project counts for each country)." },
  recs: { label: "Recommendations", kind: "count", sub: "Country-specific recommendations of the period with an ECFIN label of the policy area." },
  supported: { label: "Share supported", kind: "share", sub: "Assessed recommendations with at least one supporting project (AI-assessed links)." },
  progressing: { label: "Share progressing", kind: "share", sub: "Assessed recommendations with some progress or better in the latest assessment." },
  gap: { label: "Progress gap", kind: "diverging", sub: "Share progressing of the supported minus the others, in percentage points." },
  weak: { label: "Stalling without support", kind: "count", sub: "Recommendations assessed Limited or No progress and without a supporting project." },
};
export const FEW_CELL = 5; // assessed recommendations under which a share is greyed

const weak = (rank) => rank !== null && rank < PROGRESS_FROM && rank <= 1;

/**
 * data: { csrs [{ csr_id, country, year, assessment, policy_areas }], parts [{ part_id, csr_id }], links [{ project_id, part_id, level }],
 *         projects [{ project_id, year, countries, policy_area }] }
 * opts: { countries ([] = every country with data), topics ([] = every topic key), from, to, metric, mode: "countries" | "years",
 *         topicKeys (all policy-area slugs, in order), labelsOf (slug -> ECFIN labels), level: "all" | "direct" }
 * -> { rows [{ key }], cols [{ key }], cell(row, col) -> { value, n, few, ...counts }, max, metric }
 */
export function compareMatrix(data, opts) {
  const { from = -Infinity, to = Infinity, metric = "projects", mode = "countries", topicKeys = [], labelsOf = {}, level = "all" } = opts;
  const topics = opts.topics?.length ? opts.topics : topicKeys;
  const inYears = (y) => Number.isFinite(+y) && y !== null && +y >= from && +y <= to;
  const partCsr = new Map(data.parts.map((p) => [p.part_id, p.csr_id]));
  const known = new Set(data.projects.map((p) => p.project_id));
  const supported = new Set();
  for (const l of data.links) {
    if (level === "direct" && l.level !== "direct") continue;
    const cid = partCsr.get(l.part_id);
    if (cid && known.has(l.project_id)) supported.add(cid);
  }
  const labelTopic = new Map(); // ECFIN label -> topics
  for (const t of topics) for (const lab of labelsOf[t] || []) labelTopic.set(lab, [...(labelTopic.get(lab) || []), t]);
  const recs = data.csrs.filter((c) => inYears(c.year)).map((c) => ({
    country: c.country, year: +c.year, rank: progressRank(c.assessment), supported: supported.has(c.csr_id),
    topics: new Set((Array.isArray(c.policy_areas) ? c.policy_areas : []).flatMap((a) => labelTopic.get(a) || [])),
  })).filter((r) => r.topics.size);
  const projects = data.projects.filter((p) => inYears(p.year) && topics.includes(p.policy_area))
    .map((p) => ({ countries: Array.isArray(p.countries) ? p.countries : [], year: +p.year, topic: p.policy_area }));

  const present = [...new Set([...recs.map((r) => r.country), ...projects.flatMap((p) => p.countries)])].sort();
  const countries = opts.countries?.length ? [...opts.countries] : present;
  const years = Number.isFinite(from) && Number.isFinite(to) ? Array.from({ length: to - from + 1 }, (_, i) => from + i) : [];
  const rows = mode === "years" ? topics.map((t) => ({ key: t })) : countries.map((c) => ({ key: c }));
  const cols = [...(mode === "years" ? years : topics).map((k) => ({ key: k })), { key: "all" }];

  /** Counts of one cell; country/topic/year null = any (within the selection). */
  const stats = (country, topic, year) => {
    const inCountry = (c) => (country ? c === country : countries.includes(c));
    const inTopic = (set) => (topic ? set.has(topic) : true);
    const rs = recs.filter((r) => inCountry(r.country) && inTopic(r.topics) && (year === null || r.year === year));
    const ps = projects.filter((p) => p.countries.some(inCountry) && (!topic || p.topic === topic) && (year === null || p.year === year));
    const rated = rs.filter((r) => r.rank !== null);
    const sup = rated.filter((r) => r.supported);
    const uns = rated.filter((r) => !r.supported);
    const ok = (list) => list.filter((r) => r.rank >= PROGRESS_FROM).length;
    return { projects: ps.length, recs: rs.length, rated: rated.length, supported: sup.length, progressing: ok(rated),
      ratedS: sup.length, ratedU: uns.length, progressingS: ok(sup), progressingU: ok(uns), weak: uns.filter((r) => weak(r.rank)).length };
  };
  const pct = (a, b) => (b > 0 ? Math.round((100 * a) / b) : null);
  const valueOf = (s) => {
    switch (metric) {
      case "recs": return { value: s.recs, n: s.recs, few: false };
      case "supported": return { value: pct(s.supported, s.rated), n: s.rated, few: s.rated < FEW_CELL };
      case "progressing": return { value: pct(s.progressing, s.rated), n: s.rated, few: s.rated < FEW_CELL };
      case "gap": {
        const a = pct(s.progressingS, s.ratedS);
        const b = pct(s.progressingU, s.ratedU);
        return { value: a === null || b === null ? null : a - b, n: s.rated, few: s.ratedS < FEW_CELL || s.ratedU < FEW_CELL };
      }
      case "weak": return { value: s.weak, n: s.weak, few: false };
      default: return { value: s.projects, n: s.projects, few: false };
    }
  };
  const cells = new Map();
  for (const r of rows) {
    for (const c of cols) {
      const all = c.key === "all";
      const s = mode === "years" ? stats(null, r.key, all ? null : c.key) : stats(r.key, all ? null : c.key, null);
      cells.set(`${r.key}|${c.key}`, { ...s, ...valueOf(s) });
    }
  }
  const inner = [...cells.entries()].filter(([k, v]) => !k.endsWith("|all") && v.value !== null && !v.few).map(([, v]) => Math.abs(v.value));
  const max = inner.length ? Math.max(...inner) : 0;
  return { rows, cols, metric, mode, max, cell: (r, c) => cells.get(`${r}|${c}`) };
}

/** Colour step of a cell: 0 (none, few or empty), 1-5 by share of the maximum; diverging: -2..2, 0 under 5 points. */
export function heatBin(cell, max, kind) {
  const v = cell?.value;
  if (v === null || v === undefined || cell.few || !max) return 0;
  if (kind === "diverging") {
    if (Math.abs(v) < 5) return 0;
    return Math.sign(v) * (Math.abs(v) >= Math.max(max, 10) / 2 ? 2 : 1);
  }
  if (v <= 0) return 0;
  return Math.min(5, Math.max(1, Math.ceil((5 * v) / max)));
}
