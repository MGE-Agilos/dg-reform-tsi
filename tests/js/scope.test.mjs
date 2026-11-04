// Tests of the shared scope (countries x themes x years), pure module.
// Run: node --test tests/js/scope.test.mjs
import { test } from "node:test";
import assert from "node:assert/strict";
import { EU_CODES, COUNTRY_GROUPS, MAX_TOPICS, cleanScope, groupOf, scopeLabel, parseScope, scopeParams, scopeFromText, mergeScope }
  from "../../web/scope.js";

const NAMES = { EE: "Estonia", LV: "Latvia", LT: "Lithuania", GR: "Greece", PT: "Portugal", HR: "Croatia", FR: "France", IT: "Italy" };
const AREAS = { "digital-transition": "Digital transition", "green-transition": "Green transition",
  "skills-education-and-training": "Skills, education and training", "health-and-long-term-care": "Health and long-term care" };
const KEYWORDS = { "digital-transition": /\b(digital\w*)\b/i, "green-transition": /\b(energy|climate)\b/i,
  "skills-education-and-training": /\b(education|skills?)\b/i, "health-and-long-term-care": /\b(health\w*)\b/i };
const OPTS = { names: NAMES, areas: AREAS, keywords: KEYWORDS };

test("groups: EU-27 is every code, other groups are subsets, euro area has 21 members", () => {
  assert.equal(EU_CODES.length, 27);
  assert.deepEqual(COUNTRY_GROUPS.eu27.countries, EU_CODES);
  for (const g of Object.values(COUNTRY_GROUPS)) for (const c of g.countries) assert.ok(EU_CODES.includes(c), c);
  assert.equal(COUNTRY_GROUPS.euro.countries.length, 21);
  assert.deepEqual(COUNTRY_GROUPS.baltic.countries, ["EE", "LT", "LV"]);
});

test("cleanScope: unknown codes dropped, sorted, unique; at most MAX_TOPICS themes; years swapped when reversed", () => {
  assert.deepEqual(cleanScope({ countries: ["lv", "EE", "XX", "EE"], topics: ["a", "b", "c", "d", "e", "zz"], from: 2026, to: 2021 }, { topics: ["a", "b", "c", "d", "e"] }),
    { countries: ["EE", "LV"], topics: ["a", "b", "c", "d"], from: 2021, to: 2026 });
  assert.equal(MAX_TOPICS, 4);
  assert.deepEqual(cleanScope({}), { countries: [], topics: [], from: null, to: null });
});

test("groupOf: exact match of a group, else null", () => {
  assert.equal(groupOf(["LV", "EE", "LT"]), "baltic");
  assert.equal(groupOf(EU_CODES), "eu27");
  assert.equal(groupOf(["EE", "LV"]), null);
});

test("scopeLabel: group name, up to three names, else a count; themes joined", () => {
  const labels = { names: NAMES, areas: AREAS };
  assert.equal(scopeLabel({ countries: ["EE", "LT", "LV"], topics: [] }, labels), "Baltic States");
  assert.equal(scopeLabel({ countries: ["GR", "PT"], topics: ["digital-transition", "green-transition"] }, labels),
    "Greece and Portugal · Digital transition and Green transition");
  assert.equal(scopeLabel({ countries: ["FR", "GR", "IT", "PT"], topics: ["digital-transition"] }, labels), "4 countries · Digital transition");
  assert.equal(scopeLabel({ countries: ["FR", "GR", "IT"], topics: [] }, labels), "France, Greece and Italy");
});

test("parseScope / scopeParams: round trip, legacy country and area parameters", () => {
  const valid = { topics: Object.keys(AREAS) };
  const s = parseScope(new URLSearchParams("countries=LV,EE&topics=green-transition,digital-transition&from=2021&to=2026"), valid);
  assert.deepEqual(s, { countries: ["EE", "LV"], topics: ["green-transition", "digital-transition"], from: 2021, to: 2026 });
  assert.deepEqual(scopeParams(s), { countries: "EE,LV", topics: "green-transition,digital-transition", from: "2021", to: "2026" });
  assert.deepEqual(parseScope(new URLSearchParams("country=GR&area=digital-transition"), valid),
    { countries: ["GR"], topics: ["digital-transition"], from: null, to: null });
  assert.deepEqual(scopeParams({ countries: [], topics: [], from: null, to: null }), {});
});

test("scopeFromText: names in English and other languages, demonyms, CSR ids, groups", () => {
  assert.deepEqual(scopeFromText("Which projects support pension reforms in Portugal and Greece?", OPTS).countries, ["GR", "PT"]);
  assert.deepEqual(scopeFromText("Projets en Grèce et en Croatie", OPTS).countries, ["GR", "HR"]);
  assert.deepEqual(scopeFromText("Croatian and Italian reforms", OPTS).countries, ["HR", "IT"]);
  assert.deepEqual(scopeFromText("What supports csr-HR-2025-1?", OPTS).countries, ["HR"]);
  assert.deepEqual(scopeFromText("Compare the Baltic States", OPTS).countries, ["EE", "LT", "LV"]);
  assert.deepEqual(scopeFromText("de la France", OPTS).countries, ["FR"]);
  assert.deepEqual(scopeFromText("a question with DE and IT in capitals", OPTS).countries, []);
});

test("scopeFromText: themes by label then key words (first mentions first), years as a period", () => {
  const s = scopeFromText("Digital transition and climate in Estonia between 2021 and 2024, then health", OPTS);
  assert.deepEqual(s.topics, ["digital-transition", "green-transition", "health-and-long-term-care"]);
  assert.equal(s.from, 2021);
  assert.equal(s.to, 2024);
  assert.deepEqual(scopeFromText("Projects in 2023", OPTS), { countries: [], topics: [], from: 2023, to: 2023 });
  assert.deepEqual(scopeFromText("", OPTS), { countries: [], topics: [], from: null, to: null });
});

test("mergeScope: what the conversation names wins, the page fills the rest", () => {
  const page = { countries: ["FR"], topics: ["digital-transition"], from: 2020, to: 2025 };
  assert.deepEqual(mergeScope(page, { countries: ["GR", "PT"], topics: [], from: null, to: null }),
    { countries: ["GR", "PT"], topics: ["digital-transition"], from: 2020, to: 2025 });
  assert.deepEqual(mergeScope(page, { countries: [], topics: ["green-transition"], from: 2022, to: 2023 }),
    { countries: ["FR"], topics: ["green-transition"], from: 2022, to: 2023 });
});
