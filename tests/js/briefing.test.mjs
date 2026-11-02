// Tests of the briefing helpers (pure module, no DOM).
// Run: node --test tests/js/briefing.test.mjs
import { test } from "node:test";
import assert from "node:assert/strict";
import { OCCASION_FORMATS, OCCASION_MAX, cleanOccasion, occasionErrors, briefingFileName, keyFigures } from "../../web/briefing.js";

const VALID = { recipient: "Director X", title: "Meeting with the Minister of Finance", date: "2026-11-12", place: "Vilnius", format: "bilateral", context: "" };

test("cleanOccasion: single-line fields squeezed, context keeps its lines, unknown format -> other", () => {
  assert.deepEqual(cleanOccasion({ recipient: "  Director   X ", title: "A\nB", date: " 2026-11-12 ", place: null, format: "party", context: " a\r\nb " }),
    { recipient: "Director X", title: "A B", date: "2026-11-12", place: "", format: "other", context: "a\nb" });
  assert.deepEqual(Object.keys(OCCASION_FORMATS), ["bilateral", "panel", "speech", "mission", "conference", "other"]);
});

test("occasionErrors: required fields, lengths and date, same rules as the edge function", () => {
  assert.deepEqual(occasionErrors(VALID), {});
  const e = occasionErrors({ ...VALID, recipient: "", title: "x".repeat(OCCASION_MAX.title + 1), date: "2026-02-30",
    place: "x".repeat(121), context: "x".repeat(8001) });
  assert.deepEqual(Object.keys(e), ["recipient", "title", "place", "date", "context"]);
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
