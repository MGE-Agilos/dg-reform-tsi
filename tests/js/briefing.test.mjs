// Tests of the briefing helpers (pure module, no DOM).
// Run: node --test tests/js/briefing.test.mjs
import { test } from "node:test";
import assert from "node:assert/strict";
import { OCCASION_FORMATS, OCCASION_MAX, cleanOccasion, occasionErrors, briefingFileName, keyFigures } from "../../web/briefing.js";

const VALID = { recipient: "Director X", title: "Meeting with the Minister of Finance", date: "2026-11-12", place: "Vilnius", format: "bilateral", context: "", background: "" };

test("cleanOccasion: single-line fields squeezed, context keeps its lines, unknown format -> other", () => {
  assert.deepEqual(cleanOccasion({ recipient: "  Director   X ", title: "A\nB", date: " 2026-11-12 ", place: null, format: "party", context: " a\r\nb ", background: " Grant\r\nsince 2024 " }),
    { recipient: "Director X", title: "A B", date: "2026-11-12", place: "", format: "other", context: "a\nb", background: "Grant\nsince 2024" });
  assert.deepEqual(Object.keys(OCCASION_FORMATS), ["bilateral", "panel", "speech", "mission", "conference", "other"]);
});

test("occasionErrors: required fields, lengths and date, same rules as the edge function", () => {
  assert.deepEqual(occasionErrors(VALID), {});
  const e = occasionErrors({ ...VALID, recipient: "", title: "x".repeat(OCCASION_MAX.title + 1), date: "2026-02-30",
    place: "x".repeat(121), context: "x".repeat(8001), background: "x".repeat(2001) });
  assert.deepEqual(Object.keys(e), ["recipient", "title", "place", "date", "context", "background"]);
  assert.deepEqual(occasionErrors({ ...VALID, date: "1999-05-01" }), { date: "Enter a valid date (2010 to 2100)." });
});

test("briefingFileName: country, occasion day (or today) and an ASCII slug of the occasion", () => {
  assert.equal(briefingFileName("lt", VALID), "briefing-LT-2026-11-12-meeting-with-the-minister-of-finance.docx");
  assert.equal(briefingFileName("LT", { title: "Réunion à Bruxelles — été" }, new Date(2026, 10, 2)), "briefing-LT-2026-11-02-reunion-a-bruxelles-ete.docx");
  assert.equal(briefingFileName("LT", { title: "x".repeat(80) }, new Date(2026, 0, 5)), `briefing-LT-2026-01-05-${"x".repeat(60)}.docx`);
  assert.equal(briefingFileName("LT", {}, new Date(2026, 0, 5)), "briefing-LT-2026-01-05.docx");
});

test("keyFigures: projects by programme, coverage, impact analyses; absent figures left out", () => {
  const m = { programmes: ["TSI", "SRSP", "SRSS"], projects: [{ programme: "TSI" }, { programme: "TSI" }, { programme: "SRSP" }],
    coverage: { recs: 10, supported: 4 }, impacts: [{}, {}], impactMean: 6.25 };
  assert.deepEqual(keyFigures(m), [
    { label: "Projects in the period", value: "3 (TSI 2, SRSP 1)" },
    { label: "Assessed recommendations supported by a project", value: "4 of 10 (40%)" },
    { label: "Impact analyses (AI-generated)", value: "2, mean score 6.3/10" },
  ]);
  assert.deepEqual(keyFigures({ ...m, projects: [], coverage: { recs: 0, supported: 0 }, impacts: [], impactMean: null }),
    [{ label: "Projects in the period", value: "0" }]);
});

test("keyFigures: projects of another programme are counted as other", () => {
  const m = { programmes: ["TSI", "SRSP", "SRSS"], projects: [{ programme: "TSI" }, { programme: "EEA" }, { programme: "" }],
    coverage: { recs: 0, supported: 0 }, impacts: [], impactMean: null };
  assert.equal(keyFigures(m)[0].value, "3 (TSI 1, other 2)");
  assert.equal(keyFigures({ ...m, projects: [{ programme: "EEA" }] })[0].value, "1 (other 1)");
});

// ---------- several themes, several countries ----------
import { briefingModel, compareModel, compareFacts, compareFileName, FACTS_MAX } from "../../web/briefing.js";

const DIG = "digital-transition";
const EDU = "skills-education-and-training";
const HEA = "health-and-long-term-care";
const project = (id, cc, area, year = 2023, extra = {}) => ({ project_id: id, title: `Project ${id}`, programme: "TSI", year, countries: [cc],
  policy_area: area, results: "achieved", deliverables: 1, url: `https://reform.example/${id}`, detail_page: true, ...extra });
const csr = (id, year, areas, assessment, links = []) => ({ csr_id: id, year, number: id.split("-").pop(), text: `Recommendation ${id}. More text.`,
  policy_areas: areas, assessment, assessment_year: assessment ? year + 1 : null,
  csr_parts: [{ part_id: `${id}.1`, policy_areas: areas, project_csr_links: links.map((p) => ({ project_id: p, level: "direct" })) }] });
const rawOf = (cc, projects, csrs) => ({ projects, linked: [], docs: [], impacts: [], anomalies: [], csrs });
const modelOf = (cc, raw, topics) => briefingModel(raw, { country: cc, countryName: { EE: "Estonia", LV: "Latvia" }[cc] || cc, from: 2021, to: 2026, topics,
  areaLabel: (s) => s, date: new Date(2026, 10, 4) });

const EE = rawOf("EE", [project("e1", "EE", DIG), project("e2", "EE", EDU), project("e3", "EE", HEA)], [
  csr("csr-EE-2022-1", 2022, ["Digitalisation of businesses"], "Some Progress", ["e1"]),
  csr("csr-EE-2022-2", 2022, ["Education"], "Limited Progress"),
  csr("csr-EE-2022-3", 2022, ["Healthcare"], "No Progress"),
]);
const LV = rawOf("LV", [project("l1", "LV", EDU)], [
  csr("csr-LV-2022-1", 2022, ["Education"], "Substantial Progress", ["l1"]),
  csr("csr-LV-2022-2", 2022, ["Education"], "No Progress"),
]);

test("briefingModel: several themes keep the projects and recommendations of any of them, with the matched themes", () => {
  const m = modelOf("EE", EE, [DIG, EDU]);
  assert.deepEqual(m.projects.map((p) => p.id).sort(), ["e1", "e2"]);
  assert.deepEqual(m.recs.map((r) => [r.id, r.topics]), [["csr-EE-2022-1", [DIG]], ["csr-EE-2022-2", [EDU]]]);
  assert.deepEqual(m.topics, [DIG, EDU]);
  assert.equal(m.topic, "");
  const one = briefingModel(EE, { country: "EE", countryName: "Estonia", from: 2021, to: 2026, topic: EDU, areaLabel: (s) => s });
  assert.deepEqual(one.topics, [EDU]);
  assert.equal(one.topic, EDU);
  assert.deepEqual(one.recs.map((r) => r.id), ["csr-EE-2022-2"]);
});

test("compareModel: country x theme matrix, totals, shared gaps", () => {
  const cmp = compareModel([modelOf("EE", EE, [DIG, EDU]), modelOf("LV", LV, [DIG, EDU])], { topics: [DIG, EDU], topicLabels: { [DIG]: "Digital", [EDU]: "Education" } });
  assert.deepEqual(cmp.columns.map((c) => c.key), [DIG, EDU, "all"]);
  const ee = cmp.rows.find((r) => r.country === "EE");
  assert.deepEqual(ee.cells[EDU], { projects: 1, recs: 1, assessed: 1, supported: 0, rated: 1, progressing: 0, weakUnsupported: 1 });
  assert.deepEqual(ee.cells.all, { projects: 2, recs: 2, assessed: 2, supported: 1, rated: 2, progressing: 1, weakUnsupported: 1 });
  assert.equal(cmp.totals[EDU].recs, 3);
  assert.equal(cmp.totals[EDU].weakUnsupported, 2);
  assert.deepEqual(cmp.sharedGaps.map((g) => [g.topic, g.countries.map((c) => c.country)]), [[EDU, ["EE", "LV"]]]);
  assert.deepEqual(cmp.highlights, []); // fewer than 5 assessed recommendations per cell
  assert.equal(cmp.label, "Estonia and Latvia · Digital and Education");
});

test("compareModel: highlights compare cells with at least 5 assessed recommendations", () => {
  const many = (cc, n, ok) => rawOf(cc, [project(`${cc}p`, cc, EDU)], Array.from({ length: n }, (_, i) =>
    csr(`csr-${cc}-2022-${i + 1}`, 2022, ["Education"], i < ok ? "Some Progress" : "No Progress", i < ok ? [`${cc}p`] : [])));
  const cmp = compareModel([modelOf("EE", many("EE", 6, 5), [EDU]), modelOf("LV", many("LV", 5, 1), [EDU])], { topics: [EDU], topicLabels: { [EDU]: "Education" } });
  const h = cmp.highlights.find((x) => x.kind === "progressing" && x.topic === EDU);
  assert.deepEqual([h.high.country, h.high.pct, h.low.country, h.low.pct], ["EE", 83, "LV", 20]);
});

test("compareFacts: detail up to 3 countries, matrix only for many; always under FACTS_MAX", () => {
  const two = compareFacts(compareModel([modelOf("EE", EE, [EDU]), modelOf("LV", LV, [EDU])], { topics: [EDU], topicLabels: { [EDU]: "Education" } }));
  assert.equal(two.mode, "detail");
  assert.equal(two.countries_detail.length, 2);
  assert.ok(two.countries_detail[0].latest_document !== undefined);
  const codes = ["AT", "BE", "BG", "CY", "CZ", "DE", "DK", "EE", "ES", "FI", "FR", "GR", "HR", "HU", "IE", "IT", "LT", "LU", "LV", "MT", "NL", "PL", "PT", "RO", "SE", "SI", "SK"];
  const big = (cc) => rawOf(cc, Array.from({ length: 40 }, (_, i) => project(`${cc}${i}`, cc, [DIG, EDU, HEA][i % 3])),
    Array.from({ length: 30 }, (_, i) => csr(`csr-${cc}-2022-${i + 1}`, 2022, [["Education", "Healthcare", "Digitalisation of businesses"][i % 3]], "Limited Progress")));
  const all = compareFacts(compareModel(codes.map((cc) => modelOf(cc, big(cc), [DIG, EDU, HEA])), { topics: [DIG, EDU, HEA], topicLabels: {} }));
  assert.equal(all.mode, "matrix");
  assert.equal(all.matrix.length, 27);
  assert.ok(JSON.stringify(all).length <= FACTS_MAX);
  const six = compareFacts(compareModel(codes.slice(0, 6).map((cc) => modelOf(cc, big(cc), [DIG])), { topics: [DIG], topicLabels: {} }));
  assert.equal(six.mode, "summary");
  assert.ok(JSON.stringify(six).length <= FACTS_MAX);
});

test("compareFileName: group key or codes, occasion day and slug", () => {
  const day = new Date(2026, 10, 4);
  assert.equal(compareFileName(["EE", "LT", "LV"], { title: "Baltic panel", date: "2026-11-20" }, day), "briefing-baltic-2026-11-20-baltic-panel.docx");
  assert.equal(compareFileName(["FR", "GR"], { title: "" }, day), "briefing-FR-GR-2026-11-04.docx");
  assert.equal(compareFileName(["AT", "BE", "CY", "DE", "FR", "GR", "IT"], {}, day), "briefing-AT-BE-CY-DE-FR-2-more-2026-11-04.docx");
});
