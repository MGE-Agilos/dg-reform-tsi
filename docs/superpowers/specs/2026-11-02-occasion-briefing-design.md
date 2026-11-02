# Occasion briefing — design

Date: 2026-11-02 · Branch: `feature/occasion-briefing` (from `feature/csr-documents`)

## Goal

Reshape the explorer's "Country briefing" Word export after a real European Commission briefing
("Judit Briefing EPSA 2025 2026.docx", project folder): a document written **for one person and one occasion**,
whose front part is what the reader uses on the day (scene setter, purpose, role, speaking points), and whose
data sits in background and annexes.

Today's document is a data report on a country and period: 7 numbered sections (key messages, key figures, CSRs,
lineage, results and impact, gaps, data quality) plus an annex, with AI key messages and per-section comments.

## Decisions

| Question | Decision |
|---|---|
| Use | Briefing for an occasion (meeting, panel, mission…), replacing the current format |
| Scope | Always one country (current model reused), optional policy area |
| Occasion input | A few fields plus a free-text box (invitation, agenda, participants, questions) |
| Latitude of speaking points | Facts from the data plus general policy framing without figures; figures checked after generation |
| Architecture | One AI call (`tsi-assistant`, service `briefing`), new response schema, document rebuilt |

## 1. Dialog (`web/app.js`, `briefingFormHtml` / `generateBriefing`)

Kept: country, from/to years, policy area (optional), "Include AI summary".

New "Occasion" block, above them:

| Field | Type | Rule |
|---|---|---|
| Recipient | text | required, ≤ 120 characters (e.g. "Director Judit Rozsa, DG REFORM A") |
| Occasion | text | required, ≤ 200 characters |
| Date | date | optional |
| Place | text | optional, ≤ 120 characters |
| Format | select | `bilateral`, `panel`, `speech`, `mission`, `conference`, `other` (default `other`) |
| Context | textarea | optional, ≤ 8 000 characters; hint "Paste the invitation, agenda, participants or announced questions" |

- Browser validation shows the message next to the field; the generation does not start.
- The occasion fields are kept in `sessionStorage` (try/catch) and restored when the dialog is opened again, so a
  failed generation does not lose the pasted context; they are cleared after a successful download.
- Labels: dialog title "Briefing", button "Prepare a briefing".

## 2. AI contract (`supabase/functions/tsi-assistant/index.ts`)

### Request

`{ service: "briefing", country, period, topic, facts, occasion: { recipient, title, date, place, format, context } }`

`validateBriefing` adds the occasion rules of section 1 (400 with a readable message). `MAX_FACTS` (25 000) is unchanged;
`context` is limited separately to 8 000 characters. `date` is `YYYY-MM-DD` or empty.

### Response schema (`BRIEFING_SCHEMA`, replaces `key_messages` + `comments`)

```
scene_setter:    string, 3–6 sentences
purpose:         string[], 2–5 bullets
role:            { summary: string, participants: [{ name, function }] }
speaking_points: [{ question: string ("" when none), points: string[] (3–6) }]  (2–5 blocks)
defensives:      [{ question, answer }]  (0–4)
background:      string, 3–6 sentences
```

### Prompt (`BRIEFING_SYSTEM`, rewritten)

- Figures, projects, recommendations, assessments and outcomes come only from FACTS; names, dates, places,
  participants and questions come only from OCCASION. Participants are listed only if the context names them.
- General policy framing is allowed (role of public administration in competitiveness, value of demand-driven
  support…), with no number and no named fact, consistent with EU policy.
- Speaking points: first person, short, sayable aloud. If the context announces questions, one block per question,
  in that order, with the question text; otherwise 2–3 blocks with `question: ""`.
- Defensives: sensitive points visible in FACTS (limited/no progress assessments, recommendations without support,
  high-severity anomalies), each with a cautious suggested answer. Empty when there are none.
- Background: Commission technical support to the country, from FACTS.
- Existing caveats kept: links assessed by AI, AI impact analyses few, results status not an assessment of success,
  Commission-stage recommendations not adopted, projects known by title only not linked; no causal wording
  ("support", never "implement"/"achieve").
- JSON object only. `max_tokens` ≈ 4 000, effort `low`, one call within `MODEL_TIMEOUT_MS`.

### Figure check

After parsing, every number in the response text (integers, decimals, percentages, years) must appear in
`JSON.stringify(facts)` or in the occasion fields (including the year of `date`), or be the rounded share of two
counts present in FACTS. Failing strings are returned as `unverified: string[]` (the sentences or bullets that hold
them); the response is still returned. Reply: `{ briefing, unverified, saved: false }`.

### Parsing

`parseBriefing` normalises the response: missing optional fields become empty values, list lengths are clipped to
the limits above. An empty `scene_setter` or an empty `speaking_points` is a failure (502, readable message).

## 3. Word document (`web/word-export.js`, `buildBriefingDocument`)

### Header

A one-cell bordered table, EC style:

> **Briefing for** <recipient>
> <occasion> — <place>, <date in long form>

Below it, one small metadata line: country, period, policy area, "Generated on … by …", and `BRIEFING_NOTICE` (unchanged).

### Body (unnumbered headings)

1. **Scene setter** — AI text. Without AI: "You will attend <occasion>[ on <date>][ in <place>]. Format: <format>."
2. **Purpose** — bullets.
3. **Role** — summary, then "You will be joined by:" and the participants (name, function) when there are any.
4. **Speaking points** — one Heading 2 per block ("Question n: <question>" or "Points n"), then bullets.
5. **Defensives** — "Q: <question>" in bold, then the answer.
6. **Background** — AI text, then 3–4 key figures built by the model (not the AI): number of projects in the period,
   share of assessed recommendations supported by a project, number of impact analyses and mean score if any.

AI parts keep today's marking (shading `AI_FILL`, label "AI ·"). A text listed in `unverified` gets
"⚠ figure not found in the data". Without AI, sections 2–5 read "Not generated (AI summary not requested)" or
"Not generated (AI summary unavailable: <reason>)", and the red banner reads "speaking points missing".
The body refers to the annexes where relevant ("see Annex C").

### Annexes (today's sections, code reused, renamed)

- Annex A — Key figures (projects by year and programme, by policy area)
- Annex B — Country-specific recommendations
- Annex C — Lineage: recommendations and supporting projects
- Annex D — Results and impact
- Annex E — Gaps and related past projects
- Annex F — Data quality
- Annex G — Sources and method

The policy-area filter note stays, at the top of Annex A.

### File name

`briefing-<ISO>-<occasion date or today, YYYY-MM-DD>-<occasion slug ≤ 60>.docx`
(e.g. `briefing-LT-2026-11-12-meeting-with-the-lithuanian-minister-of-finance.docx`). `briefingFileName` changes accordingly.

## 4. Facts (`web/briefing.js`)

`briefingModel` and `briefingFacts` are unchanged, except a new `keyFigures(model)` helper that returns the
background figures of section 3 (also used by the document, so the AI and the document read the same numbers).

## 5. Errors

| Case | Behaviour |
|---|---|
| Missing or too long occasion field | Browser message next to the field; edge function 400 |
| AI box unticked, or AI call failed / timed out | Document generated without AI parts (fallback scene setter, annexes, red banner) |
| Malformed or partial AI JSON | Normalised; empty scene setter or speaking points → treated as failed |
| Unverified figures | Document generated, texts flagged ⚠ |

## 6. Tests

- `supabase/functions/tsi-assistant/index.test.ts`: existing briefing tests adapted; new tests for occasion
  validation (required, lengths, format, date), the schema and prompt rules, the figure check (an invented number lands
  in `unverified`, a number from FACTS or from the date does not), parsing normalisation, `max_tokens`.
- `tests/js/briefing.test.mjs` (new): `briefingFileName` with the occasion, `keyFigures`.
- `tests/js/word-export-briefing.test.mjs` (new): document built with AI, without AI, with `unverified` texts;
  heading order Scene setter → Purpose → Role → Speaking points → Defensives → Background → Annex A … Annex G;
  the header table holds the recipient.

## 7. Documentation

Guide page "Country briefing" (`app.js`, guide entry `briefing`), the INFO entry and the tour step are rewritten for
the occasion briefing; glossary entry updated.

## Out of scope

Several countries or Europe-wide events, saving briefings in the database, editing in the app before export,
languages other than English.
