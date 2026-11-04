// Tests of the Compare page model (countries x policy areas, or policy areas x years), pure module.
// Run: node --test tests/js/compare.test.mjs
import { test } from "node:test";
import assert from "node:assert/strict";
import { METRICS, compareMatrix, heatBin, FEW_CELL } from "../../web/compare.js";

const LABELS = { edu: ["Education"], health: ["Healthcare"], dig: ["Digitalisation of businesses"] };
const csr = (id, country, year, areas, assessment) => ({ csr_id: id, country, year, assessment, assessment_year: year + 1, policy_areas: areas });
const data = {
  csrs: [
    csr("ee1", "EE", 2022, ["Education"], "Some Progress"),
    csr("ee2", "EE", 2022, ["Education"], "No Progress"),
    csr("ee3", "EE", 2023, ["Healthcare", "Education"], "Limited Progress"),
    csr("lv1", "LV", 2022, ["Healthcare"], "Substantial Progress"),
    csr("lv2", "LV", 2026, ["Healthcare"], null),
  ],
  parts: [{ part_id: "ee1.1", csr_id: "ee1" }, { part_id: "lv1.1", csr_id: "lv1" }],
  links: [{ project_id: "p1", part_id: "ee1.1", level: "direct" }, { project_id: "p2", part_id: "lv1.1", level: "partial" }],
  projects: [
    { project_id: "p1", year: 2021, countries: ["EE"], policy_area: "edu" },
    { project_id: "p2", year: 2022, countries: ["LV", "EE"], policy_area: "health" },
    { project_id: "p3", year: 2019, countries: ["EE"], policy_area: "edu" },
  ],
};
const OPTS = { countries: ["EE", "LV"], topics: ["edu", "health"], from: 2020, to: 2026, topicKeys: ["edu", "health", "dig"], labelsOf: LABELS };

test("metrics: the six measures, with their kind", () => {
  assert.deepEqual(Object.keys(METRICS), ["projects", "recs", "supported", "progressing", "gap", "weak"]);
  assert.equal(METRICS.gap.kind, "diverging");
  assert.equal(METRICS.projects.kind, "count");
});

test("countries x policy areas: counts, shares, the all column counts a recommendation once", () => {
  const m = compareMatrix(data, { ...OPTS, metric: "recs" });
  assert.deepEqual(m.rows.map((r) => r.key), ["EE", "LV"]);
  assert.deepEqual(m.cols.map((c) => c.key), ["edu", "health", "all"]);
  assert.equal(m.cell("EE", "edu").value, 3);
  assert.equal(m.cell("EE", "health").value, 1);
  assert.equal(m.cell("EE", "all").value, 3);
  assert.equal(m.cell("LV", "health").value, 2);
  const p = compareMatrix(data, { ...OPTS, metric: "projects" });
  assert.equal(p.cell("EE", "edu").value, 1); // p3 is out of the period
  assert.equal(p.cell("EE", "health").value, 1); // multi-country project counts for each country
  const s = compareMatrix(data, { ...OPTS, metric: "supported" });
  assert.equal(s.cell("EE", "edu").value, 33); // 1 of 3 assessed
  assert.equal(s.cell("EE", "edu").few, true);
  assert.equal(s.cell("LV", "health").n, 1); // the 2026 recommendation is not assessed
  const w = compareMatrix(data, { ...OPTS, metric: "weak" });
  assert.equal(w.cell("EE", "edu").value, 2);
});

test("no policy area selected: every topic key is a column; no country: every country with data", () => {
  const m = compareMatrix(data, { ...OPTS, countries: [], topics: [], metric: "recs" });
  assert.deepEqual(m.cols.map((c) => c.key), ["edu", "health", "dig", "all"]);
  assert.deepEqual(m.rows.map((r) => r.key), ["EE", "LV"]);
  assert.equal(m.cell("EE", "dig").value, 0);
});

test("gap: progressing share supported minus not supported, null when a side is empty", () => {
  const many = { ...data, csrs: [...data.csrs, ...Array.from({ length: 6 }, (_, i) => csr(`x${i}`, "EE", 2022, ["Education"], i < 2 ? "Some Progress" : "No Progress"))],
    parts: [...data.parts, ...Array.from({ length: 5 }, (_, i) => ({ part_id: `x${i}.1`, csr_id: `x${i}` }))],
    links: [...data.links, ...Array.from({ length: 5 }, (_, i) => ({ project_id: "p1", part_id: `x${i}.1`, level: "direct" }))] };
  const g = compareMatrix(many, { ...OPTS, metric: "gap" });
  // supported: ee1 + x0..x4 = 6, progressing ee1, x0, x1 = 3 -> 50%; not supported: ee2, ee3, x5 = 3, none progressing -> 0%
  assert.equal(g.cell("EE", "edu").value, 50);
  assert.equal(g.cell("EE", "edu").few, true); // 3 on the unsupported side
  assert.equal(compareMatrix(data, { ...OPTS, metric: "gap" }).cell("LV", "health").value, null);
});

test("policy areas x years for the selected countries", () => {
  const m = compareMatrix(data, { ...OPTS, from: 2021, to: 2023, metric: "recs", mode: "years" });
  assert.deepEqual(m.rows.map((r) => r.key), ["edu", "health"]);
  assert.deepEqual(m.cols.map((c) => c.key), [2021, 2022, 2023, "all"]);
  assert.equal(m.cell("edu", 2022).value, 2);
  assert.equal(m.cell("health", 2022).value, 1);
  assert.equal(m.cell("edu", "all").value, 3);
});

test("heatBin: 0 for none or few, 1 to 5 by share of the maximum; diverging -2..2", () => {
  assert.equal(heatBin({ value: 0 }, 10, "count"), 0);
  assert.equal(heatBin({ value: 10 }, 10, "count"), 5);
  assert.equal(heatBin({ value: 1 }, 10, "count"), 1);
  assert.equal(heatBin({ value: 40, few: true }, 100, "share"), 0);
  assert.equal(heatBin({ value: null }, 100, "share"), 0);
  assert.equal(heatBin({ value: -30 }, 30, "diverging"), -2);
  assert.equal(heatBin({ value: 2 }, 30, "diverging"), 0);
  assert.equal(heatBin({ value: 12 }, 30, "diverging"), 1);
  assert.equal(FEW_CELL, 5);
});
