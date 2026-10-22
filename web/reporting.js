// Excel reporting export (Projects tab, Links to review): pure model of the rows of the workbook and its writing with
// SheetJS CE (loaded by app.js on first use). No DOM, no network, so Node can test this module.
// SheetJS CE writes neither cell styles nor frozen panes: the bold, frozen header row is added to the written file
// (styles.xml and the sheet XML) through SheetJS's own zip layer (XLSX.CFB).

export const SHEETJS_URL = "https://cdnjs.cloudflare.com/ajax/libs/xlsx/0.18.5/xlsx.full.min.js"; // pinned, loaded on first use
export const SHEETJS_SRI = "sha384-vtjasyidUo0kW94K5MXDXntzOJpQgBKXmE7e2Ga4LG0skTTLeBi97eFAXsqewJjw"; // of that exact file
export const REVIEW_LABELS = { confirmed: "Confirmed", rejected: "Rejected", to_review: "To review", none: "Not reviewed" };

const arr = (v) => (Array.isArray(v) ? v : []);
const num = (v) => { const n = Number(v); return Number.isFinite(n) ? n : 0; };
const str = (v) => (v === null || v === undefined ? "" : String(v));
const MAX_CELL = 32000;
const BASIS_LABELS = { full_text: "project text", title_only: "project title only" }; // Excel's limit is 32,767 characters per cell

/** Key of a project–CSR sub-part link (reviews, chips). */
export function reviewKey(projectId, partId) {
  return `${projectId}|${partId}`;
}

/** tsi-reporting-YYYY-MM-DD.xlsx (local date). */
export function reportFileName(date = new Date()) {
  const p = (n) => String(n).padStart(2, "0");
  return `tsi-reporting-${date.getFullYear()}-${p(date.getMonth() + 1)}-${p(date.getDate())}.xlsx`;
}

/** YYYY-MM-DD of a timestamp, or "" (dates are written as text: no time-zone shift in Excel). */
export function isoDay(value) {
  if (!value) return "";
  const d = new Date(value);
  return Number.isNaN(d.getTime()) ? "" : d.toISOString().slice(0, 10);
}

/** Latest scored analysis per project (the latest one when none is scored); rows in any order. */
function latestImpacts(rows) {
  const sorted = [...arr(rows)].sort((a, b) => str(b?.created_at).localeCompare(str(a?.created_at)) || num(b?.id) - num(a?.id));
  const out = new Map();
  for (const r of sorted) {
    if (!r || !r.project_id) continue;
    const had = out.get(r.project_id);
    if (!had || (!Number.isFinite(had.score) && Number.isFinite(r.score))) out.set(r.project_id, r);
  }
  return out;
}

/** "2 confirmed, 1 rejected, 3 not reviewed" over the links of a project. */
export function reviewSummary(statuses) {
  const counts = { confirmed: 0, rejected: 0, to_review: 0, none: 0 };
  for (const s of statuses) counts[Object.hasOwn(counts, s) ? s : "none"]++;
  return ["confirmed", "rejected", "to_review", "none"].filter((k) => counts[k])
    .map((k) => `${counts[k]} ${REVIEW_LABELS[k].toLowerCase()}`).join(", ");
}

/**
 * Rows of the three sheets.
 * data: { projects (rows of projects), links (project_csr_links with csr_parts(csr_id, csrs(country, year, number, title))),
 *         impacts (impact_reports), anomalies ({ project_id, type }), reviews (link_review_latest rows) or null when the
 *         review tables are not available }
 * opts: { date, filters: [[label, value]], country(code) -> name, area(slug) -> label, anomaly(type) -> label, detailUrl(id) -> URL }
 */
export function reportingModel(data, opts = {}) {
  const country = opts.country || ((c) => c);
  const area = opts.area || ((a) => a);
  const anomaly = opts.anomaly || ((t) => t);
  const detailUrl = opts.detailUrl || (() => "");
  const date = opts.date || new Date();
  const reviewsOn = Array.isArray(data.reviews);
  const projects = arr(data.projects).filter((p) => p && p.project_id);
  const ids = new Set(projects.map((p) => p.project_id));
  const reviews = new Map(arr(data.reviews).filter((r) => r && r.project_id && r.part_id).map((r) => [reviewKey(r.project_id, r.part_id), r]));
  const impacts = latestImpacts(data.impacts);
  const anomalies = new Map();
  for (const a of arr(data.anomalies)) {
    if (!a?.project_id || !ids.has(a.project_id)) continue;
    if (!anomalies.has(a.project_id)) anomalies.set(a.project_id, []);
    anomalies.get(a.project_id).push(a.type);
  }
  const byProject = new Map();
  const links = arr(data.links).filter((l) => l && ids.has(l.project_id))
    .sort((a, b) => str(a.project_id).localeCompare(str(b.project_id)) || str(a.part_id).localeCompare(str(b.part_id), "en", { numeric: true }));
  for (const l of links) {
    if (!byProject.has(l.project_id)) byProject.set(l.project_id, []);
    byProject.get(l.project_id).push(l);
  }
  const title = new Map(projects.map((p) => [p.project_id, p.title]));

  const projectHeader = ["Project ID", "Title", "Programme", "Year", "Countries", "Policy area", "Results", "Deliverables", "Detail page",
    "Project page (explorer)", "Source URL", "Linked CSR sub-parts", "Linked recommendations", "Recommendations", "Strongest link level",
    "Latest impact score", "Impact level", "Impact confidence", "Impact date", "Anomalies", "Anomaly types",
    ...(reviewsOn ? ["Link reviews"] : [])];
  const projectRows = projects.map((p) => {
    const own = byProject.get(p.project_id) || [];
    const recs = [...new Set(own.map((l) => l.csr_parts?.csr_id).filter(Boolean))].sort((a, b) => a.localeCompare(b, "en", { numeric: true }));
    const level = own.some((l) => l.level === "direct") ? "direct" : own.length ? "partial" : "";
    const imp = impacts.get(p.project_id);
    const types = anomalies.get(p.project_id) || [];
    return [
      p.project_id, str(p.title), str(p.programme), p.year ?? "", arr(p.countries).map(country).join(", "), area(p.policy_area) || "",
      str(p.results) || "not stated", p.deliverables ?? 0, p.detail_page ? "yes" : "no", detailUrl(p.project_id), str(p.url),
      own.length, recs.length, recs.join("; "), level,
      imp && Number.isFinite(imp.score) ? imp.score : "", imp ? str(imp.level) : "", imp ? str(imp.confidence) : "", imp ? isoDay(imp.created_at) : "",
      types.length, [...new Set(types)].map(anomaly).join("; "),
      ...(reviewsOn ? [own.length ? reviewSummary(own.map((l) => reviews.get(reviewKey(l.project_id, l.part_id))?.status || "none")) : ""] : []),
    ];
  });

  const linkHeader = ["Project ID", "Project title", "CSR sub-part", "Recommendation", "Country", "CSR year", "Link level", "Basis", "Rationale (AI)",
    ...(reviewsOn ? ["Review status", "Review comment", "Reviewer", "Review date"] : [])];
  const linkRows = links.map((l) => {
    const part = l.csr_parts || {};
    const csr = part.csrs || {};
    const r = reviews.get(reviewKey(l.project_id, l.part_id));
    return [
      l.project_id, str(title.get(l.project_id)), str(l.part_id), str(part.csr_id), csr.country ? country(csr.country) : "", csr.year ?? "",
      str(l.level), BASIS_LABELS[l.basis] || str(l.basis), str(l.rationale).slice(0, MAX_CELL),
      ...(reviewsOn ? [REVIEW_LABELS[r?.status] || REVIEW_LABELS.none, str(r?.comment).slice(0, MAX_CELL), str(r?.reviewer_email), isoDay(r?.created_at)] : []),
    ];
  });

  const notes = [
    ["Item", "Value"],
    ["Generated", `${isoDay(date)} ${date.toTimeString().slice(0, 5)}`],
    ["Exported by", str(opts.email)],
    ["Filters", arr(opts.filters).length ? arr(opts.filters).map(([k, v]) => `${k}: ${v}`).join("; ") : "none (all projects)"],
    ["Projects", projects.length],
    ["Project–CSR sub-part links", `${links.length} (Links sheet: every CSR sub-part link of the projects listed, whatever the country of the CSR)`],
    ["Project–CSR links", "Assessed by AI from the published project text, one CSR sub-part at a time (direct: the project designs or implements what the sub-part recommends; partial: it contributes to one aspect or a precondition). Not an official classification: verify before use. Projects known only by their title are not linked yet."],
    ["Link reviews", reviewsOn ? "Latest review of each link by the explorer's business users (Confirmed, Rejected, To review); a new review supersedes the older ones. Not reviewed: no review yet."
      : "Not available yet in this database (link review not deployed): the review columns are left out."],
    ["Results", "Status read from the heading of the results section of the project page (achieved: completed project presenting its results; expected: ongoing project). Not an assessment of success."],
    ["Impact score", "Latest saved impact analysis of the project, generated by AI (Claude, Anthropic) from the project record and a web search: score 0 to 10, level and confidence of the evidence. Few analyses so far: indicative only, verify the cited sources."],
    ["Anomalies", "Rule-based checks of the Commission's publication (no AI), not judgements on the projects."],
    ["Sources", "reforms-investments.ec.europa.eu (projects, detail pages, deliverables), SRSP list of approved requests, ECFIN CSR database, Commission 2026 country-specific recommendations."],
  ];
  return {
    fileName: reportFileName(date),
    sheets: [
      { name: "Projects", header: projectHeader, rows: projectRows, widths: [34, 60, 10, 6, 22, 30, 12, 12, 11, 40, 40, 12, 14, 40, 12, 12, 12, 12, 12, 10, 34, ...(reviewsOn ? [36] : [])] },
      { name: "Links", header: linkHeader, rows: linkRows, widths: [34, 50, 18, 16, 14, 9, 10, 18, 80, ...(reviewsOn ? [14, 50, 28, 12] : [])] },
      { name: "Notes", header: notes[0], rows: notes.slice(1), widths: [26, 120], noFilter: true },
    ],
  };
}

const te = new TextEncoder();
const td = new TextDecoder();

/** Bold font + frozen first row, added to the file SheetJS CE wrote (it ignores cell styles and panes). */
function styleHeaders(X, data, sheetCount) {
  const zip = X.CFB.read(data, { type: "array" });
  const get = (p) => { const e = X.CFB.find(zip, p); return e ? td.decode(e.content) : null; };
  const put = (p, s) => { const e = X.CFB.find(zip, p); e.content = te.encode(s); e.size = e.content.length; };
  let styles = get("/xl/styles.xml");
  if (!styles) return data;
  const fonts = /<fonts count="(\d+)">/.exec(styles);
  const xfs = /<cellXfs count="(\d+)">/.exec(styles);
  if (!fonts || !xfs) return data;
  const fontId = Number(fonts[1]);
  const xfId = Number(xfs[1]);
  styles = styles
    .replace(/<fonts count="\d+">([\s\S]*?)<\/fonts>/, (m, inner) => `<fonts count="${fontId + 1}">${inner}<font><b/><sz val="12"/><color theme="1"/><name val="Calibri"/><family val="2"/><scheme val="minor"/></font></fonts>`)
    .replace(/<cellXfs count="\d+">([\s\S]*?)<\/cellXfs>/, (m, inner) => `<cellXfs count="${xfId + 1}">${inner}<xf numFmtId="0" fontId="${fontId}" fillId="0" borderId="0" xfId="0" applyFont="1"/></cellXfs>`);
  put("/xl/styles.xml", styles);
  for (let i = 1; i <= sheetCount; i++) {
    const path = `/xl/worksheets/sheet${i}.xml`;
    let xml = get(path);
    if (!xml) continue;
    xml = xml.replace(/<sheetView workbookViewId="0"\s*\/>/, '<sheetView workbookViewId="0"><pane ySplit="1" topLeftCell="A2" activePane="bottomLeft" state="frozen"/><selection pane="bottomLeft" activeCell="A2" sqref="A2"/></sheetView>')
      .replace(/<row r="1"([^>]*)>([\s\S]*?)<\/row>/, (m, attrs, inner) => `<row r="1"${attrs}>${inner.replace(/<c r="([A-Z]+1)"(?![^>]*\ss=)/g, `<c r="$1" s="${xfId}"`)}</row>`);
    put(path, xml);
  }
  return X.CFB.write(zip, { fileType: "zip", type: "array", compression: true });
}

/** The .xlsx file (Uint8Array or ArrayBuffer) of a reporting model; X is the SheetJS namespace. */
export function buildWorkbook(X, model) {
  const wb = X.utils.book_new();
  for (const s of model.sheets) {
    const ws = X.utils.aoa_to_sheet([s.header, ...s.rows]);
    ws["!cols"] = s.widths.map((wch) => ({ wch }));
    if (!s.noFilter && s.rows.length) ws["!autofilter"] = { ref: X.utils.encode_range({ s: { r: 0, c: 0 }, e: { r: s.rows.length, c: s.header.length - 1 } }) };
    X.utils.book_append_sheet(wb, ws, s.name);
  }
  wb.Props = { Title: "TSI × CSR Explorer — reporting export", Author: "TSI × CSR Explorer" };
  const raw = new Uint8Array(X.write(wb, { bookType: "xlsx", type: "array", compression: true }));
  try {
    const styled = styleHeaders(X, raw, model.sheets.length);
    return styled instanceof Uint8Array || styled instanceof ArrayBuffer ? styled : new Uint8Array(styled);
  } catch {
    return raw; // header styling is cosmetic: the plain workbook is still valid
  }
}
