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
export function coverBlock(D, { title, subtitle, meta = [] }) {
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
      children: [new D.TextRun({ text: DISCLAIMER, bold: true, color: BLUE })],
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
