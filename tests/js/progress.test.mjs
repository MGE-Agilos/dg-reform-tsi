// Tests of the CSR progress comparison (pure module, no DOM).
// Run: node --test tests/js/progress.test.mjs
import { test } from "node:test";
import assert from "node:assert/strict";
import { progressRank, progressModel, gapPoints, okShare, assessedYears, sameYearGap, lastSupportedYear } from "../../web/progress.js";

const data = {
  csrs: [
    { csr_id: "a", country: "LT", year: 2019, assessment: "Some Progress", assessment_year: 2021, policy_areas: ["Health", "Pensions"] },
    { csr_id: "b", country: "LT", year: 2019, assessment: "Limited Progress", assessment_year: 2021, policy_areas: ["Health"] },
    { csr_id: "c", country: "HR", year: 2020, assessment: "Substantial Progress", assessment_year: 2022, policy_areas: [] },
    { csr_id: "d", country: "HR", year: 2020, assessment: "No Progress", assessment_year: 2022, policy_areas: null },
    { csr_id: "e", country: "HR", year: 2022, assessment: "Not Assessed", assessment_year: 2023, policy_areas: [] },
    { csr_id: "f", country: "HR", year: 2026, assessment: null, assessment_year: null, policy_areas: [] },
  ],
  parts: [
    { part_id: "a.1", csr_id: "a" }, { part_id: "a.2", csr_id: "a" }, { part_id: "b.1", csr_id: "b" },
    { part_id: "c.1", csr_id: "c" }, { part_id: "d.1", csr_id: "d" },
  ],
  links: [
    { project_id: "p1", part_id: "a.1", level: "direct" },
    { project_id: "p2", part_id: "a.2", level: "partial" },
    { project_id: "p3", part_id: "c.1", level: "partial" },
    { project_id: "gone", part_id: "d.1", level: "direct" }, // project not in the table: ignored
  ],
  projects: [{ project_id: "p1", year: 2020 }, { project_id: "p2", year: 2023 }, { project_id: "p3", year: null }],
};

test("progressRank: the five-step scale, case-insensitive; anything else is out", () => {
  assert.equal(progressRank("No Progress"), 0);
  assert.equal(progressRank(" some progress "), 2);
  assert.equal(progressRank("Full Implementation"), 4);
  assert.equal(progressRank("Not Assessed"), null);
  assert.equal(progressRank(null), null);
  assert.equal(progressRank("Partial"), null);
});

test("progressModel: supported vs other recommendations, unassessed left out", () => {
  const m = progressModel(data);
  assert.deepEqual([m.s.n, m.s.ok, m.u.n, m.u.ok], [2, 2, 2, 0]); // a, c supported; b, d not
  assert.deepEqual(m.s.counts, [0, 0, 1, 1, 0]);
  assert.equal(m.unassessed, 2);
  assert.equal(m.projects, 3); // p1, p2 (a) and p3 (c)
  assert.equal(m.gap, 100);
  assert.deepEqual(m.byYear.map((r) => [r.key, r.s.n, r.u.n]), [[2019, 1, 1], [2020, 1, 1]]);
  assert.deepEqual(m.byCountry.map((r) => r.key), ["HR", "LT"]);
  assert.deepEqual(m.byArea.map((r) => [r.key, r.s.n, r.u.n]), [["Health", 1, 1], ["Pensions", 1, 0]]);
});

test("progressModel: direct links only", () => {
  const m = progressModel(data, { level: "direct" });
  assert.deepEqual([m.s.n, m.u.n], [1, 3]); // only a (p1 direct)
  assert.equal(m.projects, 1);
});

test("progressModel: only projects started by the assessment year", () => {
  const m = progressModel(data, { before: true });
  // a: p1 (2020 <= 2021) counts, p2 (2023) does not; c: p3 has no year, so c is not supported
  assert.deepEqual([m.s.n, m.u.n], [1, 3]);
  assert.equal(m.projects, 1);
});

test("progressModel: CSR year range", () => {
  const m = progressModel(data, { from: 2020, to: 2020 });
  assert.deepEqual([m.s.n, m.u.n, m.unassessed], [1, 1, 0]);
});

test("gapPoints and okShare: empty sides give null", () => {
  assert.equal(okShare({ n: 0, ok: 0 }), null);
  assert.equal(gapPoints({ n: 4, ok: 3 }, { n: 0, ok: 0 }), null);
  assert.equal(gapPoints({ n: 3, ok: 2 }, { n: 3, ok: 1 }), 33.3);
});

test("assessedYears: years with at least one assessed recommendation", () => {
  assert.deepEqual(assessedYears(data.csrs), [2019, 2020]);
});

test("sameYearGap: weighted mean of the yearly gaps, years missing a side skipped", () => {
  const t = (n, ok) => ({ n, ok });
  // 2019: 50% vs 0% (w = 1), 2020: 100% vs 50% (w = 2*2/4 = 1), 2021: no unsupported -> skipped
  const rows = [{ key: 2019, s: t(2, 1), u: t(2, 0) }, { key: 2020, s: t(2, 2), u: t(2, 1) }, { key: 2021, s: t(3, 3), u: t(0, 0) }];
  assert.equal(sameYearGap(rows), 50);
  assert.equal(sameYearGap([{ key: 2025, s: t(0, 0), u: t(5, 2) }]), null);
});

test("lastSupportedYear: last CSR year with a supported recommendation", () => {
  assert.equal(lastSupportedYear(progressModel(data)), 2020);
  assert.equal(lastSupportedYear(progressModel(data, { from: 2019, to: 2019, level: "direct", before: true })), 2019);
});
