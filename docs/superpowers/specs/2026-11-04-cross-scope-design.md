# Cross-scope: several countries × several themes, briefing from the chat, CSR progress for all — design

Date: 2026-11-04 · Branch: `feature/cross-scope` (from `feature/csr-documents`)

## Goal

Client feedback (Notes, 4 Nov 2026):

1. The briefing must not be limited to one country: several countries and several themes, crossed to bring insight
   (e.g. 2 themes × 3 countries).
2. From the chat assistant, generate a briefing based on what was asked and on the current selections.
3. More ways of crossing the data in the app.
4. Open the CSR progress page to everybody, with an explanation of how it works and of its added value.

## Decisions (taken without client questions, see the conversation of 4 Nov)

| Question | Decision |
|---|---|
| What a multi-country briefing is | A **comparison**: one document crossing countries × themes (matrix, similarities, differences, speaking points on the group) |
| Country choice | Free multi-select **plus ready-made groups** (EU-27, euro area, cohesion countries, Baltic States, Nordic countries, Benelux, Visegrád Four, Southern Europe) that fill the selection |
| Themes | 0 to 4 TSI policy areas (0 = all) |
| Size | 1 to 27 countries; the AI digest gets coarser with the number of countries (detail ≤ 3, summary 4–8, matrix > 8) |
| One country, ≤ 1 theme | Unchanged document (current Commission layout); multi-scope only adds a comparison section |
| Briefing from the chat | A button in the assistant panel opens the briefing dialog **prefilled**: scope read from the conversation (countries, themes, years) merged with the current page selection, and the questions put in the Context field. No extra AI call; everything stays editable |
| "More combinations" | A new **Compare** page: country × theme matrix (heatmap) with a metric selector, cell → filtered lists, and "Prepare a briefing" for the selection. CSR progress gains country (with groups) and policy-area filters |
| CSR progress | Visible to every signed-in user; a "How it works · why it matters" panel at the top, guide section and an "About" text updated |

## Shared piece: the scope (`web/scope.js`, new, pure)

```
Scope = { countries: string[] (ISO, sorted, unique, 1..27), topics: string[] (policy-area slugs, 0..4), from: int, to: int }
```

- `COUNTRY_GROUPS`: `{ key: { label, countries } }`, the eight groups above (EU-27 = all of `COUNTRIES`).
  Euro area 2026: AT BE BG CY DE EE ES FI FR GR HR IE IT LT LU LV MT NL PT SI SK. Cohesion countries 2021–2027:
  BG CY CZ EE GR HR HU LT LV MT PL PT RO SI SK. Baltic: EE LV LT. Nordic: DK FI SE. Benelux: BE NL LU.
  Visegrád: CZ HU PL SK. Southern: CY ES GR IT MT PT.
- `groupOf(countries)`: the group key whose list equals the selection, else null (used for titles and file names).
- `scopeLabel(scope, names)`: "Baltic States" / "Estonia, Latvia and Lithuania" / "5 countries"; themes joined the same way.
- `parseScope(params)` / `scopeParams(scope)`: URL form `countries=EE,LV,LT&topics=a,b&from=2021&to=2026`
  (the existing single `country=` and `area=` parameters are still read).
- `scopeFromText(text, { countries, areas, keywords })`: deterministic reading of a conversation: country names
  (English, and the common French/German/Spanish forms), ISO codes in CSR ids (`csr-HR-2025-1`), group names
  ("Baltic", "euro area"…), policy areas by label and by `TOPIC_KEYWORDS`, years 2012–2030 (min/max). Returns a partial scope.
- `mergeScope(base, extra)`: the conversation wins for what it names; the page selection fills the rest.

## Part A — CSR progress for everyone

- Remove `PROGRESS_OWNER_SHA256`, `isProgressOwner`, `paintProgressTab`'s hiding: the tab is always shown.
- New panel at the top of `#progress` (closable; closed state kept in `localStorage`, try/catch):
  **How it works**: the Commission assesses each year how far each CSR was implemented (ECFIN database); the page
  splits assessed recommendations into those supported by at least one TSI/SRSP project (AI-assessed links) and the
  others, and compares the share progressing (some progress or better).
  **Why it matters**: shows where technical support meets the reforms the Council asked for, where reforms advance
  without support, and where recommendations stall without support (candidates for future requests).
  **Read with care**: association, not cause; links are AI-assessed; small groups greyed; same-year gap explained.
- Filters added: countries (multi-select with groups) and policy area (ECFIN labels mapped through `TOPIC_CSR_AREAS`).
  `progressModel` gets `countries` and `areaLabels` options (pure, tested).
- Guide: a "CSR progress" section and screenshot slot; tour step added; "About this comparison" text kept.

## Part B — multi-country, multi-theme briefing

### Model (`web/briefing.js`)

- `briefingModel` takes `topics: string[]` instead of `topic` (a recommendation or project matches when it matches
  any theme; each recommendation records `topics: string[]` it matched). `topic` still accepted (one-item list).
- New `compareModel(models, scope)`: one per-country model per country, plus
  - `matrix[country][topic|"all"]`: projects, recommendations, assessed, supported, progressing (rank ≥ some progress),
    weak (limited/no progress) without support;
  - `totals` per theme and for the group; `highlights`: the 5 largest differences between countries on the share
    supported and on the share progressing (only cells with ≥ 5 assessed recommendations), and recommendations
    shared by theme across ≥ 2 countries without support.
- `compareFacts(cmp)`: ≤ 3 countries → per-country `briefingFacts` at a reduced level + matrix; 4–8 → matrix + per
  country 3 gaps and 3 top projects; > 8 → matrix + totals + highlights only. Same 24 000-character target ladder.

### Dialog (`web/app.js`)

- "Country" select replaced by a **scope picker**: country chips (multi-select list with search) + group buttons;
  themes as a multi-select (max 4). Years unchanged.
- Data: `fetchBriefingData` per country, at most 4 in parallel; progress shown "Collecting data (3 of 8 countries)…".
- One country and ≤ 1 theme → current path, byte for byte. Otherwise → comparison path.

### AI (`supabase/functions/tsi-assistant`)

- Request: `countries: string[]` (1–27) or legacy `country`; `topics: string[]` (0–4) or legacy `topic`.
- Prompt: the existing one, plus a "When several countries or themes" block: write for the group; compare factually
  (no ranking of Member States, no "best"/"worst"), name a country only with a figure or fact from FACTS; speaking
  points on shared challenges and differences; defensives may include "why does country X get less support".
- Schema: new required field `cross_insights: string[]` (0–5 bullets, patterns across countries/themes, from FACTS;
  empty for one country and one theme). `max_tokens` 5 000.
- Number check unchanged (matrix numbers are in FACTS).

### Document (`web/word-export.js`)

- Comparison path: front part as today (scene setter … defensives), then **Cross-country insights** (AI bullets),
  **Comparison** tables (country × theme: projects; supported / assessed; progressing %), group key figures, and
  annexes per country condensed (key figures, latest recommendations, gaps), full lists only up to 3 countries.
- File name: `briefing-<group key or ISO codes joined, max 5 then "+n">-<day>-<slug>.docx`.

## Part C — briefing from the chat

- A "Briefing" button in the assistant panel footer (enabled when the conversation has an answer).
- On click: `scopeFromText` over the user questions → merged with the current page scope (route params) → the
  briefing dialog opens with that scope; the occasion's Context is prefilled with
  "Questions raised with the assistant:\n- …" (user questions, last 5, clipped to fit 8 000 characters) and the
  title with the first question clipped to 200 characters. A notice says "Scope read from the conversation: check it".
- Nothing else changes: the briefing data still come from the database, not from the chat answers.

## Part D — Compare page (`#compare`, `web/compare.js` pure + render in `app.js`)

- Filters: scope picker (countries/groups, themes, years) and **metric**: projects · recommendations · share
  supported · share progressing (assessed) · supported but weak · not supported and weak.
- Rows = countries, columns = themes (all 10 when none selected) + total; or switch "rows: themes, columns: years".
- Heatmap SVG (dataviz method, sequential palette; cells with too few recommendations hatched and greyed).
- Cell click → projects list or CSR list with the same filters; "Prepare a briefing" button with the scope.
- Data: same tables as CSR progress (cached) plus projects `countries,policy_area,year`.

## Error handling

- Scope invalid (0 countries, > 4 themes) → message next to the picker, no generation.
- One country fetch fails → the briefing fails with the country named (no partial document).
- Edge function: 400 on unknown country codes, too many countries/themes; legacy body still valid.

## Testing

- `tests/js/scope.test.mjs`: groups, parse/serialise, `scopeFromText` (names, CSR ids, groups, keywords, years), merge.
- `tests/js/briefing.test.mjs`: multi-topic matching, `compareModel` matrix and highlights, `compareFacts` size ladder.
- `tests/js/progress.test.mjs`: country and area filters.
- `tests/js/compare.test.mjs`: metrics and small-cell flag.
- `tests/js/word-export-briefing.test.mjs`: comparison document sections; single-country document unchanged.
- `supabase/functions/tsi-assistant/index.test.ts`: validation (countries/topics, legacy), prompt block, `cross_insights`.

## Order

A (small, ships alone) → shared scope → B → D → C.
