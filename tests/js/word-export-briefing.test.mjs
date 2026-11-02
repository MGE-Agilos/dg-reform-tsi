// Structure of the occasion briefing (Word), built with a fake docx namespace that records what is created.
// Run: node --test tests/js/word-export-briefing.test.mjs
import { test } from "node:test";
import assert from "node:assert/strict";
import { buildBriefingDocument } from "../../web/word-export.js";
import { briefingModel } from "../../web/briefing.js";

const ENUMS = new Set(["HeadingLevel", "BorderStyle", "ShadingType", "AlignmentType", "WidthType", "LevelFormat", "PageNumber"]);
const enumValue = new Proxy({}, { get: (_, k) => String(k) });
const classes = {};
const D = new Proxy({}, {
  get: (_, k) => (ENUMS.has(k) ? enumValue : (classes[k] ||= class {
    constructor(o) { this.kind = k; this.o = typeof o === "string" ? { text: o } : (o ?? {}); }
  })),
});

function textOf(node) {
  if (typeof node === "string") return node;
  if (Array.isArray(node)) return node.map(textOf).join("");
  if (!node || typeof node !== "object" || !node.kind) return "";
  if (node.kind === "TextRun") return (node.o.text ?? "") + textOf(node.o.children);
  return textOf(node.o.children) + textOf(node.o.rows);
}

const body = (doc) => doc.o.sections[0].children;
const headings = (doc) => body(doc).filter((n) => n.kind === "Paragraph" && n.o.heading === "HEADING_1").map(textOf);
const allText = (doc) => body(doc).map(textOf).join("\n");

const model = briefingModel({
  projects: [{ project_id: "p1", title: "Pension reform", programme: "TSI", year: 2023, countries: ["LT"], policy_area: "labour-market-and-social-protection",
    results: "achieved", deliverables: 2, url: "https://reform.example/p1", detail_page: true }],
  linked: [], docs: [], impacts: [], anomalies: [],
  csrs: [{ csr_id: "csr-LT-2023-1", year: 2023, number: "1", text: "Improve the pension system. More.", assessment: "Limited Progress", assessment_year: 2024,
    csr_parts: [{ part_id: "1.1", policy_areas: [], project_csr_links: [{ project_id: "p1", level: "direct" }] }] }],
}, { country: "LT", countryName: "Lithuania", from: 2021, to: 2026, topic: "", date: new Date(2026, 10, 2, 10) });

const OCCASION = { recipient: "Director Judit Rozsa", title: "Meeting with the Minister of Finance", date: "2026-11-12", place: "Vilnius", format: "bilateral", context: "" };
const AI = {
  scene_setter: "You will meet the Minister of Finance in Vilnius.",
  purpose: ["Take stock of the pension reform support."],
  role: { summary: "You open the meeting.", participants: [{ name: "Ana Horvat", function: "Minister of Finance" }] },
  speaking_points: [{ question: "How did the TSI help?", points: ["We supported 1 project.", "We supported 17 projects."] }, { question: "", points: ["Reforms need capable administrations."] }],
  defensives: [{ question: "Why so little support?", answer: "Support is demand-driven." }],
  background: "Lithuania had 1 project in the period.",
};
const ORDER = ["Scene setter", "Purpose", "Role", "Speaking points", "Defensives", "Background",
  "Annex A — Key figures", "Annex B — Country-specific recommendations", "Annex C — Lineage: recommendations and supporting projects",
  "Annex D — Results and impact", "Annex E — Gaps and related past projects", "Annex F — Data quality", "Annex G — Sources and method"];

test("with AI: header table, sections in the Commission order, questions and participants, annexes", () => {
  const doc = buildBriefingDocument(D, { model, occasion: OCCASION, ai: AI, unverified: [], email: "a@b.eu", date: model.date });
  assert.deepEqual(headings(doc), ORDER);
  const first = body(doc)[0];
  assert.equal(first.kind, "Table");
  assert.match(textOf(first), /^Briefing for Director Judit Rozsa/);
  assert.match(textOf(first), /Meeting with the Minister of Finance — Vilnius, 12 November 2026/);
  const text = allText(doc);
  assert.match(text, /Question 1: How did the TSI help\?/);
  assert.match(text, /Points 2/);
  assert.match(text, /Ana Horvat, Minister of Finance/);
  assert.match(text, /Q: Why so little support\?/);
  assert.match(text, /Projects in the period: 1 \(TSI 1\)/);
  assert.ok(!text.includes("⚠ figure not found")); // the Method annex mentions the ⚠ mark itself
  assert.equal(doc.o.title, "Briefing for Director Judit Rozsa — Meeting with the Minister of Finance");
});

test("unverified texts are flagged, and only them", () => {
  const doc = buildBriefingDocument(D, { model, occasion: OCCASION, ai: AI, unverified: ["We supported 17 projects."], date: model.date });
  const flagged = body(doc).map(textOf).filter((t) => t.includes("⚠ figure not found in the data"));
  assert.equal(flagged.length, 1);
  assert.match(flagged[0], /We supported 17 projects\./);
});

test("AI not requested: scene setter from the fields, four sections not generated, no AI label", () => {
  const doc = buildBriefingDocument(D, { model, occasion: OCCASION, ai: null, aiSkipped: true, date: model.date });
  assert.deepEqual(headings(doc), ORDER);
  const text = allText(doc);
  assert.match(text, /You will attend: Meeting with the Minister of Finance, on 12 November 2026, in Vilnius\. Format: bilateral meeting\./);
  assert.equal(text.split("Not generated (AI summary not requested).").length - 1, 4);
  assert.ok(!text.includes("AI · "));
  assert.ok(!text.includes("AI summary not available"));
});

test("AI failed: red banner and the reason in each missing section", () => {
  const doc = buildBriefingDocument(D, { model, occasion: { ...OCCASION, date: "", place: "", format: "other" }, ai: null, aiError: "The AI summary took too long.", date: model.date });
  const text = allText(doc);
  assert.match(text, /AI summary not available\. The AI summary took too long\./);
  assert.match(text, /Not generated \(AI summary unavailable: The AI summary took too long\)\./);
  assert.match(text, /You will attend: Meeting with the Minister of Finance\.\n/);
});
