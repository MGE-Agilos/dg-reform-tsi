// Word (.docx) export of the chat and of the impact reports, generated in the browser with docx-js.
// No DOM and no network here: the docx namespace (D) and marked's lexer are passed in, so Node can test the module.
// Markdown is mapped token by token to Word structures; raw HTML is never interpreted (tags dropped, text kept);
// only https: and mailto: links become hyperlinks.
// Layout in the spirit of the European Commission visual identity (Arial, blue #004494, A4, generous margins),
// without any logo, emblem, flag or wording that could make the document look official.

const BLUE = "004494";
const ACCENT = "FFD617";
const GREY = "6B6B6B";
const TEXT = "1B1F24";
const LIGHT_BLUE = "DCE6F2";
const PALE_BLUE = "EEF3FA";
const BORDER = "B8C4D2";
const FONT = "Arial";
const MONO = "Consolas";
const PAGE = { width: 11906, height: 16838, margin: 1417 }; // A4, 2.5 cm margins (twips)
export const CONTENT_WIDTH = PAGE.width - 2 * PAGE.margin; // 9072 twips
const HEADER_TEXT = "TSI × CSR Explorer — DG REFORM technical support projects";
const FOOTER_TEXT = "AI-generated content — verify before use · ";
const DISCLAIMER = "AI-generated: verify the cited sources before use.";
const MAX_HEADING = 4;
const LINK_URI = /^(?:https:|mailto:)/i;
const IMPACT_LINE = /impact\s*score\s*:\s*(\d{1,2})\s*\/\s*10\s*\(\s*(strong|moderate|weak)\s*\)\s*[—–-]\s*confidence\s*:\s*(high|medium|low)\b/i;

// ---------- small pure helpers ----------

const ENTITIES = { "&amp;": "&", "&lt;": "<", "&gt;": ">", "&quot;": '"', "&#39;": "'", "&#x27;": "'", "&nbsp;": " " };

/** Text of a marked 12 token (its tokenizer HTML-escapes text): back to plain characters. */
export function decode(text) {
  return String(text ?? "").replace(/&(?:amp|lt|gt|quot|nbsp|#39|#x27);/g, (e) => ENTITIES[e]);
}

/** Raw HTML is never interpreted: tags (and comments) are dropped, the remaining text kept. */
export function stripHtml(html) {
  return decode(String(html ?? "").replace(/<!--[\s\S]*?-->/g, "").replace(/<\/?[A-Za-z][^>]*>/g, ""));
}

export function safeLink(href) {
  const h = String(href ?? "").trim();
  return LINK_URI.test(h) ? h : null;
}

/** Report text without a leading YAML front matter (local reports start with one). */
export function stripFrontMatter(text) {
  return String(text ?? "").replace(/^﻿?---\r?\n[\s\S]*?\r?\n---[ \t]*(?:\r?\n|$)/, "");
}

/** Same reading as the edge function's parseImpact: the last "Impact score: X/10 (level) — Confidence: c" line. */
export function parseImpact(text) {
  const lines = String(text ?? "").split(/\r?\n/);
  for (let i = lines.length - 1; i >= 0; i--) {
    const m = IMPACT_LINE.exec(lines[i].replace(/[*_`]/g, ""));
    if (!m) continue;
    const score = Number(m[1]);
    if (score > 10) return null;
    return { score, level: m[2].toLowerCase(), confidence: m[3].toLowerCase() };
  }
  return null;
}

const pad = (n) => String(n).padStart(2, "0");
const ymd = (d) => `${d.getFullYear()}-${pad(d.getMonth() + 1)}-${pad(d.getDate())}`;

/** File-name part: letters, digits, - and _ only. */
export function sanitizeName(text, max = 80) {
  return String(text ?? "").normalize("NFKD").replace(/[̀-ͯ]/g, "")
    .replace(/[^A-Za-z0-9_-]+/g, "-").replace(/-{2,}/g, "-").replace(/^-+|-+$/g, "").slice(0, max).replace(/-+$/, "") || "report";
}

export function chatFileName(date = new Date()) {
  return `TSI-chat-${ymd(date)}-${pad(date.getHours())}${pad(date.getMinutes())}.docx`;
}

export function impactFileName(projectId, date = new Date()) {
  const d = date instanceof Date && !Number.isNaN(date.getTime()) ? date : new Date();
  return `TSI-impact-${sanitizeName(projectId)}-${ymd(d)}.docx`;
}

export function fmtDateTime(value) {
  const d = value instanceof Date ? value : new Date(value);
  if (Number.isNaN(d.getTime())) return "";
  return d.toLocaleString("en-GB", { day: "numeric", month: "long", year: "numeric", hour: "2-digit", minute: "2-digit" });
}

/** Column widths (twips) proportional to the text length of each column, summing to `total`. */
export function columnWidths(rows, total = CONTENT_WIDTH) {
  const n = Math.max(1, ...rows.map((r) => r.length));
  const weight = Array.from({ length: n }, (_, c) => {
    const lens = rows.map((r) => String(r[c] ?? "").length);
    const avg = lens.reduce((a, b) => a + b, 0) / Math.max(1, lens.length);
    return Math.min(60, Math.max(8, avg));
  });
  const sum = weight.reduce((a, b) => a + b, 0);
  const widths = weight.map((w) => Math.floor((total * w) / sum));
  widths[n - 1] += total - widths.reduce((a, b) => a + b, 0);
  return widths;
}

/** Plain text of inline tokens (for widths and alt texts). */
export function plainText(tokens) {
  return (tokens || []).map((t) => {
    if (t.type === "html") return stripHtml(t.text);
    if (t.tokens) return plainText(t.tokens);
    return decode(t.text ?? "");
  }).join("");
}

// ---------- Markdown tokens -> docx ----------

/** Conversion context: numbering instances used by ordered lists, heading offset, quote depth. */
export function newContext(opts = {}) {
  return { headingOffset: opts.headingOffset ?? 0, quote: 0, ordered: new Set(), instance: 0 };
}

function runProps(fmt) {
  const p = {};
  if (fmt.bold) p.bold = true;
  if (fmt.italics) p.italics = true;
  if (fmt.strike) p.strike = true;
  if (fmt.code) { p.font = MONO; p.size = 19; p.shading = { type: "clear", color: "auto", fill: "F1F3F5" }; }
  if (fmt.link) { p.color = BLUE; p.underline = { type: "single", color: BLUE }; }
  if (fmt.color) p.color = fmt.color;
  return p;
}

/** Inline tokens -> TextRun / ExternalHyperlink list. */
export function inlineRuns(tokens, D, fmt = {}) {
  const out = [];
  const text = (t, f = fmt) => { if (t) out.push(new D.TextRun({ text: t, ...runProps(f) })); };
  for (const t of tokens || []) {
    switch (t.type) {
      case "strong": out.push(...inlineRuns(t.tokens, D, { ...fmt, bold: true })); break;
      case "em": out.push(...inlineRuns(t.tokens, D, { ...fmt, italics: true })); break;
      case "del": out.push(...inlineRuns(t.tokens, D, { ...fmt, strike: true })); break;
      case "codespan": text(decode(t.text), { ...fmt, code: true }); break;
      case "br": out.push(new D.TextRun({ text: "", break: 1 })); break;
      case "html": text(stripHtml(t.text)); break;
      case "image": text(decode(t.text) ? `[${decode(t.text)}]` : ""); break;
      case "link": {
        const href = fmt.link ? null : safeLink(t.href);
        const children = inlineRuns(t.tokens?.length ? t.tokens : [{ type: "text", text: t.text }], D, href ? { ...fmt, link: true } : fmt);
        if (href) out.push(new D.ExternalHyperlink({ link: href, children }));
        else out.push(...children);
        break;
      }
      case "text":
        if (t.tokens?.length) out.push(...inlineRuns(t.tokens, D, fmt));
        else text(decode(t.text));
        break;
      default:
        if (t.tokens?.length) out.push(...inlineRuns(t.tokens, D, fmt));
        else text(decode(t.text ?? ""));
    }
  }
  return out;
}

function paragraph(D, ctx, children, extra = {}) {
  const quote = ctx.quote > 0 ? { style: "TsiQuote", indent: { left: 360 * ctx.quote } } : {};
  return new D.Paragraph({ ...quote, ...extra, children });
}

const HEADINGS = (D) => [D.HeadingLevel.HEADING_1, D.HeadingLevel.HEADING_2, D.HeadingLevel.HEADING_3, D.HeadingLevel.HEADING_4];

function headingPara(D, level, children) {
  return new D.Paragraph({ heading: HEADINGS(D)[Math.min(Math.max(level, 1), MAX_HEADING) - 1], children });
}

function codeParas(D, ctx, code) {
  const lines = decode(code).split(/\r?\n/);
  const runs = lines.map((line, i) => new D.TextRun({ text: line, break: i ? 1 : 0, font: MONO, size: 18 }));
  return [paragraph(D, ctx, runs, { style: "TsiCode" })];
}

function hrPara(D) {
  return new D.Paragraph({ border: { bottom: { style: D.BorderStyle.SINGLE, size: 6, color: BORDER, space: 1 } }, spacing: { after: 120 }, children: [] });
}

const cellBorders = (D) => {
  const b = { style: D.BorderStyle.SINGLE, size: 4, color: BORDER };
  return { top: b, bottom: b, left: b, right: b };
};

/** A Word table: rows of cell paragraphs-lists; first row as a light blue repeated header when `header`. */
export function makeTable(D, rows, { header = true, widths, alignments = [] } = {}) {
  const w = widths || columnWidths(rows.map((r) => r.map((c) => c.text)));
  const total = w.reduce((a, b) => a + b, 0);
  const align = { center: D.AlignmentType.CENTER, right: D.AlignmentType.RIGHT };
  return new D.Table({
    width: { size: total, type: D.WidthType.DXA },
    columnWidths: w,
    rows: rows.map((cells, ri) => new D.TableRow({
      tableHeader: header && ri === 0,
      cantSplit: true,
      children: w.map((width, ci) => {
        const cell = cells[ci] || { runs: [] };
        const isHead = (header && ri === 0) || cell.label;
        return new D.TableCell({
          width: { size: width, type: D.WidthType.DXA },
          borders: cellBorders(D),
          margins: { top: 60, bottom: 60, left: 100, right: 100 },
          shading: isHead ? { type: D.ShadingType.CLEAR, color: "auto", fill: LIGHT_BLUE } : undefined,
          children: [new D.Paragraph({
            spacing: { before: 0, after: 0 },
            alignment: align[alignments[ci]],
            children: cell.runs.length ? cell.runs : [new D.TextRun("")],
          })],
        });
      }),
    })),
  });
}

function tableFrom(D, t) {
  const head = t.header.map((c) => ({ text: plainText(c.tokens), runs: inlineRuns(c.tokens, D, { bold: true, color: BLUE }) }));
  const body = t.rows.map((r) => r.map((c) => ({ text: plainText(c.tokens), runs: inlineRuns(c.tokens, D) })));
  const all = [head, ...body];
  return [makeTable(D, all, { header: true, alignments: t.align || [] }), new D.Paragraph({ spacing: { after: 60 }, children: [] })];
}

function listParas(D, ctx, list, level) {
  const out = [];
  let ref = "tsi-bullets";
  let instance;
  if (list.ordered) {
    const start = Number.isInteger(list.start) && list.start > 0 ? list.start : 1;
    ctx.ordered.add(start);
    ref = `tsi-ordered-${start}`;
    instance = ++ctx.instance;
  }
  const lvl = Math.min(level, 8);
  for (const item of list.items || []) {
    let first = true;
    const prefix = item.task ? [new D.TextRun({ text: item.checked ? "☑ " : "☐ " })] : [];
    for (const tok of item.tokens || []) {
      if (tok.type === "list") { out.push(...listParas(D, ctx, tok, level + 1)); continue; }
      if (tok.type === "text" || tok.type === "paragraph") {
        const runs = [...(first ? prefix : []), ...inlineRuns(tok.tokens?.length ? tok.tokens : [{ type: "text", text: tok.text }], D)];
        out.push(first
          ? paragraph(D, ctx, runs, { numbering: { reference: ref, level: lvl, ...(instance ? { instance } : {}) }, spacing: { after: 60 } })
          : paragraph(D, ctx, runs, { indent: { left: 360 * (lvl + 1) + 360 * ctx.quote }, spacing: { after: 60 } }));
        first = false;
        continue;
      }
      if (tok.type === "space") continue;
      out.push(...blocksToDocx([tok], D, ctx));
      first = false;
    }
  }
  return out;
}

/** Block tokens (marked.lexer output) -> Paragraph / Table list. */
export function blocksToDocx(tokens, D, ctx = newContext()) {
  const out = [];
  for (const t of tokens || []) {
    switch (t.type) {
      case "heading": out.push(headingPara(D, t.depth + ctx.headingOffset, inlineRuns(t.tokens, D))); break;
      case "paragraph": out.push(paragraph(D, ctx, inlineRuns(t.tokens, D))); break;
      case "text": out.push(paragraph(D, ctx, inlineRuns(t.tokens?.length ? t.tokens : [{ type: "text", text: t.text }], D))); break;
      case "list": out.push(...listParas(D, ctx, t, 0)); break;
      case "blockquote": ctx.quote++; out.push(...blocksToDocx(t.tokens, D, ctx)); ctx.quote--; break;
      case "table": out.push(...tableFrom(D, t)); break;
      case "code": out.push(...codeParas(D, ctx, t.text)); break;
      case "hr": out.push(hrPara(D)); break;
      case "html": {
        const plain = stripHtml(t.text).trim();
        if (plain) out.push(paragraph(D, ctx, [new D.TextRun(plain)]));
        break;
      }
      case "space": case "def": break;
      default:
        if (t.tokens?.length) out.push(paragraph(D, ctx, inlineRuns(t.tokens, D)));
        else if (t.text) out.push(paragraph(D, ctx, [new D.TextRun(decode(t.text))]));
    }
  }
  return out;
}

/** Markdown text -> docx blocks (lexer = marked.lexer). */
export function markdownToDocx(text, D, lexer, ctx = newContext()) {
  return blocksToDocx(lexer(String(text ?? "")), D, ctx);
}

// ---------- document frame ----------

function numberingConfig(D, ctx) {
  const indent = (l) => ({ paragraph: { indent: { left: 360 * (l + 1), hanging: 300 } } });
  const bullets = ["•", "◦", "▪"];
  const ordered = [D.LevelFormat.DECIMAL, D.LevelFormat.LOWER_LETTER, D.LevelFormat.LOWER_ROMAN];
  const levels = (fmt, start) => Array.from({ length: 9 }, (_, l) => ({
    level: l, format: fmt(l), text: fmt(l) === D.LevelFormat.BULLET ? bullets[l % 3] : `%${l + 1}.`,
    alignment: D.AlignmentType.LEFT, start: l === 0 ? start : 1, style: indent(l),
  }));
  return [
    { reference: "tsi-bullets", levels: levels(() => D.LevelFormat.BULLET, 1) },
    ...[...ctx.ordered].map((s) => ({ reference: `tsi-ordered-${s}`, levels: levels((l) => ordered[l % 3], s) })),
  ];
}

function styles() {
  const heading = (id, name, size, level, extra = {}) => ({
    id, name, basedOn: "Normal", next: "Normal", quickFormat: true,
    run: { font: FONT, size, bold: true, color: extra.color || BLUE },
    paragraph: { spacing: { before: extra.before ?? 240, after: 100 }, keepNext: true, keepLines: true, outlineLevel: level, ...(extra.paragraph || {}) },
  });
  return {
    default: {
      document: { run: { font: FONT, size: 21, color: TEXT }, paragraph: { spacing: { after: 120, line: 276 } } },
      hyperlink: { run: { color: BLUE, underline: { type: "single", color: BLUE } } },
    },
    paragraphStyles: [
      { id: "Title", name: "Title", basedOn: "Normal", next: "Normal", quickFormat: true,
        run: { font: FONT, size: 44, bold: true, color: BLUE },
        paragraph: { spacing: { before: 0, after: 80 } } },
      { id: "Subtitle", name: "Subtitle", basedOn: "Normal", next: "Normal", quickFormat: true,
        run: { font: FONT, size: 26, color: GREY },
        paragraph: { spacing: { before: 0, after: 200 } } },
      heading("Heading1", "Heading 1", 32, 0, { before: 360, paragraph: { border: { bottom: { style: "single", size: 6, color: BLUE, space: 2 } } } }),
      heading("Heading2", "Heading 2", 26, 1, { before: 300 }),
      heading("Heading3", "Heading 3", 23, 2),
      heading("Heading4", "Heading 4", 21, 3, { color: "2B3A4F", before: 180 }),
      { id: "TsiQuote", name: "TSI Quote", basedOn: "Normal", next: "Normal",
        run: { italics: true, color: "4A5563" },
        paragraph: { border: { left: { style: "single", size: 12, color: BLUE, space: 8 } } } },
      { id: "TsiCode", name: "TSI Code", basedOn: "Normal", next: "Normal",
        run: { font: MONO, size: 18 },
        paragraph: { spacing: { line: 240 }, shading: { type: "clear", color: "auto", fill: "F1F3F5" } } },
      { id: "TsiMeta", name: "TSI Meta", basedOn: "Normal", next: "Normal",
        run: { size: 19, color: GREY }, paragraph: { spacing: { after: 40 } } },
    ],
  };
}

function headerFooter(D) {
  return {
    headers: { default: new D.Header({ children: [new D.Paragraph({
      border: { bottom: { style: D.BorderStyle.SINGLE, size: 4, color: BORDER, space: 4 } },
      children: [new D.TextRun({ text: HEADER_TEXT, size: 16, color: GREY })],
    })] }) },
    footers: { default: new D.Footer({ children: [new D.Paragraph({
      alignment: D.AlignmentType.CENTER,
      children: [new D.TextRun({ size: 16, color: GREY, children: [FOOTER_TEXT, "Page ", D.PageNumber.CURRENT, " of ", D.PageNumber.TOTAL_PAGES] })],
    })] }) },
  };
}

/** Cover block on page 1: title, subtitle, blue rule, meta lines, thin accent rule, shaded disclaimer box. */
export function coverBlock(D, { title, subtitle, meta = [], notice = DISCLAIMER }) {
  const lines = meta.filter(([, v]) => v).map(([k, v]) => new D.Paragraph({
    style: "TsiMeta",
    children: [new D.TextRun({ text: `${k}: `, bold: true, color: TEXT }), new D.TextRun(String(v))],
  }));
  return [
    new D.Paragraph({ style: "Title", children: [new D.TextRun(title || "Untitled")] }),
    new D.Paragraph({
      style: "Subtitle",
      border: { bottom: { style: D.BorderStyle.SINGLE, size: 18, color: BLUE, space: 6 } },
      children: [new D.TextRun(subtitle || "")],
    }),
    ...lines,
    new D.Paragraph({ border: { bottom: { style: D.BorderStyle.SINGLE, size: 6, color: ACCENT, space: 1 } }, spacing: { after: 160 }, children: [] }),
    new D.Paragraph({
      shading: { type: D.ShadingType.CLEAR, color: "auto", fill: PALE_BLUE },
      border: { left: { style: D.BorderStyle.SINGLE, size: 18, color: BLUE, space: 8 } },
      spacing: { before: 60, after: 240 },
      indent: { left: 160, right: 160 },
      children: [new D.TextRun({ text: notice, bold: true, color: BLUE })],
    }),
  ];
}

function makeDocument(D, { title, description, children, ctx }) {
  return new D.Document({
    creator: "TSI × CSR Explorer",
    title,
    description,
    styles: styles(),
    numbering: { config: numberingConfig(D, ctx) },
    sections: [{
      properties: { page: { size: { width: PAGE.width, height: PAGE.height }, margin: { top: PAGE.margin, right: PAGE.margin, bottom: PAGE.margin, left: PAGE.margin, header: 708, footer: 708 } } },
      ...headerFooter(D),
      children,
    }],
  });
}

// ---------- the two documents ----------

/** Conversation turns: each user message opens a turn, the following assistant message is its answer. */
export function chatTurns(messages) {
  const turns = [];
  for (const m of messages || []) {
    if (!m || typeof m.content !== "string" || !m.content.trim()) continue;
    if (m.role === "user") turns.push({ question: m.content, answer: null });
    else if (m.role === "assistant") {
      const last = turns[turns.length - 1];
      if (last && last.answer === null) last.answer = m.content;
      else turns.push({ question: null, answer: m.content });
    }
  }
  return turns;
}

/** Chat transcript document. opts: { messages, email, date } */
export function buildChatDocument(D, lexer, { messages, email, date = new Date() }) {
  const ctx = newContext({ headingOffset: 3 });
  const turns = chatTurns(messages);
  const children = [
    ...coverBlock(D, {
      title: "Conversation on TSI projects and CSRs",
      subtitle: "Chat transcript",
      meta: [
        ["Exported", fmtDateTime(date)],
        ["Exported by", email],
        ["Questions", String(turns.filter((t) => t.question).length)],
        ["Model", "Answers generated by AI (Claude, Anthropic) from project records, CSRs and AI-assessed links"],
      ],
    }),
    headingPara(D, 1, [new D.TextRun("Conversation")]),
  ];
  let n = 0;
  for (const t of turns) {
    if (t.question !== null) {
      n++;
      children.push(headingPara(D, 2, [new D.TextRun(`Question ${n}`)]));
      for (const line of t.question.split(/\r?\n/)) {
        children.push(new D.Paragraph({ children: [new D.TextRun({ text: line, italics: false })], spacing: { after: 60 } }));
      }
    }
    children.push(headingPara(D, 3, [new D.TextRun("Answer")]));
    if (t.answer === null) children.push(new D.Paragraph({ children: [new D.TextRun({ text: "No answer received.", italics: true, color: GREY })] }));
    else children.push(...markdownToDocx(t.answer, D, lexer, ctx));
  }
  return makeDocument(D, { title: "Chat transcript — TSI × CSR Explorer", description: "AI-generated chat transcript", children, ctx });
}

/** Impact analysis document. opts: { project, report, at, source, score, email, date }
 * project: { project_id, title, programme, year, countries (names), policy_area (label), url (https project page or null) }
 * score: { score, level, confidence } fallback when the report has no parseable score line. */
export function buildImpactDocument(D, lexer, { project = {}, report, at, source, score, email, date = new Date() }) {
  const ctx = newContext({ headingOffset: 0 });
  const body = stripFrontMatter(report);
  const s = parseImpact(body) || (score && Number.isFinite(score.score) ? score : null);
  const generated = fmtDateTime(at);
  const url = typeof project.url === "string" && project.url.startsWith("https://") ? project.url : null;
  const titleRuns = url
    ? [new D.ExternalHyperlink({ link: url, children: [new D.TextRun({ text: project.title || project.project_id || "", ...runProps({ link: true }) })] })]
    : [new D.TextRun(project.title || project.project_id || "")];
  const row = (label, value) => {
    if (!Array.isArray(value) && String(value ?? "").trim() === "") return null;
    return [{ text: label, label: true, runs: [new D.TextRun({ text: label, bold: true, color: BLUE })] },
      { text: "", runs: Array.isArray(value) ? value : [new D.TextRun(String(value))] }];
  };
  const info = [
    row("Project", titleRuns),
    row("Project ID", project.project_id),
    row("Programme", project.programme),
    row("Year", project.year),
    row("Countries", project.countries),
    row("Policy area", project.policy_area),
    row("Analysis generated", generated),
    row("Source", source),
  ].filter(Boolean);
  const scoreText = s
    ? `Impact score: ${s.score}/10${s.level ? ` (${s.level})` : ""}${s.confidence ? ` — Confidence: ${s.confidence}` : ""}`
    : "Impact score: not stated in the report";
  const children = [
    ...coverBlock(D, {
      title: project.title || project.project_id || "Impact analysis",
      subtitle: "Impact analysis",
      meta: [
        ["Exported", fmtDateTime(date)],
        ["Exported by", email],
        ["Analysis generated", generated],
        ["Source", source],
        ["Model", "AI-generated impact analysis (Claude, Anthropic) with web search"],
      ],
    }),
    headingPara(D, 2, [new D.TextRun("Project information")]),
    makeTable(D, info, { header: false, widths: [2400, CONTENT_WIDTH - 2400] }),
    new D.Paragraph({
      shading: { type: D.ShadingType.CLEAR, color: "auto", fill: LIGHT_BLUE },
      border: { left: { style: D.BorderStyle.SINGLE, size: 24, color: BLUE, space: 8 } },
      spacing: { before: 240, after: 240 },
      indent: { left: 160, right: 160 },
      children: [new D.TextRun({ text: scoreText, bold: true, size: 26, color: BLUE })],
    }),
    ...markdownToDocx(body, D, lexer, ctx),
    hrPara(D),
    new D.Paragraph({ style: "TsiMeta", children: [new D.TextRun(
      [generated ? `Analysis generated ${generated}` : "", source ? `source: ${source}` : ""].filter(Boolean).join(" · ") || "Generation date not recorded")] }),
  ];
  return makeDocument(D, { title: `Impact analysis — ${project.title || project.project_id || ""}`, description: "AI-generated impact analysis", children, ctx });
}

// ---------- country briefing ----------

export const BRIEFING_NOTICE = "Prepared with AI assistance from public European Commission sources; AI-generated parts are marked; verify before use.";
const AI_FILL = "FFF8DB"; // pale accent: AI-generated paragraphs stand out from the data
const AI_BORDER = "C9A227";
const AI_LABEL = "7A5C00";
const WEAK = /limited|no progress/i;

function fmtDay(value) {
  const d = value instanceof Date ? value : new Date(value);
  return Number.isNaN(d.getTime()) ? "" : d.toLocaleDateString("en-GB", { day: "numeric", month: "long", year: "numeric" });
}

function clipText(text, max) {
  const t = String(text ?? "").replace(/\s+/g, " ").trim();
  return t.length > max ? t.slice(0, max - 1).replace(/\s+\S*$/, "") + "…" : t;
}

/** A paragraph marked as AI-generated (label, shaded box with an accent border). */
function aiPara(D, text, label = "AI-generated comment") {
  return new D.Paragraph({
    shading: { type: D.ShadingType.CLEAR, color: "auto", fill: AI_FILL },
    border: { left: { style: D.BorderStyle.SINGLE, size: 18, color: AI_BORDER, space: 8 } },
    spacing: { before: 80, after: 160 },
    indent: { left: 160, right: 160 },
    children: [new D.TextRun({ text: `${label} · `, bold: true, size: 18, color: AI_LABEL }), new D.TextRun({ text: String(text) })],
  });
}

function noteLine(D, text, { italics = true } = {}) {
  return new D.Paragraph({ style: "TsiMeta", spacing: { after: 120 }, children: [new D.TextRun({ text: String(text), italics })] });
}

function linkRuns(D, text, url, extra = {}) {
  const u = typeof url === "string" && url.startsWith("https://") ? url : null;
  return u ? [new D.ExternalHyperlink({ link: u, children: [new D.TextRun({ text: String(text), ...runProps({ link: true }), ...extra })] })]
    : [new D.TextRun({ text: String(text), ...extra })];
}

const cellOf = (D, text, extra = {}) => ({ text: String(text ?? ""), runs: [new D.TextRun({ text: String(text ?? ""), ...extra })] });
const headOf = (D, text) => ({ text: String(text), runs: [new D.TextRun({ text: String(text), bold: true, color: BLUE })] });
const runsCell = (text, runs) => ({ text: String(text ?? ""), runs });
const spacer = (D) => new D.Paragraph({ spacing: { after: 60 }, children: [] });

/** Column widths (twips) from fractions of the content width. */
function cols(...fr) {
  const total = fr.reduce((a, b) => a + b, 0);
  const w = fr.map((f) => Math.floor((CONTENT_WIDTH * f) / total));
  w[w.length - 1] += CONTENT_WIDTH - w.reduce((a, b) => a + b, 0);
  return w;
}

const RESULTS_TEXT = { achieved: "Achieved", expected: "Expected", none: "Not stated" };
const scoreText = (imp) => (imp && Number.isFinite(imp.score) ? `${imp.score}/10` : "—");
const assessText = (r) => (r.assessment ? `${r.assessment}${r.assessmentYear ? ` (${r.assessmentYear})` : ""}` : "—");

/** "<Country> — Country briefing" file: built from briefing.js's model (data) and the AI summary (optional).
 * opts: { model, ai ({ key_messages, comments } or null), aiError (text), aiSkipped (summary turned off),
 *         email, date, labels: { anomaly(type) -> label } } */
export function buildBriefingDocument(D, { model: m, ai = null, aiError = "", aiSkipped = false, email = "", date = new Date(), labels = {} }) {
  const ctx = newContext();
  const anomalyLabel = labels.anomaly || ((t) => t);
  const comment = (key) => {
    const t = ai?.comments?.[key];
    return typeof t === "string" && t.trim() ? [aiPara(D, t.trim())] : [];
  };
  const bullet = (runs, extra = {}) => new D.Paragraph({ numbering: { reference: "tsi-bullets", level: 0 }, spacing: { after: 60 }, ...extra, children: runs });
  const small = { size: 18 };
  const period = m.from === m.to ? String(m.from) : `${m.from}–${m.to}`;
  const n = (v) => Number(v || 0).toLocaleString("en");
  const share = (a, b) => (b > 0 ? `${Math.round((100 * a) / b)}%` : "—");
  const children = [];

  // Cover
  children.push(...coverBlock(D, {
    title: `${m.countryName} — Country briefing`,
    subtitle: "Technical support (TSI/SRSP/SRSS) and country-specific recommendations",
    meta: [
      ["Period", `${period} (years of the projects and of the CSR documents)`],
      ["Policy area", m.topicLabel],
      ["Generated", fmtDateTime(date)],
      ["Generated by", email],
      ["AI summary", ai ? "Claude (Anthropic), written only from the figures of this document" : aiSkipped ? "not requested" : "not available"],
    ],
    notice: BRIEFING_NOTICE,
  }));
  if (!ai && !aiSkipped) {
    children.push(new D.Paragraph({
      shading: { type: D.ShadingType.CLEAR, color: "auto", fill: "FDE8E7" },
      border: { left: { style: D.BorderStyle.SINGLE, size: 18, color: "B42318", space: 8 } },
      indent: { left: 160, right: 160 }, spacing: { after: 200 },
      children: [new D.TextRun({ text: "AI summary not available. ", bold: true, color: "B42318" }),
        new D.TextRun(`${aiError ? `${aiError} ` : ""}This document contains the data sections only: the key messages and the AI comments are missing.`)],
    }));
  }
  if (m.topicMatch) {
    const t = m.topicMatch;
    children.push(noteLine(D, `Policy area filter "${m.topicLabel}": projects of that policy area; recommendations whose ECFIN policy areas `
      + `correspond to it (${t.areas}) or supported by a project of that area (${t.projects})`
      + (t.text ? `, or, not yet classified (2026), whose text mentions the topic (${t.text})` : "")
      + `; ${t.excluded} other recommendation${t.excluded === 1 ? "" : "s"} of the period left out.`));
  }

  // 1. Key messages
  children.push(headingPara(D, 1, [new D.TextRun("1. Key messages")]));
  if (ai?.key_messages?.length) {
    children.push(noteLine(D, "AI-generated from the figures and lists of this document: verify against the sections below and the sources."));
    for (const k of ai.key_messages) {
      children.push(bullet([new D.TextRun({ text: "AI · ", bold: true, size: 18, color: AI_LABEL }), new D.TextRun(String(k))],
        { shading: { type: D.ShadingType.CLEAR, color: "auto", fill: AI_FILL }, spacing: { after: 80 } }));
    }
  } else {
    children.push(noteLine(D, aiSkipped ? "AI summary not requested: see the figures below." : "AI summary not available: see the figures below."));
  }

  // 2. Key figures
  children.push(headingPara(D, 1, [new D.TextRun("2. Key figures")]));
  const scoredN = m.impacts.filter((p) => Number.isFinite(p.impact.score)).length;
  const sev = Object.fromEntries(m.anomalyCounts.map((a) => [a.key, a.n]));
  const row = (label, value) => [{ ...headOf(D, label), label: true }, cellOf(D, value)];
  children.push(makeTable(D, [
    row("Projects", `${n(m.projects.length)}${m.projects.length ? ` (${m.programmes.map((g) => [g, m.projects.filter((p) => p.programme === g).length]).filter(([, k]) => k).map(([g, k]) => `${g} ${k}`).join(", ")})` : ""}`),
    row("Results", `achieved ${n(m.results.achieved)} · expected ${n(m.results.expected)} · not stated ${n(m.results.none)}`),
    row("Deliverables", n(m.deliverables)),
    row("Project–CSR links", `${n(m.links.pairs)} recommendation–project pairs (${share(m.links.direct, m.links.pairs)} direct), ${n(m.links.projects)} projects — AI-assessed`),
    row("Recommendations supported", `${n(m.coverage.supported)} of ${n(m.coverage.recs)} assessed recommendations (${share(m.coverage.supported, m.coverage.recs)}) have at least one supporting project`),
    row("Impact analyses", m.impacts.length ? `${n(m.impacts.length)}${scoredN ? `, mean score ${(Math.round(m.impactMean * 10) / 10).toFixed(1)}/10` : ""} — AI-generated` : "none"),
    row("Anomalies", m.anomalies.length ? `${n(m.anomalies.length)} (${["high", "medium", "low", "info"].filter((k) => sev[k]).map((k) => `${k} ${sev[k]}`).join(", ")})` : "none"),
  ], { header: false, widths: cols(1, 2.6) }));
  children.push(noteLine(D, "Projects: those of the country whose year is in the period. Links: supporting projects of any year. Coverage excludes the Commission-stage recommendations (not yet adopted nor assessed)."));

  children.push(headingPara(D, 2, [new D.TextRun("Projects by year and programme")]));
  if (m.byYear.length) {
    const progs = m.programmes.filter((g) => m.byYear.some((y) => y[g]));
    const other = m.byYear.some((y) => y.other);
    const header = [headOf(D, "Year"), ...progs.map((g) => headOf(D, g)), ...(other ? [headOf(D, "Other")] : []), headOf(D, "Total")];
    const body = m.byYear.map((y) => [cellOf(D, y.year), ...progs.map((g) => cellOf(D, y[g] || "—")), ...(other ? [cellOf(D, y.other || "—")] : []), cellOf(D, y.total, { bold: true })]);
    const totals = [cellOf(D, "Total", { bold: true }), ...progs.map((g) => cellOf(D, m.projects.filter((p) => p.programme === g).length, { bold: true })),
      ...(other ? [cellOf(D, m.projects.filter((p) => !progs.includes(p.programme)).length, { bold: true })] : []), cellOf(D, m.projects.length, { bold: true })];
    const k = header.length;
    children.push(makeTable(D, [header, ...body, totals], { widths: cols(...Array.from({ length: k }, () => 1)), alignments: Array.from({ length: k }, (_, i) => (i ? "right" : undefined)) }), spacer(D));
  } else children.push(noteLine(D, "No project in the period."));

  children.push(headingPara(D, 2, [new D.TextRun("Projects by policy area")]));
  if (m.byArea.length) {
    children.push(makeTable(D, [[headOf(D, "Policy area"), headOf(D, "Projects"), headOf(D, "Share")],
      ...m.byArea.map((a) => [cellOf(D, a.label), cellOf(D, a.n), cellOf(D, share(a.n, m.projects.length))])],
    { widths: cols(4, 1, 1), alignments: [undefined, "right", "right"] }), spacer(D));
  } else children.push(noteLine(D, "No project in the period."));
  children.push(...comment("figures"));

  // 3. Country-specific recommendations
  children.push(headingPara(D, 1, [new D.TextRun("3. Country-specific recommendations")]));
  const recRuns = (r) => [new D.TextRun({ text: `${r.number}. `, bold: true, size: 19 }), new D.TextRun({ text: r.summary || "", size: 19 })];
  const shownDocs = m.docs.filter((d) => d.recs.length);
  if (!shownDocs.length) children.push(noteLine(D, "No recommendation in the period."));
  for (const d of shownDocs) {
    const latest = d === m.latest;
    children.push(headingPara(D, 2, [new D.TextRun(latest ? `Latest document: ${d.title}` : d.title)]));
    children.push(new D.Paragraph({ style: "TsiMeta", children: [
      new D.TextRun({ text: `${d.stage}${d.commission ? " — not yet adopted by the Council nor assessed" : ""}`, bold: true, color: TEXT }),
      new D.TextRun(` · ${d.recs.length === d.total ? `${d.total}` : `${d.recs.length} of ${d.total}`} recommendation${d.total === 1 ? "" : "s"}${d.url ? " · source: " : ""}`),
      ...(d.url ? linkRuns(D, /\.pdf(?:$|[?#])/i.test(d.url) ? "document (PDF)" : "ECFIN CSR database", d.url) : []),
    ] }));
    const rows = latest
      ? [[headOf(D, "Recommendation (first sentence)"), headOf(D, "Policy areas (ECFIN)"), headOf(D, d.commission ? "Stage" : "Latest assessment"), headOf(D, "Projects")],
        ...d.recs.map((r) => [runsCell(r.summary, recRuns(r)),
          cellOf(D, r.areas.length ? `${r.areas.slice(0, 3).join("; ")}${r.areas.length > 3 ? `; +${r.areas.length - 3} more` : ""}` : "not classified", small),
          cellOf(D, d.commission ? "not yet assessed" : assessText(r), small), cellOf(D, r.supporters.length)])]
      : [[headOf(D, "Recommendation (first sentence)"), headOf(D, "Latest assessment"), headOf(D, "Projects")],
        ...d.recs.map((r) => [runsCell(r.summary, recRuns(r)), cellOf(D, assessText(r), { ...small, bold: WEAK.test(r.assessment || "") }), cellOf(D, r.supporters.length)])];
    children.push(makeTable(D, rows, latest ? { widths: cols(5, 2.2, 1.6, 0.9), alignments: [undefined, undefined, undefined, "right"] }
      : { widths: cols(6.2, 2, 0.9), alignments: [undefined, undefined, "right"] }), spacer(D));
  }
  children.push(noteLine(D, "Assessments: the Commission's latest yearly assessment, from the ECFIN CSR database. Projects: distinct projects linked to the recommendation's sub-parts (AI-assessed)."));
  children.push(...comment("csrs"));

  // 4. Lineage
  children.push(headingPara(D, 1, [new D.TextRun("4. Lineage: recommendations and supporting projects")]));
  if (m.lineage.length) {
    const rows = [["Recommendation", "Supporting project", "Prog.", "Year", "Level", "Results", "Deliv.", "Impact"].map((h) => headOf(D, h))];
    for (const l of m.lineage) {
      l.projects.forEach((p, i) => rows.push([
        i ? cellOf(D, "") : runsCell(l.rec.id, [new D.TextRun({ text: l.rec.id, bold: true, ...small }), new D.TextRun({ text: ` — ${clipText(l.rec.summary, 80)}`, ...small })]),
        runsCell(p.title, linkRuns(D, p.title, p.detail ? p.url : null, small)), cellOf(D, p.programme, small), cellOf(D, p.year, small),
        cellOf(D, p.level, { ...small, bold: p.level === "direct" }), cellOf(D, RESULTS_TEXT[p.results], small), cellOf(D, p.deliverables, small), cellOf(D, scoreText(p.impact), small),
      ]));
    }
    children.push(makeTable(D, rows, { widths: cols(2.5, 4.6, 0.75, 0.7, 0.85, 1.05, 0.7, 0.8), alignments: [undefined, undefined, undefined, undefined, undefined, undefined, "right", "right"] }), spacer(D));
    children.push(noteLine(D, "One row per CSR Recommendation (one recommendation linked to one project); direct when at least one sub-part link is direct. Links assessed by AI from the project text, not an official classification."));
  } else children.push(noteLine(D, "No project is linked to a recommendation of the period."));
  children.push(...comment("lineage"));

  // 5. Results and impact
  children.push(headingPara(D, 1, [new D.TextRun("5. Results and impact")]));
  const withResults = m.projects.filter((p) => p.results !== "none");
  children.push(headingPara(D, 2, [new D.TextRun(`Projects with results (${withResults.length})`)]));
  if (withResults.length) {
    children.push(makeTable(D, [["Project", "Year", "Prog.", "Results", "Deliv."].map((h) => headOf(D, h)),
      ...withResults.map((p) => [runsCell(p.title, linkRuns(D, p.title, p.detail ? p.url : null, small)), cellOf(D, p.year, small), cellOf(D, p.programme, small),
        cellOf(D, RESULTS_TEXT[p.results], small), cellOf(D, p.deliverables, small)])],
    { widths: cols(6, 0.8, 0.9, 1.1, 0.8), alignments: [undefined, undefined, undefined, undefined, "right"] }), spacer(D));
    children.push(noteLine(D, "Results status from the heading of the results section of the project page (achieved: completed project; expected: ongoing). Not an assessment of success."));
  } else children.push(noteLine(D, "No project of the period states results."));
  children.push(headingPara(D, 2, [new D.TextRun(`Latest impact analyses (${m.impacts.length})`)]));
  if (m.impacts.length) {
    children.push(makeTable(D, [["Project", "Score", "Level", "Confidence", "Date", "Verdict (first sentence)"].map((h) => headOf(D, h)),
      ...m.impacts.map((p) => [runsCell(p.title, linkRuns(D, p.title, p.detail ? p.url : null, small)), cellOf(D, scoreText(p.impact), { ...small, bold: true }),
        cellOf(D, p.impact.level || "—", small), cellOf(D, p.impact.confidence || "—", small), cellOf(D, fmtDay(p.impact.date), small), cellOf(D, p.impact.verdict || "—", small)])],
    { widths: cols(2.6, 0.75, 1, 1.1, 1.2, 4.2) }), spacer(D));
    children.push(noteLine(D, "Impact analyses are AI-generated (Claude, Anthropic, with web search): latest analysis of each project; with few analyses the mean is only indicative."));
  } else children.push(noteLine(D, "No impact analysis for these projects yet."));
  children.push(...comment("results"));

  // 6. Gaps and opportunities
  children.push(headingPara(D, 1, [new D.TextRun("6. Gaps and opportunities")]));
  if (m.gaps.length) {
    const weak = m.gaps.filter((r) => WEAK.test(r.assessment || "")).length;
    children.push(new D.Paragraph({ children: [new D.TextRun(`${m.gaps.length} assessed recommendation${m.gaps.length === 1 ? "" : "s"} of the period ha${m.gaps.length === 1 ? "s" : "ve"} no supporting project`
      + (weak ? `; ${weak} of them assessed Limited or No progress (listed first, in bold).` : "."))] }));
    children.push(makeTable(D, [["Recommendation", "First sentence", "Latest assessment"].map((h) => headOf(D, h)),
      ...m.gaps.map((r) => {
        const w = WEAK.test(r.assessment || "");
        return [cellOf(D, r.id, { ...small, bold: w }), cellOf(D, r.summary, small), cellOf(D, assessText(r), { ...small, bold: w })];
      })], { widths: cols(1.7, 6, 1.7) }), spacer(D));
    children.push(noteLine(D, "No link does not prove no support: projects known only by their title are not linked yet, and links are AI-assessed."));
  } else children.push(noteLine(D, "Every assessed recommendation of the period has at least one supporting project."));
  const gapComment = ai?.comments?.gaps;
  if (typeof gapComment === "string" && gapComment.trim()) children.push(aiPara(D, gapComment.trim(), "AI-generated suggestions, to verify"));
  if (m.related?.length) {
    children.push(headingPara(D, 2, [new D.TextRun(`Latest recommendations (${m.latest?.year ?? ""}): related past projects (AI-suggested)`)]));
    children.push(noteLine(D, "Not yet supported by a project (too recent): past projects that worked on the same issues, suggested by AI, "
      + "same country first, at most 3 per recommendation. Leads to build on, not links assessed from the project text: verify before use."));
    const rows = [["Recommendation", "Related past project", "Scope", "Relevance", "Why (AI-suggested)"].map((h) => headOf(D, h))];
    for (const x of m.related) {
      x.projects.forEach((p, i) => rows.push([
        i ? cellOf(D, "") : runsCell(x.rec.id, [new D.TextRun({ text: x.rec.id, bold: true, ...small }),
          new D.TextRun({ text: ` — ${clipText(x.rec.summary, 70)}${x.total > x.projects.length ? ` (${x.total} suggested)` : ""}`, ...small })]),
        cellOf(D, `${p.title}${p.year ? ` (${p.programme ? `${p.programme} ` : ""}${p.year})` : ""}`, small),
        cellOf(D, p.scope === "same_country" ? "same country" : "other country", { ...small, bold: p.scope === "same_country" }),
        cellOf(D, p.relevance, small), cellOf(D, p.rationale || "—", small),
      ]));
    }
    children.push(makeTable(D, rows, { widths: cols(2.3, 3.2, 1.1, 0.9, 3.6) }), spacer(D));
  }

  // 7. Data quality
  children.push(headingPara(D, 1, [new D.TextRun("7. Data quality")]));
  if (m.anomalies.length) {
    children.push(new D.Paragraph({ children: [new D.TextRun(`${m.anomalies.length} publication anomal${m.anomalies.length === 1 ? "y" : "ies"} `
      + `(${m.anomalyCounts.map((a) => `${a.key} ${a.n}`).join(", ")})${m.anomalies.length > 20 ? "; the 20 most severe:" : ":"}`)] }));
    children.push(makeTable(D, [["Type", "Severity", "Project", "Year"].map((h) => headOf(D, h)),
      ...m.anomalies.slice(0, 20).map((a) => [cellOf(D, anomalyLabel(a.type), small), cellOf(D, a.severity, { ...small, bold: a.severity === "high" }),
        cellOf(D, a.title || a.project_id || "—", small), cellOf(D, a.year ?? "—", small)])], { widths: cols(2.2, 1, 5.2, 0.8) }), spacer(D));
    children.push(noteLine(D, "Rule-based checks of the Commission's publication (no AI), not judgements on the projects."));
  } else children.push(noteLine(D, "No publication anomaly for these projects."));

  // 8. Annex
  children.push(headingPara(D, 1, [new D.TextRun("Annex — sources and method")]));
  children.push(headingPara(D, 2, [new D.TextRun(`Sources (${m.sources.length})`)]));
  if (m.sources.length) {
    for (const s of m.sources) {
      const i = s.label.indexOf(": ");
      children.push(bullet([new D.TextRun({ text: `${s.label.slice(0, i)}: `, bold: true, ...small }), ...linkRuns(D, s.label.slice(i + 2) || s.url, s.url, small)]));
    }
  } else children.push(noteLine(D, "No source link."));
  children.push(headingPara(D, 2, [new D.TextRun("Method")]));
  for (const t of [
    "Projects: DG REFORM technical support projects (TSI, SRSP, SRSS) published on reforms-investments.ec.europa.eu, with the country among their beneficiaries and a year in the period.",
    "Results: status read from the project page (results achieved or expected); deliverables: publications attached to the project page.",
    "CSRs: country-specific recommendations of the European Semester; assessments and sub-parts from the ECFIN CSR database. 2026: Commission recommendations, not yet adopted by the Council nor assessed.",
    "Project–CSR links: assessed by an AI model from the project text, one CSR sub-part at a time (direct or partial); projects without a detail page are not linked yet.",
    "Impact analyses: AI-generated (Claude, Anthropic, with web search); latest analysis per project.",
    ...(m.related?.length ? ["Related past projects (latest recommendations): suggested by an AI model for each 2026 Commission recommendation (same country or other countries, high or medium relevance, with a rationale); leads, not assessed links."] : []),
    "AI summary: key messages and comments written by Claude (Anthropic) only from the figures and lists of this document, without web search. Verify before use.",
    ...(m.topicMatch ? [`Policy area filter: project policy area "${m.topicLabel}"; recommendations matched through the corresponding ECFIN labels (${m.topicMatch.labels.join("; ") || "none"}), through a supporting project of that area, or, when not yet classified, through key words of their text.`] : []),
  ]) children.push(bullet([new D.TextRun({ text: t, size: 19 })]));

  return makeDocument(D, { title: `${m.countryName} — Country briefing (${period})`, description: "Country briefing: data and AI-generated summary", children, ctx });
}
