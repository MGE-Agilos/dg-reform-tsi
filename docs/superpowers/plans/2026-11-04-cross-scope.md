# Cross-scope Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** CSR progress for everyone, a shared country × theme scope, a comparison briefing over several countries and
themes, a Compare page, and a briefing launched from the chat (spec `docs/superpowers/specs/2026-11-04-cross-scope-design.md`).

**Architecture:** Pure ES modules in `web/` (`scope.js`, `compare.js`, extended `briefing.js`, `progress.js`) tested with
`node --test`; DOM and network in `web/app.js`; Word in `web/word-export.js`; AI contract in
`supabase/functions/tsi-assistant/index.ts` tested with `deno test`.

**Tech Stack:** Vanilla JS (ES modules, no build), Supabase JS, docx-js, Deno edge function, Claude structured output.

Test commands: `node --test tests/js/` · `deno test supabase/functions/tsi-assistant/index.test.ts` (if Deno is installed).

---

### Task 1: CSR progress for everyone (filters, explanation)

**Files:** Modify `web/progress.js`, `web/app.js` (CSR progress section, `route`), `web/index.html:42`, `web/style.css`;
Test `tests/js/progress.test.mjs`.

- [ ] Test: `progressModel(data, { countries: ["EE"] })` counts only EE recommendations; `{ areaLabels: ["Education"] }`
      keeps recommendations having one of those ECFIN labels; empty list / undefined = no filter.
- [ ] Implement the two options in `progressModel` (filter in the CSR loop).
- [ ] `index.html`: remove `hidden` from `#progress-tab`. `app.js`: delete `PROGRESS_OWNER_SHA256`, `progressOwnerCheck`,
      `isProgressOwner`, `paintProgressTab` and its callers; `route`: `else if (r === "progress") await renderProgress(p, seq)`.
- [ ] `renderProgress`: read `countries` (via `S.parseScope`, Task 2) and `area` (TSI policy area slug mapped with
      `B.TOPIC_CSR_AREAS`); add the scope picker (country groups select + countries multi-select) and policy area select
      to the filter form; pass to `progressModel`.
- [ ] Explanation panel "How it works · Why it matters · Read with care" (`<details open>` remembered in localStorage).
- [ ] Guide section `progress` and About text unchanged in substance; run tests; commit.

### Task 2: Shared scope module

**Files:** Create `web/scope.js`, `tests/js/scope.test.mjs`.

- [ ] Tests for `COUNTRY_GROUPS`, `groupOf`, `scopeLabel`, `parseScope`/`scopeParams` round trip (legacy `country`,
      `area`), `scopeFromText` (English names, "Grèce"/"Griechenland"/"Grecia", CSR ids, "Baltic", "euro area",
      policy area labels and keywords, years min/max), `mergeScope`.
- [ ] Implement; run; commit.

### Task 3: Scope picker UI (shared by briefing, progress, compare)

**Files:** Modify `web/app.js` (helpers section), `web/style.css`.

- [ ] `scopePickerHtml({ countries, topics }, { maxTopics, withTopics })`: a "Group" select (fills the countries),
      a countries `<select multiple size=6>` and, when `withTopics`, a themes `<select multiple>`; `readScopePicker(form)`.
- [ ] Group select `change` → select the group's countries (no form submit); filters forms send `countries` joined by commas.
- [ ] Commit.

### Task 4: Multi-theme briefing model and comparison model

**Files:** Modify `web/briefing.js`; Test `tests/js/briefing.test.mjs`.

- [ ] Tests: `briefingModel` with `topics: [a, b]` keeps projects of a or b, records `rec.topics`; `topic` still works.
      `compareModel` matrix numbers on a 2-country fixture; `highlights` ignores cells with < 5 assessed; `compareFacts`
      stays under `FACTS_MAX` for 27 synthetic countries and uses the detail level for ≤ 3.
- [ ] Implement `topics`, `compareModel(models, { topics, topicLabels, from, to, label })`, `compareFacts(cmp)`,
      `compareFileName(scope, occasion, today)`; run; commit.

### Task 5: AI contract for several countries and themes

**Files:** Modify `supabase/functions/tsi-assistant/index.ts`; Test `index.test.ts`.

- [ ] Tests: `validateBriefing` accepts `countries` (1–27, unique, EU codes) and `topics` (≤ 4), legacy `country`/`topic`
      still valid, 400 otherwise; user message lists countries and themes; `parseBriefing` returns `cross_insights` (≤ 5,
      [] when missing); `unverifiedTexts` checks `cross_insights`.
- [ ] Implement: `BriefingInput.countries`, `topics`; prompt block "Several countries or themes"; schema field
      `cross_insights`; `MAX_TOKENS.briefing` 5000; run; commit.

### Task 6: Comparison document

**Files:** Modify `web/word-export.js`; Test `tests/js/word-export-briefing.test.mjs`.

- [ ] Test: `buildCompareDocument` output contains "Cross-country insights", "Comparison", one "Annex — <country>" per
      country; the single-country `buildBriefingDocument` test stays green.
- [ ] Implement `buildCompareDocument(D, { cmp, occasion, ai, unverified, aiError, aiSkipped, email, date, labels })`
      reusing the front-part code (extract `briefingFront(D, …)` shared by both builders); run; commit.

### Task 7: Briefing dialog with the scope

**Files:** Modify `web/app.js` (briefing section).

- [ ] `briefingFormHtml(scope, { notice })` uses the scope picker (themes ≤ 4); `openBriefing(scopeOrCountry, opener, prefill)`.
- [ ] `generateBriefing`: one country and ≤ 1 theme → existing path; else per-country fetch (4 in parallel, progress
      "n of N countries"), `compareModel`, `compareFacts`, `callBriefing({ countries, topics, … })`, `buildCompareDocument`.
- [ ] Commit.

### Task 8: Compare page

**Files:** Create `web/compare.js`, `tests/js/compare.test.mjs`; Modify `web/app.js` (route, tab), `web/index.html`.

- [ ] Tests: `compareMatrix(data, { countries, topics, from, to, metric })` for each metric; `few` flag under 5 assessed;
      rows "themes × years" mode.
- [ ] Render `#compare`: filters (scope picker, metric, rows mode), heatmap SVG, table fallback, cell links to
      `#projects`/`#csrs`, "Prepare a briefing" for the scope. Commit.

### Task 9: Briefing from the chat

**Files:** Modify `web/app.js` (chat, assistant drawer).

- [ ] "Briefing" button in `.chat-actions`; enabled when an answer exists.
- [ ] Click → `S.scopeFromText(questions)` merged with the page scope (`currentPageScope()` from the route params) →
      `openBriefing(scope, button, { title, context, notice })`. Commit.

### Task 10: Guide, help texts, final checks

- [ ] Guide sections (compare, progress, briefing from chat, multi-country briefing); About texts; README section.
- [ ] Full test run; manual check in the browser if possible; commit.
