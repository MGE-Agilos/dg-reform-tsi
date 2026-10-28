// CSR progress: implementation of the recommendations (Commission assessment, ECFIN database) compared between
// recommendations supported by at least one TSI/SRSP project (AI-assessed links) and the others.
// Pure functions, no DOM: app.js renders the page.

/** Assessments on the progress scale, worst to best: [key, label, colour key, rank]. Other values are left out. */
export const PROGRESS_SCALE = [
  ["no progress", "No progress", "o5", 0], ["limited progress", "Limited progress", "o4", 1],
  ["some progress", "Some progress", "o3", 2], ["substantial progress", "Substantial progress", "o2", 3],
  ["full implementation", "Full implementation", "o1", 4],
];
/** Rank from which a recommendation counts as progressing ("at least some progress"). */
export const PROGRESS_FROM = 2;
/** Groups under this size are flagged as too small to compare. */
export const FEW = 10;

const RANK = new Map(PROGRESS_SCALE.map(([k, , , r]) => [k, r]));

/** Rank of an assessment on the scale, or null (not assessed, no assessment, unknown value). */
export function progressRank(value) {
  const r = RANK.get(String(value ?? "").trim().toLowerCase());
  return r === undefined ? null : r;
}

function tally() {
  return { n: 0, ok: 0, counts: PROGRESS_SCALE.map(() => 0) };
}

function add(t, rank) {
  t.n++;
  t.counts[rank]++;
  if (rank >= PROGRESS_FROM) t.ok++;
}

/** Share of the group with at least some progress (0..1), or null for an empty group. */
export function okShare(t) {
  return t.n ? t.ok / t.n : null;
}

/** Gap in percentage points between supported and other recommendations, or null when a side is empty. */
export function gapPoints(s, u) {
  const a = okShare(s);
  const b = okShare(u);
  return a === null || b === null ? null : Math.round((a - b) * 1000) / 10;
}

/**
 * The comparison for the selected options.
 * data: { csrs: [{ csr_id, country, year, assessment, assessment_year, policy_areas }], parts: [{ part_id, csr_id }],
 *         links: [{ project_id, part_id, level }], projects: [{ project_id, year }] }
 * opts: { from, to (CSR years, inclusive), level: "all" | "direct", before: only projects whose year is at most the
 *         assessment year (projects without a year then do not count) }
 * A recommendation is supported when one of its sub-part links (at the selected level) leads to a counted project.
 */
export function progressModel({ csrs, parts, links, projects }, { from = -Infinity, to = Infinity, level = "all", before = false } = {}) {
  const partCsr = new Map(parts.map((p) => [p.part_id, p.csr_id]));
  const projYear = new Map(projects.map((p) => [p.project_id, Number.isFinite(+p.year) && p.year !== null ? +p.year : null]));
  const support = new Map(); // csr_id -> Set of project ids (level filter applied, timing checked later)
  for (const l of links) {
    if (level === "direct" && l.level !== "direct") continue;
    const cid = partCsr.get(l.part_id);
    if (!cid || !projYear.has(l.project_id)) continue;
    if (!support.has(cid)) support.set(cid, new Set());
    support.get(cid).add(l.project_id);
  }
  const groups = { s: tally(), u: tally() };
  const byYear = new Map();
  const byCountry = new Map();
  const byArea = new Map();
  const bump = (map, key, side, rank) => {
    if (!map.has(key)) map.set(key, { key, s: tally(), u: tally() });
    add(map.get(key)[side], rank);
  };
  let unassessed = 0;
  const supportedProjects = new Set();
  for (const c of csrs) {
    const year = +c.year;
    if (!(year >= from && year <= to)) continue;
    const rank = progressRank(c.assessment);
    if (rank === null) { unassessed++; continue; }
    const ay = Number.isFinite(+c.assessment_year) && c.assessment_year !== null ? +c.assessment_year : null;
    const ps = [...(support.get(c.csr_id) || [])].filter((pid) => {
      if (!before) return true;
      const py = projYear.get(pid);
      return py !== null && ay !== null && py <= ay;
    });
    const side = ps.length ? "s" : "u";
    ps.forEach((pid) => supportedProjects.add(pid));
    add(groups[side], rank);
    bump(byYear, year, side, rank);
    bump(byCountry, c.country, side, rank);
    for (const a of new Set(Array.isArray(c.policy_areas) ? c.policy_areas : [])) bump(byArea, a, side, rank);
  }
  const rows = (map) => [...map.values()];
  return {
    ...groups,
    gap: gapPoints(groups.s, groups.u),
    sameYearGap: sameYearGap(rows(byYear)),
    projects: supportedProjects.size,
    unassessed,
    byYear: rows(byYear).sort((a, b) => a.key - b.key),
    byCountry: rows(byCountry).sort((a, b) => String(a.key).localeCompare(String(b.key))),
    byArea: rows(byArea).sort((a, b) => b.s.n - a.s.n || b.u.n - a.u.n || String(a.key).localeCompare(String(b.key))),
  };
}

/**
 * Gap within the same CSR year: the yearly gaps averaged with the weight s·u/(s+u) (Mantel-Haenszel), so that years
 * with many supported or many unsupported recommendations do not tilt the result. Years missing a side are skipped.
 */
export function sameYearGap(byYear) {
  let sum = 0;
  let weight = 0;
  for (const r of byYear) {
    const g = gapPoints(r.s, r.u);
    if (g === null) continue;
    const w = (r.s.n * r.u.n) / (r.s.n + r.u.n);
    sum += w * g;
    weight += w;
  }
  return weight ? Math.round((sum / weight) * 10) / 10 : null;
}

/** Last CSR year with at least one supported recommendation (default end of the period), or null. */
export function lastSupportedYear(model) {
  const ys = model.byYear.filter((r) => r.s.n > 0).map((r) => r.key);
  return ys.length ? Math.max(...ys) : null;
}

/** CSR years present in the data (Council recommendations with a progress assessment). */
export function assessedYears(csrs) {
  return [...new Set(csrs.filter((c) => progressRank(c.assessment) !== null).map((c) => +c.year))].sort((a, b) => a - b);
}
