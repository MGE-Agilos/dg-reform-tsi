// TSI x CSR Explorer: static page over Supabase (RLS: signed-in testers only).
// Every database string goes through esc() before being injected; only https:// URLs become links.
// Chat and Impact answers (Markdown) are inserted only after marked + DOMPurify; without them, as escaped text.
import { SUPABASE_URL, SUPABASE_ANON_KEY, PROXY_URL } from "./config.js";
import * as B from "./briefing.js";
import * as R from "./reporting.js";
import * as A from "./admin.js";
import * as T from "./tracking.js";
import * as PG from "./progress.js";

const SUPABASE_JS = "https://cdn.jsdelivr.net/npm/@supabase/supabase-js@2.45.4/+esm";
const MARKED_JS = "https://cdn.jsdelivr.net/npm/marked@12.0.2/lib/marked.esm.js";
const PURIFY_JS = "https://cdn.jsdelivr.net/npm/dompurify@3.1.6/dist/purify.es.mjs";
const DOCX_JS = "https://cdn.jsdelivr.net/npm/docx@9.5.1/+esm"; // Word export, loaded on first use
const PROXY_TIMEOUT_MS = 160000; // above tsi-assistant's 150 s deadline (and haystack-proxy's 130 s), so their errors arrive first
const MD_TAGS = ["p", "a", "ul", "ol", "li", "strong", "em", "b", "i", "code", "pre", "blockquote",
  "h1", "h2", "h3", "h4", "table", "thead", "tbody", "tr", "th", "td", "hr", "br"];
const MD_ATTRS = ["href", "title", "target", "rel"];
const MD_URI = /^(?:https:|mailto:|#)/i;
const MAX_CONTENT = 8000; // per message, as enforced by tsi-assistant (and haystack-proxy)
const CHAT_MAX = 20; // messages kept (and sent), oldest dropped first
const CHAT_KEY = "tsi-chat-history";
const IMPACT_KEY = "tsi-impact-report:";
const CHAT_EXAMPLES = [
  "Which countries have projects on wildfires or forest fires?",
  "What does the Spanish primary care project cover, and which CSRs is it linked to?",
  "What CSRs did Croatia receive on public administration since 2022, and how were they assessed?",
  "Which projects support Croatia's 2022 country-specific recommendations?",
];
const PAGE_SIZE = 50;
const CSV_LIMIT = 5000;
const CSV_PAGE = 1000;

const COUNTRIES = {
  AT: "Austria", BE: "Belgium", BG: "Bulgaria", CY: "Cyprus", CZ: "Czechia", DE: "Germany", DK: "Denmark",
  EE: "Estonia", ES: "Spain", FI: "Finland", FR: "France", GR: "Greece", HR: "Croatia", HU: "Hungary",
  IE: "Ireland", IT: "Italy", LT: "Lithuania", LU: "Luxembourg", LV: "Latvia", MT: "Malta", NL: "Netherlands",
  PL: "Poland", PT: "Portugal", RO: "Romania", SE: "Sweden", SI: "Slovenia", SK: "Slovakia",
};
const PROGRAMMES = ["TSI", "SRSP", "SRSS"];
const POLICY_AREAS = {
  "competitiveness": "Competitiveness",
  "digital-transition": "Digital transition",
  "financial-sector-and-access-finance": "Financial sector and access to finance",
  "green-transition": "Green transition",
  "health-and-long-term-care": "Health and long-term care",
  "labour-market-and-social-protection": "Labour market and social protection",
  "migration-management": "Migration management",
  "public-administration-and-governance": "Public administration and governance",
  "revenue-administration-and-public-financial-management": "Revenue administration and public financial management",
  "skills-education-and-training": "Skills, education and training",
};
const ANOMALY_TYPES = {
  tsi_not_published: "TSI project not published",
  srsp_title_only: "SRSP known by title only",
  published_no_deliverable: "Published without deliverable",
  missing_results: "Missing results",
  orphan_deliverable: "Orphan deliverable",
  possible_duplicate: "Possible duplicate",
  no_csr_link: "No CSR link",
};
// Severity filter: by default only high and medium (low/info are numerous and informational).
// Server-side order is order("severity") then order("type"): alphabetical, which equals the rank order
// (high, medium) for the default filter; with "All severities" it is high, info, low, medium (shown in the UI).
const SEVERITY_OPTIONS = { "": "High and medium", all: "All severities", high: "High", medium: "Medium", low: "Low", info: "Info" };

const $ = (id) => document.getElementById(id);
let sb = null;
let testerCache = null; // { userId, ok }
let renderSeq = 0;
let authError = "";
let currentUid; // user id of the last rendered session (undefined before the first render)
let lastViewKey = null; // route + id of the last rendered view: focus moves only when it changes
let pendingFocus = null; // name of the filter control that triggered the render
let markdown = null; // { marked, purify } once loaded
let markdownLoading = null; // Promise while loading
let aiEpoch = 0; // bumped on sign-out: late answers of the previous user are dropped
let chatHistory = null; // [{ role, content }], mirrored in sessionStorage
let chatPending = false; // a chat question is waiting for its answer
let chatError = ""; // last chat error (not stored)
let chatDraft = ""; // text being typed, kept across re-renders
const impactPending = new Set(); // project ids whose analysis is running
const impactReports = new Map(); // project id -> { report, at }, mirrored in sessionStorage
const impactErrors = new Map(); // project id -> last error message
let wordLib = null; // { D: docx namespace, W: ./word-export.js, lexer } once loaded
let wordLoading = null; // Promise while loading
let currentEmail = ""; // e-mail of the signed-in user (Word export "Exported by")
let impactProject = null; // project shown on the Impact or project page (Word export of its reports)

// ---------- helpers ----------

function esc(value) {
  return String(value ?? "").replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" })[c]);
}

function safeUrl(url) {
  return typeof url === "string" && url.startsWith("https://") ? url : null;
}

function extLink(url, label) {
  const u = safeUrl(url);
  return u ? `<a href="${esc(u)}" target="_blank" rel="noopener noreferrer">${esc(label ?? u)}</a>` : esc(label ?? "");
}

function hashLink(route, id, label) {
  return `<a href="#${route}/${encodeURIComponent(id)}">${esc(label)}</a>`;
}

function deslug(text) {
  const t = String(text ?? "").replace(/[-_]+/g, " ").trim();
  return t ? t[0].toUpperCase() + t.slice(1) : "";
}

/**
 * What a project's source URL is: a real project page, or the listing where only its title appears
 * (TSI country factsheet, SRSP PDF list of approved requests, policy-area or other listing page).
 * listing: true forces the listing reading (project without a detail page). Returns null for a non-https URL.
 */
function sourceInfo(url, { listing = false } = {}) {
  const u = safeUrl(url);
  if (!u) return null;
  let path;
  try { path = new URL(u).pathname; } catch { return null; }
  let last = path.split("/").filter(Boolean).pop() || "";
  try { last = decodeURIComponent(last); } catch { /* keep raw */ }
  last = last.replace(/\.pdf$/i, "").replace(/_[a-z]{2}$/, "");
  const sheet = /\/tsi-country-factsheets\/([^/]+?)(?:_[a-z]{2})?\/?$/i.exec(path);
  if (sheet) {
    const slug = sheet[1].toLowerCase();
    const name = Object.values(COUNTRIES).find((n) => n.toLowerCase().replace(/\s+/g, "-") === slug) || slug;
    return { url: u, listing: true, label: `Listed in: ${name} country factsheet` };
  }
  if (/\.pdf$/i.test(path)) return { url: u, listing: true, label: "Listed in: SRSP list of approved requests (PDF)" };
  const area = Object.hasOwn(POLICY_AREAS, last);
  if (area || /\/our-projects\//.test(path) || listing) {
    return { url: u, listing: true, label: `Listed in: ${area ? POLICY_AREAS[last] : deslug(last) || "listing"} page` };
  }
  return { url: u, listing: false, label: "Project page" };
}

function sourceLink(info) {
  return info ? `${extLink(info.url, info.label)} ↗` : "";
}

/** Project page: the Commission's page, or (no detail page) the listing it is known from. */
function projectSourceHtml(r) {
  const src = sourceInfo(r.url, { listing: !r.detail_page });
  if (!src) return "";
  if (!src.listing) return `<p>${extLink(src.url, "Project page")} ↗</p>`;
  return `<p>${sourceLink(src)}${r.detail_page ? ""
    : `<br><span class="muted small">No project page published by the Commission; known only from this listing.</span>`}</p>`;
}

/** Anomalies table, title cell: a listing URL is shown as the source, not as the project's link. */
function anomalyTitleCell(r) {
  const src = sourceInfo(r.url);
  if (!src?.listing) {
    return r.project_id ? hashLink("project", r.project_id, r.title || r.project_id) : extLink(r.url, r.title || r.url || "—");
  }
  const title = r.project_id ? hashLink("project", r.project_id, r.title || r.project_id) : esc(r.title || "—");
  const note = ["tsi_not_published", "srsp_title_only"].includes(r.type) ? ` <span class="muted">· no project page published</span>` : "";
  return `${title}<div class="small source-line">${sourceLink(src)}${note}</div>`;
}

/** Current route: { route, id, params } from location.hash. */
function params() {
  const raw = location.hash.replace(/^#/, "");
  const [path, query = ""] = raw.split("?");
  const [route, ...rest] = path.split("/");
  let id = rest.join("/");
  try { id = decodeURIComponent(id); } catch { /* keep raw */ }
  return { route: route || "projects", id, params: new URLSearchParams(query) };
}

/** Navigate to a list route with the given filters (empty values dropped). */
function setParams(route, values) {
  const qs = new URLSearchParams();
  for (const [k, v] of Object.entries(values)) {
    if (v !== undefined && v !== null && v !== "" && v !== false) qs.set(k, v === true ? "1" : String(v));
  }
  const query = qs.toString();
  location.hash = query ? `${route}?${query}` : route;
}

function pageOf(p) {
  const n = parseInt(p.get("page"), 10);
  return Number.isFinite(n) && n > 0 ? n : 1;
}

function yearOf(p, key) {
  const n = parseInt(p.get(key), 10);
  return Number.isFinite(n) && n > 1900 && n < 2100 ? n : null;
}

/** ilike pattern: escape LIKE wildcards; PostgREST turns `*` into `%`, so it becomes the one-character `_`. */
function likePattern(text) {
  const clean = text.trim().replace(/[\\%_]/g, "\\$&").replace(/\*/g, "_");
  return `%${clean}%`;
}

/** Hash filter value kept only if known (allowed: object keys or array). */
function pick(value, allowed) {
  if (!value) return "";
  const ok = Array.isArray(allowed) ? allowed.includes(value) : Object.hasOwn(allowed, value);
  return ok ? value : "";
}

function countryName(code) {
  return COUNTRIES[code] || code;
}

function countriesText(list) {
  return (Array.isArray(list) ? list : []).map(countryName).join(", ");
}

function areaLabel(slug) {
  return POLICY_AREAS[slug] || slug || "";
}

function badge(value) {
  const known = ["direct", "partial", "high", "medium", "low", "info"].includes(value);
  return `<span class="badge badge-${known ? value : "neutral"}">${esc(value || "n/a")}</span>`;
}

function table(headers, rows, emptyText = "No results.") {
  if (!rows.length) return `<p class="empty">${esc(emptyText)}</p>`;
  const head = headers.map((h) => `<th scope="col">${esc(h)}</th>`).join("");
  const body = rows.map((cells) => `<tr>${cells.join("")}</tr>`).join("");
  return `<div class="table-wrap"><table><thead><tr>${head}</tr></thead><tbody>${body}</tbody></table></div>`;
}

function td(html, cls = "") {
  return cls ? `<td class="${cls}">${html}</td>` : `<td>${html}</td>`;
}

function pager(route, p, page, count) {
  const total = count ?? 0;
  const last = Math.max(1, Math.ceil(total / PAGE_SIZE));
  const from = total ? (page - 1) * PAGE_SIZE + 1 : 0;
  const to = Math.min(page * PAGE_SIZE, total);
  const link = (n, label) => {
    const q = new URLSearchParams(p);
    if (n > 1) q.set("page", String(n)); else q.delete("page");
    const query = q.toString();
    return `<a class="btn btn-secondary" href="#${route}${query ? "?" + query : ""}">${label}</a>`;
  };
  const prev = page > 1 ? link(page - 1, "← Previous") : "";
  const next = page < last ? link(page + 1, "Next →") : "";
  return `<div class="pager"><span class="muted">${from}–${to} of ${total.toLocaleString("en")} · page ${page} of ${last}</span>${prev}${next}</div>`;
}

function options(map, value, emptyLabel) {
  const first = emptyLabel === null ? "" : `<option value="">${esc(emptyLabel)}</option>`;
  return first + Object.entries(map)
    .map(([k, label]) => `<option value="${esc(k)}"${k === value ? " selected" : ""}>${esc(label)}</option>`).join("");
}

function selectField(name, label, map, value, emptyLabel = "All") {
  return `<label class="field"><span>${esc(label)}</span><select name="${name}">${options(map, value, emptyLabel)}</select></label>`;
}

function countriesSelect(value) {
  const map = Object.fromEntries(Object.entries(COUNTRIES).map(([code, name]) => [code, `${name} (${code})`]));
  return selectField("country", "Country", map, value, "All countries");
}

function yearField(name, label, value) {
  return `<label class="field field-half"><span>${esc(label)}</span>`
    + `<input type="number" name="${name}" min="2015" max="2035" step="1" inputmode="numeric" value="${value ?? ""}"></label>`;
}

function textField(name, label, value, placeholder) {
  return `<label class="field field-grow"><span>${esc(label)}</span>`
    + `<input type="text" name="${name}" value="${esc(value)}" placeholder="${esc(placeholder)}"></label>`;
}

function checkField(name, label, checked) {
  return `<label class="check"><input type="checkbox" name="${name}" value="1"${checked ? " checked" : ""}> ${esc(label)}</label>`;
}

function filterForm(route, fieldsHtml) {
  return `<form class="card filters" id="filters" data-route="${route}" role="search">${fieldsHtml}`
    + `<div class="filter-actions"><button type="submit" class="btn">Apply</button>`
    + `<a class="btn btn-secondary" href="#${route}">Reset</a></div></form>`;
}

/** Filters update the hash: selects and checkboxes apply immediately, text on Enter/Apply. */
function bindFilters() {
  const form = $("filters");
  if (!form) return;
  const apply = (control) => {
    const values = {};
    for (const [k, v] of new FormData(form)) values[k] = typeof v === "string" ? v.trim() : v;
    pendingFocus = control?.name || null;
    setParams(form.dataset.route, values);
  };
  form.addEventListener("submit", (e) => { e.preventDefault(); apply(document.activeElement); });
  form.querySelectorAll("select, input[type=checkbox]").forEach((el) => el.addEventListener("change", () => apply(el)));
}

function view(html) {
  $("view").innerHTML = html;
}

/** After a render: heading focus when the view changed, else back to the filter control that was used. */
function restoreFocus(viewKey) {
  const changed = lastViewKey !== null && viewKey !== lastViewKey;
  lastViewKey = viewKey;
  const name = pendingFocus;
  pendingFocus = null;
  if (changed) {
    $("view").querySelector("h2")?.focus();
  } else if (name) {
    const form = $("filters");
    const el = form && [...form.elements].find((x) => x.name === name);
    el?.focus();
  }
}

/** One page of a list: build(opts) returns the filtered, ordered query. Past the end, go to the last page. */
async function fetchPage(build, page) {
  const res = await build({ count: "exact" }).range((page - 1) * PAGE_SIZE, page * PAGE_SIZE - 1);
  if (res.error && (res.error.code === "PGRST103" || res.status === 416) && page > 1) {
    const head = await build({ count: "exact", head: true });
    if (head.error) return head;
    return { redirectPage: Math.max(1, Math.ceil((head.count || 0) / PAGE_SIZE)) };
  }
  return res;
}

function redirectToPage(route, p, n) {
  const q = new URLSearchParams(p);
  if (n > 1) q.set("page", String(n)); else q.delete("page");
  const query = q.toString();
  location.replace(`#${route}${query ? "?" + query : ""}`);
}

function heading(text) {
  return `<h2 tabindex="-1">${esc(text)}</h2>`;
}

function loading() {
  view(`<p class="loading" role="status">Loading…</p>`);
}

function showError(error, context = "") {
  const msg = error?.message || String(error);
  const hint = error?.code ? ` (code ${error.code})` : "";
  view(`<div class="error" role="alert"><strong>${esc(context || "Something went wrong")}.</strong> ${esc(msg)}${esc(hint)}</div>`);
}

/** True if a newer render started meanwhile (its result must win). */
function stale(seq) {
  return seq !== renderSeq;
}

function setActiveTab(route) {
  const tab = { project: "projects", csr: "csrs", csrdoc: "csrs", reviews: "projects" }[route] || route;
  document.querySelectorAll("#tabs a").forEach((a) => {
    if (a.dataset.tab === tab) a.setAttribute("aria-current", "page"); else a.removeAttribute("aria-current");
  });
}

// ---------- projects ----------

const PROJECT_LIST_COLS = "project_id,title,programme,year,countries,policy_area,detail_page,csrs";

function projectsQuery(f, opts, cols = PROJECT_LIST_COLS) {
  let q = sb.from("projects").select(cols, opts)
    .order("year", { ascending: false }).order("title").order("project_id");
  if (f.q) q = q.ilike("title", likePattern(f.q));
  if (f.country) q = q.contains("countries", [f.country]);
  if (f.programme) q = q.eq("programme", f.programme);
  if (f.from) q = q.gte("year", f.from);
  if (f.to) q = q.lte("year", f.to);
  if (f.area) q = q.eq("policy_area", f.area);
  if (f.linked === "yes") q = q.not("csrs", "eq", "{}");
  if (f.linked === "no") q = q.or("csrs.is.null,csrs.eq.{}");
  if (f.detail) q = q.eq("detail_page", true);
  return q;
}

/** Projects filter on CSR links: URL value -> label; empty = all (linked=1 from the former checkbox = yes). */
const LINKED_OPTIONS = { yes: "Linked", no: "Not linked" };

async function renderProjects(p, seq) {
  const page = pageOf(p);
  const f = {
    q: p.get("q") || "", country: pick(p.get("country"), COUNTRIES), programme: pick(p.get("programme"), PROGRAMMES),
    from: yearOf(p, "from"), to: yearOf(p, "to"), area: pick(p.get("area"), POLICY_AREAS),
    linked: p.get("linked") === "1" ? "yes" : pick(p.get("linked"), LINKED_OPTIONS), detail: p.get("detail") === "1",
  };
  const [res, reviews] = await Promise.all([fetchPage((opts) => projectsQuery(f, opts), page), reviewsAvailable()]);
  if (stale(seq)) return;
  if (res.redirectPage) return redirectToPage("projects", p, res.redirectPage);
  const { data, error, count } = res;
  if (error) return showError(error, "Could not load projects");

  const form = filterForm("projects",
    textField("q", "Search titles", f.q, "e.g. pension, hydrogen")
    + countriesSelect(f.country)
    + selectField("programme", "Programme", Object.fromEntries(PROGRAMMES.map((x) => [x, x])), f.programme)
    + yearField("from", "From year", f.from) + yearField("to", "To year", f.to)
    + selectField("area", "Policy area", POLICY_AREAS, f.area, "All areas")
    + selectField("linked", "Linked to a CSR", LINKED_OPTIONS, f.linked)
    + checkField("detail", "Has a detail page", f.detail));
  const rows = data.map((r) => [
    td(hashLink("project", r.project_id, r.title), "title-cell"),
    td(esc(r.programme)), td(esc(r.year), "nowrap"), td(esc(countriesText(r.countries))),
    td(esc(areaLabel(r.policy_area))), td(esc((r.csrs || []).length), "num"),
  ]);
  const tools = `<div class="toolbar"><span class="muted small">${fmtN(count ?? 0)} project${count === 1 ? "" : "s"} match these filters.</span>`
    + `<span class="toolbar-actions"><span id="xlsx-note" class="muted small" role="status" aria-live="polite"></span>`
    + (reviews ? `<a class="btn btn-secondary" href="#reviews">Links to review</a>` : "")
    + `<button type="button" class="btn btn-secondary" id="xlsx"${count ? "" : " disabled"}>Export to Excel</button></span></div>`;
  view(`${heading("Projects")}${form}${tools}${pager("projects", p, page, count)}`
    + table(["Title", "Programme", "Year", "Countries", "Policy area", "CSRs"], rows, "No project matches these filters.")
    + (data.length ? pager("projects", p, page, count) : ""));
  bindFilters();
  $("xlsx").addEventListener("click", () => exportExcel($("xlsx"), $("xlsx-note"), {
    filters: projectFilterLabels(f),
    projects: () => pagedRows(() => projectsQuery(f, undefined, REPORT_PROJECT_COLS)),
  }));
}

/** The Projects filters in words (Notes sheet of the Excel export). */
function projectFilterLabels(f) {
  return [
    f.q ? ["Title contains", f.q] : null, f.country ? ["Country", countryName(f.country)] : null, f.programme ? ["Programme", f.programme] : null,
    f.from ? ["From year", String(f.from)] : null, f.to ? ["To year", String(f.to)] : null, f.area ? ["Policy area", areaLabel(f.area)] : null,
    f.linked ? ["Linked to a CSR", LINKED_OPTIONS[f.linked]] : null, f.detail ? ["Has a detail page", "yes"] : null,
  ].filter(Boolean);
}

function linkItem(link, project) {
  const part = link.csr_parts || {};
  const csr = part.csrs || {};
  const partLabel = part.csr_id ? hashLink("csr", part.csr_id, link.part_id) : esc(link.part_id);
  const basis = link.basis === "title_only" ? `<span class="basis-note">based on the project title only</span>` : "";
  return `<li class="link-item"><div class="link-head">${badge(link.level)}<span class="part-id">${partLabel}</span>`
    + `<span>${esc(csr.title)}${csr.year ? ` (${esc(csr.year)})` : ""}</span>${basis}${reviewControls(project.project_id, link.part_id, project.title)}</div>`
    + (part.text ? `<p class="prose">${esc(part.text)}</p>` : "")
    + (part.assessment ? `<p><span class="label">Assessment:</span> ${esc(part.assessment)}</p>` : "")
    + `<p><span class="label">Rationale:</span> ${esc(link.rationale)}</p></li>`;
}

async function renderProject(id, seq) {
  const [proj, links, latest] = await Promise.all([
    sb.from("projects").select("project_id,title,programme,year,countries,policy_area,results,detail_page,url,summary,deliverables,csrs").eq("project_id", id).single(),
    sb.from("project_csr_links").select("level,rationale,basis,part_id,csr_parts(text,assessment,csr_id,csrs(title,year,country))").eq("project_id", id),
    impactHistoryQuery(id, 1),
  ]);
  if (!links.error && links.data?.length) await loadReviews(links.data.map((l) => ({ project_id: id, part_id: l.part_id })));
  if (stale(seq)) return;
  if (proj.error) return showError(proj.error, proj.error.code === "PGRST116" ? "Project not found" : "Could not load the project");
  if (links.error) return showError(links.error, "Could not load the CSR links");

  const r = proj.data;
  impactProject = r;
  assistantContext = { kind: "project", label: r.title, prompt: `Tell me about project ${r.title} (id ${r.project_id})` };
  const rank = { direct: 0, partial: 1 };
  const sorted = [...links.data].sort((a, b) => (rank[a.level] ?? 2) - (rank[b.level] ?? 2) || a.part_id.localeCompare(b.part_id));
  const facts = [
    ["Programme", esc(r.programme)], ["Year", esc(r.year)], ["Countries", esc(countriesText(r.countries))],
    ["Policy area", esc(areaLabel(r.policy_area))], ["Results", esc(r.results || "not stated")],
    ["Deliverables", esc(r.deliverables)], ["Detail page", r.detail_page ? "yes" : "no"], ["Project ID", `<code>${esc(r.project_id)}</code>`],
  ].map(([k, v]) => `<dt>${k}</dt><dd>${v}</dd>`).join("");
  // Lineage: the country with the most links of this project, years of its linked CSRs.
  const byCountry = new Map();
  for (const l of sorted) {
    const c = l.csr_parts?.csrs;
    if (c && Object.hasOwn(COUNTRIES, c.country)) byCountry.set(c.country, [...(byCountry.get(c.country) || []), num(c.year)]);
  }
  const [lnCountry, lnYears] = [...byCountry.entries()].sort((a, b) => b[1].length - a[1].length)[0] || [];
  const lineage = lnCountry
    ? ` <a class="btn btn-secondary" href="${esc(lineageHref(lnCountry, lnYears, `project:${r.project_id}`))}">Show lineage</a>` : "";
  const page = projectSourceHtml(r)
    + `<p class="toolbar-actions"><a class="btn btn-secondary" href="#impact/${encodeURIComponent(r.project_id)}">Impact analysis</a>${lineage}</p>`;
  const summary = r.summary
    ? `<div class="card"><h3 class="section-label">Summary <span class="ai-note">AI-generated summary</span></h3><p class="prose">${esc(r.summary)}</p></div>`
    : "";
  const linksHtml = sorted.length ? `<ul class="link-list">${sorted.map((l) => linkItem(l, r)).join("")}</ul>` : `<p class="empty">No CSR link assessed.</p>`;
  const reviewNote = feature.reviews === true && sorted.length
    ? `<p class="muted small">Confirm or reject each link with <em>Review</em>: your review is shared with the other users. ${infoButton("linkreview", "About link review")}</p>` : "";
  view(`<a class="back" href="#projects">← Projects</a><div class="card"><h2 class="detail-title" tabindex="-1">${esc(r.title)}</h2>`
    + `<dl class="facts">${facts}</dl>${page}`
    + `${latest.error ? "" : `<div id="impact-latest">${latestImpactHtml(latest.data)}</div>`}</div>${summary}`
    + `<div class="card"><h3 class="section-label">Supported CSRs (AI-assessed) <span class="muted small">${sorted.length}</span></h3>${reviewNote}${linksHtml}</div>`);
  bindReportToggles($("impact-latest"));
}

// ---------- CSRs: documents (country x year) -> recommendations -> sub-parts ----------

const COUNCIL_STAGE = "Council recommendation";
const COMMISSION_STAGE = "Commission recommendation";
const CSR_FIRST_YEAR = 2016;
// csr_documents not deployed yet (migration 005 not applied, or an older schema cache): documents derived from csrs.
const DOC_MISSING_CODES = new Set(["42P01", "42703", "PGRST200", "PGRST204", "PGRST205"]);
let csrDocsMissing = false;
let csrYearsCache = null; // { uid, years } (descending)

function docsTableMissing(error) {
  return Boolean(error) && (DOC_MISSING_CODES.has(error.code) || /csr_documents|doc_id/.test(error.message || ""));
}

function docIdOf(country, year) {
  return `csrdoc-${country}-${year}`;
}

function parseDocId(id) {
  const m = /^csrdoc-([A-Z]{2})-(\d{4})$/.exec(String(id ?? ""));
  return m ? { country: m[1], year: Number(m[2]) } : null;
}

function docTitle(country, year) {
  return `${countryName(country)} — ${year} country-specific recommendations`;
}

/** Fallback only (no csr_documents): a document of 2026 or later with no assessment at all is the Commission's proposal. */
function derivedStage(year, recs) {
  return year >= 2026 && recs.every((c) => !c.assessment) ? COMMISSION_STAGE : COUNCIL_STAGE;
}

/** Documents rebuilt from recommendation rows, grouped by country and year (same ids and titles as csr_documents). */
function deriveDocuments(rows) {
  const by = new Map();
  for (const c of arr(rows)) {
    if (!c || !c.country || !num(c.year)) continue;
    const id = c.doc_id || docIdOf(c.country, num(c.year));
    if (!by.has(id)) by.set(id, { doc_id: id, country: String(c.country), year: num(c.year), csrs: [] });
    by.get(id).csrs.push(c);
  }
  return [...by.values()].map((d) => ({
    ...d, title: docTitle(d.country, d.year), stage: derivedStage(d.year, d.csrs), n_recommendations: d.csrs.length,
    document_url: d.csrs.map((c) => safeUrl(c.source_url)).find(Boolean) || null, derived: true,
  }));
}

function isCommission(stage) {
  return stage === COMMISSION_STAGE;
}

function stageBadge(stage) {
  return `<span class="badge ${isCommission(stage) ? "badge-partial" : "badge-info"}">${esc(stage || COUNCIL_STAGE)}</span>`;
}

/** Link to the document's source: the country PDF (2026) or the ECFIN database (no URL per document before 2026). */
function docSourceLink(url, stage, { short = false } = {}) {
  const u = safeUrl(url);
  if (!u) return "";
  const pdf = /\.pdf(?:$|[?#])/i.test(u);
  const label = short ? (pdf ? "PDF" : "ECFIN")
    : pdf ? (isCommission(stage) ? "Commission recommendation (PDF)" : "Document (PDF)") : "ECFIN CSR database";
  return `${extLink(u, label)} ↗`;
}

/** Recommendations, sub-parts, distinct linked projects and the most frequent latest assessment of a document. */
function docStats(doc) {
  const recs = arr(doc.csrs);
  const projects = new Set();
  const counts = new Map();
  let parts = 0;
  for (const c of recs) {
    for (const p of arr(c.csr_parts)) {
      parts++;
      for (const l of arr(p?.project_csr_links)) {
        const pid = l?.project_id ?? l?.projects?.project_id;
        if (pid) projects.add(String(pid));
      }
    }
    const a = assessmentInfo(c.assessment);
    const hit = counts.get(a.label) || { ...a, n: 0 };
    hit.n++;
    counts.set(a.label, hit);
  }
  const dominant = [...counts.values()].sort((a, b) => b.n - a.n || a.label.localeCompare(b.label))[0] || null;
  return { recs: recs.length || num(doc.n_recommendations), parts, projects: projects.size, dominant };
}

const COMMISSION_NOTE = "Commission recommendations, not yet adopted by the Council and not yet assessed. No project is linked yet: "
  + "a recommendation of a given year can only be supported by projects of the following years.";

async function fetchCsrYears() {
  if (csrYearsCache?.uid === currentUid) return { years: csrYearsCache.years };
  let years = [];
  if (!csrDocsMissing) {
    const res = await sb.from("csr_documents").select("year").order("year", { ascending: false });
    if (!res.error) years = [...new Set(arr(res.data).map((r) => num(r.year)).filter(Boolean))].sort((a, b) => b - a);
    else if (!docsTableMissing(res.error)) return { error: res.error };
    if (!years.length) csrDocsMissing = true; // table missing, or created but not filled yet
  }
  if (!years.length) {
    const res = await sb.from("csrs").select("year").order("year", { ascending: false }).limit(1);
    if (res.error) return { error: res.error };
    const max = num(res.data?.[0]?.year) || 2025;
    for (let y = max; y >= CSR_FIRST_YEAR; y--) years.push(y);
  }
  csrYearsCache = { uid: currentUid, years };
  return { years };
}

/** Documents of one year (optionally one country), with what the cards need; derived from csrs as a fallback. */
async function fetchDocuments(year, country) {
  if (!csrDocsMissing) {
    let q = sb.from("csr_documents")
      .select("doc_id,country,year,title,stage,n_recommendations,document_url,csrs(csr_id,number,assessment,csr_parts(part_id,project_csr_links(project_id)))")
      .eq("year", year).order("country");
    if (country) q = q.eq("country", country);
    const res = await q;
    if (!res.error) return { docs: arr(res.data) };
    if (!docsTableMissing(res.error)) return { error: res.error };
    csrDocsMissing = true;
  }
  let q = sb.from("csrs").select("csr_id,country,year,number,assessment,source_url,csr_parts(part_id,project_csr_links(project_id))")
    .eq("year", year).order("country").order("number");
  if (country) q = q.eq("country", country);
  const res = await q;
  if (res.error) return { error: res.error };
  return { docs: deriveDocuments(res.data), derived: true };
}

function compareCsr(a, b) {
  return (b.year - a.year) || a.country.localeCompare(b.country)
    || (parseFloat(a.number) - parseFloat(b.number)) || String(a.number).localeCompare(String(b.number));
}

function csrsQuery(f, opts) {
  let q = sb.from("csrs").select("csr_id,country,year,number,title,assessment,assessment_year", opts)
    .order("year", { ascending: false }).order("country").order("number").order("csr_id");
  if (f.country) q = q.eq("country", f.country);
  if (f.year) q = q.eq("year", f.year);
  if (f.q) q = q.ilike("text", likePattern(f.q));
  return q;
}

/** Year select in descending order (an object map would enumerate numeric keys in ascending order). */
function yearSelect(list, value) {
  const opts = list.map(([k, label]) => `<option value="${esc(k)}"${k === value ? " selected" : ""}>${esc(label)}</option>`).join("");
  return `<label class="field"><span>Year</span><select name="year">${opts}</select></label>`;
}

function csrsIntro() {
  return `<p class="muted">Each year the EU addresses one CSR document to each Member State, 27 per year. A document contains numbered `
    + `recommendations; the ECFIN database splits each recommendation into sub-parts, the level at which project links are assessed.</p>`;
}

/** Switch between the document view (default) and the list of all recommendations (also used by the text search). */
function csrsModeLinks(recsMode, f) {
  const docs = hrefFor("csrs", { year: f.year || "", country: f.country });
  const recs = hrefFor("csrs", { view: "recs", year: f.year || "", country: f.country, q: f.q });
  const a = (href, label, on) => `<a class="btn btn-secondary seg"${on ? ' aria-current="page"' : ""} href="${esc(href)}">${label}</a>`;
  return `<p class="seg-group" role="group" aria-label="Show">${a(docs, "By document", !recsMode)}${a(recs, "All recommendations", recsMode)}</p>`;
}

function docCard(d, related = null) {
  const s = docStats(d);
  const commission = isCommission(d.stage);
  const nRelated = related ? relatedProjectCount(related, arr(d.csrs).map((c) => c.csr_id)) : null;
  const assess = commission || !s.dominant ? `<span class="viz-key viz-key-${commission ? "faint" : "muted"}" aria-hidden="true"></span>${commission ? "Not yet assessed" : "—"}`
    : `<span class="viz-key viz-key-${esc(s.dominant.key)}" aria-hidden="true"></span>${esc(s.dominant.label)}`
      + ` <span class="muted">(${s.dominant.n} of ${s.recs})</span>`;
  const href = `#csrdoc/${encodeURIComponent(d.doc_id)}`;
  return `<li class="doc-card"><div class="doc-head"><h4 class="doc-country"><a href="${href}">${esc(countryName(d.country))}</a>`
    + ` <span class="muted small">${esc(d.country)}</span></h4></div>`
    + `<dl class="doc-facts"><div><dt>Recommendations</dt><dd>${fmtN(s.recs)}</dd></div>`
    + `<div><dt>Linked projects</dt><dd>${commission && !s.projects ? `<span class="muted">none yet</span>` : fmtN(s.projects)}</dd></div>`
    + `<div class="doc-assess"><dt>Main assessment</dt><dd>${assess}</dd></div>`
    + (nRelated !== null ? `<div class="doc-related"><dt>Related past projects <span class="ai-note">AI</span></dt><dd>${nRelated
      ? `<a href="${href}" aria-label="${esc(`${nRelated} related past projects: open the document of ${countryName(d.country)}`)}">${fmtN(nRelated)} related past project${nRelated === 1 ? "" : "s"}</a>`
      : `<span class="muted">none suggested</span>`}</dd></div>` : "") + `</dl>`
    + `<p class="small doc-links"><a href="${href}" aria-label="${esc(`Open the document: ${d.title}`)}">Open the document</a>${d.document_url ? ` · ${docSourceLink(d.document_url, d.stage, { short: true })}` : ""}</p></li>`;
}

async function renderCsrs(p, seq) {
  const recsMode = p.get("view") === "recs" || Boolean((p.get("q") || "").trim());
  const country = pick(p.get("country"), COUNTRIES);
  const yrs = await fetchCsrYears();
  if (stale(seq)) return;
  if (yrs.error) return showError(yrs.error, "Could not load CSRs");
  const years = yrs.years;
  const asked = yearOf(p, "year");
  const title = `<div class="title-row">${heading("Country-specific recommendations")}${infoButton("csrlevels", "About the three levels")}</div>`;
  if (recsMode) return renderCsrList(p, seq, { title, years, country, year: asked });

  const year = asked && years.includes(asked) ? asked : years[0];
  const yearList = years.map((y, i) => [i === 0 ? "" : String(y), i === 0 ? `${y} (latest)` : String(y)]);
  const form = filterForm("csrs",
    textField("q", "Search recommendation text", "", "e.g. pension, energy")
    + yearSelect(yearList, year === years[0] ? "" : String(year))
    + countriesSelect(country));
  const res = await fetchDocuments(year, country);
  if (stale(seq)) return;
  if (res.error) return showError(res.error, "Could not load the CSR documents");
  const docs = [...res.docs].sort((a, b) => countryName(a.country).localeCompare(countryName(b.country)));
  const commission = docs.some((d) => isCommission(d.stage));
  const related = year >= RELATED_YEAR && docs.length ? await loadRelated() : null;
  if (stale(seq)) return;
  const nRecs = docs.reduce((s, d) => s + docStats(d).recs, 0);
  const others = country ? years.filter((y) => y !== year)
    .map((y) => `<a href="${esc(hrefFor("csrs", { year: y, country }))}">${y}</a>`).join(" · ") : "";
  const head = `<h3 class="docs-head">${year} · ${fmtN(docs.length)} document${docs.length === 1 ? "" : "s"}`
    + ` · ${fmtN(nRecs)} recommendation${nRecs === 1 ? "" : "s"}${docs.length ? ` ${stageBadge(commission ? COMMISSION_STAGE : COUNCIL_STAGE)}` : ""}</h3>`;
  view(`${title}${csrsIntro()}${form}${csrsModeLinks(false, { year: year === years[0] ? "" : year, country })}`
    + (res.derived ? `<p class="muted small" role="note">Documents rebuilt from the recommendations (document table not available yet).</p>` : "")
    + head + (commission ? `<p class="notice-few" role="note">${esc(`${year}: ${COMMISSION_NOTE}`)}</p>` : "")
    + (docs.length ? `<ul class="doc-grid">${docs.map((d) => docCard(d, related)).join("")}</ul>`
      : `<p class="empty">No CSR document for ${country ? esc(countryName(country)) + " in " : ""}${year}.</p>`)
    + (related ? relatedCoverageHtml(related, docs, year) : "")
    + (others ? `<p class="small">${esc(countryName(country))} in other years: ${others}</p>` : "")
    + (country ? `<p class="toolbar-actions">${briefingButton(country)}</p>` : ""));
  bindFilters();
}

/** All recommendations (one row each), with the text search: the former CSR list, now linked to the documents. */
async function renderCsrList(p, seq, { title, years, country, year }) {
  const page = pageOf(p);
  const f = { country, year, q: p.get("q") || "" };
  const res = await fetchPage((opts) => csrsQuery(f, opts), page);
  if (stale(seq)) return;
  if (res.redirectPage) return redirectToPage("csrs", p, res.redirectPage);
  const { data, error, count } = res;
  if (error) return showError(error, "Could not load CSRs");

  const form = filterForm("csrs",
    `<input type="hidden" name="view" value="recs">`
    + textField("q", "Search recommendation text", f.q, "e.g. pension, energy")
    + countriesSelect(f.country)
    + yearSelect([["", "All years"], ...years.map((y) => [String(y), String(y)])], f.year ? String(f.year) : ""));
  // Server sorts "number" as text; re-sort the page numerically (10 after 9).
  const rows = [...data].sort(compareCsr).map((r) => [
    td(hashLink("csr", r.csr_id, r.csr_id), "nowrap"),
    td(hashLink("csrdoc", docIdOf(r.country, r.year), `${countryName(r.country)} ${r.year}`), "nowrap"),
    td(esc(r.number), "num"), td(esc(r.title), "title-cell"),
    td(r.assessment ? `${esc(r.assessment)}${r.assessment_year ? ` <span class="muted">(${esc(r.assessment_year)})</span>` : ""}`
      : `<span class="muted">not yet assessed</span>`),
  ]);
  view(`${title}${csrsIntro()}${form}${csrsModeLinks(true, f)}${pager("csrs", p, page, count)}`
    + table(["Recommendation", "Document", "No.", "Title", "Latest assessment"], rows, "No recommendation matches these filters.")
    + (data.length ? pager("csrs", p, page, count) : ""));
  bindFilters();
}

/** A sub-part (or the recommendation itself when it has none) with its assessment and linked projects. */
function partBlock(part, { tag = "h3", label = part.part_id, showText = true, showAssessment = true, commission = false } = {}) {
  const rank = { direct: 0, partial: 1 };
  const links = [...arr(part.project_csr_links)].sort((a, b) => (rank[a.level] ?? 2) - (rank[b.level] ?? 2));
  const items = links.map((l) => {
    const pr = l.projects || {};
    const meta = [pr.programme, pr.year].filter(Boolean).map(esc).join(" · ");
    return `<li class="link-item"><div class="link-head">${badge(l.level)}`
      + `${pr.project_id ? hashLink("project", pr.project_id, pr.title || pr.project_id) : esc(pr.title)}`
      + `${meta ? `<span class="muted small">${meta}</span>` : ""}${reviewControls(pr.project_id, part.part_id, pr.title)}</div>`
      + `<p><span class="label">Rationale:</span> ${esc(l.rationale)}</p></li>`;
  }).join("");
  const areas = arr(part.policy_areas).map(areaLabel).join(", ");
  const assessment = !showAssessment ? "" : part.assessment ? esc(part.assessment) : commission ? "not yet assessed" : "";
  const linked = commission && !links.length ? ""
    : `<p class="label">Linked projects (${links.length})</p>` + (items ? `<ul class="link-list">${items}</ul>` : `<p class="empty">No project linked.</p>`);
  return `<div class="part">${label ? `<${tag} class="part-h">${esc(label)}</${tag}>` : ""}`
    + (showText ? `<p class="prose">${esc(part.text)}</p>` : "")
    + (areas ? `<p class="small"><span class="label">Policy areas:</span> ${esc(areas)}</p>` : "")
    + (assessment ? `<p><span class="label">Assessment:</span> ${assessment}</p>` : "")
    + linked + `</div>`;
}

/** True when the recommendation has no ECFIN sub-part: its only part is the recommendation itself. */
function wholeRecommendation(csr, parts) {
  return parts.length === 1 && parts[0].part_id === csr.csr_id;
}

const DOC_CSR_COLS = "csr_id,country,year,number,title,text,assessment,assessment_year,source_url";
const DOC_PART_COLS = "part_id,text,policy_areas,assessment,project_csr_links(level,rationale,projects(project_id,title,programme,year))";

async function fetchDocument(id) {
  const ref = parseDocId(id);
  if (!csrDocsMissing) {
    const res = await sb.from("csr_documents")
      .select(`doc_id,country,year,title,stage,n_recommendations,document_url,csrs(${DOC_CSR_COLS},csr_parts(${DOC_PART_COLS}))`)
      .eq("doc_id", id).single();
    if (!res.error && res.data) return { doc: res.data };
    if (res.error && docsTableMissing(res.error)) csrDocsMissing = true;
    else if (res.error && res.error.code !== "PGRST116") return { error: res.error };
  }
  if (!ref) return { doc: null };
  // Not in csr_documents (table missing or not filled yet): rebuilt from the recommendations of that country and year.
  const res = await sb.from("csrs").select(`${DOC_CSR_COLS},csr_parts(${DOC_PART_COLS})`).eq("country", ref.country).eq("year", ref.year);
  if (res.error) return { error: res.error };
  return { doc: deriveDocuments(res.data)[0] || null };
}

/** Project–sub-part pairs of nested csr_parts(project_csr_links(projects(project_id))) rows (review chips). */
function partLinkKeys(parts) {
  return arr(parts).flatMap((pt) => arr(pt?.project_csr_links).map((l) => ({ project_id: l?.projects?.project_id, part_id: pt.part_id })))
    .filter((k) => k.project_id && k.part_id);
}

async function renderCsrDoc(id, seq) {
  const res = await fetchDocument(id);
  if (stale(seq)) return;
  if (res.error) return showError(res.error, "Could not load the CSR document");
  const d = res.doc;
  if (!d) return showError({ message: `No document ${id}.` }, "CSR document not found");
  const year = num(d.year);
  const commission = isCommission(d.stage);
  const keys = partLinkKeys(arr(d.csrs).flatMap((c) => arr(c.csr_parts)));
  const [, related] = await Promise.all([keys.length ? loadReviews(keys) : null, year >= RELATED_YEAR ? loadRelated() : null]);
  if (stale(seq)) return;
  const recs = [...arr(d.csrs)].sort(csrNumber);
  const s = docStats(d);
  assistantContext = { kind: "csrdoc", label: `${countryName(d.country)} ${year} · CSR document`,
    prompt: `Tell me about the ${year} country-specific recommendations for ${countryName(d.country)} (document id ${d.doc_id}): what they ask and how they were assessed` };
  const facts = [
    ["Stage", `${stageBadge(d.stage)}${commission ? ` <span class="muted small">not yet adopted by the Council</span>` : ""}`],
    ["Recommendations", fmtN(s.recs)], ["Sub-parts", fmtN(s.parts)],
    ["Linked projects", commission && !s.projects ? "none yet" : `${fmtN(s.projects)} <span class="ai-note">AI-assessed</span>`],
    ["Main assessment", commission ? "not yet assessed" : s.dominant ? `${esc(s.dominant.label)} <span class="muted">(${s.dominant.n} of ${s.recs})</span>` : "—"],
    ["Source", docSourceLink(d.document_url, d.stage) || "—"], ["Document ID", `<code>${esc(d.doc_id)}</code>`],
  ].map(([k, v]) => `<dt>${k}</dt><dd>${v}</dd>`).join("");
  const lineage = Object.hasOwn(COUNTRIES, d.country) && !commission
    ? `<p><a class="btn btn-secondary" href="${esc(lineageHref(d.country, [year], `doc:${d.doc_id}`, s.projects === 0))}">Show in lineage network</a></p>` : "";
  const recBlocks = recs.map((c) => {
    const parts = [...arr(c.csr_parts)].sort((a, b) => String(a.part_id).localeCompare(String(b.part_id), "en", { numeric: true }));
    const whole = wholeRecommendation(c, parts);
    const assess = c.assessment
      ? `<span class="label">Latest assessment${c.assessment_year ? ` (${esc(c.assessment_year)})` : ""}:</span> ${esc(c.assessment)}`
      : `<span class="label">Assessment:</span> ${commission ? "not yet assessed" : "none"}`;
    const body = whole ? partBlock(parts[0], { label: "", showAssessment: false, commission })
      : parts.length ? parts.map((pt) => partBlock(pt, { tag: "h4", commission })).join("")
        : `<p class="prose">${esc(c.text)}</p>`;
    return `<section class="card rec" aria-labelledby="rec-${esc(c.csr_id)}"><div class="rec-head">`
      + `<h3 id="rec-${esc(c.csr_id)}">${hashLink("csr", c.csr_id, `Recommendation ${c.number}`)}</h3>`
      + `<span class="muted small">${whole ? "no sub-part" : `${parts.length} sub-part${parts.length === 1 ? "" : "s"}`}</span></div>`
      + (c.title ? `<p class="rec-title">${esc(c.title)}</p>` : "") + `<p class="small">${assess}</p>${body}`
      + (related ? relatedBlock(related, c.csr_id, { compact: true }) : "") + `</section>`;
  }).join("");
  const relatedIntro = related && recs.some((c) => related.byCsr.has(c.csr_id))
    ? `<div class="card"><div class="title-row"><h3 class="section-label">Related past projects <span class="ai-note">AI-suggested</span></h3>`
      + `${infoButton("related", "About related past projects")}</div><p class="small">${esc(relatedDocSummary(related, recs))}</p>`
      + `<p class="muted small">Listed under each recommendation below: past projects that worked on the same issues, same country first. `
      + `Suggested by AI, not links assessed from the project text: verify before use.</p></div>` : "";
  view(`<nav class="crumbs" aria-label="Breadcrumb"><a href="${esc(hrefFor("csrs", { year }))}">← CSRs ${year}</a></nav>`
    + `<div class="card"><p class="muted small">${esc(countryName(d.country))} · ${year} · CSR document</p>`
    + `<div class="title-row"><h2 class="detail-title" tabindex="-1">${esc(d.title)}</h2>${infoButton("csrlevels", "About the three levels")}</div>`
    + `<dl class="facts">${facts}</dl>`
    + (commission ? `<p class="notice-few" role="note">${esc(COMMISSION_NOTE)}</p>` : "")
    + (d.derived ? `<p class="muted small" role="note">Document rebuilt from its recommendations (document table not available yet).</p>` : "")
    + `${lineage}</div>` + relatedIntro
    + (recBlocks || `<div class="card"><p class="empty">No recommendation in this document.</p></div>`));
}

async function renderCsr(id, seq) {
  const [csr, parts] = await Promise.all([
    sb.from("csrs").select("*").eq("csr_id", id).single(),
    sb.from("csr_parts").select(`${DOC_PART_COLS}`).eq("csr_id", id).order("part_id"),
  ]);
  if (stale(seq)) return;
  if (csr.error) return showError(csr.error, csr.error.code === "PGRST116" ? "CSR not found" : "Could not load the CSR");
  if (parts.error) return showError(parts.error, "Could not load the CSR sub-parts");
  const r = csr.data;
  const docId = r.doc_id || docIdOf(r.country, r.year);
  let doc = null;
  if (!csrDocsMissing) {
    const res = await sb.from("csr_documents").select("doc_id,title,stage,document_url").eq("doc_id", docId).single();
    if (stale(seq)) return;
    if (!res.error) doc = res.data;
    else if (docsTableMissing(res.error)) csrDocsMissing = true;
  }
  const stage = doc?.stage || derivedStage(num(r.year), [r]);
  const commission = isCommission(stage);
  const keys = partLinkKeys(parts.data);
  const [, related] = await Promise.all([keys.length ? loadReviews(keys) : null, num(r.year) >= RELATED_YEAR ? loadRelated() : null]);
  if (stale(seq)) return;
  const list = [...parts.data].sort((a, b) => String(a.part_id).localeCompare(String(b.part_id), "en", { numeric: true }));
  const whole = wholeRecommendation(r, list);

  assistantContext = { kind: "csr", label: `${countryName(r.country)} ${r.year} · CSR ${r.number}`,
    prompt: `Tell me about CSR ${r.country} ${r.year} CSR ${r.number} (id ${r.csr_id}): ${r.title}` };
  const projectIds = new Set(list.flatMap((pt) => arr(pt.project_csr_links).map((l) => l.projects?.project_id).filter(Boolean)));
  const n = projectIds.size;
  const areas = arr(r.policy_areas).map(areaLabel).join(", ");
  const assessment = r.assessment
    ? `<p><span class="label">Latest assessment${r.assessment_year ? ` (${esc(r.assessment_year)})` : ""}:</span> ${esc(r.assessment)}</p>`
    : `<p><span class="label">Assessment:</span> ${commission ? "not yet assessed" : "none"}</p>`;
  const source = docSourceLink(r.source_url, stage);
  const lineage = Object.hasOwn(COUNTRIES, r.country) && !commission
    ? `<p><a class="btn btn-secondary" href="${esc(lineageHref(r.country, [num(r.year)], `csr:${r.csr_id}`, n === 0))}">Show in lineage network</a></p>` : "";
  const supported = commission && !n
    ? `<p><strong>No project linked yet</strong> <span class="muted small">(${esc(COMMISSION_STAGE)}: projects can support it from ${num(r.year) + 1})</span></p>`
    : `<p><strong>${n} project${n === 1 ? "" : "s"} support${n === 1 ? "s" : ""} this CSR</strong> <span class="ai-note">AI-assessed</span></p>`;
  const docLabel = `${countryName(r.country)} ${r.year} document`;
  view(`<nav class="crumbs" aria-label="Breadcrumb"><a href="${esc(hrefFor("csrs", { year: r.year }))}">← CSRs ${esc(r.year)}</a>`
    + ` <span aria-hidden="true">›</span> ${hashLink("csrdoc", docId, docLabel)} <span aria-hidden="true">›</span> <span>Recommendation ${esc(r.number)}</span></nav>`
    + `<div class="card"><p class="muted small">${esc(countryName(r.country))} · ${esc(r.year)} · CSR ${esc(r.number)} · <code>${esc(r.csr_id)}</code> ${stageBadge(stage)}</p>`
    + `<h2 class="detail-title" tabindex="-1">${esc(r.title)}</h2>`
    + `<p class="small">Part of ${hashLink("csrdoc", docId, doc?.title || docTitle(r.country, r.year))}</p>`
    + supported + `<p class="prose">${esc(r.text)}</p>`
    + (areas ? `<p class="small"><span class="label">Policy areas:</span> ${esc(areas)}</p>` : "")
    + `${assessment}${source ? `<p class="small">${source}</p>` : ""}${lineage}</div>`
    + (related ? relatedBlock(related, r.csr_id, { card: true }) : "")
    + (whole ? (commission ? "" : `<div class="card"><h3>Linked projects</h3>${partBlock(list[0], { label: "", showText: false })}</div>`)
      : `<div class="card"><h3>Sub-parts</h3>${list.length ? list.map((pt) => partBlock(pt)).join("") : `<p class="empty">No sub-part.</p>`}</div>`));
}

// ---------- link reviews and related past projects (migration 006: hidden until its tables exist) ----------

const REVIEW_STATUS = { confirmed: "Confirmed", rejected: "Rejected", to_review: "To review" };
const REVIEW_COLS = "project_id,part_id,status,comment,reviewer_email,created_at";
const REVIEW_COMMENT_MAX = 2000;
const RELATED_YEAR = 2026; // csr_related_projects covers the 2026 Commission recommendations
const MISSING_CODES = new Set(["42P01", "PGRST205"]); // table or view not created yet (or not in the schema cache)
const feature = { reviews: null, related: null }; // null: not known yet; then true or false for the session
const featureProbe = {}; // name -> Promise while probing
const reviewLatest = new Map(); // reviewKey -> latest review row, or null (checked: none)
let relatedCache = null; // { uid, byCsr: Map csr_id -> rows (sorted), rows }
let relatedLoading = null;
let reviewLinksCache = null; // { uid, rows } for the Links to review view
let reviewStats = null; // { uid, stats } for the dashboard card
let reviewOpener = null;
let reviewTarget = null; // { project_id, part_id, title } of the open review dialog

const reviewKey = R.reviewKey;

/** True when a PostgREST answer says the table does not exist (yet). */
function relationMissing(res) {
  const e = res?.error;
  return Boolean(e) && (res.status === 404 || MISSING_CODES.has(e.code) || /schema cache|does not exist/i.test(e.message || ""));
}

/** One cheap request per session tells whether a migration-006 object exists; a transient error is retried later. */
function probeFeature(name, table, column) {
  if (feature[name] !== null) return Promise.resolve(feature[name]);
  if (!featureProbe[name]) {
    featureProbe[name] = Promise.resolve(sb.from(table).select(column).limit(1)).then((res) => {
      if (relationMissing(res)) feature[name] = false;
      else if (!res.error) feature[name] = true;
      featureProbe[name] = null;
      return feature[name] === true;
    }, () => { featureProbe[name] = null; return false; });
  }
  return featureProbe[name];
}

const reviewsAvailable = () => probeFeature("reviews", "link_review_latest", "project_id");
const relatedAvailable = () => probeFeature("related", "csr_related_projects", "csr_id");

/** Every row of a filtered, ordered query, 1,000 per request (PostgREST cap). */
async function pagedRows(build) {
  const rows = [];
  for (let from = 0; ; from += 1000) {
    const { data, error } = await build().range(from, from + 999);
    if (error) throw error;
    rows.push(...arr(data));
    if (arr(data).length < 1000) return rows;
  }
}

/** Latest reviews of these project–sub-part links (fresh from the server). False when reviews are not available. */
async function loadReviews(keys) {
  const list = arr(keys).filter((k) => k?.project_id && k?.part_id);
  if (!list.length || !(await reviewsAvailable())) return false;
  const projects = [...new Set(list.map((k) => k.project_id))];
  const parts = [...new Set(list.map((k) => k.part_id))];
  const [column, values] = projects.length <= parts.length ? ["project_id", projects] : ["part_id", parts];
  const found = new Map();
  try {
    for (let i = 0; i < values.length; i += 100) {
      const chunk = values.slice(i, i + 100);
      for (const r of await pagedRows(() => sb.from("link_review_latest").select(REVIEW_COLS).in(column, chunk).order("project_id").order("part_id"))) {
        found.set(reviewKey(r.project_id, r.part_id), r);
      }
    }
  } catch (e) {
    if (relationMissing({ error: e })) feature.reviews = false;
    return false;
  }
  for (const k of list) {
    const key = reviewKey(k.project_id, k.part_id);
    reviewLatest.set(key, found.get(key) || null);
  }
  return true;
}

function reviewStatusOf(row) {
  return row && Object.hasOwn(REVIEW_STATUS, row.status) ? row.status : "none";
}

function reviewWho(row) {
  if (!row) return "";
  return [row.reviewer_email || "unknown reviewer", fmtDate(row.created_at)].filter(Boolean).join(", ");
}

function reviewChip(key) {
  const row = reviewLatest.get(key) || null;
  const s = reviewStatusOf(row);
  const title = row ? `${REVIEW_STATUS[s] || "Reviewed"} by ${reviewWho(row)}${row.comment ? ` — ${B.clip(row.comment, 160)}` : ""}` : "No review yet";
  return `<span class="rv-chip rv-${s}" data-review-chip="${esc(key)}" title="${esc(title)}"><span class="sr-only">Review: </span>`
    + `${esc(s === "none" ? "Not reviewed" : REVIEW_STATUS[s])}</span>`;
}

/** Status chip and "Review" button of one project–sub-part link (nothing while reviews are not available). */
function reviewControls(projectId, partId, projectTitle) {
  if (feature.reviews !== true || !projectId || !partId) return "";
  return `<span class="rv-ctl">${reviewChip(reviewKey(projectId, partId))}`
    + `<button type="button" class="btn-link rv-open" data-review-project="${esc(projectId)}" data-review-part="${esc(partId)}"`
    + ` data-review-title="${esc(projectTitle || projectId)}" aria-haspopup="dialog"`
    + ` aria-label="${esc(`Review the link between ${partId} and ${projectTitle || projectId}`)}">Review</button></span>`;
}

function paintReviewChips(key) {
  for (const el of document.querySelectorAll(`[data-review-chip="${CSS.escape(key)}"]`)) el.outerHTML = reviewChip(key);
}

function reviewHistoryHtml(rows) {
  if (!rows.length) return `<p class="muted small">No review yet: yours will be the first.</p>`;
  return `<ol class="rv-history">${rows.map((r, i) => `<li><div class="rv-history-head">${`<span class="rv-chip rv-${reviewStatusOf(r)}">${esc(REVIEW_STATUS[r.status] || r.status || "—")}</span>`}`
    + `<span class="small">${esc(reviewWho(r))}</span>${i === 0 ? ` <span class="muted small">(current)</span>` : ""}</div>`
    + (r.comment ? `<p class="prose small">${esc(r.comment)}</p>` : "") + `</li>`).join("")}</ol>`;
}

function reviewFormHtml(t) {
  const current = reviewLatest.get(reviewKey(t.project_id, t.part_id)) || null;
  const s = reviewStatusOf(current);
  const choices = Object.entries(REVIEW_STATUS).map(([k, label]) => `<label class="rv-choice rv-choice-${k}">`
    + `<input type="radio" name="status" value="${k}"${k === s ? " checked" : ""} required> <span>${esc(label)}</span></label>`).join("");
  return `<form id="review-form" class="info-inner" novalidate>`
    + `<div class="info-head"><div class="title-row"><h2 id="review-title">Review the link</h2>${infoButton("linkreview", "About link review")}</div></div>`
    + `<div class="info-body rv-body">`
    + `<dl class="facts rv-facts"><dt>Project</dt><dd>${hashLink("project", t.project_id, t.title || t.project_id)}</dd>`
    + `<dt>CSR sub-part</dt><dd><code>${esc(t.part_id)}</code></dd><dt>Current</dt><dd>${reviewChip(reviewKey(t.project_id, t.part_id))}`
    + `${current ? ` <span class="muted small">${esc(reviewWho(current))}</span>` : ""}</dd></dl>`
    + `<p class="muted small" id="review-intro">The link was assessed by AI. Is it right? Your review is shared with the other users and replaces the current status (the history is kept).</p>`
    + `<fieldset class="rv-choices"><legend>Your review</legend>${choices}</fieldset>`
    + `<label class="field rv-comment"><span>Comment (optional)</span><textarea name="comment" rows="3" maxlength="${REVIEW_COMMENT_MAX}"`
    + ` placeholder="Why: what the project does or does not cover"></textarea></label>`
    + `<p id="review-status" class="briefing-status" role="status" aria-live="polite"></p>`
    + `<h3 class="rv-history-title">History</h3><div id="review-history"><p class="muted small">Loading…</p></div></div>`
    + `<div class="briefing-actions"><button type="button" class="btn btn-secondary" id="review-cancel">Cancel</button>`
    + `<button type="submit" class="btn" id="review-save">Save review</button></div></form>`;
}

async function loadReviewHistory(t) {
  const box = $("review-history");
  const res = await sb.from("link_reviews").select("id,status,comment,reviewer_email,created_at")
    .eq("project_id", t.project_id).eq("part_id", t.part_id).order("created_at", { ascending: false }).order("id", { ascending: false }).limit(50);
  if (reviewTarget !== t || !box?.isConnected) return;
  box.innerHTML = res.error ? `<p class="muted small">${esc(relationMissing(res) ? "History not available yet." : `Could not load the history: ${res.error.message || ""}`)}</p>`
    : reviewHistoryHtml(arr(res.data));
}

function openReview(t, opener) {
  const dialog = $("review-dialog");
  if (!dialog || feature.reviews !== true) return;
  reviewTarget = t;
  reviewOpener = opener || null;
  dialog.innerHTML = reviewFormHtml(t);
  dialog.setAttribute("aria-describedby", "review-intro");
  $("review-form").addEventListener("submit", (e) => { e.preventDefault(); submitReview(); });
  $("review-cancel").addEventListener("click", () => dialog.close());
  if (typeof dialog.showModal === "function") dialog.showModal(); else dialog.setAttribute("open", "");
  (dialog.querySelector("input[name=status]:checked") || dialog.querySelector("input[name=status]")).focus();
  loadReviewHistory(t);
}

/** Inserts one review (append-only: the server fills the reviewer and the date). Returns { row } or { error }. */
async function saveReview(t, status, comment) {
  if (!Object.hasOwn(REVIEW_STATUS, status)) return { error: { message: "Choose Confirmed, Rejected or To review." } };
  const text = String(comment || "").trim().slice(0, REVIEW_COMMENT_MAX);
  const res = await sb.from("link_reviews").insert({ project_id: t.project_id, part_id: t.part_id, status, comment: text || null }).select(REVIEW_COLS);
  if (res.error) {
    if (relationMissing(res)) feature.reviews = false;
    return { error: res.error };
  }
  const row = arr(res.data)[0] || { project_id: t.project_id, part_id: t.part_id, status, comment: text || null, reviewer_email: currentEmail, created_at: new Date().toISOString() };
  const key = reviewKey(t.project_id, t.part_id);
  reviewLatest.set(key, row);
  paintReviewChips(key);
  const link = reviewLinksCache?.rows.find((l) => l.key === key);
  if (link) link.review = row;
  reviewStats = null; // the dashboard card recounts
  $("live-status").textContent = `Review saved: ${REVIEW_STATUS[status]}.`;
  return { row };
}

function reviewErrorText(error) {
  if (relationMissing({ error })) return "Link review is not available yet in this database.";
  if (error?.code === "42501") return "You are not allowed to save reviews (tester access required).";
  return `Could not save the review: ${error?.message || error}`;
}

async function submitReview() {
  const form = $("review-form");
  const t = reviewTarget;
  if (!form || !t) return;
  const status = form.elements.status.value;
  if (!status) {
    briefingStatusIn("review-status", "Choose Confirmed, Rejected or To review.", true);
    form.querySelector("input[name=status]")?.focus();
    return;
  }
  $("review-save").disabled = true;
  briefingStatusIn("review-status", "Saving…");
  const res = await saveReview(t, status, form.elements.comment.value);
  if (reviewTarget !== t || !form.isConnected) return;
  $("review-save").disabled = false;
  if (res.error) return briefingStatusIn("review-status", reviewErrorText(res.error), true);
  $("review-dialog").close();
}

function briefingStatusIn(id, text, error = false) {
  const el = $(id);
  if (!el) return;
  el.textContent = text;
  el.classList.toggle("note-error", error);
}

function bindReviewDialog() {
  const dialog = $("review-dialog");
  if (!dialog) return;
  document.addEventListener("click", async (e) => {
    const el = e.target instanceof Element ? e.target : null;
    const open = el?.closest(".rv-open");
    if (open) {
      openReview({ project_id: open.dataset.reviewProject, part_id: open.dataset.reviewPart, title: open.dataset.reviewTitle }, open);
      return;
    }
    const quick = el?.closest("[data-review-quick]");
    if (quick && !quick.disabled) {
      const t = { project_id: quick.dataset.reviewProject, part_id: quick.dataset.reviewPart };
      const row = quick.closest("tr");
      row?.querySelectorAll("[data-review-quick]").forEach((b) => { b.disabled = true; });
      const res = await saveReview(t, quick.dataset.reviewQuick, "");
      row?.querySelectorAll("[data-review-quick]").forEach((b) => { b.disabled = false; });
      const note = $("rv-note");
      if (note) {
        note.textContent = res.error ? reviewErrorText(res.error) : `Saved: ${REVIEW_STATUS[quick.dataset.reviewQuick]} (${t.part_id}).`;
        note.classList.toggle("note-error", Boolean(res.error));
      }
      if (!res.error && row) {
        const who = row.querySelector("[data-review-who]");
        if (who) who.textContent = reviewWho(res.row);
      }
    }
  });
  dialog.addEventListener("click", (e) => { // backdrop, or a link (project page) that leaves the view
    if (e.target === dialog || (e.target instanceof Element && e.target.closest("a[href^='#']"))) dialog.close();
  });
  dialog.addEventListener("close", () => {
    reviewTarget = null;
    if (reviewOpener?.isConnected) reviewOpener.focus();
    reviewOpener = null;
  });
  window.addEventListener("hashchange", () => { if (dialog.open) dialog.close(); });
}

// Links to review (#reviews): every project–CSR sub-part link with its latest review, filtered and paged in the browser.
const REVIEW_FILTERS = { "": "Not reviewed, to review or rejected", none: "Not reviewed", to_review: "To review", rejected: "Rejected", confirmed: "Confirmed", all: "All links" };
const REVIEW_LINK_COLS = "project_id,part_id,level,rationale,basis,projects(title,programme,year),csr_parts(csr_id,csrs(country,year,number))";

async function fetchReviewLinks() {
  if (reviewLinksCache?.uid === currentUid) return reviewLinksCache.rows;
  const [links, reviews] = await Promise.all([
    fetchAllRows("project_csr_links", REVIEW_LINK_COLS, ["project_id", "part_id"]),
    fetchAllRows("link_review_latest", REVIEW_COLS, ["project_id", "part_id"]),
  ]);
  const latest = new Map(reviews.map((r) => [reviewKey(r.project_id, r.part_id), r]));
  const rows = links.map((l) => {
    const key = reviewKey(l.project_id, l.part_id);
    const csr = l.csr_parts?.csrs || {};
    return { ...l, key, csrId: l.csr_parts?.csr_id || "", country: csr.country || "", csrYear: num(csr.year), review: latest.get(key) || null };
  }).sort((a, b) => a.country.localeCompare(b.country) || b.csrYear - a.csrYear
    || a.part_id.localeCompare(b.part_id, "en", { numeric: true }) || String(a.projects?.title).localeCompare(String(b.projects?.title)));
  for (const r of rows) reviewLatest.set(r.key, r.review);
  reviewLinksCache = { uid: currentUid, rows };
  return rows;
}

function reviewLinkFilter(f) {
  const q = f.q.trim().toLowerCase();
  return (r) => {
    const s = reviewStatusOf(r.review);
    if (f.status === "" ? s === "confirmed" : f.status !== "all" && s !== f.status) return false;
    if (f.country && r.country !== f.country) return false;
    if (f.level && r.level !== f.level) return false;
    if (q && !String(r.projects?.title || "").toLowerCase().includes(q) && !r.part_id.toLowerCase().includes(q)) return false;
    return true;
  };
}

async function renderReviews(p, seq) {
  const head = `<a class="back" href="#projects">← Projects</a><div class="title-row">${heading("Links to review")}${infoButton("linkreview", "About link review")}</div>`;
  if (!(await reviewsAvailable())) {
    if (stale(seq)) return;
    view(`${head}<div class="card"><p><strong>Link review is not available yet.</strong> The database update that stores the reviews has not been applied; `
      + `the project–CSR links stay AI-assessed only. Try again later.</p><p><a href="#projects">Back to the projects</a></p></div>`);
    return;
  }
  let rows;
  try {
    rows = await fetchReviewLinks();
  } catch (e) {
    if (stale(seq)) return;
    if (relationMissing({ error: e })) feature.reviews = false;
    return showError(e, "Could not load the links");
  }
  if (stale(seq)) return;
  const f = { status: pick(p.get("status") || "", REVIEW_FILTERS), country: pick(p.get("country"), COUNTRIES), level: pick(p.get("level"), LINK_LABELS), q: p.get("q") || "" };
  const list = rows.filter(reviewLinkFilter(f));
  const page = pageOf(p);
  const last = Math.max(1, Math.ceil(list.length / PAGE_SIZE));
  if (page > last) return redirectToPage("reviews", p, last);
  const counts = { confirmed: 0, rejected: 0, to_review: 0, none: 0 };
  for (const r of rows) counts[reviewStatusOf(r.review)]++;
  const reviewed = rows.length - counts.none;
  const form = filterForm("reviews",
    selectField("status", "Review status", REVIEW_FILTERS, f.status, null)
    + countriesSelect(f.country)
    + selectField("level", "Link level", LINK_LABELS, f.level, "All levels")
    + textField("q", "Search project or sub-part", f.q, "e.g. pension, csr-LT-2023"));
  const quick = (r, status, label) => `<button type="button" class="btn btn-secondary btn-small" data-review-quick="${status}" data-review-project="${esc(r.project_id)}"`
    + ` data-review-part="${esc(r.part_id)}" aria-label="${esc(`${label}: ${r.part_id}, ${r.projects?.title || r.project_id}`)}">${label}</button>`;
  const body = list.slice((page - 1) * PAGE_SIZE, page * PAGE_SIZE).map((r) => [
    td(`${hashLink("project", r.project_id, r.projects?.title || r.project_id)}`
      + `<div class="muted small">${esc([r.projects?.programme, r.projects?.year].filter(Boolean).join(" · "))}</div>`
      + (r.rationale ? `<details class="rv-why"><summary>Rationale (AI)</summary><p class="small">${esc(r.rationale)}</p></details>` : ""), "title-cell"),
    td(`${r.csrId ? hashLink("csr", r.csrId, r.part_id) : esc(r.part_id)}<div class="muted small">${esc(countryName(r.country))}${r.csrYear ? ` ${r.csrYear}` : ""}</div>`, "nowrap"),
    td(badge(r.level)),
    td(`${reviewChip(r.key)}<div class="muted small" data-review-who>${esc(reviewWho(r.review))}</div>`),
    td(`<div class="rv-actions">${quick(r, "confirmed", "Confirm")}${quick(r, "rejected", "Reject")}`
      + `<button type="button" class="btn-link rv-open" data-review-project="${esc(r.project_id)}" data-review-part="${esc(r.part_id)}"`
      + ` data-review-title="${esc(r.projects?.title || r.project_id)}" aria-haspopup="dialog" aria-label="${esc(`Review with a comment: ${r.part_id}, ${r.projects?.title || r.project_id}`)}">With comment…</button></div>`),
  ]);
  const summary = `<p class="muted small">${fmtN(reviewed)} of ${fmtN(rows.length)} project–CSR sub-part links reviewed (${share(reviewed, rows.length) || "0%"}): `
    + `${fmtN(counts.confirmed)} confirmed, ${fmtN(counts.rejected)} rejected, ${fmtN(counts.to_review)} to review, ${fmtN(counts.none)} not reviewed.</p>`;
  const tools = `<div class="toolbar"><span id="rv-note" class="muted small" role="status" aria-live="polite"></span>`
    + `<span class="toolbar-actions"><span id="xlsx-note" class="muted small" role="status" aria-live="polite"></span>`
    + `<button type="button" class="btn btn-secondary" id="xlsx"${list.length ? "" : " disabled"}>Export to Excel</button></span></div>`;
  view(`${head}<p class="muted">Links between projects and CSR sub-parts are assessed by AI. Confirm or reject them here; the latest review of each link is shown everywhere in the explorer.</p>`
    + summary + form + tools + pager("reviews", p, page, list.length)
    + `<div class="rv-table">${table(["Project", "CSR sub-part", "Level", "Review", "Quick review"], body, "No link matches these filters.")}</div>`
    + (list.length ? pager("reviews", p, page, list.length) : ""));
  bindFilters();
  $("xlsx").addEventListener("click", () => {
    const keys = new Set(list.map((r) => r.key));
    const ids = new Set(list.map((r) => r.project_id));
    exportExcel($("xlsx"), $("xlsx-note"), {
      filters: [["View", "Links to review"], ["Review status", REVIEW_FILTERS[f.status]], f.country ? ["Country (of the CSR)", countryName(f.country)] : null,
        f.level ? ["Link level", LINK_LABELS[f.level]] : null, f.q ? ["Search", f.q] : null].filter(Boolean),
      projects: async () => (await fetchAllRows("projects", REPORT_PROJECT_COLS, ["project_id"])).filter((x) => ids.has(x.project_id)),
      keepLink: (l) => keys.has(reviewKey(l.project_id, l.part_id)),
    });
  });
}

/** Dashboard card: reviewed share and statuses by link level (tsi_dashboard figures when present, else counted here). */
async function fetchReviewStats(model) {
  if (reviewStats?.uid === currentUid) return reviewStats.stats;
  let stats = null;
  if (model.reviews) stats = model.reviews;
  else if (await reviewsAvailable()) {
    const [links, reviews] = await Promise.all([
      fetchAllRows("project_csr_links", "project_id,part_id,level", ["project_id", "part_id"]),
      fetchAllRows("link_review_latest", "project_id,part_id,status", ["project_id", "part_id"]),
    ]);
    const status = new Map(reviews.map((r) => [reviewKey(r.project_id, r.part_id), r.status]));
    const by = new Map();
    for (const l of links) {
      const k = l.level === "direct" ? "direct" : "partial";
      if (!by.has(k)) by.set(k, { level: k, confirmed: 0, rejected: 0, to_review: 0, total: 0 });
      const b = by.get(k);
      b.total++;
      const s = status.get(reviewKey(l.project_id, l.part_id));
      if (Object.hasOwn(REVIEW_STATUS, s)) b[s]++;
    }
    stats = { links: links.length, byLevel: [...by.values()] };
  }
  if (stats) {
    for (const b of stats.byLevel) b.none = Math.max(0, num(b.total) - num(b.confirmed) - num(b.rejected) - num(b.to_review));
    stats.reviewed = stats.byLevel.reduce((s, b) => s + num(b.confirmed) + num(b.rejected) + num(b.to_review), 0);
  }
  reviewStats = { uid: currentUid, stats };
  return stats;
}

const REVIEW_KEYS = [["confirmed", "Confirmed", "s3"], ["rejected", "Rejected", "sev-high"], ["to_review", "To review", "sev-medium"], ["none", "Not reviewed", "faint"]];

function reviewRows(stats) {
  return inOrder(stats.byLevel.map((b) => ({ ...b, key: b.level })), ["direct", "partial"]).map((b) => {
    const total = num(b.total);
    return { label: LINK_LABELS[b.level] || b.level, display: `${share(total - b.none, total) || "0%"} reviewed`,
      segs: REVIEW_KEYS.map(([k, label, color]) => ({ v: num(b[k]), key: color, href: hrefFor("reviews", { status: k === "none" ? "none" : k, level: b.level }),
        tip: { series: label, label: `${LINK_LABELS[b.level] || b.level} links`, value: `${fmtN(b[k])} links`, share: total ? `${share(b[k], total)} of ${fmtN(total)}` : "", key: color } })) };
  });
}

function reviewCardHtml(stats) {
  const total = stats.byLevel.reduce((s, b) => s + num(b.total), 0) || num(stats.links);
  return `<h3 id="dash-reviews-h">Link reviews ${infoButton("linkreview", "About link review")}</h3>`
    + `<div class="dash-grid"><figure class="viz viz-wide"><figcaption><div class="title-row"><h4 class="viz-title">Reviews of the AI-assessed links, by level</h4></div>`
    + `<p class="viz-sub">${esc(`${fmtN(stats.reviewed)} of ${fmtN(total)} links reviewed (${share(stats.reviewed, total) || "0%"}) by the explorer's users. Click a segment to list those links.`)}</p></figcaption>`
    + legend(REVIEW_KEYS.map(([k, label, color]) => ({ key: color, label, n: stats.byLevel.reduce((s, b) => s + num(b[k]), 0), href: hrefFor("reviews", { status: k }) })))
    + `<div class="viz-slot" data-chart="reviews"></div>`
    + dataTable(["Level", "Confirmed", "Rejected", "To review", "Not reviewed", "Total"],
      inOrder(stats.byLevel.map((b) => ({ ...b, key: b.level })), ["direct", "partial"]).map((b) => [LINK_LABELS[b.level] || b.level, fmtN(b.confirmed), fmtN(b.rejected), fmtN(b.to_review), fmtN(b.none), fmtN(b.total)]))
    + `<p class="small"><a href="#reviews">Open the links to review</a></p></figure></div>`;
}

async function paintReviewCard() {
  const box = $("dash-reviews");
  if (!box || !dashCache) return;
  const model = dashCache.model;
  let stats = null;
  try { stats = await fetchReviewStats(model); } catch { stats = null; }
  if (!stats || !box.isConnected) return;
  model.reviewStats = stats;
  box.innerHTML = reviewCardHtml(stats);
  box.hidden = false;
  const slot = box.querySelector(".viz-slot");
  if (slot) slot.innerHTML = DASH_CHARTS.reviews(model, slot.clientWidth);
}

// Related past projects (csr_related_projects): AI-suggested past projects for the 2026 recommendations.

/** All rows (with their project) once per session; null when the table is missing or empty. */
async function loadRelated() {
  if (relatedCache?.uid === currentUid) return relatedCache.rows.length ? relatedCache : null;
  if (!(await relatedAvailable())) return null;
  if (!relatedLoading) {
    const uid = currentUid;
    relatedLoading = (async () => {
      let rows;
      try {
        rows = await fetchAllRows("csr_related_projects", "csr_id,project_id,relevance,scope,rationale,projects(title,programme,year,countries)", ["csr_id", "project_id"]);
      } catch (e) {
        if (relationMissing({ error: e })) { feature.related = false; return null; }
        if (e?.code !== "PGRST200") throw e; // no foreign key to projects: titles fetched separately
        rows = await fetchAllRows("csr_related_projects", "csr_id,project_id,relevance,scope,rationale", ["csr_id", "project_id"]);
        const ids = [...new Set(rows.map((r) => r.project_id).filter(Boolean))];
        const projects = new Map();
        for (let i = 0; i < ids.length; i += 100) {
          const res = await sb.from("projects").select("project_id,title,programme,year,countries").in("project_id", ids.slice(i, i + 100));
          if (res.error) throw res.error;
          for (const p of arr(res.data)) projects.set(p.project_id, p);
        }
        rows = rows.map((r) => ({ ...r, projects: projects.get(r.project_id) || null }));
      }
      const rank = (r) => (r.scope === "same_country" ? 0 : 2) + (r.relevance === "high" ? 0 : 1);
      const byCsr = new Map();
      for (const r of rows) {
        if (!r?.csr_id || !r.project_id) continue;
        if (!byCsr.has(r.csr_id)) byCsr.set(r.csr_id, []);
        byCsr.get(r.csr_id).push(r);
      }
      for (const list of byCsr.values()) list.sort((a, b) => rank(a) - rank(b) || num(b.projects?.year) - num(a.projects?.year) || String(a.projects?.title).localeCompare(String(b.projects?.title)));
      const cache = { uid, byCsr, rows };
      if (currentUid === uid) relatedCache = cache;
      return rows.length ? cache : null;
    })().catch(() => null).finally(() => { relatedLoading = null; });
  }
  return relatedLoading;
}

function relatedProjectCount(related, csrIds) {
  const ids = new Set();
  for (const c of csrIds) for (const r of related.byCsr.get(c) || []) ids.add(r.project_id);
  return ids.size;
}

function relatedDocSummary(related, recs) {
  const withAny = recs.filter((c) => related.byCsr.has(c.csr_id)).length;
  const n = relatedProjectCount(related, recs.map((c) => c.csr_id));
  return `${n} distinct past project${n === 1 ? "" : "s"} suggested for ${withAny} of ${recs.length} recommendation${recs.length === 1 ? "" : "s"}.`;
}

function relevanceChip(value) {
  const k = value === "high" ? "high" : "medium";
  return `<span class="rel-chip rel-${k}">${k} relevance</span>`;
}

function relatedItem(r) {
  const pr = r.projects || {};
  const meta = [pr.programme, pr.year, countriesText(pr.countries)].filter(Boolean).map(esc).join(" · ");
  return `<li class="link-item"><div class="link-head">${relevanceChip(r.relevance)}${hashLink("project", r.project_id, pr.title || r.project_id)}`
    + `${meta ? `<span class="muted small">${meta}</span>` : ""}</div>`
    + (r.rationale ? `<p class="small"><span class="label">Why (AI):</span> ${esc(r.rationale)}</p>` : "") + `</li>`;
}

/** Related past projects of one recommendation: same country first, then the other countries. */
function relatedBlock(related, csrId, { compact = false, card = false } = {}) {
  const list = related.byCsr.get(csrId) || [];
  if (!list.length) {
    return compact ? `<p class="small related-none"><span class="label">Related past projects:</span> <span class="muted">none suggested</span></p>` : "";
  }
  const same = list.filter((r) => r.scope === "same_country");
  const other = list.filter((r) => r.scope !== "same_country");
  const group = (title, rows) => (rows.length ? `<h4 class="related-h">${esc(title)} <span class="muted small">${rows.length}</span></h4><ul class="link-list">${rows.map(relatedItem).join("")}</ul>` : "");
  const inner = group("Same country", same) + group("Other countries", other);
  if (compact) {
    return `<details class="related-mini"><summary><span class="label">Related past projects</span> <span class="ai-note">AI-suggested</span> `
      + `${esc(`${list.length} (${same.length} same country, ${other.length} other ${other.length === 1 ? "country" : "countries"})`)}</summary>${inner}</details>`;
  }
  const head = `<div class="title-row"><h3 class="section-label">Related past projects <span class="ai-note">AI-suggested</span></h3>${infoButton("related", "About related past projects")}</div>`
    + `<p class="muted small">Past projects that worked on the same issues as this recommendation, suggested by AI: a lead to explore, not a link assessed from the project text. Same country first.</p>`;
  return card ? `<div class="card related">${head}${inner}</div>` : head + inner;
}

/** CSRs tab, 2026: related past projects per recommendation, recommendations without any first. */
function relatedCoverageHtml(related, docs, year) {
  const recs = docs.flatMap((d) => arr(d.csrs).map((c) => ({ ...c, country: d.country }))).filter((c) => c.csr_id);
  if (!recs.length) return "";
  const rows = recs.map((c) => {
    const list = related.byCsr.get(c.csr_id) || [];
    return { c, same: list.filter((r) => r.scope === "same_country").length, other: list.filter((r) => r.scope !== "same_country").length,
      high: list.filter((r) => r.relevance === "high").length };
  }).sort((a, b) => (a.same + a.other) - (b.same + b.other) || a.same - b.same || countryName(a.c.country).localeCompare(countryName(b.c.country)) || csrNumber(a.c, b.c));
  const none = rows.filter((r) => !r.same && !r.other).length;
  const noSame = rows.filter((r) => !r.same).length;
  const body = rows.map((r) => [
    td(`${hashLink("csr", r.c.csr_id, `${r.c.country} ${year} · CSR ${r.c.number ?? ""}`)}<div class="muted small">${esc(countryName(r.c.country))}</div>`, "nowrap"),
    td(r.same ? fmtN(r.same) : `<span class="rv-zero">0</span>`, "num"), td(fmtN(r.other), "num"), td(fmtN(r.high), "num"),
  ]);
  return `<details class="card related-coverage"><summary><strong>${year} coverage: related past projects per recommendation</strong> `
    + `<span class="ai-note">AI-suggested</span> <span class="muted small">${esc(`${none} of ${rows.length} recommendations without any related past project, ${noSame} without one in the same country`)}</span></summary>`
    + `<p class="muted small">Recommendations with the fewest suggestions first: where no earlier technical support addressed the issue. ${infoButton("related", "About related past projects")}</p>`
    + table(["Recommendation", "Same country", "Other countries", "High relevance"], body) + `</details>`;
}

// Excel reporting export (SheetJS CE, loaded on first use from cdnjs with its integrity hash).
const REPORT_PROJECT_COLS = "project_id,title,programme,year,countries,policy_area,results,deliverables,detail_page,url";
let xlsxLoading = null;

function loadSheetJs() {
  if (window.XLSX?.utils && window.XLSX?.CFB) return Promise.resolve(window.XLSX);
  if (!xlsxLoading) {
    xlsxLoading = new Promise((resolve, reject) => {
      const s = document.createElement("script");
      s.src = R.SHEETJS_URL;
      s.integrity = R.SHEETJS_SRI;
      s.crossOrigin = "anonymous";
      s.async = true;
      s.onload = () => (window.XLSX?.utils ? resolve(window.XLSX) : reject(new Error("the Excel library did not start")));
      s.onerror = () => { s.remove(); reject(new Error("the Excel library could not be loaded (network or content blocker)")); };
      document.head.append(s);
    }).catch((e) => { xlsxLoading = null; throw e; });
  }
  return xlsxLoading;
}

/** Builds and downloads the reporting workbook. src: { filters, projects: () => Promise<rows>, keepLink?(link) }. */
async function exportExcel(button, note, src) {
  if (!button || button.dataset.busy) return;
  button.dataset.busy = "1";
  button.disabled = true;
  const label = button.textContent;
  const say = (text, error = false) => { if (note?.isConnected) { note.textContent = text; note.classList.toggle("note-error", error); } };
  try {
    button.textContent = "Preparing…";
    say("Loading the Excel library…");
    const X = await loadSheetJs();
    say("Collecting the data…");
    const withReviews = await reviewsAvailable();
    const [projects, links, impacts, anomalies, reviews] = await Promise.all([
      src.projects(),
      fetchAllRows("project_csr_links", "project_id,part_id,level,rationale,basis,csr_parts(csr_id,csrs(country,year,number))", ["project_id", "part_id"]),
      fetchAllRows("impact_reports", "id,project_id,score,level,confidence,created_at", ["created_at", "id"]).catch(() => []),
      fetchAllRows("anomalies", "id,project_id,type", ["id"]).catch(() => []),
      withReviews ? fetchAllRows("link_review_latest", REVIEW_COLS, ["project_id", "part_id"]).catch(() => null) : Promise.resolve(null),
    ]);
    say(`Building the workbook (${fmtN(projects.length)} projects)…`);
    const base = location.href.split("#")[0];
    const model = R.reportingModel({ projects, links: src.keepLink ? links.filter(src.keepLink) : links, impacts, anomalies, reviews }, {
      date: new Date(), filters: src.filters, email: currentEmail, country: countryName, area: areaLabel, anomaly: (t) => ANOMALY_TYPES[t] || t,
      detailUrl: (id) => `${base}#project/${encodeURIComponent(id)}`,
    });
    const data = R.buildWorkbook(X, model);
    saveBlob(model.fileName, new Blob([data], { type: "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet" }));
    say(`Downloaded ${model.fileName}: ${fmtN(model.sheets[0].rows.length)} projects, ${fmtN(model.sheets[1].rows.length)} links.`);
  } catch (e) {
    say(`Could not export to Excel: ${e?.message || e}`, true);
  } finally {
    delete button.dataset.busy;
    if (button.isConnected) { button.disabled = false; button.textContent = label; }
  }
}

// ---------- anomalies ----------

function anomalyFilters(p) {
  const sev = p.get("severity") || "";
  return {
    type: pick(p.get("type"), ANOMALY_TYPES), severity: pick(sev, SEVERITY_OPTIONS),
    programme: pick(p.get("programme"), PROGRAMMES), from: yearOf(p, "from"), to: yearOf(p, "to"),
    country: pick(p.get("country"), COUNTRIES),
  };
}

function anomalyQuery(f, columns = "*", opts = {}) {
  let q = sb.from("anomalies").select(columns, opts).order("severity").order("type").order("year", { ascending: false }).order("id");
  if (f.type) q = q.eq("type", f.type);
  if (f.severity === "") q = q.in("severity", ["high", "medium"]);
  else if (f.severity !== "all") q = q.eq("severity", f.severity);
  if (f.programme) q = q.eq("programme", f.programme);
  if (f.from) q = q.gte("year", f.from);
  if (f.to) q = q.lte("year", f.to);
  if (f.country) q = q.ilike("countries", `%${f.country}%`);
  return q;
}

async function renderAnomalies(p, seq) {
  const page = pageOf(p);
  const f = anomalyFilters(p);
  const res = await fetchPage((opts) => anomalyQuery(f, "*", opts), page);
  if (stale(seq)) return;
  if (res.redirectPage) return redirectToPage("anomalies", p, res.redirectPage);
  const { data, error, count } = res;
  if (error) return showError(error, "Could not load anomalies");

  const form = filterForm("anomalies",
    selectField("type", "Type", ANOMALY_TYPES, f.type, "All types")
    + selectField("severity", "Severity", SEVERITY_OPTIONS, f.severity, null)
    + selectField("programme", "Programme", Object.fromEntries(PROGRAMMES.map((x) => [x, x])), f.programme)
    + yearField("from", "From year", f.from) + yearField("to", "To year", f.to)
    + countriesSelect(f.country));
  const rows = data.map((r) => [
    td(anomalyTitleCell(r), "title-cell"),
    td(esc(ANOMALY_TYPES[r.type] || r.type)), td(badge(r.severity)), td(esc(r.programme)), td(esc(r.year), "nowrap"),
    td(esc(r.countries)), td(esc(r.detail)),
  ]);
  const order = f.severity === "all" ? "Sorted by severity (alphabetical: high, info, low, medium), then type." : "Sorted by severity, then type.";
  view(`<div class="title-row">${heading("Publication anomalies")}${infoButton("anomalies", "About the anomalies")}</div>${form}`
    + `<div class="toolbar"><span class="muted small">${order}</span>`
    + `<span class="toolbar-actions"><span id="csv-note" class="muted small" role="status"></span>`
    + `<button type="button" class="btn btn-secondary" id="csv"${count ? "" : " disabled"}>Download CSV</button></span></div>`
    + `${pager("anomalies", p, page, count)}`
    + table(["Title", "Type", "Severity", "Programme", "Year", "Countries", "Detail"], rows, "No anomaly matches these filters.")
    + (data.length ? pager("anomalies", p, page, count) : ""));
  bindFilters();
  $("csv").addEventListener("click", () => downloadCsv(f));
}

/** Quoted CSV cell; a leading = + - @ tab or CR gets a ' so Excel does not run it as a formula. */
function csvCell(value) {
  let text = String(value ?? "");
  if (/^[=+\-@\t\r]/.test(text)) text = "'" + text;
  return `"${text.replace(/"/g, '""')}"`;
}

/** All matching anomalies up to CSV_LIMIT, in pages of CSV_PAGE (the PostgREST max-rows cap). */
async function fetchAllAnomalies(f, columns) {
  const rows = [];
  let total = null;
  for (let from = 0; from < CSV_LIMIT; from += CSV_PAGE) {
    const to = Math.min(from + CSV_PAGE, CSV_LIMIT) - 1;
    const { data, error, count } = await anomalyQuery(f, columns, from === 0 ? { count: "exact" } : {}).range(from, to);
    if (error) return { error };
    if (from === 0) total = count;
    rows.push(...data);
    if (data.length < to - from + 1) break;
  }
  return { rows, total: total ?? rows.length };
}

async function downloadCsv(f) {
  const button = $("csv");
  button.disabled = true;
  button.textContent = "Preparing…";
  const cols = ["project_id", "title", "programme", "year", "countries", "type", "severity", "detail", "url"];
  const { rows, total, error } = await fetchAllAnomalies(f, cols.join(","));
  button.disabled = false;
  button.textContent = "Download CSV";
  if (error) return showError(error, "Could not export anomalies");
  $("csv-note").textContent = total > rows.length
    ? `Export limited to the first ${rows.length.toLocaleString("en")} of ${total.toLocaleString("en")} rows.` : "";
  saveCsv("anomalies.csv", cols, rows);
}

/** Download rows (objects) as CSV: separator ";" for Excel with Belgian/French regional settings; UTF-8 BOM and CRLF. */
function saveCsv(filename, cols, rows) {
  const lines = [cols.map(csvCell).join(";"), ...rows.map((r) => cols.map((c) => csvCell(r[c])).join(";"))];
  saveBlob(filename, new Blob(["\uFEFF" + lines.join("\r\n") + "\r\n"], { type: "text/csv;charset=utf-8" }));
}

function saveBlob(filename, blob) {
  const url = URL.createObjectURL(blob);
  const a = Object.assign(document.createElement("a"), { href: url, download: filename });
  document.body.appendChild(a);
  a.click();
  a.remove();
  setTimeout(() => URL.revokeObjectURL(url), 1000);
}

// ---------- AI services (tsi-assistant, Claude): storage, Markdown, relay ----------

function storageGet(key) {
  try { return sessionStorage.getItem(key); } catch { return null; }
}

function storageSet(key, value) {
  try { sessionStorage.setItem(key, value); } catch { /* memory copy only */ }
}

/** Forget the conversation and the reports (sign-out or another user). */
function clearAiState() {
  aiEpoch++;
  chatHistory = [];
  chatError = "";
  chatDraft = "";
  impactReports.clear();
  impactErrors.clear();
  impactPending.clear();
  chatPending = false;
  unmountChat();
  closeAssistant({ focusLauncher: false });
  dashCache = null;
  lineageCache.clear();
  lineageState = null;
  ovCache = null;
  ovLoading = null;
  progressCache = null;
  ovState.countryHash = "";
  csrYearsCache = null;
  csrDocsMissing = false;
  feature.reviews = null;
  feature.related = null;
  reviewLatest.clear();
  reviewLinksCache = null;
  reviewStats = null;
  relatedCache = null;
  if ($("review-dialog")?.open) $("review-dialog").close();
  briefingRun?.controller.abort();
  if ($("briefing-dialog")?.open) $("briefing-dialog").close();
  try {
    sessionStorage.removeItem(CHAT_KEY);
    for (let i = sessionStorage.length - 1; i >= 0; i--) {
      const k = sessionStorage.key(i);
      if (k && k.startsWith(IMPACT_KEY)) sessionStorage.removeItem(k);
    }
  } catch { /* storage unavailable */ }
}

/** marked + DOMPurify, loaded once on demand; null if the CDN fails (a later call retries). */
function loadMarkdown() {
  if (markdown) return Promise.resolve(markdown);
  if (!markdownLoading) {
    markdownLoading = Promise.all([import(MARKED_JS), import(PURIFY_JS)])
      .then(([m, p]) => {
        const purify = p.default;
        purify.addHook("afterSanitizeAttributes", (node) => {
          if (node.tagName === "A" && node.hasAttribute("href") && !node.getAttribute("href").startsWith("#")) {
            node.setAttribute("target", "_blank");
            node.setAttribute("rel", "noopener noreferrer");
          }
        });
        markdown = { marked: m.marked, purify };
        return markdown;
      })
      .catch(() => null)
      .finally(() => { markdownLoading = null; });
  }
  return markdownLoading;
}

/** Markdown to HTML sanitised against an allow-list (no img/style/form/id/class; links https:, mailto: or #);
 * plain escaped text when the libraries are not available. */
function md(text) {
  const plain = `<p class="prose">${esc(text)}</p>`;
  if (!markdown) return plain;
  try {
    return markdown.purify.sanitize(markdown.marked.parse(String(text ?? ""), { async: false }), {
      ALLOWED_TAGS: MD_TAGS, ALLOWED_ATTR: MD_ATTRS, ALLOWED_URI_REGEXP: MD_URI, ALLOW_DATA_ATTR: false,
    });
  } catch {
    return plain;
  }
}

function proxyMessage(status, body) {
  const cause = [body?.error, body?.detail].filter((x) => typeof x === "string" && x).join(": ");
  if (status === 403) return "Access not authorised.";
  if (status === 401) return "Your session has expired: sign in again.";
  if (status === 502 || status === 504) {
    return `The analysis service did not answer${cause ? ` (${cause})` : ""}.${status === 504 ? " Please try again." : ""}`;
  }
  if (status >= 200 && status < 300) return "Unexpected response from the analysis service.";
  return cause || `Unexpected error (HTTP ${status}).`;
}

/** POST {service, messages} to PROXY_URL (tsi-assistant) with the session token; returns { answer, saved } or throws. */
async function callProxy(service, messages) {
  const { data } = await sb.auth.getSession();
  const token = data?.session?.access_token;
  if (!token) throw new Error("Your session has expired: sign in again.");
  const controller = new AbortController();
  const timer = setTimeout(() => controller.abort(), PROXY_TIMEOUT_MS);
  let res;
  let body;
  try {
    res = await fetch(PROXY_URL, {
      method: "POST",
      headers: { Authorization: `Bearer ${token}`, apikey: SUPABASE_ANON_KEY, "Content-Type": "application/json" },
      body: JSON.stringify({ service, messages }),
      signal: controller.signal,
    });
    body = await res.json().catch(() => ({}));
  } catch (e) {
    throw new Error(e?.name === "AbortError"
      ? "No answer after 160 seconds. Please try again."
      : "Could not reach the analysis service. Check your connection and try again.");
  } finally {
    clearTimeout(timer);
  }
  if (res.ok && typeof body?.answer === "string") return { answer: body.answer, saved: body.saved === true };
  throw new Error(proxyMessage(res.status, body));
}

// ---------- Word export (docx-js, generated in the browser) ----------

/** docx-js, the export module and marked's lexer, loaded once on demand; rejects if a library cannot be loaded. */
async function loadWord() {
  if (wordLib) return wordLib;
  if (!wordLoading) {
    wordLoading = Promise.all([import(DOCX_JS), import("./word-export.js"), loadMarkdown()])
      .then(([D, W, mdLib]) => {
        if (!mdLib) throw new Error("the Markdown library could not be loaded");
        wordLib = { D, W, lexer: (text) => mdLib.marked.lexer(text) };
        return wordLib;
      })
      .finally(() => { wordLoading = null; });
  }
  return wordLoading;
}

/** Project fields for the Word information table (labels, names; link only to a real https project page). */
function wordProject(r) {
  const src = sourceInfo(r?.url, { listing: r?.detail_page === false });
  return {
    project_id: r?.project_id || "", title: r?.title || r?.project_id || "", programme: r?.programme || "",
    year: r?.year ?? "", countries: countriesText(r?.countries), policy_area: areaLabel(r?.policy_area),
    url: src && !src.listing ? src.url : null,
  };
}

/** Build (lib -> { doc, name }) and download a .docx; progress and errors in `note` (a role=status element). */
async function exportWord(button, note, build) {
  if (!button || button.dataset.busy) return;
  const epoch = aiEpoch;
  const label = button.textContent;
  button.dataset.busy = "1";
  button.disabled = true;
  button.textContent = "Preparing…";
  if (note) { note.textContent = "Preparing document…"; note.classList.remove("note-error"); }
  try {
    const lib = await loadWord().catch((e) => {
      throw new Error(`could not load the Word library (${e?.message || "network error"}). Check your connection and try again.`);
    });
    const { doc, name } = await build(lib);
    const blob = await lib.D.Packer.toBlob(doc);
    if (epoch !== aiEpoch) return;
    saveBlob(name, blob);
    if (note) note.textContent = "Word document downloaded.";
  } catch (e) {
    if (epoch !== aiEpoch) return;
    if (note) {
      note.textContent = `Could not create the Word document: ${e?.message || e}`;
      note.classList.add("note-error");
    }
  } finally {
    delete button.dataset.busy;
    button.textContent = label;
    // The chat button stays disabled while a question is pending or the conversation is empty.
    button.disabled = button.id === "chat-word" ? chatPending || !getChat().length : false;
  }
}

// ---------- chat ----------

const usable = (m) => m && (m.role === "user" || m.role === "assistant") && typeof m.content === "string" && m.content.trim();

/** Conversation of the signed-in user: a stored conversation of another user id is ignored. */
function getChat() {
  if (!chatHistory) {
    try {
      const saved = JSON.parse(storageGet(CHAT_KEY) || "null");
      const ok = saved && saved.uid && saved.uid === currentUid && Array.isArray(saved.messages);
      chatHistory = ok ? saved.messages.filter(usable).slice(-CHAT_MAX) : [];
    } catch {
      chatHistory = [];
    }
  }
  return chatHistory;
}

function setChat(history) {
  chatHistory = history.filter(usable).slice(-CHAT_MAX);
  storageSet(CHAT_KEY, JSON.stringify({ uid: currentUid, messages: chatHistory }));
}

function bubble(m) {
  return m.role === "user"
    ? `<div class="bubble bubble-user"><span class="sr-only">You:</span><p class="prose">${esc(m.content)}</p></div>`
    : `<div class="bubble bubble-assistant"><span class="sr-only">Assistant:</span><div class="md">${md(m.content)}</div></div>`;
}

function paintChat({ scroll = false, status = "" } = {}) {
  const log = $("chat-log");
  if (!log) return;
  const history = getChat();
  let html = history.map(bubble).join("");
  if (!history.length && !chatPending) {
    html = `<div class="examples"><p class="muted">Try an example:</p>`
      + CHAT_EXAMPLES.map((q, i) => `<button type="button" class="example" data-example="${i}">${esc(q)}</button>`).join("") + `</div>`;
  }
  if (chatError) {
    html += `<div class="error" role="alert">${esc(chatError)} `
      + `<button type="button" class="btn btn-secondary" id="chat-retry">Retry</button></div>`;
  }
  log.innerHTML = html;
  $("chat-status").textContent = chatPending ? "Thinking… (up to 2 minutes)" : status;
  $("chat-status").classList.remove("note-error");
  $("chat-send").disabled = chatPending;
  $("chat-new").disabled = chatPending || (!history.length && !chatError);
  $("chat-word").disabled = chatPending || !history.length;
  log.querySelectorAll("[data-example]").forEach((b) => b.addEventListener("click", () => sendChat(CHAT_EXAMPLES[b.dataset.example])));
  $("chat-retry")?.addEventListener("click", () => sendChat(null));
  if (scroll) log.lastElementChild?.scrollIntoView({ block: "start", behavior: "smooth" });
  // The example or Retry button that had the focus was just replaced: give it to the question field.
  if (scroll && (!document.activeElement || document.activeElement === document.body)) $("chat-input")?.focus({ preventScroll: true });
}

/** Send a new question (text) or retry the last unanswered one (null). On error the question stays in the conversation. */
async function sendChat(text) {
  if (chatPending) return;
  const history = [...getChat()];
  if (text !== null) {
    const q = String(text ?? "").trim();
    if (!q) return;
    history.push({ role: "user", content: q.slice(0, MAX_CONTENT) });
    setChat(history);
    chatDraft = "";
    const input = $("chat-input");
    if (input) input.value = "";
  }
  const messages = getChat().filter(usable).map((m) => ({ role: m.role, content: m.content.slice(0, MAX_CONTENT) }));
  if (!messages.length || messages[messages.length - 1].role !== "user") return;
  const epoch = aiEpoch;
  chatError = "";
  chatPending = true;
  paintChat({ scroll: true });
  let status = "";
  try {
    const { answer } = await callProxy("chat", messages);
    if (epoch !== aiEpoch) return;
    setChat([...getChat(), { role: "assistant", content: answer }]);
    status = "Answer received.";
  } catch (e) {
    if (epoch !== aiEpoch) return;
    chatError = e?.message || String(e);
  } finally {
    if (epoch === aiEpoch) chatPending = false;
  }
  paintChat({ scroll: true, status });
}

// ---------- assistant drawer (floating launcher; the chat lives in a right-side panel) ----------

const ASSISTANT_KEY = "tsi-assistant-open"; // "1" while the drawer is open (this browser session)
const MOBILE_QUERY = "(max-width: 600px)";
let assistantMounted = false; // chat markup built inside the drawer
let assistantContext = null; // { kind, label, prompt } of the project or CSR page on screen

function assistantIsMobile() {
  return typeof window.matchMedia === "function" && window.matchMedia(MOBILE_QUERY).matches;
}

function assistantOpen() {
  return Boolean($("assistant") && !$("assistant").hidden);
}

/** Builds the chat (history, examples, form, Word export, New conversation) inside the drawer, once per user. */
function mountChat() {
  const body = $("assistant-body");
  if (!body || assistantMounted) return;
  assistantMounted = true;
  body.innerHTML = `<p class="muted small assistant-intro">Ask about TSI projects and the country-specific recommendations. `
    + `Answers use project records, AI summaries, CSRs and AI-assessed links; deliverable full texts are not searched here. `
    + `Answers are generated by AI (Claude, Anthropic): verify the cited sources. <a href="#guide/assistant">How it works</a></p>`
    + `<div id="assistant-context" class="assistant-context"></div>`
    + `<div id="chat-log" class="chat-log assistant-log"></div>`
    + `<p id="chat-status" class="chat-status" role="status"></p>`
    + `<form id="chat-form" class="chat-form"><label for="chat-input">Your question</label>`
    + `<div class="chat-row"><textarea id="chat-input" rows="3" maxlength="${MAX_CONTENT}" `
    + `placeholder="e.g. Which projects support pension reforms in Portugal?"></textarea>`
    + `<button type="submit" class="btn" id="chat-send">Send</button></div>`
    + `<div class="chat-actions"><span class="muted small">Enter to send · Shift+Enter for a new line</span>`
    + `<span class="toolbar-actions"><button type="button" class="btn btn-secondary" id="chat-word" `
    + `aria-label="Export this conversation to Word">Export to Word</button>`
    + `<button type="button" class="btn btn-secondary" id="chat-new">New conversation</button></span></div></form>`;
  const input = $("chat-input");
  input.value = chatDraft;
  input.addEventListener("input", () => { chatDraft = input.value; });
  input.addEventListener("keydown", (e) => {
    if (e.key === "Enter" && !e.shiftKey && !e.isComposing) {
      e.preventDefault();
      $("chat-form").requestSubmit();
    }
  });
  $("chat-form").addEventListener("submit", (e) => { e.preventDefault(); sendChat(input.value); });
  $("chat-word").addEventListener("click", () => exportWord($("chat-word"), $("chat-status"), ({ D, W, lexer }) => {
    const date = new Date();
    return { doc: W.buildChatDocument(D, lexer, { messages: getChat(), email: currentEmail, date }), name: W.chatFileName(date) };
  }));
  $("chat-new").addEventListener("click", () => {
    setChat([]);
    chatError = "";
    paintChat();
    input.focus();
  });
  paintChat();
  paintAssistantContext();
  // Markdown libraries arrive later: repaint the answers once they are there.
  if (!markdown) loadMarkdown().then((lib) => { if (lib && assistantMounted) paintChat(); });
}

/** Drops the chat markup (sign-out or another user): rebuilt on the next opening. */
function unmountChat() {
  assistantMounted = false;
  const body = $("assistant-body");
  if (body) body.innerHTML = "";
}

/** "Ask about this project/CSR" chip: pre-fills the question, the user still presses Send. */
function paintAssistantContext() {
  const box = $("assistant-context");
  if (!box) return;
  const c = assistantContext;
  if (!c) {
    box.innerHTML = "";
    return;
  }
  box.innerHTML = `<button type="button" class="chip" id="assistant-ask">Ask about this ${{ csr: "CSR", csrdoc: "CSR document" }[c.kind] || "project"}</button>`
    + `<span class="muted small assistant-context-label">${esc(c.label)}</span>`;
  $("assistant-ask").addEventListener("click", () => {
    const input = $("chat-input");
    if (!input) return;
    input.value = c.prompt.slice(0, MAX_CONTENT);
    chatDraft = input.value;
    input.focus();
    input.setSelectionRange(input.value.length, input.value.length);
  });
}

function syncAssistantMode() {
  const panel = $("assistant");
  if (!panel) return;
  const modal = assistantOpen() && assistantIsMobile();
  panel.setAttribute("aria-modal", String(modal));
  document.body.classList.toggle("assistant-modal", modal);
}

function openAssistant({ focus = true } = {}) {
  const panel = $("assistant");
  const launcher = $("assistant-launcher");
  if (!panel || !launcher || $("view").hidden) return;
  mountChat();
  panel.hidden = false;
  launcher.hidden = true;
  launcher.setAttribute("aria-expanded", "true");
  storageSet(ASSISTANT_KEY, "1");
  if (trackOn) chatTracker.start("chat", null);
  syncAssistantMode();
  paintAssistantContext();
  if (focus) ($("chat-input") || $("assistant-title")).focus();
}

function closeAssistant({ focusLauncher = true } = {}) {
  const panel = $("assistant");
  const launcher = $("assistant-launcher");
  if (!panel || panel.hidden) return;
  panel.hidden = true;
  chatTracker.end();
  launcher.setAttribute("aria-expanded", "false");
  launcher.hidden = $("view").hidden;
  try { sessionStorage.removeItem(ASSISTANT_KEY); } catch { /* storage unavailable */ }
  syncAssistantMode();
  if (focusLauncher && !launcher.hidden) launcher.focus();
}

/** Launcher, Close, Escape, focus trap (full-screen mobile mode only), links that leave the drawer on mobile. */
function bindAssistant() {
  const panel = $("assistant");
  const launcher = $("assistant-launcher");
  if (!panel || !launcher) return;
  launcher.addEventListener("click", () => openAssistant());
  $("assistant-close").addEventListener("click", () => closeAssistant());
  panel.addEventListener("keydown", (e) => {
    if (e.key === "Escape") {
      e.stopPropagation(); // the page-level Escape handler must not act as well
      closeAssistant();
      return;
    }
    if (e.key !== "Tab" || !assistantIsMobile()) return;
    const items = [...panel.querySelectorAll('a[href], button:not([disabled]), textarea:not([disabled]), input:not([disabled]), [tabindex]:not([tabindex="-1"])')]
      .filter((el) => el.offsetParent !== null || el === document.activeElement);
    if (!items.length) return;
    const first = items[0];
    const last = items[items.length - 1];
    if (e.shiftKey && document.activeElement === first) { e.preventDefault(); last.focus(); }
    else if (!e.shiftKey && document.activeElement === last) { e.preventDefault(); first.focus(); }
  });
  panel.addEventListener("click", (e) => {
    const link = e.target instanceof Element ? e.target.closest('a[href^="#"]') : null;
    if (link && assistantIsMobile()) closeAssistant({ focusLauncher: false }); // full screen: show the page the link opens
  });
}

// ---------- impact ----------

function impactMessage(r) {
  const countries = (Array.isArray(r.countries) ? r.countries : []).join(", ");
  return `Project: ${r.title} (id ${r.project_id}; ${[r.programme, r.year].filter(Boolean).join(" ")}; countries ${countries})`;
}

function loadReport(id) {
  if (impactReports.has(id)) return impactReports.get(id);
  try {
    const saved = JSON.parse(storageGet(IMPACT_KEY + id) || "null");
    if (saved && saved.uid && saved.uid === currentUid && typeof saved.report === "string" && saved.report.trim()) {
      impactReports.set(id, saved);
      return saved;
    }
  } catch { /* ignore a corrupt entry */ }
  return null;
}

function saveReport(id, report) {
  const entry = { uid: currentUid, report, at: new Date().toISOString() };
  impactReports.set(id, entry);
  storageSet(IMPACT_KEY + id, JSON.stringify(entry));
}

async function renderImpactSearch(p, seq) {
  const q = (p.get("q") || "").trim();
  let results = "";
  if (q) {
    const { data, error } = await sb.from("projects").select("project_id,title,programme,year,countries")
      .ilike("title", likePattern(q)).order("year", { ascending: false }).order("title").limit(10);
    if (stale(seq)) return;
    if (error) return showError(error, "Could not search projects");
    const items = data.map((r) => `<li>${hashLink("impact", r.project_id, r.title)} `
      + `<span class="muted small">${esc([r.programme, r.year, countriesText(r.countries)].filter(Boolean).join(" · "))}</span></li>`).join("");
    results = `<div class="card"><h3>Projects</h3>`
      + (items ? `<ul class="pick-list">${items}</ul>` : `<p class="empty">No project title matches this search.</p>`)
      + (data.length === 10 ? `<p class="muted small">First 10 matches: refine the search if needed.</p>` : "") + `</div>`;
  }
  view(`${heading("Impact analysis")}`
    + `<p class="muted">Choose a project, or open one from the project list. The analysis combines the project data with a web search `
    + `and ends with an impact score. Takes 30 s to 2 min; uses web search (Anthropic).</p>`
    + filterForm("impact", textField("q", "Search project titles", q, "e.g. wildfire, pension")) + results);
  bindFilters();
}

function paintImpact(id, status = "") {
  const box = $("impact-report");
  if (!box || box.dataset.id !== id) return;
  const pending = impactPending.has(id);
  const saved = loadReport(id);
  const err = impactErrors.get(id);
  $("impact-run").disabled = pending;
  $("impact-status").textContent = pending ? "Analysing… (30 s to 2 minutes)" : status;
  let html = err ? `<div class="error" role="alert">${esc(err)}</div>` : "";
  if (saved) {
    const at = new Date(saved.at);
    html += `<div class="card"><div class="toolbar"><h3 class="section-label">Impact report <span class="ai-note">AI-generated</span></h3>`
      + `<span class="toolbar-actions"><span id="copy-note" class="muted small" role="status"></span>`
      + `<button type="button" class="btn btn-secondary" id="impact-copy">Copy report</button>`
      + `<button type="button" class="btn btn-secondary" id="impact-word" aria-label="Export this report to Word">Export to Word</button></span></div>`
      + (Number.isNaN(at.getTime()) ? "" : `<p class="muted small">Generated ${esc(at.toLocaleString("en-GB"))}</p>`)
      + `<div class="md">${md(saved.report)}</div></div>`;
  }
  box.innerHTML = html;
  $("impact-copy")?.addEventListener("click", async () => {
    try {
      await navigator.clipboard.writeText(saved.report);
      $("copy-note").textContent = "Copied.";
    } catch {
      $("copy-note").textContent = "Copy failed: select the text manually.";
    }
  });
  $("impact-word")?.addEventListener("click", () => {
    const project = impactProject?.project_id === id ? impactProject : { project_id: id };
    exportWord($("impact-word"), $("copy-note"), ({ D, W, lexer }) => ({
      doc: W.buildImpactDocument(D, lexer, { project: wordProject(project), report: saved.report, at: saved.at, source: "web", email: currentEmail }),
      name: W.impactFileName(id, new Date(saved.at)),
    }));
  });
}

async function runImpact(r) {
  const id = r.project_id;
  if (impactPending.has(id)) return;
  const epoch = aiEpoch;
  impactPending.add(id);
  impactErrors.delete(id);
  paintImpact(id);
  let status = "";
  try {
    const { answer: report, saved } = await callProxy("impact", [{ role: "user", content: impactMessage(r).slice(0, MAX_CONTENT) }]);
    if (epoch !== aiEpoch) return;
    saveReport(id, report);
    status = saved ? "Report ready. Saved to the project's analyses." : "Report ready (not saved to the project's analyses).";
    dashCache = null; // dashboard figures now out of date
    if (saved) refreshPrevious(id).catch(() => {});
  } catch (e) {
    if (epoch !== aiEpoch) return;
    impactErrors.set(id, e?.message || String(e));
  } finally {
    if (epoch === aiEpoch) impactPending.delete(id);
  }
  paintImpact(id, status);
}

async function renderImpact(id, p, seq) {
  if (!id) return renderImpactSearch(p, seq);
  const [proj, previous] = await Promise.all([
    sb.from("projects").select("project_id,title,programme,year,countries,policy_area,url,detail_page").eq("project_id", id).single(),
    impactHistoryQuery(id, 5),
    loadMarkdown(),
  ]);
  if (stale(seq)) return;
  if (proj.error) return showError(proj.error, proj.error.code === "PGRST116" ? "Project not found" : "Could not load the project");
  const r = proj.data;
  impactProject = r;
  const meta = [r.programme, r.year, countriesText(r.countries)].filter(Boolean).map(esc).join(" · ");
  view(`<a class="back" href="#impact">← Choose another project</a>`
    + `<div class="card"><p class="muted small">Impact analysis</p><h2 class="detail-title" tabindex="-1">${esc(r.title)}</h2>`
    + `<p class="muted small">${meta}${meta ? " · " : ""}${hashLink("project", r.project_id, "Project details")}</p>`
    + `<div id="impact-previous" class="impact-previous" data-id="${esc(r.project_id)}">${previous.error ? "" : previousImpactsHtml(previous.data)}</div>`
    + `<div class="toolbar"><span class="toolbar-actions"><button type="button" class="btn" id="impact-run">Run impact analysis</button>`
    + `<span class="muted small">Takes 30 s to 2 min; uses web search (Anthropic).</span></span></div>`
    + `<p id="impact-status" class="chat-status" role="status"></p></div>`
    + `<div id="impact-report" data-id="${esc(r.project_id)}"></div>`);
  $("impact-run").addEventListener("click", () => runImpact(r));
  bindReportToggles($("impact-previous"));
  paintImpact(r.project_id);
}

// ---------- saved impact analyses (impact_reports) ----------

function fmtDate(value) {
  const d = new Date(value);
  return Number.isNaN(d.getTime()) ? "" : d.toLocaleDateString("en-GB", { day: "numeric", month: "short", year: "numeric" });
}

/** "7/10 (strong, high confidence) — 16 Oct 2026" from an impact_reports row (plain text: escape before use). */
function impactLine(r) {
  const score = Number.isFinite(r?.score) ? `${r.score}/10` : "no score";
  const qual = [r?.level, r?.confidence ? `${r.confidence} confidence` : ""].filter(Boolean).join(", ");
  const date = fmtDate(r?.created_at);
  return `${score}${qual ? ` (${qual})` : ""}${date ? ` — ${date}` : ""}`;
}

function reportToggle(r) {
  if (!/^\d+$/.test(String(r?.id ?? ""))) return "";
  const id = String(r.id);
  return `<span class="muted small" id="word-note-${id}" role="status"></span>`
    + `<button type="button" class="btn btn-secondary" data-report="${id}" aria-expanded="false" aria-controls="report-${id}">Show report</button>`
    + `<button type="button" class="btn btn-secondary" data-word="${id}" aria-label="Export this saved report to Word">Export to Word</button>`;
}

function reportBody(r) {
  return /^\d+$/.test(String(r?.id ?? "")) ? `<div class="md report-body" id="report-${r.id}" hidden></div>` : "";
}

/** "Show report" buttons: the report text is loaded on first opening, rendered through md().
 * "Export to Word" buttons: the saved report (impact_reports row) as a .docx. */
function bindReportToggles(container) {
  container?.querySelectorAll("button[data-word]").forEach((button) => {
    button.addEventListener("click", () => exportWord(button, $(`word-note-${button.dataset.word}`), async ({ D, W, lexer }) => {
      const { data: row, error } = await sb.from("impact_reports")
        .select("id,project_id,title,score,level,confidence,report,created_at,source").eq("id", button.dataset.word).single();
      if (error) throw new Error(error.message || "Could not load the report");
      if (!row?.report?.trim()) throw new Error("This saved analysis has no report text.");
      const project = impactProject?.project_id === row.project_id ? impactProject : { project_id: row.project_id, title: row.title };
      return {
        doc: W.buildImpactDocument(D, lexer, {
          project: wordProject(project), report: row.report, at: row.created_at, source: row.source || "",
          score: { score: row.score, level: row.level, confidence: row.confidence }, email: currentEmail,
        }),
        name: W.impactFileName(row.project_id, new Date(row.created_at)),
      };
    }));
  });
  container?.querySelectorAll("button[data-report]").forEach((button) => {
    button.addEventListener("click", async () => {
      const id = button.dataset.report;
      const body = $(`report-${id}`);
      if (!body) return;
      if (body.dataset.loaded) {
        body.hidden = !body.hidden;
      } else {
        button.disabled = true;
        button.textContent = "Loading…";
        const [res] = await Promise.all([
          sb.from("impact_reports").select("report").eq("id", id).single(),
          loadMarkdown(),
        ]);
        button.disabled = false;
        if (!body.isConnected) return;
        if (res.error) {
          body.innerHTML = `<p class="error">${esc(res.error.message || "Could not load the report")}</p>`;
        } else {
          body.innerHTML = md(res.data?.report || "");
          body.dataset.loaded = "1";
        }
        body.hidden = false;
      }
      button.textContent = body.hidden ? "Show report" : "Hide report";
      button.setAttribute("aria-expanded", String(!body.hidden));
    });
  });
}

function latestImpactHtml(rows) {
  const r = rows?.[0];
  if (!r) return `<p class="muted small">No saved impact analysis yet.</p>`;
  return `<div class="impact-latest"><div class="toolbar"><p class="impact-line"><strong>Latest impact analysis:</strong> `
    + `${esc(impactLine(r))} <span class="ai-note">AI-generated</span></p>`
    + `<span class="toolbar-actions">${reportToggle(r)}</span></div>${reportBody(r)}</div>`;
}

function previousImpactsHtml(rows) {
  if (!rows?.length) return "";
  const items = rows.map((r) => `<li><div class="toolbar"><span>${esc(impactLine(r))}`
    + `${r.source ? ` <span class="muted small">· ${esc(r.source)}</span>` : ""}</span>`
    + `<span class="toolbar-actions">${reportToggle(r)}</span></div>${reportBody(r)}</li>`).join("");
  return `<h3>Previous analyses</h3><ul class="prev-list">${items}</ul>`;
}

function impactHistoryQuery(id, limit) {
  return sb.from("impact_reports").select("id,score,level,confidence,created_at,source")
    .eq("project_id", id).order("created_at", { ascending: false }).order("id", { ascending: false }).limit(limit);
}

/** After a new analysis: reload the "Previous analyses" list if the project is still on screen. */
async function refreshPrevious(id) {
  const res = await impactHistoryQuery(id, 5);
  const box = $("impact-previous");
  if (res.error || !box || box.dataset.id !== id) return;
  box.innerHTML = previousImpactsHtml(res.data);
  bindReportToggles(box);
}

// ---------- "About this chart" dialogs (static English text written here: no database string) ----------

const INFO = {
  progress: () => ({
    title: "CSR progress and project support",
    html: `<p>Each year the European Commission assesses how far every country-specific recommendation has been implemented
      (ECFIN CSR database). The page uses the <strong>latest assessment</strong> of each recommendation: no progress, limited
      progress, some progress, substantial progress or full implementation. Recommendations marked <em>not assessed</em> and the
      2026 Commission recommendations (not yet assessed) are left out.</p>
      <h3>Groups</h3><ul>
      <li><strong>Supported</strong>: at least one TSI or SRSP project is linked to one of its sub-parts (AI-assessed links;
      <em>Direct links only</em> keeps the direct ones).</li>
      <li><strong>Not supported</strong>: no linked project.</li></ul>
      <p><strong>Progressing</strong> = some progress, substantial progress or full implementation. The <strong>gap</strong> is the
      share progressing among supported recommendations minus the share among the others, in percentage points. The gap
      <strong>within the same CSR year</strong> compares recommendations of the same year only, then averages the yearly gaps
      (weighted by the size of both groups): older recommendations have had more time to progress and more time to be supported,
      so this figure is the fairer one.</p>
      <p>By default the period ends with the last CSR year that has a supported recommendation: projects supporting the most recent
      recommendations are not published yet.</p>
      <p><em>Only projects started by the assessment year</em> keeps a project only when its year is at most the year of the
      assessment (projects without a year are then left out), so that the support came before the assessment.</p>
      <h3>Reading with care</h3><ul>
      <li>A correlation, not proof of impact: countries committed to a reform may also ask for more support.</li>
      <li>One assessment per recommendation (the latest), not the year-by-year history.</li>
      <li>Small groups (fewer than 10 recommendations on one side) are greyed: too few to compare.</li>
      <li>The project–CSR links are assessed by AI: verify before use (link review).</li></ul>`,
  }),
  results: () => ({
    title: "Projects by results",
    html: `<p>The value comes from the official heading of the results section on the project's detail page
      (reforms-investments.ec.europa.eu):</p>
      <ul><li><strong>Results achieved</strong>: a completed project presenting its results.</li>
      <li><strong>Results expected</strong>: an ongoing project announcing the results it expects.</li>
      <li><strong>Not stated</strong>: projects known only by their title (no detail page: TSI country factsheets, SRSP PDF list),
      and detail pages without a results section (see the <em>Missing results</em> anomaly).</li></ul>
      <p>This is <strong>not</strong> an assessment of success: for that, see the impact score of the impact analyses.</p>`,
  }),
  anomalies: () => {
    const y = new Date().getFullYear();
    return {
      title: "Publication anomalies",
      html: `<p>A deterministic, rule-based report (no AI) on the publication of the projects. The reference year is the current year
        (${y}); severities shift automatically with the year.</p>
        <h3>Types</h3><dl class="info-dl">
        <dt>TSI project not published</dt><dd>TSI project listed in a country factsheet but without a published detail page.</dd>
        <dt>Published without deliverable</dt><dd>Published detail page, but no deliverable attached.</dd>
        <dt>Missing results</dt><dd>Published detail page without a results section.</dd>
        <dt>Possible duplicate</dt><dd>Two projects of the same programme and country with near-identical titles. Often recurring
          projects or successive phases: check manually.</dd>
        <dt>Orphan deliverable</dt><dd>Deliverable linked to no known project.</dd>
        <dt>No CSR link</dt><dd>Published TSI project for which the AI found no supported CSR sub-part.</dd>
        <dt>SRSP known by title only</dt><dd>SRSP project known only by its title: expected for this programme.</dd></dl>
        <h3>Severity</h3><ul>
        <li><strong>High</strong>: TSI project not published, from ${y - 2} or earlier (2 or more years before ${y}).</li>
        <li><strong>Medium</strong>: TSI project not published from ${y - 1} or of unknown year; published without deliverable while
          the results are &ldquo;achieved&rdquo;; possible duplicate; orphan deliverable.</li>
        <li><strong>Low</strong>: TSI project not published from ${y}; published without deliverable with results &ldquo;expected&rdquo;;
          missing results section.</li>
        <li><strong>Info</strong>: SRSP known by title only; TSI project without CSR link. Not errors: context.</li></ul>`,
    };
  },
  impact: () => ({
    title: "Impact analyses",
    html: `<p>Generated by AI (Claude, Anthropic) from the project record and a web search. The statistics use the
      <strong>latest analysis of each project</strong>; with few analyses they are only indicative.</p>
      <h3>Impact score (0 to 10)</h3><ul>
      <li><strong>0</strong>: no evidence of positive impact.</li>
      <li><strong>1–3, weak</strong>: outputs delivered but little uptake. A reform adopted outside of, or against, the project
        counts as no uptake.</li>
      <li><strong>4–6, moderate</strong>: reform adopted or partly implemented; attribution plausible but partial or early.</li>
      <li><strong>7–8, strong</strong>: documented outcomes, plausibly attributable to the project.</li>
      <li><strong>9–10, strong</strong>: well-documented, sustained outcomes, clearly attributable.</li></ul>
      <h3>Confidence</h3><p><strong>High</strong>, <strong>medium</strong> or <strong>low</strong>: the strength of the evidence
      found, not of the impact.</p>`,
  }),
  csrlevels: () => ({
    title: "CSR documents, recommendations and sub-parts",
    html: `<p>Country-specific recommendations are read on three levels:</p><ol>
      <li><strong>CSR document</strong>: one per Member State and year, so 27 per year. From 2016 to 2025 it is the
        <em>Council recommendation</em> adopted in the European Semester. For 2026 it is the <em>Commission recommendation</em>
        (the Commission's proposal, published in spring), not yet adopted by the Council.</li>
      <li><strong>Recommendations</strong>: the numbered recommendations of a document (1 to 5).</li>
      <li><strong>Sub-parts</strong>: each recommendation split by the ECFIN CSR database into its distinct requests
        (e.g. 3.1, 3.2). Project links are assessed at this level. The 2026 recommendations have no sub-part: each
        recommendation counts as a single sub-part.</li></ol>
      <p>The 2026 recommendations are <strong>not yet assessed</strong>, and no project is linked to them yet: a recommendation
        can only be supported by projects of the following years (2027 onwards).</p>`,
  }),
  assessments: () => ({
    title: "CSR assessment",
    html: `<p>The Commission's latest yearly assessment of the implementation of each country-specific recommendation, from the
      ECFIN CSR database.</p><ul>
      <li>From <strong>Full implementation</strong> through <strong>Substantial progress</strong>, <strong>Some progress</strong>
        and <strong>Limited progress</strong> to <strong>No progress</strong>.</li>
      <li><strong>Not assessed</strong>: the Commission did not assess the recommendation that year.</li>
      <li><strong>Not yet assessed</strong>: no assessment yet, for instance the 2026 Commission recommendations.</li></ul>
      <p>Assessments are counted per recommendation; a document's <em>main assessment</em> is the most frequent latest
        assessment of its recommendations.</p>`,
  }),
  linklevel: () => ({
    title: "Link level",
    html: `<p>How strongly a project supports a recommendation. The level is <strong>assessed by AI</strong> from the published
      project text, one CSR sub-part at a time: it is not an official classification, so verify before use.</p><ul>
      <li><strong>Direct</strong>: the project's objective is to design or implement what the CSR recommends
        (e.g. the CSR asks for a pension reform and the project supports that very reform).</li>
      <li><strong>Partial</strong>: the project contributes to one aspect of the recommendation, or to a precondition for it
        (e.g. the CSR asks for a more efficient public administration and the project digitalises one service).</li></ul>
      <p>In the Sankey, Network and Sunburst views a CSR Recommendation counts as <em>direct</em> when at least one of its
      sub-part links is direct, and <em>partial</em> otherwise. <strong>All</strong> shows both.</p>
      <p>This is not the CSR claimed by the country in its request (self-declared in JIRA): that one will appear in the
      <em>Initial request</em> stage once the JIRA export is available.</p>`,
  }),
  links: () => ({
    title: "Project–CSR links",
    html: `<p>Links assessed by AI from the project text: verify before use.</p><ul>
      <li><strong>Direct</strong>: the project's objective is to design or implement what the CSR sub-part recommends.</li>
      <li><strong>Partial</strong>: the project contributes to one aspect of the recommendation, or to a precondition.</li></ul>
      <p>So far only projects with a detail page are linked; projects known only by their title are not yet linked.</p>
      <p>The Lineage tab shows these links in four views: <em>Sankey</em>, <em>Network</em> and <em>Sunburst</em> for all countries
      at once, and <em>By country</em>, the chain below for one country.</p>
      <h3>The chain, stage by stage (By country)</h3><ol>
      <li><strong>CSR document</strong>: the European Semester proposes the recommendations addressed to the country (one document per year).</li>
      <li><strong>Recommendation</strong>: each numbered recommendation, with its latest assessment.</li>
      <li><strong>Initial request</strong> (pending): the country's request for support, with the CSRs it claims to address.
        Not public: it will come from the JIRA export.</li>
      <li><strong>Project</strong>: the selected project, linked to the recommendations by the AI assessment above.</li>
      <li><strong>Outcome</strong>: the project's results status, its deliverables and its latest impact analysis.</li></ol>`,
  }),
  overview: () => ({
    title: "Lineage views",
    html: `<p>The Lineage tab has four views. The first three show <strong>all countries at once</strong>; the last one follows a
      single country stage by stage.</p>
      <p>A <strong>CSR Recommendation</strong> (in these counts) is one recommendation linked to one project: the project's links to the sub-parts of a recommendation
      are grouped, and it is <em>direct</em> if at least one of them is direct. The country is that of the recommendation, so a
      project supporting recommendations of three countries counts in each of them. Only recommendations with at least one linked
      project appear (none of the 2026 Commission recommendations yet).</p>
      <h3>Filters</h3><p><strong>CSR years</strong> (year of the recommendation) and <strong>link level</strong> (all, direct, partial)
      apply to the Sankey, the Network and the Sunburst and are kept when you switch between them. The summary line gives the CSR Recommendations,
      distinct recommendations, projects and countries selected and the share of direct CSR Recommendations.</p>
      <h3>The four views</h3><dl class="info-dl">
      <dt>Sankey: where support concentrates</dt><dd>Country → policy area of the project → programme → stated outcome; ribbon width is the
        number of pairs. Click a country to split it by CSR year (the other countries merge into one block).</dd>
      <dt>Network: projects that bridge countries</dt><dd>Countries as dark discs (size = CSR Recommendations), projects as dots (fill = programme,
        ring = outcome). Opens with all projects; choose <em>Linked to several countries only</em> in <em>Projects</em> to keep the multi-country ones. Click a country to
        highlight its projects, a project for its link; drag, zoom and search.</dd>
      <dt>Sunburst: each country's policy mix</dt><dd>Rings: country → policy area → project (coloured by outcome); angle = pairs.
        Click a segment to zoom in, the centre to zoom out.</dd>
      <dt>By country: the full chain</dt><dd>One country's CSR documents, recommendations, initial request (pending JIRA), projects and
        outcomes, with every recommendation and project as a node.</dd></dl>
      <p>Below each chart, the selected country or project links to its By country view, its projects or its project page.
      Counts are CSR Recommendations, not projects: a project linked to three recommendations weighs three. Links assessed by AI: verify before use.</p>`,
  }),
  briefing: () => ({
    title: "Country briefing",
    html: `<p>A Word document (.docx) on one country, for a period of years and optionally one policy area, built in your
      browser from the explorer's data, with an optional summary written by AI.</p>
      <h3>Contents</h3><ol>
      <li><strong>Key messages</strong> (AI-generated, 5 to 7 bullets).</li>
      <li><strong>Key figures</strong>: projects by programme, year and policy area, results status, deliverables, project–CSR
        links, share of the recommendations supported by a project, impact analyses and anomalies.</li>
      <li><strong>Country-specific recommendations</strong>: the latest document of the period, then the earlier ones, each
        recommendation with its latest assessment and number of supporting projects.</li>
      <li><strong>Lineage</strong>: each recommendation with its supporting projects (level, results, deliverables, impact score).</li>
      <li><strong>Results and impact</strong>: projects with results, latest impact analyses with their verdict.</li>
      <li><strong>Gaps and opportunities</strong>: assessed recommendations without a supporting project (Limited or No progress first),
        with AI suggestions to verify, and, for the latest (2026) recommendations, the related past projects suggested by AI
        (top 3 each), when available.</li>
      <li><strong>Data quality</strong> (anomalies) and an <strong>annex</strong> with the sources and the method.</li></ol>
      <h3>Limits</h3><ul>
      <li>The AI summary is one call to Claude (Anthropic), written only from the figures of the document (no web search): its parts
        are marked <em>AI-generated</em>. If it fails, the document is still created, without them, and says so.</li>
      <li>Links between projects and CSRs are assessed by AI; projects known only by their title are not linked yet, so a gap is
        not proof of absence of support.</li>
      <li>Policy area filter: projects by their policy area; CSRs through the corresponding ECFIN policy areas (the two
        classifications differ), through a supporting project of that area, or, for the 2026 recommendations not yet classified,
        through key words of their text.</li>
      <li>Coverage leaves out the Commission-stage recommendations (2026), not yet adopted nor assessed.</li></ul>`,
  }),
  linkreview: () => ({
    title: "Link review",
    html: `<p>The project–CSR links are <strong>assessed by AI</strong> from the project text. Business users check them: wherever a
      link is shown (project page, recommendation and document pages, lineage By country), its chip gives the latest review and
      <em>Review</em> opens a short form.</p><ul>
      <li><strong>Confirmed</strong>: the project does support this CSR sub-part at the level shown.</li>
      <li><strong>Rejected</strong>: the link is wrong (the project does not address this request).</li>
      <li><strong>To review</strong>: unsure; needs a closer look, for instance by the country desk.</li>
      <li><strong>Not reviewed</strong>: nobody has reviewed the link yet.</li></ul>
      <p>A review is shared with all users and kept: a new review replaces the current status, the history (who, when, comment)
      stays visible in the form. Reviews do not change the AI assessment itself; the Excel export gives both.</p>
      <p><em>Links to review</em> (from the Projects tab or the dashboard) lists the links not reviewed, to review or rejected, with
      quick <em>Confirm</em> and <em>Reject</em> buttons.</p>`,
  }),
  related: () => ({
    title: "Related past projects (AI-suggested)",
    html: `<p>The 2026 Commission recommendations are too recent to be supported by a project: a recommendation of a given year can
      only be supported by projects of the following years. To help prepare the support, an AI model suggests, for each 2026
      recommendation, <strong>past projects that worked on the same issues</strong>:</p><ul>
      <li><strong>Same country</strong>: earlier support to the same Member State, to build on.</li>
      <li><strong>Other countries</strong>: comparable projects elsewhere, to learn from.</li>
      <li><strong>Relevance</strong>: high or medium, with a short rationale.</li></ul>
      <p>These are leads, not links assessed from the project text, and not a statement that the recommendation is covered: verify
      before use. The CSRs tab (2026) counts them per country and lists the recommendations without any related past project.</p>`,
  }),
  request: () => ({
    title: "Initial request (pending)",
    html: `<p>Before a project is selected, the Member State submits a <strong>request for support</strong>. In it the country
      declares the CSRs the request addresses: claiming CSR support improves its chances of selection. These claimed CSRs are
      self-declared and recorded in <strong>JIRA</strong>; the request is not public.</p>
      <p>The column is a placeholder: no request is shown yet and the lines from the recommendations to the projects cross it
      unchanged. Once the JIRA export is provided, the view will show, for each project, the CSRs <strong>claimed</strong> by
      the country next to the links <strong>assessed by AI</strong>, and the gaps between them:</p><ul>
      <li><strong>Claimed but not confirmed</strong>: a CSR declared in the request that the project text does not support.</li>
      <li><strong>Confirmed but not claimed</strong>: a CSR the project supports although the request did not mention it.</li></ul>`,
  }),
  monitoring: () => ({
    title: "Usage monitoring",
    html: `<p>To improve the explorer, the pages each user opens and the time spent on them are recorded with their e-mail
      address. Only administrators can see them, here.</p>
      <ul><li><strong>View</strong>: the section of the explorer (Dashboard, Projects, a project page, Lineage, Guide, Admin…). Opening the
      assistant counts as <em>Assistant (chat)</em>, for as long as it stays open (it overlaps the page behind it).</li>
      <li><strong>Detail</strong>: the parameters of the page only (a project or CSR id, a lineage view, a country, filters). Texts typed by
      the user (searches, questions to the assistant) are never recorded.</li>
      <li><strong>Duration</strong>: visible, active time only. The clock stops while the tab is hidden and 5 minutes after the last
      mouse, keyboard or touch input; views shorter than 2 seconds are not recorded.</li>
      <li><strong>Session</strong>: one browser tab of one user, from the first view to the tab being closed.</li>
      <li><strong>Days</strong> follow the time zone of this browser.</li></ul>
      <p>Figures start when the monitoring was installed (database migration 007); a closed laptop or a lost network connection can drop
      the last view.</p>`,
  }),
};

const INFO_GUIDE = { monitoring: "usage", linkreview: "reviews", related: "csrs", briefing: "briefing", results: "projects", anomalies: "anomalies", impact: "impact", csrlevels: "csrs", assessments: "csrs", links: "lineage", linklevel: "lineage", request: "lineage", overview: "lineage" };

// Icone « i » des boutons d'information (SVG, couleur du texte).
const INFO_ICON = `<svg class="info-icon" viewBox="0 0 16 16" aria-hidden="true" focusable="false"><circle cx="8" cy="4.1" r="1.35"/><path d="M6.4 6.7h2.4v5.6h1.1v1.4H6.2v-1.4h1.1V8.1h-.9z"/></svg>`;
// Meme icone dans un texte (guide) : rappel visuel du bouton.
const INFO_GLYPH = `<span class="info-glyph" aria-label="information button">${INFO_ICON}</span>`;

function infoButton(key, label = "About this chart") {
  return `<button type="button" class="info-btn" data-info="${key}" aria-label="${esc(label)}" aria-haspopup="dialog">${INFO_ICON}</button>`;
}

let infoOpener = null;

/** Opens the shared <dialog>: Escape, the Close button or a click on the backdrop close it; focus returns to the button. */
function openInfo(key, opener) {
  const dialog = $("info-dialog");
  const content = INFO[key]?.();
  if (!dialog || !content) return;
  $("info-title").textContent = content.title;
  $("info-body").innerHTML = content.html
    + (INFO_GUIDE[key] ? `<p class="info-more"><a href="#guide/${INFO_GUIDE[key]}">Read more in the guide</a></p>` : "");
  infoOpener = opener || null;
  if (typeof dialog.showModal === "function") dialog.showModal(); else dialog.setAttribute("open", "");
  $("info-body").scrollTop = 0;
  $("info-close").focus();
}

function bindInfoDialog() {
  const dialog = $("info-dialog");
  if (!dialog) return;
  document.addEventListener("click", (e) => {
    const button = e.target instanceof Element ? e.target.closest(".info-btn") : null;
    if (button) openInfo(button.dataset.info, button);
  });
  $("info-close").addEventListener("click", () => dialog.close());
  dialog.addEventListener("click", (e) => { // backdrop, or a link to the guide
    if (e.target === dialog || (e.target instanceof Element && e.target.closest('a[href^="#guide"]'))) dialog.close();
  });
  dialog.addEventListener("close", () => {
    if (infoOpener?.isConnected) infoOpener.focus();
    infoOpener = null;
  });
}

// ---------- user guide (#guide, #guide/<section>; static English text, no database string) ----------

/** Info dialog text reused in the guide (single source of truth), headings one level down. */
function infoHtml(key) {
  return INFO[key]().html.replace(/<(\/?)h3>/g, "<$1h4>");
}

const GUIDE = [
  { id: "start", title: "Getting started", html: () => `
    <p>The explorer is reserved for registered testers. Sign in with your e-mail address and password.</p>
    <ul><li><strong>First sign-in</strong>: the administrator gives you a temporary password; you are asked to replace it with your
      own (at least 12 characters) before anything else. You can change it later with <em>Change password</em> in the menu under your e-mail address (top right).</li>
    <li><strong>No password, or forgotten it?</strong> Use <em>Send me a sign-in link instead</em>: a one-time sign-in link (&ldquo;magic link&rdquo;) arrives by e-mail.
      Only invited addresses receive it.</li>
    <li>If your session expires, you are asked to sign in again; nothing you were reading is changed.</li>
    <li><strong>Usage data</strong>: to improve the explorer, the pages you open and the time spent on them are recorded with your
      e-mail address; only administrators can see them (<a href="#guide/usage">details</a>).</li></ul>
    <p>The tabs give the main views: Dashboard, Projects, CSRs, Lineage, Anomalies, Impact and this Guide. Your e-mail address at the
      top right opens a menu with <em>Change password</em> and <em>Sign out</em> (and <em>Admin</em> for administrators). The <em>?</em> button in the header brings you back to this guide, and the round button at the bottom right opens
      the <a href="#guide/assistant">assistant</a> on any page.</p>` },
  { id: "projects", title: "Projects", html: () => `
    <p>All projects of the Technical Support Instrument (TSI) and of its predecessors SRSP and SRSS.</p>
    <ul><li><strong>Filters</strong>: title search, country, programme, years, policy area, &ldquo;Linked to a CSR&rdquo; and
      &ldquo;Has a detail page&rdquo;. Selects and checkboxes apply at once, the text search on Enter or <em>Apply</em>;
      the address of the page keeps the filters, so you can bookmark or share it.</li>
    <li><strong>Project page</strong>: programme, year, countries, policy area, results, deliverables, an AI-generated summary,
      the CSRs it supports (AI-assessed), the latest impact analysis and a link to the lineage network.</li>
    <li><strong>Export to Excel</strong>: downloads the projects matching the current filters, with their links, as a workbook
      (see <a href="#guide/reporting">Excel reporting</a>).</li>
    <li><strong>Links to review</strong>: the button above the list opens the project–CSR links waiting for a review
      (see <a href="#guide/reviews">Link review</a>).</li>
    <li><strong>Detail page or listed only</strong>: a project with a <em>detail page</em> has its own page on
      reforms-investments.ec.europa.eu (description, results, deliverables). Many projects are only <em>listed</em>: known by
      their title in a TSI country factsheet or in the SRSP list of approved requests (PDF). Their page shows that listing as the
      source, with the note that no project page was published.</li></ul>
    <h4>Results</h4>${infoHtml("results")}` },
  { id: "csrs", title: "CSRs", html: () => `
    <p>Country-specific recommendations (CSRs) are addressed every year to each Member State in the European Semester.</p>
    <h4>Three levels</h4>${infoHtml("csrlevels")}
    <h4>The CSRs tab</h4>
    <ul><li><strong>By document</strong> (default): choose a year (the latest by default) to see its 27 country documents, each with
      its stage, number of recommendations, main assessment and number of linked projects, and a link to the source (the country
      PDF for 2026, the ECFIN CSR database before). The country filter keeps one country and links to its other years.</li>
    <li><strong>Document page</strong>: the numbered recommendations with their sub-parts, assessments and linked projects, and a
      link to the lineage network.</li>
    <li><strong>All recommendations</strong>: one row per recommendation, with the text search, country and year filters
      (a text search always opens this list). Each row links to its recommendation page and to its document.</li>
    <li><strong>Recommendation page</strong>: the full text, the sub-parts, their assessment, the linked projects, the document it
      belongs to (breadcrumb at the top) and a link to the lineage network.</li>
    <li><strong>2026: related past projects</strong>: each 2026 country card counts the past projects suggested by AI for its
      recommendations; the document and recommendation pages list them (same country first), and <em>2026 coverage</em>, below the
      cards, lists the recommendations with the fewest suggestions first.</li></ul>
    <h4>Assessments</h4>${infoHtml("assessments")}
    <h4>Related past projects</h4>${infoHtml("related")}` },
  { id: "reviews", title: "Link review", html: () => `
    <p>Business users confirm or reject the project–CSR links assessed by AI. The chips next to each link give its latest review.</p>
    <ul><li><strong>Review a link</strong>: <em>Review</em> next to a link (project page, recommendation and document pages, lineage
      By country details) opens a form: choose Confirmed, Rejected or To review, add a comment if useful, then <em>Save review</em>.
      The chip updates at once. The form also shows the history of the link: who reviewed it, when, with which comment.</li>
    <li><strong>Links to review</strong> (button on the Projects tab, or the dashboard card): all links not reviewed, to review or
      rejected, with filters (status, country, level, search), quick <em>Confirm</em> and <em>Reject</em> buttons, and the Excel export.</li>
    <li><strong>Dashboard</strong>: the <em>Link reviews</em> card gives the share of links reviewed and the statuses by level.</li></ul>
    ${infoHtml("linkreview")}` },
  { id: "lineage", title: "Lineage", html: () => `
    <p>The Lineage tab has four sub-tabs, in this order: <strong>Sankey</strong> (opened by default), <strong>Network</strong>,
      <strong>Sunburst</strong> and <strong>By country</strong>. The first three give an overview of the CSR Recommendations
      of all 27 countries; By country follows one country stage by stage. Links from a project, CSR or document page, from the
      dashboard and from this guide open By country directly. The address keeps the sub-tab, the filters and the drilled country
      (for instance <code>#lineage?view=sunburst&amp;cc=IT</code>), so a view can be bookmarked or shared.</p>
    <h4>Overviews: Sankey, Network, Sunburst</h4>
    <ul><li><strong>Filters</strong>: CSR years (from, to) and link level (all, direct, partial), shared by the three views and kept
      when you switch between them; the summary line gives the CSR Recommendations, distinct recommendations, projects, countries and the share of direct CSR Recommendations.</li>
    <li><strong>Sankey</strong>: country of the recommendation → policy area → programme → outcome, ribbon width = pairs. Click a
      country (or choose it in <em>Drill into</em>) to split it by CSR year; <em>All countries</em> returns.</li>
    <li><strong>Network</strong>: countries (dark discs) and projects (dots: fill = programme, ring = outcome), all projects shown.
      Choose <em>Linked to several countries only</em> in <em>Projects</em> to keep the projects shared by several countries. Click a country (or Enter) to highlight
      its projects, a project for its details; drag, scroll to zoom, search by title or country, <em>Reset</em> and <em>Fit to view</em>.</li>
    <li><strong>Sunburst</strong>: rings country → policy area → project (coloured by outcome). Click a segment (or choose a country in
      <em>Zoom to</em>) to zoom in, the centre to zoom out.</li>
    <li><strong>From an overview to the details</strong>: below each chart, the drilled, highlighted or zoomed country links to its
      By country view and its projects, and a selected project to its project page.</li>
    <li>The links of all countries (several thousand rows) are loaded once per session; the dashboard's <em>Refresh</em> reloads them.
      On a narrow screen the Sankey scrolls sideways inside its card.</li></ul>
    <h4>By country</h4>
    <p>The lineage network follows, for one country, the whole workflow of a project in five stages: the European Semester
      proposes CSRs, the country submits a request claiming CSR support, the project is selected and carried out, then delivers
      its results.</p>
    <p class="ln-stages">CSR document → recommendation → initial request (pending JIRA) → project → outcome</p>
    <ul><li><strong>Documents</strong> (first column): the country's CSR document of each year, with its number of recommendations.</li>
    <li><strong>Recommendations</strong>: the recommendations of each document; the small coloured marker gives the
      latest assessment.</li>
    <li><strong>Initial request</strong> (dashed band, placeholder): the country's request and the CSRs it claims, self-declared in
      JIRA and not public. It shows no data until the JIRA export is provided; the lines cross it unchanged.</li>
    <li><strong>Projects</strong>: ordered to reduce crossings (each sits near the average height of its recommendations); the
      marker gives the programme.</li>
    <li><strong>Outcome</strong> (last column, on the row of its project): the results status (filled disc = results achieved,
      ring = results expected, dashed ring = not stated), the number of deliverables and a pill such as &ldquo;6/10&rdquo; for
      the latest impact score.</li>
    <li><strong>Lines</strong>: thin lines join a document to its recommendations; one line per CSR Recommendation,
      solid = direct, dashed = partial (direct if at least one sub-part link is direct); a short line joins a project to its outcome.</li>
    <li><strong>Reading it</strong>: hover or focus a node to highlight its chain (a document lights up its recommendations, their
      projects and outcomes), click (or Enter) to pin the highlight and see the details below the graph (for a project or an
      outcome: results, deliverables with a link to the project page, latest impact with a link to the impact analysis); click
      again or press Escape to release it. Hover a line for the sub-parts and the rationale.</li>
    <li><strong>Filters</strong>: country, years (default 2019–2025; the 2026 recommendations have no project yet) and
      &ldquo;Show CSRs without a project&rdquo;. On a narrow screen the network scrolls sideways inside its card.
      The table of links below the graph (with the results, deliverables and impact score of each project) can be downloaded as CSV.</li></ul>
    <h4>Links</h4>${infoHtml("links")}
    <h4>Link level</h4>${infoHtml("linklevel")}
    <h4>Initial request</h4>${infoHtml("request")}
    <p>The <em>Country briefing</em> button at the top of By country downloads a Word document on the country shown
      (see <a href="#guide/briefing">Country briefing</a>).</p>` },
  { id: "dashboard", title: "Dashboard", html: () => `
    <p>Figures across all projects and CSRs, loaded once per session (<em>Refresh</em> reloads them).</p>
    <ul><li><strong>Tiles</strong>: projects (share with a detail page), deliverables, CSR documents (27 per year, with the total
      documents, recommendations and sub-parts), project–CSR links (share of projects linked), impact analyses (mean score),
      anomalies (high severity).</li>
    <li><strong>Projects</strong>: by year and programme, by country, by policy area, by results.</li>
    <li><strong>Impact analyses</strong>: score distribution, level, confidence, mean score by policy area and by country (greyed
      when fewer than 2 projects), latest analyses.</li>
    <li><strong>CSRs and lineage</strong>: CSR documents by year (hover a bar for its recommendations), latest assessment of the
      recommendations (2026: not yet assessed), share of each country's recommendations supported by a project, links by level.</li>
    <li><strong>Link reviews</strong> (once available): share of the project–CSR links reviewed by the users, and the confirmed,
      rejected and to-review links by level; a segment opens the matching <a href="#guide/reviews">links to review</a>.</li>
    <li><strong>Data quality</strong>: anomalies by type and severity.</li></ul>
    <p><strong>Click-through</strong>: a bar opens the matching filtered list (a country's projects, a year's CSRs, the lineage
      network of a country, the anomalies of a type and severity). Hover or focus a bar for its value and share; every chart
      has a <em>Table</em> view. The ${INFO_GLYPH} buttons explain how a figure is defined.</p>` },
  { id: "anomalies", title: "Anomalies", html: () => `
    <p>The Anomalies tab lists them with filters (type, severity, programme, years, country) and a CSV export. By default only high
      and medium severities are shown.</p>${infoHtml("anomalies")}` },
  { id: "assistant", title: "Assistant", html: () => `
    <p>The round button at the bottom right opens the assistant beside the page (full screen on a phone). Escape or <em>Close</em>
      closes it; the conversation is kept while the browser tab is open.</p>
    <ul><li><strong>What it can answer</strong>: questions on TSI projects and CSRs: which projects work on a topic or in a country,
      what a project covers, which CSRs a country received and how they were assessed, which projects support them.</li>
    <li><strong>Data it uses</strong>: project records, AI summaries, CSRs and their sub-parts, AI-assessed links.
      <strong>Deliverable full texts are not searched</strong> by the assistant.</li>
    <li><strong>On a project or CSR page</strong>, <em>Ask about this project</em> (or CSR) pre-fills the question; you still press Send.</li>
    <li><strong>Export to Word</strong> downloads the conversation as a .docx document.</li></ul>
    <h4>Examples</h4><ul>${CHAT_EXAMPLES.map((q) => `<li>${esc(q)}</li>`).join("")}</ul>
    <p>Answers are generated by AI (Claude, Anthropic) and can take up to 2 minutes: verify the cited sources.</p>` },
  { id: "impact", title: "Impact analysis", html: () => `
    <p>From the Impact tab (or <em>Impact analysis</em> on a project page), choose a project and run the analysis. It combines the
      project data with a web search and ends with an impact score and a confidence level.</p>
    <ul><li><strong>Duration and cost</strong>: 30 seconds to 2 minutes; each run uses a web search and a large language model,
      so it has a cost. Run it when useful, not repeatedly.</li>
    <li><strong>Saved analyses</strong>: each analysis with a score is kept. The project page shows the latest one, the Impact tab
      the previous ones, and the dashboard their statistics.</li>
    <li><strong>Export to Word</strong>: a report (new or saved) can be downloaded as a .docx document.</li></ul>
    ${infoHtml("impact")}` },
  { id: "briefing", title: "Country briefing", html: () => `
    <p><em>Country briefing</em> (in <a href="#lineage?view=country">Lineage › By country</a>, and on the CSRs tab once a country is
      chosen) downloads a Word document on one country. In the dialog choose the country, the period (years of the projects and
      of the CSR documents; by default the last five years and the current one), optionally a policy area, and whether to
      include the AI summary, then <em>Generate</em>.</p>
    <ul><li><strong>Steps</strong>: collecting the data (a few seconds), writing the summary (AI, about 10 to 30 seconds), building
      the document. <em>Cancel</em> or Escape stops it; nothing is saved.</li>
    <li><strong>File</strong>: <code>briefing-&lt;country&gt;-&lt;from&gt;-&lt;to&gt;[-&lt;policy area&gt;].docx</code>, styled like the
      other Word exports.</li>
    <li><strong>Cost</strong>: the AI summary is one model call (a few cents); untick <em>Include AI summary</em> for a data-only document.</li>
    <li><strong>Limits</strong>: the AI parts (key messages, comments, suggestions for the gaps) are written only from the figures of
      the document and marked as AI-generated; the figures themselves come from the explorer's data, with the same caveats (AI-assessed
      links, projects known only by their title not linked).</li></ul>
    ${infoHtml("briefing")}` },
  { id: "reporting", title: "Excel reporting", html: () => `
    <p><em>Export to Excel</em>, on the Projects tab and on <em>Links to review</em>, downloads a workbook built in your browser
      (<code>tsi-reporting-YYYY-MM-DD.xlsx</code>, bold and frozen header rows):</p>
    <ul><li><strong>Projects</strong>: one row per project matching the current filters, with its programme, year, countries, policy
      area, results, deliverables, its linked recommendations and CSR sub-parts with the strongest link level, latest impact score,
      anomalies and a summary of the reviews of its links, and a link back to its page in the explorer.</li>
    <li><strong>Links</strong>: one row per project–CSR sub-part link of these projects, with the level, the AI rationale, the basis
      (project text or title only) and the latest review (status, reviewer, date, comment).</li>
    <li><strong>Notes</strong>: export date, the filters applied, who exported it, and the definitions of the columns.</li></ul>
    <p>From <em>Links to review</em>, the export keeps the links matching that view's filters. Other exports: CSV for the anomalies
      and the lineage table, Word for impact reports, assistant conversations and country briefings.</p>` },
  { id: "data", title: "Data and updates", html: () => `
    <ul><li><strong>Sources</strong>: reforms-investments.ec.europa.eu (TSI projects, detail pages, country factsheets, deliverables),
      the SRSP list of approved requests (PDF), and the ECFIN CSR database (2016–2025 recommendations and their assessments) and the Commission's 2026 country-specific
      recommendations (one PDF per Member State).</li>
    <li><strong>Monthly update</strong>: an automated run each month adds new or changed projects, publications, CSRs and links
      (incremental: only what changed is re-read; a full check runs quarterly), then refreshes the anomalies and this explorer.</li>
    <li><strong>Data releases</strong>: each update is also published as a dated data release (data-YYYY-MM-DD) in the project's
      repository, so the figures of a given date can be retrieved.</li>
    <li><strong>AI batches</strong>: besides the analyses run from this page, impact analyses are also run in batches by the data
      team (source <em>local</em> in the lists), and the related past projects of the 2026 recommendations are suggested by a batch
      run as well. Both are loaded into the explorer with the other data at the next update.</li>
    <li><strong>Your contributions</strong>: link reviews are written directly by the users and are never overwritten by the
      updates.</li></ul>` },
  { id: "limits", title: "Limits and good practice", html: () => `
    <ul><li>Summaries, project–CSR links, assistant answers and impact analyses are <strong>generated by AI</strong>: useful to find
      and compare, not authoritative. Verify against the sources (project page, deliverables, CSR text) before use.</li>
    <li>Links are assessed from the project text; projects known only by their title are not yet linked, so absence of a link is
      not proof of absence of support.</li>
    <li>Impact statistics rest on the analyses run so far: with few analyses they are indicative only.</li>
    <li>Anomalies are deterministic checks of the publication, not judgements on the projects.</li>
    <li>Cite the official sources, not the explorer, in documents.</li></ul>` },
  { id: "usage", title: "Usage data", html: () => `
    <p>To improve the explorer, the pages you open and the time spent on them are recorded with your e-mail address; only
      administrators can see them.</p>
    ${infoHtml("monitoring").replace(/^\s*<p>[\s\S]*?<\/p>/, "")}` },
  { id: "admin", title: "Administration", admin: true, html: () => `
    <p>The <em>Admin</em> page is shown to administrators only: open it from the menu under your e-mail address (top right).</p>
    <ul><li><strong>Users</strong>: every user with their role (User or Admin), status (active, pending first password change, not
      confirmed), last sign-in and invitation. <em>Make admin</em> / <em>Make user</em> changes the role (the last administrator cannot
      be demoted), <em>Reset password</em> gives a new temporary password (it also changes the password of any other application using
      the same account), <em>Revoke</em> removes the access to the explorer but keeps the account.</li>
    <li><strong>Invite</strong>: a new address gets an account and a temporary password, shown once: transmit it through a separate
      channel. An address that already has an account in the shared sign-in service keeps its password. The optional sign-in link is
      the same e-mail as <em>Send me a sign-in link</em>.</li>
    <li><strong>Usage monitoring</strong>: active users, sessions and time spent over a period, sessions per day, time by section,
      per-user figures and the visit log, with CSV and Excel exports.</li></ul>
    <p>Fallback without the tab: in the Supabase SQL editor,
      <code>update public.testers set role = 'admin' where email = 'name@example.org';</code></p>` },
  { id: "glossary", title: "Glossary", html: () => `
    <dl class="info-dl">
    <dt>TSI</dt><dd>Technical Support Instrument (since 2021): EU programme of tailor-made technical support to Member States' reforms, managed by DG REFORM.</dd>
    <dt>SRSP</dt><dd>Structural Reform Support Programme (2017–2020), the TSI's predecessor.</dd>
    <dt>SRSS</dt><dd>Structural Reform Support Service: the Commission service that delivered technical support before DG REFORM was created in 2020.</dd>
    <dt>CSR</dt><dd>Country-specific recommendation: guidance adopted by the Council for each Member State in the European Semester.</dd>
    <dt>CSR document</dt><dd>The yearly set of recommendations addressed to one Member State: 27 per year. Council recommendation once
      adopted; Commission recommendation while it is still a proposal (2026).</dd>
    <dt>Recommendation</dt><dd>One numbered recommendation of a CSR document (1 to 5).</dd>
    <dt>European Semester</dt><dd>The EU's yearly cycle of economic and social policy coordination, which produces the CSRs.</dd>
    <dt>Detail page</dt><dd>A project's own page on reforms-investments.ec.europa.eu; projects without one are known only from a listing.</dd>
    <dt>Sub-part</dt><dd>One distinct request inside a recommendation; the level at which project links are assessed.</dd>
    <dt>Lineage</dt><dd>The chain from a CSR document to its recommendations, the country's initial request (pending the JIRA export),
      the projects that support them and their outcome: overviews of all countries (Sankey, Network, Sunburst) and a stage-by-stage
      view per country.</dd>
    <dt>Initial request</dt><dd>The Member State's request for support, in which it declares the CSRs it addresses; recorded in JIRA, not public.</dd>
    <dt>Direct / partial link</dt><dd>Level of a project–CSR sub-part link assessed by AI: the project implements what the CSR asks
      (direct), or contributes to one aspect or a precondition (partial).</dd>
    <dt>CSR Recommendation (count)</dt><dd>One recommendation linked to one project, in the Lineage overviews; direct if at least one of its sub-part links is direct.</dd>
    <dt>Review</dt><dd>A user's verdict on an AI-assessed link: confirmed, rejected or to review; shared with all users, with its history.</dd>
    <dt>Related past project</dt><dd>A project of earlier years suggested by AI as working on the same issues as a 2026 recommendation;
      a lead to verify, not a link.</dd>
    <dt>Country briefing</dt><dd>A Word document on one country and period, built from the explorer's data, with an optional AI summary.</dd></dl>` },
];

const GUIDE_IDS = GUIDE.map((s) => s.id);

// Illustrated part of the guide (screenshots in web/guide/, relative paths: the site is served under a sub-path).
// Screenshots taken on 23 Oct 2026 with the real data of that day; the figures quoted in the steps are read off them.
const GUIDE_PROJECT = "technical-support-transforming-existing-fully-state-administered-funded-pension-scheme-lithuania";
const GUIDE_SHOT_DATE = "23 October 2026";
const GUIDE_ESSENTIALS = [
  { icon: "▦", title: "Tabs", text: "Dashboard, Projects, CSRs, Lineage, Anomalies, Impact and Guide. The ? button in the header brings you back here; the menu under your e-mail address holds Change password, Sign out (and Admin for administrators)." },
  { icon: "⚲", title: "Filters", text: "Each list has its filters (country, programme, years, text search…). The address of the page keeps them: bookmark or share it." },
  { icon: "⇄", title: "Click through", text: "Bars, ribbons, cards and names open the matching view: a country's projects, a CSR document, a project page, a lineage view." },
  { icon: "⋔", title: "Lineage", text: "Sankey, Network and Sunburst show all countries at once; By country follows one country from its CSR documents to the projects and their outcome." },
  { icon: "info", title: "Definitions", text: "The round i buttons explain how a figure or a link level is defined, with a link to the reference below." },
  { icon: "✓", title: "Your review", text: "Project–CSR links are assessed by AI: confirm or reject them with Review. Reviews are shared with all users, with their history." },
  { icon: "✦", title: "Assistant", text: "The round button at the bottom right opens the assistant beside any page. On a project or CSR page it offers to ask about it." },
  { icon: "⎙", title: "Export", text: "Projects and links to Excel, lists to CSV; impact reports, assistant conversations and country briefings to Word." },
];
const LT_PROJECT_SEL = `sel=${encodeURIComponent(`project:${GUIDE_PROJECT}`)}`;
const GUIDE_STEPS = [
  { img: "01-dashboard.jpg", to: "#dashboard", title: "Start from the big picture",
    alt: "Dashboard: tiles with the totals and the chart of projects by year and programme",
    text: `The <strong>Dashboard</strong> sums up the whole data set: 2,157 projects (39% with a detail page), 536 deliverables,
      27 CSR documents per year (294 documents, 1,049 recommendations and 3,363 sub-parts for 2016–2026), 1,339 project–CSR links
      (28% of projects linked), 5 impact analyses (mean score 4.2/10) and 1,884 anomalies, 262 of them high severity. The first
      chart splits the projects by year and programme: TSI 1,185, SRSP 890, SRSS 82. Click a bar to open the matching list.` },
  { img: "02-csrs.jpg", to: "#csrs", title: "Browse the CSR documents of a year",
    alt: "CSRs tab: the 27 country documents of 2026 as cards",
    text: `The <strong>CSRs</strong> tab opens on the latest year: 2026, 27 documents, 146 recommendations. They are still
      <em>Commission recommendations</em>: not yet adopted by the Council, not yet assessed, and no project is linked yet (a
      recommendation can only be supported by projects of the following years). Each card gives the number of recommendations,
      the main assessment and the linked projects, with links to the document page and its PDF. Choose an earlier year to see
      assessments and links.` },
  { img: "03-document.jpg", to: "#csrdoc/csrdoc-LT-2023", title: "Open a country's CSR document",
    alt: "CSR document page: Lithuania, 2023 country-specific recommendations",
    text: `The <strong>Lithuania — 2023</strong> document is a Council recommendation with 4 recommendations split into 18
      sub-parts. 5 projects are linked to it (AI-assessed) and its main assessment is <em>Some progress</em> (3 of 4
      recommendations). Below, each recommendation lists its sub-parts with their policy areas, assessment and linked projects:
      sub-part 1.6, &ldquo;social protection&rdquo;, is partially supported by the Lithuanian pension project followed in the
      next steps.` },
  { img: "04-lineage-sankey.jpg", to: "#lineage", title: "See where support goes, for all countries",
    alt: "Lineage tab, Sankey view: countries, policy areas, programmes and outcomes of all CSR Recommendations",
    text: `The <strong>Lineage</strong> tab opens on the <em>Sankey</em>: for the CSRs of 2016–2025, 1,185 CSR
      Recommendations (391 distinct recommendations, 597 projects, 27 countries, 34% direct) flow from the country of the recommendation to the policy
      area of the project, its programme and its stated outcome. Croatia comes first with 90 CSR Recommendations, Romania 84, Spain 82, and
      Lithuania 64; TSI carries 948 CSR Recommendations and 954 lead to results achieved. The CSR years and the link level (all, direct, partial)
      filter the Sankey, the Network and the Sunburst alike; click a country to split it by CSR year.` },
  { img: "04-lineage.jpg", to: `#lineage?view=country&country=LT&${LT_PROJECT_SEL}`, title: "Follow one country stage by stage",
    alt: "Lineage By country, Lithuania, with the pension project pinned: its chain from two documents to its outcome is highlighted",
    text: `<em>By country</em> follows the five stages for one country: CSR document → recommendation → initial request (a dashed
      placeholder until the JIRA export) → project → outcome. Lithuania, 2019–2025: 7 CSR documents, 25 recommendations (10 with
      at least one project), 35 CSR Recommendations (11 direct) from 44 sub-part links and 23 projects. With the pension
      project pinned, its chain lights up: a solid line (direct) to LT 2021 · CSR 1, a dashed line (partial) to LT 2023 · CSR 1,
      and in the <em>Outcome</em> column: results achieved, 1 deliverable and the pill <em>2/10</em>, its latest impact score.` },
  { img: "05-project.jpg", to: `#project/${GUIDE_PROJECT}`, title: "Read the project page",
    alt: "Project page: Technical Support in transforming the existing fully state-administered funded pension scheme in Lithuania",
    text: `The project page gives the record (TSI, 2024, Lithuania, labour market and social protection, results achieved,
      1 deliverable, detail page on the Commission's site), the latest impact analysis (2/10, weak, medium confidence, 7 Oct 2026),
      the AI summary (a project implemented by the ILO on the second pension pillar) and the <strong>supported CSRs</strong>:
      2, one direct (csr-LT-2021-1.4) and one partial (csr-LT-2023-1.6), each with its rationale and its review status.` },
  { img: "10-review.jpg", to: `#project/${GUIDE_PROJECT}`, title: "Confirm or reject the AI-assessed link",
    alt: "Review dialog of the link between the pension project and csr-LT-2021-1.4, not reviewed yet",
    text: `<em>Review</em>, next to each link, opens a short form: the link of the pension project to csr-LT-2021-1.4 is
      <em>Not reviewed</em> yet. Choose Confirmed, Rejected or To review, add a comment if useful and save: the status is shared with
      all users at once, and the history (who, when, why) stays in the form. <em>Links to review</em>, on the Projects tab, lists all
      the links still to check; on the day of the screenshots none of the 1,339 links had been reviewed.` },
  { img: "06-impact.jpg", to: `#impact/${GUIDE_PROJECT}`, title: "Check the impact analysis",
    alt: "Impact tab: the saved analysis of the Lithuanian pension project, report open",
    text: `<em>Impact analysis</em> opens the project in the <strong>Impact</strong> tab. Its saved analysis (2/10, weak,
      medium confidence, 7 Oct 2026) opens with <em>Show report</em>: the overall verdict finds solid analytical outputs, but a
      parallel 2025 reform moved the second pillar in the opposite direction and the project closed two months early. Every
      claim cites numbered sources. <em>Export to Word</em> downloads the report; a new analysis takes 30 seconds to 2 minutes
      and has a cost.` },
  { img: "09-briefing.jpg", to: "#lineage?view=country&country=LT", title: "Take a country briefing away",
    alt: "Country briefing dialog opened from Lineage By country, Lithuania, years 2021 to 2026",
    text: `<em>Country briefing</em>, at the top of the By country view (and of the CSRs tab once a country is chosen), builds a Word
      document on the country: key figures, CSRs, lineage, results and impact, gaps and data quality. For Lithuania it proposes
      2021–2026 and all policy areas; tick <em>Include AI summary</em> for key messages and comments written by AI from the figures
      of the document (about 10 to 30 seconds, a small cost), then <em>Generate</em>.` },
  { img: "07-anomalies.jpg", to: "#anomalies", title: "Keep an eye on data quality",
    alt: "Anomalies tab: high and medium severity anomalies, first page",
    text: `The <strong>Anomalies</strong> tab lists rule-based checks of the Commission's publication. By default it shows the
      high and medium severities: 719 anomalies over 15 pages, starting with TSI projects listed in a country factsheet but with
      no project page published. Filter by type, severity, programme, years or country, and download the list as CSV.` },
  { img: "08-assistant.jpg", to: `#project/${GUIDE_PROJECT}`, assistant: true, title: "Ask the assistant",
    alt: "Assistant drawer open beside the project page, with the question about this project pre-filled",
    text: `The round button at the bottom right opens the <strong>assistant</strong> beside the page. On a project page,
      <em>Ask about this project</em> pre-fills the question; you still press <em>Send</em>. Examples get you started, and the
      conversation can be exported to Word. Answers use the project records, summaries, CSRs and links (not the deliverable
      full texts) and are generated by AI: verify the cited sources.` },
];
const GUIDE_CASES = [
  { title: "Direct links only", text: "The Sankey with the direct CSR Recommendations only: 399 CSR Recommendations, 227 recommendations and 282 projects.", to: "#lineage?view=sankey&level=direct", cta: "Open the Sankey" },
  { title: "Croatia, stage by stage", text: "One of the densest countries: 73 CSR Recommendations from 78 sub-part links (2019–2025), 35 projects.", to: "#lineage?view=country&country=HR", cta: "Open By country" },
  { title: "Best-covered country", text: "Belgium has the highest share of recommendations supported by a project: 65% (22 of 34).", to: "#lineage?view=country&country=BE", cta: "See Belgium" },
  { title: "Projects that bridge countries", text: "The Network shows the projects linked to recommendations of several countries.", to: "#lineage?view=network", cta: "Open the Network" },
  { title: "Highest impact score", text: "Barnahus Slovenia (SRSP 2019), support for children victims of violence: 8/10, strong.", to: "#impact/barnahus-slovenia-supporting-children-victims-violence", cta: "Read the analysis" },
  { title: "A multi-country project", text: "Inclusive education in Portugal, Italy and Spain: one project linked to four CSR sub-parts of three countries.", to: "#project/combatting-disparities-access-inclusive-education-portugal-italy-and-spain", cta: "Open the project" },
  { title: "Links still to check", text: "Every AI-assessed link not reviewed, to review or rejected, with quick Confirm and Reject buttons.", to: "#reviews", cta: "Open the list" },
  { title: "Search the 2026 recommendations", text: "Full-text search in all recommendations, here “pension” in the 2026 documents.", to: "#csrs?view=recs&year=2026&q=pension", cta: "Run the search" },
  { title: "Possible duplicates", text: "Projects that look published twice: a quick check before counting projects.", to: "#anomalies?type=possible_duplicate&severity=all", cta: "See the list" },
];
const GUIDE_ASK = [
  "Which projects support Lithuania's recommendations on pensions?",
  "What CSRs did Lithuania receive in 2023, and how were they assessed?",
  "What do Croatia's 2026 recommendations ask on public administration?",
  "Which countries have projects on e-health or the digitalisation of healthcare?",
  "Which projects work on public procurement in the Baltic states?",
  ...CHAT_EXAMPLES.slice(0, 2),
];
const GUIDE_TIPS = [
  "Summaries, project–CSR links, assistant answers, impact analyses, related past projects and the briefing summaries are generated by AI: verify against the sources before use.",
  "The 2026 recommendations are Commission proposals: not yet assessed and not yet linked to projects; related past projects suggested by AI are leads, not links.",
  "Projects known only by their title are not linked yet: no link does not mean no support.",
  "Your reviews are shared with all users at once: add a short comment when you reject a link.",
  "Impact statistics rest on the analyses run so far (5 in October 2026): indicative only.",
  "Anomalies are deterministic checks of the publication, not judgements on the projects.",
  "To improve the explorer, the pages you open and the time spent on them are recorded with your e-mail address; only administrators can see them.",
  "The data are updated every month: the figures in the screenshots may have changed since.",
];
// Parts of the guide page, reachable as #guide/<id> like the reference sections.
const GUIDE_PARTS = [["essentials", "Essentials"], ["scenario", "Scenario"], ["cases", "Other cases"], ["ask", "Questions"],
  ["tips", "Good to know"], ["reference", "Reference"]];
const GUIDE_PART_IDS = GUIDE_PARTS.map(([id]) => id);
let guideTarget = null; // section to scroll to and focus after the render

function guidePart(id, title, inner, intro = "") {
  return `<section class="guide-part" id="guide-${id}" aria-labelledby="guide-h-${id}">`
    + `<h3 id="guide-h-${id}" tabindex="-1">${esc(title)}</h3>${intro ? `<p class="guide-intro">${esc(intro)}</p>` : ""}${inner}</section>`;
}

/** Illustrated part: hero, essentials, scenario with screenshots, cases, questions, tips (static text, no database string). */
function guideIllustratedHtml() {
  const hero = `<header class="guide-hero"><p class="guide-kicker">TSI × CSR Explorer</p>`
    + `<p class="guide-lead">The explorer links DG REFORM's technical support projects to the EU country-specific recommendations
      (CSRs) they help to implement. Ten minutes are enough to find your way: follow the scenario below, each step opens the
      matching view of the explorer.</p>`
    + `<nav class="guide-pills" aria-label="Guide parts">${GUIDE_PARTS.map(([id, label]) => `<a href="#guide/${id}">${esc(label)}</a>`).join("")}</nav></header>`;
  const ess = `<div class="guide-ess-grid">${GUIDE_ESSENTIALS.map((e) => `<div class="guide-ess"><span class="guide-ess-icon" aria-hidden="true">${e.icon === "info" ? INFO_ICON : esc(e.icon)}</span>`
    + `<h4>${esc(e.title)}</h4><p>${esc(e.text)}</p></div>`).join("")}</div>`;
  const steps = `<ol class="guide-steps">${GUIDE_STEPS.map((s, i) => `<li class="guide-step"><div class="guide-step-text">`
    + `<span class="guide-step-num">Step ${i + 1}</span><h4>${esc(s.title)}</h4><p>${s.text}</p>`
    + `<a class="btn" href="${esc(s.to)}"${s.assistant ? " data-guide-assistant" : ""}>Try it <span aria-hidden="true">→</span></a></div>`
    + `<button type="button" class="guide-shot" data-zoom="${esc(s.img)}" aria-label="${esc(`Enlarge the screenshot: ${s.alt}`)}">`
    + `<img src="guide/${esc(s.img)}" alt="${esc(s.alt)}" loading="lazy" width="1440" height="860"></button></li>`).join("")}</ol>`
    + `<p class="muted small">Screenshots taken on ${GUIDE_SHOT_DATE} with the data of that day. Click a screenshot to enlarge it; Escape or a click closes it.</p>`;
  const cases = `<div class="guide-cases">${GUIDE_CASES.map((c) => `<a class="guide-case" href="${esc(c.to)}"><h4>${esc(c.title)}</h4>`
    + `<p>${esc(c.text)}</p><span class="guide-case-cta">${esc(c.cta)} <span aria-hidden="true">→</span></span></a>`).join("")}</div>`;
  const ask = `<ul class="guide-ask">${GUIDE_ASK.map((q) => `<li>&ldquo;${esc(q)}&rdquo;</li>`).join("")}</ul>`
    + `<p class="small">Open the assistant with the round button at the bottom right. <a href="#guide/assistant">How the assistant works</a></p>`;
  const tips = `<ul class="guide-tips">${GUIDE_TIPS.map((t) => `<li>${esc(t)}</li>`).join("")}</ul>`;
  return hero
    + guidePart("essentials", "The essentials in one minute", ess)
    + guidePart("scenario", "Scenario: from a CSR document to a project and its impact", steps,
      "Follow one thread through the data: Lithuania's 2023 recommendations, where support goes across all countries, the projects that support Lithuania's recommendations, the review of an AI-assessed link and the impact of one project, a TSI project on the second pension pillar, then a country briefing to take away.")
    + guidePart("cases", "Other cases to explore", cases)
    + guidePart("ask", "Ideas of questions for the assistant", ask)
    + guidePart("tips", "Good to know", tips);
}

/** Guide markup (pure: also used by the render test). */
function guideHtml(section, { mobile = false, admin = false } = {}) {
  const sections = GUIDE.filter((s) => admin || !s.admin);
  const key = sections.some((s) => s.id === section) ? section : "";
  const toc = sections.map((s) => `<li><a href="#guide/${s.id}"${s.id === key ? ' aria-current="location"' : ""}>${esc(s.title)}</a></li>`).join("");
  const body = sections.map((s) => `<section class="guide-section" id="guide-${s.id}" aria-labelledby="guide-h-${s.id}">`
    + `<h3 id="guide-h-${s.id}" tabindex="-1">${esc(s.title)}</h3>${s.html()}</section>`).join("");
  return `${heading("User guide")}<div class="guide-page">${guideIllustratedHtml()}`
    + `<section class="guide-part guide-reference" id="guide-reference" aria-labelledby="guide-h-reference">`
    + `<h3 id="guide-h-reference" tabindex="-1">Reference</h3>`
    + `<p class="guide-intro">Each part of the explorer in detail. The ${INFO_GLYPH} buttons of the explorer link to these sections.</p>`
    + `<div class="guide-layout"><nav class="guide-toc" aria-label="Guide contents">`
    + `<details id="guide-toc"${mobile ? "" : " open"}><summary>Contents</summary><ol>${toc}</ol></details></nav>`
    + `<article class="guide">${body}</article></div></section></div>`;
}

function renderGuide(section) {
  view(guideHtml(section, { mobile: assistantIsMobile(), admin: isAdmin() }));
  bindGuide();
  guideTarget = GUIDE_IDS.includes(section) || GUIDE_PART_IDS.includes(section) ? section : null;
}

/** Screenshot zoom (a modal dialog: Escape and a click close it) and the "Try it" link that opens the assistant. */
function bindGuide() {
  const root = $("view");
  root.querySelectorAll("button[data-zoom]").forEach((button) => button.addEventListener("click", () => {
    const img = button.querySelector("img");
    const dialog = document.createElement("dialog");
    dialog.className = "guide-zoom";
    dialog.setAttribute("aria-label", img.alt);
    dialog.innerHTML = `<img src="${esc(img.getAttribute("src"))}" alt="${esc(img.alt)}" width="1440" height="860">`;
    dialog.addEventListener("click", () => dialog.close());
    dialog.addEventListener("close", () => { dialog.remove(); if (button.isConnected) button.focus(); });
    document.body.append(dialog);
    dialog.showModal();
  }));
  root.querySelectorAll("a[data-guide-assistant]").forEach((a) => a.addEventListener("click", () => openAssistant({ focus: false })));
}

/** After the render (and the heading focus): bring the requested section into view and give it the focus. */
function focusGuideSection() {
  if (!guideTarget) return;
  const h = $(`guide-h-${guideTarget}`);
  guideTarget = null;
  if (!h) return;
  h.scrollIntoView({ block: "start" });
  h.focus({ preventScroll: true });
}

// ---------- dashboard (hand-made SVG; method of the dataviz skill) ----------

const CH = 7; // average glyph width (px) of the 12 px chart font, used to fit labels
const DASH_MIN_W = 280;
const PROGRAMME_KEYS = { TSI: "s1", SRSP: "s2", SRSS: "s3" }; // fixed categorical order, never by rank
const SEVERITY_KEYS = [["high", "High"], ["medium", "Medium"], ["low", "Low"], ["info", "Info"]];
const ASSESSMENT_SCALE = [
  ["full implementation", "Full implementation", "o1"], ["substantial progress", "Substantial progress", "o2"],
  ["some progress", "Some progress", "o3"], ["limited progress", "Limited progress", "o4"],
  ["no progress", "No progress", "o5"], ["not assessed", "Not assessed", "muted"], ["none", "Not yet assessed", "faint"],
];
const RESULT_LABELS = { achieved: "Results achieved", expected: "Results expected", none: "Not stated" };
const LINK_LABELS = { direct: "Direct", partial: "Partial" };
const LEVEL_ORDER = ["strong", "moderate", "weak", "none"];
const CONFIDENCE_ORDER = ["high", "medium", "low", "none"];
let dashCache = null; // { uid, model, at }
let dashWidth = 0;

const arr = (v) => (Array.isArray(v) ? v : []);
const num = (v) => { const n = Number(v); return Number.isFinite(n) ? n : 0; };
const fmtN = (v) => num(v).toLocaleString("en");
const share = (a, b) => (b > 0 ? `${Math.round((100 * a) / b)}%` : "");
const px = (v) => Math.round(v * 10) / 10;

function hrefFor(route, values) {
  const q = new URLSearchParams();
  for (const [k, v] of Object.entries(values)) if (v !== undefined && v !== null && v !== "") q.set(k, String(v));
  const s = q.toString();
  return `#${route}${s ? "?" + s : ""}`;
}

/** Text cut to a pixel width (estimate), with an ellipsis; the full text stays in the tooltip and the table. */
function fitText(text, width, charW = CH) {
  const s = String(text ?? "");
  const max = Math.max(2, Math.floor(width / charW));
  return s.length <= max ? s : `${s.slice(0, max - 1)}…`;
}

/** Clean axis scale (1/2/5 steps, about 4 ticks, integer counts). */
function niceScale(max) {
  if (!(max > 0)) return { top: 1, step: 1 };
  const rough = max / 4;
  const mag = 10 ** Math.floor(Math.log10(rough));
  const norm = rough / mag;
  const step = Math.max(1, (norm <= 1 ? 1 : norm <= 2 ? 2 : norm <= 5 ? 5 : 10) * mag);
  return { top: Math.ceil(max / step) * step, step };
}

function compact(v) {
  return v >= 1000 ? `${px(v / 1000)}K` : String(v);
}

/** Horizontal bar: square at the baseline (x), 4 px rounded data-end. */
function hPath(x, y, w, h, round) {
  if (!(w > 0)) return "";
  const r = round ? Math.min(4, w, h / 2) : 0;
  return `M${px(x)} ${px(y)}H${px(x + w - r)}`
    + (r ? `A${r} ${r} 0 0 1 ${px(x + w)} ${px(y + r)}V${px(y + h - r)}A${r} ${r} 0 0 1 ${px(x + w - r)} ${px(y + h)}` : `V${px(y + h)}`)
    + `H${px(x)}Z`;
}

/** Vertical column: square at the baseline (bottom), 4 px rounded top. */
function vPath(x, y, w, h, round) {
  if (!(h > 0)) return "";
  const r = round ? Math.min(4, h, w / 2) : 0;
  return `M${px(x)} ${px(y + h)}V${px(y + r)}`
    + (r ? `A${r} ${r} 0 0 1 ${px(x + r)} ${px(y)}H${px(x + w - r)}A${r} ${r} 0 0 1 ${px(x + w)} ${px(y + r)}` : `H${px(x + w)}`)
    + `V${px(y + h)}Z`;
}

/** Tooltip payload carried by a mark (read back with textContent, never as HTML). */
function tipAttrs(t) {
  return ` data-tip-label="${esc(t.label)}" data-tip-value="${esc(t.value)}"`
    + (t.share ? ` data-tip-share="${esc(t.share)}"` : "")
    + (t.series ? ` data-tip-series="${esc(t.series)}"` : "")
    + (t.key ? ` data-tip-key="${esc(t.key)}"` : "");
}

/** A hoverable/focusable mark: a link when it opens a filtered list, else a focusable group. */
function markOpen(href, t) {
  const aria = esc([t.series, t.label, t.value, t.share].filter(Boolean).join(", "));
  return href
    ? `<a class="viz-mark" href="${esc(href)}" aria-label="${aria}"${tipAttrs(t)}>`
    : `<g class="viz-mark" tabindex="0" role="img" aria-label="${aria}"${tipAttrs(t)}>`;
}

function markClose(href) {
  return href ? "</a>" : "</g>";
}

function emptyChart(text = "No data.") {
  return `<p class="empty viz-empty">${esc(text)}</p>`;
}

/**
 * Horizontal bars (one row per category, optional stacked segments).
 * rows: [{ label, display, href, tip, segs: [{ v, key, href, tip }] }]; max: scale end (default: largest total).
 * Value labels sit outside the bar end (space reserved), category labels are cut to their column.
 */
function hbarChart(rows, width, { max, title = "" } = {}) {
  if (!rows.length) return emptyChart();
  const w = Math.max(DASH_MIN_W, Math.floor(width) || 0);
  const longest = Math.max(...rows.map((r) => String(r.label ?? "").length));
  const labelW = Math.max(48, Math.min(Math.round(w * 0.42), longest * CH + 12));
  const valueW = Math.max(...rows.map((r) => String(r.display ?? "").length)) * CH + 10;
  const plotW = Math.max(40, w - labelW - valueW);
  const totals = rows.map((r) => r.segs.reduce((s, g) => s + num(g.v), 0));
  const top = max > 0 ? max : Math.max(1, ...totals);
  const band = 26;
  const bar = 16;
  const height = rows.length * band + 4;
  let out = "";
  rows.forEach((r, i) => {
    const y0 = 2 + i * band;
    const y = y0 + (band - bar) / 2;
    const single = r.segs.length === 1;
    const segs = r.segs.filter((g) => num(g.v) > 0);
    const label = `<text class="viz-label" x="${labelW - 8}" y="${px(y0 + band / 2 + 4)}" text-anchor="end">${esc(fitText(r.label, labelW - 12))}</text>`;
    let marks = "";
    let x = labelW;
    segs.forEach((g, j) => {
      const sw = (Math.min(num(g.v), top) / top) * plotW;
      const last = j === segs.length - 1;
      const drawW = last ? sw : Math.max(0, sw - 2); // 2 px surface gap between stacked segments
      const path = hPath(x, y, drawW, bar, last);
      const fill = path ? `<path class="viz-bar viz-fill-${esc(g.key)}" d="${path}"/>` : "";
      if (single) marks += fill;
      else marks += `${markOpen(g.href, g.tip)}<rect class="viz-hit" x="${px(x)}" y="${y0}" width="${px(Math.max(sw, 1))}" height="${band}"/>${fill}${markClose(g.href)}`;
      x += sw;
    });
    const value = `<text class="viz-value" x="${px(x + 6)}" y="${px(y0 + band / 2 + 4)}">${esc(r.display)}</text>`;
    if (single) {
      const g = r.segs[0];
      out += `${markOpen(g.href ?? r.href, g.tip ?? r.tip)}<rect class="viz-hit" x="0" y="${y0}" width="${w}" height="${band}"/>${label}${marks}${value}${markClose(g.href ?? r.href)}`;
    } else {
      out += (r.tip ? `${markOpen(r.href, r.tip)}<rect class="viz-hit" x="0" y="${y0}" width="${labelW}" height="${band}"/>${label}${markClose(r.href)}` : label)
        + marks + value;
    }
  });
  const axis = `<line class="viz-axis" x1="${labelW}" x2="${labelW}" y1="0" y2="${height}"/>`;
  return `<svg class="viz-svg" viewBox="0 0 ${w} ${height}" width="100%" height="${height}" role="group" aria-label="${esc(title)}">${axis}${out}</svg>`;
}

/**
 * Vertical columns (optionally stacked), y axis with clean ticks and hairline grid.
 * cols: [{ label, segs: [{ v, key, href, tip }] }]
 */
function vbarChart(cols, width, { height = 210, title = "" } = {}) {
  if (!cols.length) return emptyChart();
  const w = Math.max(DASH_MIN_W, Math.floor(width) || 0);
  const totals = cols.map((c) => c.segs.reduce((s, g) => s + num(g.v), 0));
  const { top, step } = niceScale(Math.max(...totals));
  const left = Math.max(28, compact(top).length * CH + 10);
  const right = 6;
  const topPad = 10;
  const axisH = 24; // x-axis band included in the height (no nested scroll)
  const plotH = height - topPad - axisH;
  const base = topPad + plotH;
  const band = (w - left - right) / cols.length;
  const colW = Math.max(2, Math.min(24, band - 2));
  const longest = Math.max(...cols.map((c) => String(c.label ?? "").length));
  const every = Math.max(1, Math.ceil((longest * CH + 6) / band)); // skip x labels that would collide
  let grid = "";
  for (let v = 0; v <= top; v += step) {
    const y = px(base - (v / top) * plotH);
    grid += `<line class="${v === 0 ? "viz-axis" : "viz-grid"}" x1="${left}" x2="${w - right}" y1="${y}" y2="${y}"/>`
      + `<text class="viz-tick" x="${left - 6}" y="${px(y + 4)}" text-anchor="end">${esc(compact(v))}</text>`;
  }
  let marks = "";
  cols.forEach((c, i) => {
    const bx = left + i * band;
    const x = bx + (band - colW) / 2;
    const segs = c.segs.filter((g) => num(g.v) > 0);
    let yc = base;
    segs.forEach((g, j) => {
      const h = (num(g.v) / top) * plotH;
      const last = j === segs.length - 1;
      const drawH = last ? h : Math.max(0, h - 2); // 2 px surface gap between stacked segments
      const path = vPath(x, yc - drawH, colW, drawH, last);
      const hitY = segs.length === 1 ? topPad : yc - h;
      const hitH = segs.length === 1 ? plotH : Math.max(h, 1);
      marks += `${markOpen(g.href, g.tip)}<rect class="viz-hit" x="${px(bx)}" y="${px(hitY)}" width="${px(band)}" height="${px(hitH)}"/>`
        + (path ? `<path class="viz-bar viz-fill-${esc(g.key)}" d="${path}"/>` : "") + markClose(g.href);
      yc -= h;
    });
    if (i % every === 0) {
      marks += `<text class="viz-tick" x="${px(bx + band / 2)}" y="${base + 16}" text-anchor="middle">${esc(c.label)}</text>`;
    }
  });
  return `<svg class="viz-svg" viewBox="0 0 ${w} ${height}" width="100%" height="${height}" role="group" aria-label="${esc(title)}">${grid}${marks}</svg>`;
}

/** Normalises the tsi_dashboard() jsonb into chart rows (missing keys give empty lists). */
function dashModel(raw) {
  const d = raw && typeof raw === "object" ? raw : {};
  const t = d.totals && typeof d.totals === "object" ? d.totals : {};
  const kv = (list) => arr(list).filter((x) => x && typeof x === "object").map((x) => ({ key: String(x.key ?? "none"), n: num(x.n) }));
  const projects = num(t.projects);
  // csrs_by_year: {year, n_documents, n_recommendations} since migration 005 (n = documents); before it, n = recommendations.
  const hasDocs = t.csr_documents !== undefined && t.csr_documents !== null;
  const csrYears = arr(d.csrs_by_year).map((x) => {
    const docs = x.n_documents !== undefined ? num(x.n_documents) : null;
    return { year: num(x.year), docs, recs: x.n_recommendations !== undefined ? num(x.n_recommendations) : docs === null ? num(x.n) : 0 };
  }).filter((x) => x.year).sort((a, b) => a.year - b.year);
  const scores = new Map(arr(d.impact_scores).map((x) => [num(x.score), num(x.n)]));
  const analysed = num(t.impact_projects) || [...scores.values()].reduce((s, n) => s + n, 0);
  return {
    totals: {
      projects, detail: num(t.projects_with_detail), deliverables: num(t.deliverables), csrs: num(t.csrs),
      docs: hasDocs ? num(t.csr_documents) : null, docsLatest: hasDocs ? num(t.csr_documents_latest_year) : null,
      csrParts: num(t.csr_parts), links: num(t.links), linkedProjects: num(t.projects_with_links),
      reports: num(t.impact_reports), analysed, mean: t.impact_mean === null || t.impact_mean === undefined ? null : num(t.impact_mean),
      anomalies: num(t.anomalies), anomaliesHigh: num(t.anomalies_high),
    },
    byProgramme: kv(d.projects_by_programme),
    yearProgramme: arr(d.projects_by_year_programme).map((x) => ({ year: num(x.year), programme: String(x.programme ?? "none"), n: num(x.n) })).filter((x) => x.year),
    byCountry: kv(d.projects_by_country).sort((a, b) => b.n - a.n),
    byArea: kv(d.projects_by_policy_area).sort((a, b) => b.n - a.n),
    byResults: kv(d.projects_by_results),
    csrYears,
    assessments: kv(d.csr_assessments),
    coverage: arr(d.csr_coverage_by_country).map((x) => ({ key: String(x.country ?? x.key ?? ""), csrs: num(x.csrs), covered: num(x.csrs_with_project),
      docs: x.documents !== undefined ? num(x.documents) : null, docsCovered: num(x.documents_with_project) }))
      .filter((x) => x.key && x.csrs > 0),
    linksByLevel: kv(d.links_by_level),
    scores: Array.from({ length: 11 }, (_, s) => ({ score: s, n: scores.get(s) || 0 })),
    byLevel: kv(d.impact_by_level),
    byConfidence: kv(d.impact_by_confidence),
    meanArea: arr(d.impact_mean_by_policy_area).map((x) => ({ key: String(x.key ?? "none"), mean: num(x.mean), n: num(x.n) })),
    meanCountry: arr(d.impact_mean_by_country).map((x) => ({ key: String(x.key ?? ""), mean: num(x.mean), n: num(x.n) })),
    recent: arr(d.impact_recent).filter((x) => x && typeof x === "object"),
    anomalies: arr(d.anomalies_by_type_severity).map((x) => ({ type: String(x.type ?? ""), severity: String(x.severity ?? ""), n: num(x.n) })),
    reviews: reviewFigures(t, d),
  };
}

/** Link reviews from tsi_dashboard() once migration 006 adds them (totals.link_reviews, review_by_level); else null. */
function reviewFigures(t, d) {
  if (t.link_reviews === undefined || t.link_reviews === null || !Array.isArray(d.review_by_level)) return null;
  const totals = new Map(arr(d.links_by_level).map((x) => [String(x?.key ?? ""), num(x?.n)]));
  const byLevel = d.review_by_level.filter((x) => x && typeof x === "object").map((x) => {
    const level = x.level === "direct" ? "direct" : "partial";
    const counted = num(x.confirmed) + num(x.rejected) + num(x.to_review);
    return { level, confirmed: num(x.confirmed), rejected: num(x.rejected), to_review: num(x.to_review), total: Math.max(totals.get(level) || 0, num(x.total), counted) };
  });
  for (const level of ["direct", "partial"]) {
    if (!byLevel.some((b) => b.level === level) && totals.get(level)) byLevel.push({ level, confirmed: 0, rejected: 0, to_review: 0, total: totals.get(level) });
  }
  return { links: num(t.links), byLevel };
}

function programmeKey(p) {
  return PROGRAMME_KEYS[p] || "muted";
}

function areaName(key) {
  return key === "none" ? "Not classified" : areaLabel(key);
}

function inOrder(rows, order) {
  return [...rows].sort((a, b) => {
    const ia = order.indexOf(a.key);
    const ib = order.indexOf(b.key);
    return (ia < 0 ? order.length : ia) - (ib < 0 ? order.length : ib) || a.key.localeCompare(b.key);
  });
}

/** Single-series rows (slot 1 colour for nominal categories). */
function simpleRows(list, { label, href, total, unit = "", key = () => "s1" }) {
  return list.map((x) => {
    const tip = { label: label(x), value: `${fmtN(x.n)}${unit}`, share: total ? `${share(x.n, total)} of the total` : "" };
    return { label: label(x), display: fmtN(x.n), segs: [{ v: x.n, key: key(x), href: href ? href(x) : null, tip }] };
  });
}

function yearProgrammeCols(m) {
  const years = [...new Set(m.yearProgramme.map((x) => x.year))].sort((a, b) => a - b);
  if (!years.length) return [];
  const all = [];
  for (let y = years[0]; y <= years[years.length - 1]; y++) all.push(y);
  const progs = [...PROGRAMMES, ...[...new Set(m.yearProgramme.map((x) => x.programme))].filter((p) => !PROGRAMMES.includes(p)).sort()];
  return all.map((year) => {
    const rows = m.yearProgramme.filter((x) => x.year === year);
    const total = rows.reduce((s, x) => s + x.n, 0);
    return {
      label: String(year),
      segs: progs.map((p) => {
        const n = rows.find((x) => x.programme === p)?.n || 0;
        const href = PROGRAMMES.includes(p) ? hrefFor("projects", { programme: p, from: year, to: year }) : hrefFor("projects", { from: year, to: year });
        return { v: n, key: programmeKey(p), href, tip: { series: p === "none" ? "No programme" : p, label: String(year), value: `${fmtN(n)} projects`, share: total ? `${share(n, total)} of ${year}` : "", key: programmeKey(p) } };
      }).filter((g) => g.v > 0),
      total,
    };
  });
}

function anomalyRows(m) {
  const types = [...new Set(m.anomalies.map((x) => x.type))];
  const total = m.anomalies.reduce((s, x) => s + x.n, 0);
  return types.map((type) => {
    const rows = m.anomalies.filter((x) => x.type === type);
    const n = rows.reduce((s, x) => s + x.n, 0);
    const name = ANOMALY_TYPES[type] || type;
    const known = Object.hasOwn(ANOMALY_TYPES, type);
    const segs = SEVERITY_KEYS.map(([sev, sevLabel]) => {
      const v = rows.find((x) => x.severity === sev)?.n || 0;
      return { v, key: `sev-${sev}`, href: known ? hrefFor("anomalies", { type, severity: sev }) : null,
        tip: { series: `${sevLabel} severity`, label: name, value: fmtN(v), share: n ? `${share(v, n)} of this type` : "", key: `sev-${sev}` } };
    }).filter((g) => g.v > 0);
    return { label: name, type, display: fmtN(n), n, href: known ? hrefFor("anomalies", { type, severity: "all" }) : null,
      tip: { label: name, value: `${fmtN(n)} anomalies`, share: total ? `${share(n, total)} of all anomalies` : "" }, segs };
  }).sort((a, b) => b.n - a.n);
}

function meanRows(list, label, href) {
  return [...list].sort((a, b) => b.mean - a.mean || b.n - a.n).map((x) => {
    const few = x.n < 2;
    const tip = { label: label(x), value: `${x.mean.toFixed(1)}/10 mean score`, share: `${x.n} project${x.n === 1 ? "" : "s"} analysed${few ? " (too few to compare)" : ""}` };
    return { label: label(x), display: `${x.mean.toFixed(1)} · n=${x.n}`, segs: [{ v: x.mean, key: few ? "faint" : "s1", href: href(x), tip }] };
  });
}

function assessmentRows(m) {
  const total = m.assessments.reduce((s, x) => s + x.n, 0);
  const byKey = new Map();
  for (const x of m.assessments) {
    const k = x.key.trim().toLowerCase();
    byKey.set(k, (byKey.get(k) || 0) + x.n);
  }
  const known = new Set(ASSESSMENT_SCALE.map(([k]) => k));
  const rows = ASSESSMENT_SCALE.filter(([k]) => byKey.has(k)).map(([k, label, key]) => ({ key: k, label, n: byKey.get(k), color: key }));
  for (const [k, n] of byKey) if (!known.has(k)) rows.push({ key: k, label: k, n, color: "muted" });
  return rows.map((x) => ({ label: x.label, display: fmtN(x.n),
    segs: [{ v: x.n, key: x.color, href: null, tip: { label: x.label, value: `${fmtN(x.n)} recommendations`, share: `${share(x.n, total)} of recommendations`, key: x.color } }] }));
}

function csrYearsHaveDocs(m) {
  return m.csrYears.some((x) => x.docs !== null);
}

function csrYearTitle(m) {
  return csrYearsHaveDocs(m) ? "CSR documents by year" : "CSR recommendations by year";
}

/** One column per year: documents (one per Member State) when known, the recommendations in the tooltip and the table. */
function csrYearCols(m) {
  const docs = csrYearsHaveDocs(m);
  return m.csrYears.map((x) => {
    const v = docs ? num(x.docs) : x.recs;
    const tip = docs
      ? { label: String(x.year), value: `${fmtN(x.docs)} document${x.docs === 1 ? "" : "s"}`, share: `${fmtN(x.recs)} recommendations` }
      : { label: String(x.year), value: `${fmtN(x.recs)} recommendations`, share: `${share(x.recs, m.totals.csrs)} of recommendations` };
    return { label: String(x.year), segs: [{ v, key: "s1", href: hrefFor("csrs", { year: x.year }), tip }] };
  });
}

function coverageRows(m) {
  return [...m.coverage].map((x) => ({ ...x, pct: x.csrs ? (100 * x.covered) / x.csrs : 0 }))
    .sort((a, b) => b.pct - a.pct || b.csrs - a.csrs).map((x) => ({
      label: countryName(x.key), display: `${Math.round(x.pct)}% (${x.covered}/${x.csrs})`, csrs: x.csrs, covered: x.covered,
      docs: x.docs, docsCovered: x.docsCovered,
      segs: [{ v: x.pct, key: "s1", href: Object.hasOwn(COUNTRIES, x.key) ? hrefFor("lineage", { country: x.key }) : null,
        tip: { label: countryName(x.key), value: `${Math.round(x.pct)}% of recommendations supported`,
          share: `${fmtN(x.covered)} of ${fmtN(x.csrs)} recommendations have a linked project`
            + (x.docs !== null ? ` · ${fmtN(x.docsCovered)} of ${fmtN(x.docs)} documents` : "") } }],
    }));
}

/** Chart builders by slot id: (model, width) -> SVG or an empty state. */
const DASH_CHARTS = {
  year: (m, w) => vbarChart(yearProgrammeCols(m), w, { title: "Projects by year and programme" }),
  country: (m, w) => hbarChart(simpleRows(m.byCountry, {
    label: (x) => countryName(x.key), total: m.totals.projects, unit: " projects",
    href: (x) => (Object.hasOwn(COUNTRIES, x.key) ? hrefFor("projects", { country: x.key }) : null),
  }), w, { title: "Projects by country" }),
  area: (m, w) => hbarChart(simpleRows(m.byArea, {
    label: (x) => areaName(x.key), total: m.totals.projects, unit: " projects",
    href: (x) => (Object.hasOwn(POLICY_AREAS, x.key) ? hrefFor("projects", { area: x.key }) : null),
  }), w, { title: "Projects by policy area" }),
  results: (m, w) => hbarChart(simpleRows(inOrder(m.byResults, ["achieved", "expected", "none"]), {
    label: (x) => RESULT_LABELS[x.key] || x.key, total: m.totals.projects, unit: " projects",
  }), w, { title: "Projects by results" }),
  scores: (m, w) => (m.totals.analysed ? vbarChart(m.scores.map((x) => ({
    label: String(x.score),
    segs: [{ v: x.n, key: "s1", href: null, tip: { label: `Score ${x.score}/10`, value: `${fmtN(x.n)} projects`, share: `${share(x.n, m.totals.analysed)} of analysed projects` } }],
  })), w, { height: 180, title: "Impact score distribution" }) : emptyChart()),
  level: (m, w) => hbarChart(simpleRows(inOrder(m.byLevel, LEVEL_ORDER), {
    label: (x) => (x.key === "none" ? "Not stated" : x.key), total: m.totals.analysed, unit: " projects",
  }), w, { title: "Impact level" }),
  confidence: (m, w) => hbarChart(simpleRows(inOrder(m.byConfidence, CONFIDENCE_ORDER), {
    label: (x) => (x.key === "none" ? "Not stated" : x.key), total: m.totals.analysed, unit: " projects",
  }), w, { title: "Confidence" }),
  meanArea: (m, w) => hbarChart(meanRows(m.meanArea, (x) => areaName(x.key),
    (x) => (Object.hasOwn(POLICY_AREAS, x.key) ? hrefFor("projects", { area: x.key }) : null)), w, { max: 10, title: "Mean impact score by policy area" }),
  meanCountry: (m, w) => hbarChart(meanRows(m.meanCountry, (x) => countryName(x.key),
    (x) => (Object.hasOwn(COUNTRIES, x.key) ? hrefFor("projects", { country: x.key }) : null)), w, { max: 10, title: "Mean impact score by country" }),
  csrYear: (m, w) => vbarChart(csrYearCols(m), w, { height: 180, title: csrYearTitle(m) }),
  assessments: (m, w) => hbarChart(assessmentRows(m), w, { title: "Latest assessment of the recommendations" }),
  coverage: (m, w) => hbarChart(coverageRows(m), w, { max: 100, title: "Recommendations supported by at least one project, by country" }),
  links: (m, w) => hbarChart(simpleRows(inOrder(m.linksByLevel, ["direct", "partial"]), {
    label: (x) => LINK_LABELS[x.key] || x.key, total: m.totals.links, unit: " links",
  }), w, { title: "Project–CSR links by level" }),
  anomalies: (m, w) => hbarChart(anomalyRows(m), w, { title: "Anomalies by type and severity" }),
  reviews: (m, w) => (m.reviewStats ? hbarChart(reviewRows(m.reviewStats), w, { title: "Reviews of the AI-assessed links, by level" }) : ""),
};

function legend(items) {
  return `<ul class="viz-legend">${items.map((it) => {
    const inner = `<span class="viz-key viz-key-${esc(it.key)}" aria-hidden="true"></span>${esc(it.label)}`
      + (it.n !== undefined ? ` <span class="muted">${esc(fmtN(it.n))}</span>` : "");
    return `<li>${it.href ? `<a href="${esc(it.href)}">${inner}</a>` : inner}</li>`;
  }).join("")}</ul>`;
}

function dataTable(headers, rows) {
  return `<details class="viz-table"><summary>Table</summary>${table(headers, rows.map((r) => r.map((c, i) => td(esc(c), i ? "num" : ""))), "No data.")}</details>`;
}

function figure(id, title, { sub = "", legendHtml = "", tableHtml = "", wide = false, info = "" } = {}) {
  return `<figure class="viz${wide ? " viz-wide" : ""}"><figcaption><div class="title-row"><h4 class="viz-title">${esc(title)}</h4>`
    + `${info ? infoButton(info) : ""}</div>`
    + (sub ? `<p class="viz-sub">${esc(sub)}</p>` : "") + `</figcaption>${legendHtml}`
    + `<div class="viz-slot" data-chart="${id}"></div>${tableHtml}</figure>`;
}

function tile(label, value, sub, info = "") {
  return `<div class="kpi"><p class="kpi-label">${esc(label)}${info ? ` ${infoButton(info, `About: ${label}`)}` : ""}</p><p class="kpi-value">${esc(value)}</p>`
    + (sub ? `<p class="kpi-sub">${esc(sub)}</p>` : "") + `</div>`;
}

function dashboardHtml(m, at) {
  const t = m.totals;
  const years = m.csrYears.length ? `${m.csrYears[0].year}–${m.csrYears[m.csrYears.length - 1].year}` : "2016–2025";
  const tiles = `<div class="kpis">`
    + tile("Projects", fmtN(t.projects), t.projects ? `${share(t.detail, t.projects)} with a detail page` : "")
    + tile("Deliverables", fmtN(t.deliverables), "")
    + (t.docs !== null && t.docs > 0
      ? tile(`CSR documents (${years})`, `${fmtN(t.docsLatest)} per year`,
        `${fmtN(t.docs)} documents · ${fmtN(t.csrs)} recommendations${t.csrParts ? ` · ${fmtN(t.csrParts)} sub-parts` : ""}`, "csrlevels")
      : tile(`CSRs (${years})`, fmtN(t.csrs), t.csrParts ? `${fmtN(t.csrParts)} sub-parts` : "", "csrlevels"))
    + tile("Project → CSR links", fmtN(t.links), t.projects ? `${share(t.linkedProjects, t.projects)} of projects linked` : "")
    + tile("Impact analyses", fmtN(t.reports), t.mean !== null && t.analysed ? `mean score ${t.mean.toFixed(1)}/10` : "none yet")
    + tile("Anomalies", fmtN(t.anomalies), `${fmtN(t.anomaliesHigh)} high severity`) + `</div>`;

  const cols = yearProgrammeCols(m);
  const progTotals = Object.fromEntries(m.byProgramme.map((x) => [x.key, x.n]));
  const progLegend = legend([...PROGRAMMES, ...Object.keys(progTotals).filter((p) => !PROGRAMMES.includes(p))]
    .filter((p) => progTotals[p] || cols.some((c) => c.segs.some((g) => g.tip.series === p)))
    .map((p) => ({ key: programmeKey(p), label: p === "none" ? "No programme" : p, n: progTotals[p],
      href: PROGRAMMES.includes(p) ? hrefFor("projects", { programme: p }) : null })));
  const progsInTable = [...PROGRAMMES, ...[...new Set(m.yearProgramme.map((x) => x.programme))].filter((p) => !PROGRAMMES.includes(p))];
  const projects = `<section class="dash-section" aria-labelledby="dash-projects"><h3 id="dash-projects">Projects</h3><div class="dash-grid">`
    + figure("year", "Projects by year and programme", { wide: true, sub: "Click a bar to list the projects.", legendHtml: progLegend,
      tableHtml: dataTable(["Year", ...progsInTable, "Total"], cols.map((c) => [c.label,
        ...progsInTable.map((p) => fmtN(m.yearProgramme.find((x) => x.year === Number(c.label) && x.programme === p)?.n || 0)), fmtN(c.total)])) })
    + figure("country", "Projects by country", { sub: "A multi-country project counts for each country.",
      tableHtml: dataTable(["Country", "Projects"], m.byCountry.map((x) => [countryName(x.key), fmtN(x.n)])) })
    + figure("area", "Projects by policy area", { tableHtml: dataTable(["Policy area", "Projects"], m.byArea.map((x) => [areaName(x.key), fmtN(x.n)])) })
    + figure("results", "Projects by results", { info: "results", tableHtml: dataTable(["Results", "Projects"], inOrder(m.byResults, ["achieved", "expected", "none"]).map((x) => [RESULT_LABELS[x.key] || x.key, fmtN(x.n)])) })
    + `</div></section>`;

  let impactBody;
  if (!t.analysed && !m.recent.length) {
    impactBody = `<p class="empty">No impact analysis saved yet. Run one from the Impact tab of a project.</p>`;
  } else {
    const few = t.analysed < 5 ? `<p class="notice-few" role="note">Few analyses so far: statistics are indicative.</p>` : "";
    const recentRows = m.recent.map((r) => [
      td(r.project_id ? hashLink("project", r.project_id, r.title || r.project_id) : esc(r.title), "title-cell"),
      td(Number.isFinite(r.score) ? `${esc(r.score)}/10` : "—", "num"), td(esc(r.level)), td(esc(r.confidence)),
      td(esc(fmtDate(r.created_at)), "nowrap"), td(esc(r.source)),
    ]);
    impactBody = few + `<p class="muted small">Statistics use the latest analysis of each project (${fmtN(t.analysed)} project${t.analysed === 1 ? "" : "s"}).</p>`
      + `<div class="dash-grid">`
      + figure("scores", "Impact score distribution", { info: "impact", sub: "Score out of 10, projects per score.",
        tableHtml: dataTable(["Score", "Projects"], m.scores.map((x) => [String(x.score), fmtN(x.n)])) })
      + `<div class="viz-pair">`
      + figure("level", "Impact level", { info: "impact", tableHtml: dataTable(["Level", "Projects"], inOrder(m.byLevel, LEVEL_ORDER).map((x) => [x.key, fmtN(x.n)])) })
      + figure("confidence", "Confidence", { info: "impact", tableHtml: dataTable(["Confidence", "Projects"], inOrder(m.byConfidence, CONFIDENCE_ORDER).map((x) => [x.key, fmtN(x.n)])) })
      + `</div>`
      + figure("meanArea", "Mean score by policy area", { info: "impact", sub: "n = projects analysed; greyed when n < 2.",
        tableHtml: dataTable(["Policy area", "Mean score", "n"], m.meanArea.map((x) => [areaName(x.key), x.mean.toFixed(1), String(x.n)])) })
      + figure("meanCountry", "Mean score by country", { info: "impact", sub: "n = projects analysed; greyed when n < 2.",
        tableHtml: dataTable(["Country", "Mean score", "n"], m.meanCountry.map((x) => [countryName(x.key), x.mean.toFixed(1), String(x.n)])) })
      + `</div><h4 class="viz-title">Latest analyses</h4>`
      + table(["Project", "Score", "Level", "Confidence", "Date", "Source"], recentRows, "No analysis yet.");
  }
  const impact = `<section class="dash-section" aria-labelledby="dash-impact"><h3 id="dash-impact">Impact analyses <span class="ai-note">AI-generated</span>${infoButton("impact", "About the impact analyses")}</h3>${impactBody}</section>`;

  const sevTotals = Object.fromEntries(SEVERITY_KEYS.map(([s]) => [s, m.anomalies.filter((x) => x.severity === s).reduce((a, x) => a + x.n, 0)]));
  const anomalyTypes = anomalyRows(m);
  const csrs = `<section class="dash-section" aria-labelledby="dash-csrs"><h3 id="dash-csrs">CSRs and lineage</h3><div class="dash-grid">`
    + figure("csrYear", csrYearTitle(m), { info: "csrlevels",
      sub: csrYearsHaveDocs(m) ? "One document per Member State and year; hover a bar for its recommendations. Click to open that year's documents."
        : "Click a bar to list that year's documents.",
      tableHtml: csrYearsHaveDocs(m)
        ? dataTable(["Year", "Documents", "Recommendations"], m.csrYears.map((x) => [String(x.year), fmtN(x.docs), fmtN(x.recs)]))
        : dataTable(["Year", "Recommendations"], m.csrYears.map((x) => [String(x.year), fmtN(x.recs)])) })
    + figure("assessments", "Latest assessment of the recommendations", { info: "assessments", sub: "From full implementation to no progress; 2026 recommendations not yet assessed.",
      tableHtml: dataTable(["Assessment", "Recommendations"], assessmentRows(m).map((r) => [r.label, r.display])) })
    + figure("coverage", "Recommendations supported by a project, by country", { wide: true, sub: "Share of the country's recommendations with at least one linked project (AI-assessed links). Click to open the lineage network.",
      tableHtml: m.coverage.some((x) => x.docs !== null)
        ? dataTable(["Country", "Recommendations", "With a project", "Share", "Documents", "Documents with a project"], coverageRows(m).map((r) => [r.label, fmtN(r.csrs), fmtN(r.covered), share(r.covered, r.csrs), fmtN(r.docs ?? 0), fmtN(r.docsCovered)]))
        : dataTable(["Country", "Recommendations", "With a project", "Share"], coverageRows(m).map((r) => [r.label, fmtN(r.csrs), fmtN(r.covered), share(r.covered, r.csrs)])) })
    + figure("links", "Project–CSR links by level", { info: "links", tableHtml: dataTable(["Level", "Links"], inOrder(m.linksByLevel, ["direct", "partial"]).map((x) => [LINK_LABELS[x.key] || x.key, fmtN(x.n)])) })
    + `</div></section>`;
  const quality = `<section class="dash-section" aria-labelledby="dash-quality"><h3 id="dash-quality">Data quality ${infoButton("anomalies", "About the anomalies")}</h3><div class="dash-grid">`
    + figure("anomalies", "Anomalies by type and severity", { info: "anomalies", wide: true, sub: "Click a segment to list those anomalies.",
      legendHtml: legend(SEVERITY_KEYS.map(([s, label]) => ({ key: `sev-${s}`, label, n: sevTotals[s], href: hrefFor("anomalies", { severity: s }) }))),
      tableHtml: dataTable(["Type", ...SEVERITY_KEYS.map(([, l]) => l), "Total"], anomalyTypes.map((r) => [r.label,
        ...SEVERITY_KEYS.map(([s]) => fmtN(m.anomalies.find((x) => x.type === r.type && x.severity === s)?.n || 0)), r.display])) })
    + `</div></section>`;
  const when = at instanceof Date ? at.toLocaleTimeString("en-GB", { hour: "2-digit", minute: "2-digit" }) : "";
  return `<div id="dash" class="dash"><div class="toolbar"><span class="muted small">Figures across all projects and CSRs${when ? ` · loaded at ${esc(when)}` : ""}.</span>`
    + `<span class="toolbar-actions"><span id="dash-status" class="muted small" role="status"></span>`
    + `<button type="button" class="btn btn-secondary" id="dash-refresh">Refresh</button></span></div>`
    + tiles + projects + impact + csrs
    + `<section class="dash-section" id="dash-reviews" aria-labelledby="dash-reviews-h" hidden></section>` + quality
    + `<div id="dash-tip" class="viz-tip" role="tooltip" hidden></div></div>`;
}

function paintDashCharts() {
  const root = $("dash");
  if (!root || !dashCache) return;
  dashWidth = root.clientWidth;
  root.querySelectorAll(".viz-slot").forEach((slot) => {
    const build = DASH_CHARTS[slot.dataset.chart];
    slot.innerHTML = build ? build(dashCache.model, slot.clientWidth) : "";
  });
}

/** One tooltip for the whole dashboard: value first, then category and share; same on hover and keyboard focus. */
function bindDashTips(root) {
  const tip = $("dash-tip");
  if (!tip) return;
  const hide = () => { tip.hidden = true; };
  const show = (mark, x, y) => {
    const ds = mark.dataset;
    const value = document.createElement("strong");
    value.textContent = ds.tipValue || "";
    const label = document.createElement("div");
    if (ds.tipKey && /^[a-z0-9-]+$/.test(ds.tipKey)) {
      const key = document.createElement("span");
      key.className = `viz-line viz-key-${ds.tipKey}`;
      label.append(key);
    }
    label.append(document.createTextNode([ds.tipSeries, ds.tipLabel].filter(Boolean).join(" · ")));
    tip.replaceChildren(value, label);
    if (ds.tipShare) {
      const s = document.createElement("div");
      s.className = "muted";
      s.textContent = ds.tipShare;
      tip.append(s);
    }
    tip.hidden = false;
    const box = root.getBoundingClientRect();
    const tw = tip.offsetWidth;
    const th = tip.offsetHeight;
    let left = x - box.left + 12;
    if (left + tw > box.width) left = Math.max(0, x - box.left - tw - 12);
    let top = y - box.top - th - 10;
    if (top < 0) top = y - box.top + 18;
    tip.style.left = `${Math.round(left)}px`;
    tip.style.top = `${Math.round(top)}px`;
  };
  const markOf = (el) => (el instanceof Element ? el.closest("[data-tip-value]") : null);
  root.addEventListener("pointermove", (e) => {
    const m = markOf(e.target);
    if (m && root.contains(m)) show(m, e.clientX, e.clientY); else hide();
  });
  root.addEventListener("pointerleave", hide);
  root.addEventListener("focusin", (e) => {
    const m = markOf(e.target);
    if (!m) return hide();
    const r = m.getBoundingClientRect();
    show(m, r.left + Math.min(r.width / 2, 160), r.top);
  });
  root.addEventListener("focusout", hide);
}

async function fetchDashboard() {
  const { data, error } = await sb.rpc("tsi_dashboard");
  if (error) return { error };
  let raw = data;
  if (typeof raw === "string") {
    try { raw = JSON.parse(raw); } catch { raw = null; }
  }
  return { model: dashModel(raw) };
}

function paintDashboard() {
  view(heading("Dashboard") + dashboardHtml(dashCache.model, dashCache.at));
  const root = $("dash");
  bindDashTips(root);
  $("dash-refresh").addEventListener("click", refreshDashboard);
  paintDashCharts();
  paintReviewCard();
}

/** Refresh keeps the current render (dimmed) until the new figures arrive. */
async function refreshDashboard() {
  const seq = ++renderSeq;
  const root = $("dash");
  const button = $("dash-refresh");
  root?.classList.add("is-refreshing");
  if (button) button.disabled = true;
  $("dash-status").textContent = "Refreshing…";
  let res;
  try {
    res = await fetchDashboard();
  } catch (e) {
    res = { error: e };
  }
  if (stale(seq) || !$("dash")) return;
  if (res.error) {
    root.classList.remove("is-refreshing");
    button.disabled = false;
    $("dash-status").textContent = `Could not refresh: ${res.error.message || res.error}`;
    return;
  }
  dashCache = { uid: currentUid, model: res.model, at: new Date() };
  ovCache = null; // the lineage views reload their links on next use
  progressCache = null;
  lineageCache.clear();
  reviewStats = null;
  reviewLinksCache = null;
  relatedCache = null;
  paintDashboard();
  $("dash-status").textContent = "Figures updated.";
  $("dash-refresh").focus();
}

async function renderDashboard(seq) {
  if (!dashCache || dashCache.uid !== currentUid) {
    const res = await fetchDashboard();
    if (stale(seq)) return;
    if (res.error) return showError(res.error, "Could not load the dashboard");
    dashCache = { uid: currentUid, model: res.model, at: new Date() };
  }
  paintDashboard();
}

// ---------- lineage: CSR documents -> recommendations -> (initial request) -> projects -> outcome, per country (hand-made SVG) ----------

const LN_FROM = 2019;
const LN_TO = 2025;
const LN_NODE_H = 30; // two text lines
const LN_STEP = 36; // vertical pitch of a node
const LN_HEAD_H = 22; // column headings (one line); the layout adds the caption lines below (L.headH)
const LN_GAP = 12; // between the recommendations of two documents
const LN_MIN_COL = 96; // below this column width the labels are unreadable: list view instead
const LN_WIDE = 720; // from this drawing width the five stages fit; below, a fixed minimum width scrolls inside the card
const LN_MIN_W = 560; // drawing width of the compact (scrolling) layout
const LN_CAP = 5.6; // average glyph width (px) of the 10 px captions
const LN_REQUEST_NOTE = "Country request and claimed CSRs — JIRA export pending";
const lineageCache = new Map(); // `${uid}|${country}|${from}|${to}` -> { csrs, scores }
let lineageState = null; // { model, country, width, pinned }

/** Results status of a project: projects.results ("achieved", "expected" or empty) -> key, labels (RESULT_LABELS). */
function resultsInfo(value) {
  const k = String(value ?? "").trim().toLowerCase();
  const key = Object.hasOwn(RESULT_LABELS, k) && k !== "none" ? k : "none";
  const short = { achieved: "Achieved", expected: "Expected", none: "Not stated" }[key];
  return { key, label: key === "none" && k ? String(value) : RESULT_LABELS[key], short };
}

/** Latest impact analysis of a project, from the value of the scores map: a number (score only) or an impact_reports row. */
function impactOf(v) {
  if (Number.isFinite(v)) return { score: v, level: "", confidence: "", created_at: "" };
  if (!v || typeof v !== "object") return null;
  return { score: Number.isFinite(v.score) ? v.score : null, level: v.level ?? "", confidence: v.confidence ?? "", created_at: v.created_at ?? "" };
}

function assessmentInfo(value) {
  const k = String(value ?? "none").trim().toLowerCase() || "none";
  const hit = ASSESSMENT_SCALE.find(([key]) => key === k);
  return hit ? { label: hit[1], key: hit[2] } : { label: String(value), key: "muted" };
}

function csrNumber(a, b) {
  return (parseFloat(a.number) - parseFloat(b.number)) || String(a.number ?? "").localeCompare(String(b.number ?? ""));
}

/**
 * Graph model from the nested csrs rows and the latest score per project.
 * One edge per (CSR, project) pair; its level is direct if any sub-part link is direct.
 */
function lineageModel(rows, scores, { unlinked = false, country = "" } = {}) {
  const csrList = arr(rows).filter((c) => c && c.csr_id).map((c) => ({
    id: String(c.csr_id), year: num(c.year), number: c.number ?? "", title: c.title ?? "", text: c.text ?? "",
    assessment: c.assessment ?? null, assessmentYear: c.assessment_year ?? null, parts: arr(c.csr_parts),
  })).sort((a, b) => a.year - b.year || csrNumber(a, b) || a.id.localeCompare(b.id));
  const projects = new Map();
  const edges = new Map();
  for (const c of csrList) {
    for (const part of c.parts) {
      for (const link of arr(part?.project_csr_links)) {
        const p = link?.projects;
        if (!p || !p.project_id) continue;
        const pid = String(p.project_id);
        if (!projects.has(pid)) {
          const impact = impactOf(scores?.get?.(pid));
          projects.set(pid, { id: pid, title: p.title ?? pid, programme: p.programme ?? "", year: p.year ?? "", results: p.results ?? "",
            outcome: resultsInfo(p.results), deliverables: Number.isFinite(Number(p.deliverables)) && p.deliverables !== null ? num(p.deliverables) : 0,
            url: p.url ?? "", impact, score: impact && Number.isFinite(impact.score) ? impact.score : null });
        }
        const key = `${c.id}\u0000${pid}`;
        if (!edges.has(key)) edges.set(key, { csr: c.id, project: pid, level: "partial", parts: [] });
        const e = edges.get(key);
        const level = link.level === "direct" ? "direct" : "partial";
        if (level === "direct") e.level = "direct";
        e.parts.push({ part: String(part.part_id ?? ""), text: part.text ?? "", level, rationale: link.rationale ?? "", basis: link.basis ?? "" });
      }
    }
  }
  const edgeList = [...edges.values()];
  const linkedCsrs = new Set(edgeList.map((e) => e.csr));
  const shown = csrList.filter((c) => unlinked || linkedCsrs.has(c.id));
  const csrIndex = new Map(shown.map((c, i) => [c.id, i]));
  const projectList = [...projects.values()];
  const visibleEdges = edgeList.filter((e) => csrIndex.has(e.csr));
  for (const e of visibleEdges) e.parts.sort((a, b) => (a.level === b.level ? a.part.localeCompare(b.part) : a.level === "direct" ? -1 : 1));
  // Documents: the country's CSR document of each year (csrdoc-<ISO>-<year>), with the indices of its recommendations shown.
  const perYear = new Map();
  for (const c of csrList) perYear.set(c.year, (perYear.get(c.year) || 0) + 1);
  const docs = [];
  const docOf = [];
  shown.forEach((c, i) => {
    let d = docs[docs.length - 1];
    if (!d || d.year !== c.year) {
      d = { id: docIdOf(country, c.year), year: c.year, recs: [], total: perYear.get(c.year) || 0, linked: false };
      docs.push(d);
    }
    d.recs.push(i);
    docOf[i] = docs.length - 1;
    if (linkedCsrs.has(c.id)) d.linked = true;
  });
  return {
    country,
    csrs: shown.map((c, i) => ({ ...c, doc: docOf[i], linked: linkedCsrs.has(c.id), info: assessmentInfo(c.assessment) })),
    docs,
    docIndex: new Map(docs.map((d, k) => [d.id, k])),
    projects: projectList,
    edges: visibleEdges,
    csrIndex,
    summary: {
      docs: perYear.size, docsLinked: new Set(csrList.filter((c) => linkedCsrs.has(c.id)).map((c) => c.year)).size,
      csrs: csrList.length, csrsLinked: linkedCsrs.size, projects: projectList.length,
      links: edgeList.length, direct: edgeList.filter((e) => e.level === "direct").length,
      subLinks: edgeList.reduce((n, e) => n + e.parts.length, 0),
      achieved: projectList.filter((p) => p.outcome.key === "achieved").length,
      expected: projectList.filter((p) => p.outcome.key === "expected").length,
      notStated: projectList.filter((p) => p.outcome.key === "none").length,
      deliverables: projectList.reduce((n, p) => n + p.deliverables, 0),
      withImpact: projectList.filter((p) => p.impact).length,
    },
  };
}

/**
 * Positions in five columns: documents (one per year), their recommendations by number, the initial request (placeholder
 * band, no node yet), projects in barycentric order (mean centre of their recommendations, pulled towards it) and one
 * outcome per project on the same row. A document sits at the middle of its recommendations.
 * Below LN_WIDE the drawing keeps a minimum width (LN_MIN_W) and scrolls inside its card; the request band shrinks to its heading.
 */
function lineageLayout(model, width) {
  const avail = Math.max(280, Math.floor(width) || 0);
  const narrow = avail < LN_WIDE;
  const W = narrow ? Math.max(avail, LN_MIN_W) : avail;
  let docW, leftW, reqW, rightW, outW, linkGap;
  if (narrow) {
    docW = 52;
    leftW = Math.round(Math.max(110, W * 0.21));
    reqW = Math.round(Math.max(76, W * 0.1));
    rightW = Math.round(Math.max(130, W * 0.27));
    outW = Math.round(Math.max(108, W * 0.18));
    linkGap = 10;
  } else {
    docW = Math.round(Math.min(116, W * 0.1));
    leftW = Math.round(Math.min(240, W * 0.2));
    reqW = Math.round(Math.min(130, Math.max(118, W * 0.11)));
    rightW = Math.round(Math.min(290, W * 0.25));
    outW = Math.round(Math.min(176, W * 0.15));
    linkGap = 14;
  }
  const rest = W - docW - leftW - rightW - outW - linkGap;
  const gapB = Math.min(rest - 8, Math.max(reqW + (narrow ? 8 : 48), Math.round(rest * 0.7))); // recommendations -> projects
  const gap1 = rest - gapB; // documents -> recommendations
  const leftX = docW + gap1;
  const rightX = leftX + leftW + gapB;
  const outX = rightX + rightW + linkGap;
  const reqX = Math.round(leftX + leftW + (gapB - reqW) / 2);
  // Headings: one line, then the captions (the request note wraps on up to three lines in the band).
  const note = narrow ? ["JIRA pending"] : wrapText(LN_REQUEST_NOTE, reqW - 8, LN_CAP).slice(0, 3);
  const headH = LN_HEAD_H + 12 * note.length + (narrow ? 0 : 4);
  const csrPos = [];
  const docPos = [];
  let y = headH;
  arr(model.docs).forEach((d, k) => {
    if (k) y += LN_GAP;
    const start = y;
    for (const i of d.recs) {
      csrPos[i] = y;
      y += LN_STEP;
    }
    docPos[k] = Math.round((start + y - LN_STEP + LN_NODE_H) / 2 - LN_NODE_H / 2);
  });
  const leftH = y;
  const centre = (i) => csrPos[i] + LN_NODE_H / 2;
  const byProject = new Map(model.projects.map((p) => [p.id, []]));
  for (const e of model.edges) byProject.get(e.project)?.push(model.csrIndex.get(e.csr));
  const order = model.projects.map((p, i) => {
    const ids = byProject.get(p.id) || [];
    const bary = ids.length ? ids.reduce((s, k) => s + centre(k), 0) / ids.length : Infinity;
    return { i, bary, p };
  }).filter((o) => Number.isFinite(o.bary))
    .sort((a, b) => a.bary - b.bary || String(b.p.year).localeCompare(String(a.p.year)) || String(a.p.title).localeCompare(String(b.p.title)));
  // Forward pass: as close to the barycentre as the minimum pitch allows; backward pass: keep within the height.
  const top = headH;
  const ys = [];
  order.forEach((o, k) => {
    const target = Math.max(top, o.bary - LN_NODE_H / 2);
    ys[k] = k ? Math.max(target, ys[k - 1] + LN_STEP) : target;
  });
  const bottom = Math.max(leftH, top + order.length * LN_STEP);
  for (let k = order.length - 1; k >= 0; k--) {
    const limit = k === order.length - 1 ? bottom - LN_STEP : ys[k + 1] - LN_STEP;
    ys[k] = Math.max(top + k * LN_STEP, Math.min(ys[k], limit));
  }
  const projPos = new Map(order.map((o, k) => [o.p.id, ys[k]]));
  const height = Math.ceil(Math.max(leftH, order.length ? ys[order.length - 1] + LN_STEP : 0)) + 4;
  return { W, narrow, scroll: W > avail, headH, note, docW, leftX, leftW, reqX, reqW, rightW, rightX, outX, outW, csrPos, docPos, projPos,
    order: order.map((o) => o.p.id), height, readable: leftW >= LN_MIN_COL && rightW >= LN_MIN_COL && gap1 >= 8 };
}

/** Words on lines of at most `width` px (estimate); a word longer than a line is cut with fitText. */
function wrapText(text, width, charW = CH) {
  const max = Math.max(2, Math.floor(width / charW));
  const lines = [];
  for (const w of String(text ?? "").split(/\s+/).filter(Boolean)) {
    const last = lines[lines.length - 1];
    if (last !== undefined && `${last} ${w}`.length <= max) lines[lines.length - 1] = `${last} ${w}`;
    else lines.push(fitText(w, width, charW));
  }
  return lines;
}

function lnAria(parts) {
  return esc(parts.filter(Boolean).join(", "));
}

/** Results marker of an outcome node: filled disc = achieved, ring = expected, dashed ring = not stated. */
function outcomeMarker(key, cx, cy, r = 5) {
  return `<circle class="ln-res ln-res-${key}" cx="${px(cx)}" cy="${px(cy)}" r="${r}"/>`;
}

/** The network as an SVG string (pure: also used by the Node render test). */
function lineageSvg(model, width) {
  const L = lineageLayout(model, width);
  const cc = model.country;
  const x0 = L.docW;
  const x1 = L.leftX + L.leftW;
  const x2 = L.rightX;
  const x3 = L.rightX + L.rightW;
  const curve = (xa, ya, xb, yb) => {
    const dx = (xb - xa) / 2;
    return `M${px(xa)} ${px(ya)}C${px(xa + dx)} ${px(ya)} ${px(xb - dx)} ${px(yb)} ${px(xb)} ${px(yb)}`;
  };
  const csrIdx = model.csrIndex;
  // Initial request: a placeholder band (no data yet); the recommendation -> project lines cross it unchanged.
  const band = `<g class="ln-band" data-band="1">`
    + `<rect class="ln-bandbox" x="${L.reqX + 0.5}" y="0.5" width="${L.reqW - 1}" height="${L.height - 1}" rx="6"/></g>`;
  let edges = "";
  model.csrs.forEach((c, i) => { // document -> recommendation (structure, always drawn)
    const dy = L.docPos[c.doc];
    if (dy === undefined) return;
    edges += `<path class="ln-tree" data-t="${i}" d="${curve(x0, dy + LN_NODE_H / 2, L.leftX, L.csrPos[i] + LN_NODE_H / 2)}"/>`;
  });
  model.edges.forEach((e, i) => {
    const ci = csrIdx.get(e.csr);
    const py = L.projPos.get(e.project);
    if (ci === undefined || py === undefined) return;
    const d = curve(x1, L.csrPos[ci] + LN_NODE_H / 2, x2, py + LN_NODE_H / 2);
    edges += `<g class="ln-edge ln-${e.level}" data-e="${i}"><path class="ln-line" d="${d}"/><path class="ln-hit" d="${d}"/></g>`;
  });
  L.order.forEach((id, k) => { // project -> outcome (same row)
    const y = L.projPos.get(id) + LN_NODE_H / 2;
    edges += `<path class="ln-olink" data-ol="${k}" d="M${px(x3)} ${px(y)}H${px(L.outX)}"/>`;
  });
  const textW1 = (w) => w - 20 - 6; // marker + padding
  const colh = (x, w, full, short, cap) => `<text class="ln-colh" x="${x}" y="13">${esc(full.length * 7.2 <= w ? full : short)}</text>`
    + (cap && !L.narrow && cap.length * LN_CAP <= w - 4 ? `<text class="ln-colcap" x="${x}" y="${LN_HEAD_H + 4}">${esc(cap)}</text>` : "");
  const reqC = L.reqX + L.reqW / 2;
  let nodes = colh(0, L.docW, "DOCUMENTS", "DOCS", "one per year") + colh(L.leftX, L.leftW, "RECOMMENDATIONS", "RECS", "marker: latest assessment")
    + colh(L.rightX, L.rightW, "PROJECTS", "PROJ.", "marker: programme") + colh(L.outX, L.outW, "OUTCOME", "RESULT", "results · deliverables · impact")
    + `<text class="ln-colh" x="${px(reqC)}" y="13" text-anchor="middle">${esc("INITIAL REQUEST".length * 7.2 <= L.reqW - 4 ? "INITIAL REQUEST" : "REQUEST")}</text>`
    + L.note.map((t, j) => `<text class="ln-colcap ln-reqnote" x="${px(reqC)}" y="${LN_HEAD_H + 4 + 12 * j}" text-anchor="middle">${esc(t)}</text>`).join("");
  arr(model.docs).forEach((d, k) => {
    const y = L.docPos[k];
    const recs = `${fmtN(d.total)} recommendation${d.total === 1 ? "" : "s"}`;
    const line1 = `${cc} ${d.year}`.trim();
    const t1 = line1.length * CH <= L.docW - 12 ? line1 : String(d.year);
    const t2 = recs.length * 6.4 <= L.docW - 12 ? recs : fitText(`${fmtN(d.total)} rec.`, L.docW - 12, 6.4);
    nodes += `<g class="ln-node ln-doc${d.linked ? "" : " ln-unlinked"}" data-node="d${k}" tabindex="0" role="button" aria-pressed="false"`
      + ` aria-label="${lnAria([`${countryName(cc)} ${d.year} CSR document`, recs])}">`
      + `<rect class="ln-box ln-docbox" x="0.5" y="${y}" width="${L.docW - 1}" height="${LN_NODE_H}" rx="4"/>`
      + `<text class="ln-t1" x="6" y="${y + 13}">${esc(t1)}</text>`
      + `<text class="ln-t2" x="6" y="${y + 25}">${esc(t2)}</text></g>`;
  });
  model.csrs.forEach((c, i) => {
    const y = L.csrPos[i];
    const x = L.leftX;
    const full = `${cc} ${c.year} · CSR ${c.number}`;
    const label = full.length * CH <= textW1(L.leftW) ? full : fitText(`CSR ${c.number}`, textW1(L.leftW));
    const n = model.edges.filter((e) => e.csr === c.id).length;
    nodes += `<g class="ln-node ln-csr${c.linked ? "" : " ln-unlinked"}" data-node="c${i}" tabindex="0" role="button" aria-pressed="false"`
      + ` aria-label="${lnAria([full, c.info.label, `${n} project${n === 1 ? "" : "s"}`])}">`
      + `<rect class="ln-box" x="${x + 0.5}" y="${y}" width="${L.leftW - 1}" height="${LN_NODE_H}" rx="4"/>`
      + `<rect class="viz-fill-${esc(c.info.key)}" x="${x + 5}" y="${y + 5}" width="8" height="${LN_NODE_H - 10}" rx="2"/>`
      + `<text class="ln-t1" x="${x + 20}" y="${y + 13}">${esc(label)}</text>`
      + `<text class="ln-t2" x="${x + 20}" y="${y + 25}">${esc(fitText(c.info.label, textW1(L.leftW), 6.4))}</text></g>`;
  });
  model.projects.forEach((p) => {
    const y = L.projPos.get(p.id);
    if (y === undefined) return;
    const k = L.order.indexOf(p.id);
    const x = L.rightX;
    const meta = [p.programme, p.year].filter(Boolean).join(" ");
    const n = model.edges.filter((e) => e.project === p.id).length;
    nodes += `<g class="ln-node ln-project" data-node="p${k}" data-id="${esc(p.id)}" tabindex="0" role="button" aria-pressed="false"`
      + ` aria-label="${lnAria([p.title, meta, `${n} CSR${n === 1 ? "" : "s"}`])}">`
      + `<rect class="ln-box" x="${x + 0.5}" y="${y}" width="${L.rightW - 1}" height="${LN_NODE_H}" rx="4"/>`
      + `<rect class="viz-fill-${programmeKey(p.programme)}" x="${x + 5}" y="${y + 5}" width="8" height="${LN_NODE_H - 10}" rx="2"/>`
      + `<text class="ln-t1" x="${x + 20}" y="${y + 13}">${esc(fitText(p.title, textW1(L.rightW)))}</text>`
      + `<text class="ln-t2" x="${x + 20}" y="${y + 25}">${esc(fitText(meta, textW1(L.rightW), 6.4))}</text></g>`;
    // Outcome: results status (marker shape and colour), deliverables and the latest impact score.
    const o = p.outcome;
    const ox = L.outX;
    const hasScore = Number.isFinite(p.score);
    const pill = hasScore ? 40 : 0;
    const res = o.label.length * CH <= textW1(L.outW) ? o.label : fitText(o.short, textW1(L.outW));
    const dl = `${fmtN(p.deliverables)} deliverable${p.deliverables === 1 ? "" : "s"}`;
    const dlFit = dl.length * 6.4 <= textW1(L.outW) - pill ? dl : fitText(`${fmtN(p.deliverables)} deliv.`, textW1(L.outW) - pill, 6.4);
    nodes += `<g class="ln-node ln-outcome" data-node="o${k}" data-id="${esc(p.id)}" tabindex="0" role="button" aria-pressed="false"`
      + ` aria-label="${lnAria([`Outcome of ${p.title}`, o.label, dl, hasScore ? `latest impact score ${p.score}/10` : "no impact analysis"])}">`
      + `<rect class="ln-box" x="${ox + 0.5}" y="${y}" width="${L.outW - 1}" height="${LN_NODE_H}" rx="4"/>`
      + outcomeMarker(o.key, ox + 10, y + LN_NODE_H / 2)
      + `<text class="ln-t1" x="${ox + 20}" y="${y + 13}">${esc(res)}</text>`
      + `<text class="ln-t2" x="${ox + 20}" y="${y + 25}">${esc(dlFit)}</text>`
      + (hasScore ? `<rect class="ln-pill" x="${ox + L.outW - 6 - 34}" y="${y + 16}" width="34" height="12" rx="6"/>`
        + `<text class="ln-pill-t" x="${ox + L.outW - 6 - 17}" y="${y + 25.5}" text-anchor="middle">${esc(`${p.score}/10`)}</text>` : "")
      + `</g>`;
  });
  const size = L.scroll ? `width="${L.W}"` : `width="100%"`;
  const svg = `<svg class="ln-svg${L.scroll ? " ln-scroll" : ""}" viewBox="0 0 ${L.W} ${L.height}" ${size} height="${L.height}" role="group"`
    + ` aria-label="${esc(`Network of CSR documents, recommendations, supporting projects and their outcome, ${countryName(cc)}`)}">`
    + `<g class="ln-bands">${band}</g><g class="ln-edges">${edges}</g><g class="ln-nodes">${nodes}</g></svg>`;
  return { svg, layout: L };
}

/** One row per link (CSR sub-part -> project), for the table view and the CSV export. */
function lineageRows(model) {
  const projects = new Map(model.projects.map((p) => [p.id, p]));
  const csrs = new Map(model.csrs.map((c) => [c.id, c]));
  return model.edges.flatMap((e) => e.parts.map((pt) => {
    const c = csrs.get(e.csr) || {};
    const p = projects.get(e.project) || {};
    return { doc_id: docIdOf(model.country, c.year), csr_id: e.csr, csr_year: c.year, csr_number: c.number, part_id: pt.part, project_id: e.project, project_title: p.title,
      programme: p.programme, project_year: p.year, level: pt.level, basis: pt.basis, rationale: pt.rationale,
      results: p.outcome?.label ?? "", deliverables: p.deliverables ?? 0, impact_score: Number.isFinite(p.score) ? p.score : "" };
  }));
}

const LN_CSV_COLS = ["doc_id", "csr_id", "csr_year", "csr_number", "part_id", "project_id", "project_title", "programme", "project_year", "level", "basis", "rationale",
  "results", "deliverables", "impact_score"];

function lineageTable(model) {
  const rows = lineageRows(model).map((r) => [
    td(hashLink("csrdoc", r.doc_id, `${model.country} ${r.csr_year}`), "nowrap"),
    td(hashLink("csr", r.csr_id, r.csr_id), "nowrap"), td(esc(r.part_id), "nowrap"),
    td(hashLink("project", r.project_id, r.project_title || r.project_id), "title-cell"), td(badge(r.level)), td(esc(r.rationale)),
    td(esc(r.results), "nowrap"), td(esc(fmtN(r.deliverables)), "num"),
    td(r.impact_score === "" ? `<span class="muted">—</span>` : hashLink("impact", r.project_id, `${r.impact_score}/10`), "nowrap"),
  ]);
  return table(["Document", "Recommendation", "Sub-part", "Project", "Level", "Rationale", "Results", "Deliverables", "Impact"], rows, "No link.");
}

function lineageLegend() {
  const line = (cls) => `<svg class="ln-key" width="26" height="10" aria-hidden="true"><line class="ln-line ${cls}" x1="1" y1="5" x2="25" y2="5"/></svg>`;
  const sw = (key, label) => `<li><span class="viz-key viz-key-${key}" aria-hidden="true"></span>${esc(label)}</li>`;
  const res = (key) => `<li><svg class="ln-key" width="14" height="14" aria-hidden="true">${outcomeMarker(key, 7, 7)}</svg>${esc(RESULT_LABELS[key])}</li>`;
  return `<div class="ln-legend">`
    + `<ul class="viz-legend"><li>${line("ln-tree-key")}Document → recommendation</li><li>${line("ln-direct-key")}Direct link</li><li>${line("ln-partial-key")}Partial link</li>`
    + `<li>${infoButton("links", "About the links")}</li>`
    + `<li><span class="ln-band-key" aria-hidden="true"></span>Initial request: JIRA export pending ${infoButton("request", "About the initial request")}</li></ul>`
    + `<ul class="viz-legend"><li class="muted">CSR assessment ${infoButton("assessments", "About the CSR assessment")}</li>${ASSESSMENT_SCALE.filter(([k]) => k !== "none").map(([, label, key]) => sw(key, label)).join("")}${sw("faint", "Not yet assessed")}</ul>`
    + `<ul class="viz-legend"><li class="muted">Programme:</li>${PROGRAMMES.map((p) => sw(programmeKey(p), p)).join("")}</ul>`
    + `<ul class="viz-legend"><li class="muted">Outcome ${infoButton("results", "About the results status")}</li>${["achieved", "expected", "none"].map(res).join("")}`
    + `<li><span class="ln-pill-key" aria-hidden="true">6/10</span>Latest impact score</li></ul></div>`;
}

function lineageSummary(s, { country = "", from = null, to = null } = {}) {
  const plural = (n, word) => `${fmtN(n)} ${word}${n === 1 ? "" : "s"}`;
  const where = [country ? countryName(country) : "", from && to ? (from === to ? String(from) : `${from}–${to}`) : ""].filter(Boolean).join(", ");
  const outcome = s.achieved !== undefined
    ? ` (${fmtN(s.achieved)} results achieved, ${fmtN(s.expected)} expected, ${fmtN(s.notStated)} not stated) · ${plural(s.deliverables, "deliverable")}`
      + ` · ${fmtN(s.withImpact)} with an impact analysis` : "";
  return `${where ? `${where}: ` : ""}${s.docs !== undefined ? `${plural(s.docs, "CSR document")} · ` : ""}`
    + `${plural(s.csrs, "recommendation")} (${fmtN(s.csrsLinked)} with at least one project) · `
    + `${plural(s.links, "CSR Recommendation")} (${fmtN(s.direct)} direct) from ${plural(s.subLinks ?? s.links, "sub-part link")} · `
    + `${plural(s.projects, "project")}${outcome}`;
}

/** Detail panel of the pinned node (links to the CSR or project page). */
function lineageDetail(model, key) {
  if (!key) return `<p class="muted small">Click a document, a recommendation, a project or an outcome to pin its chain and see the details here.</p>`;
  const projects = new Map(model.projects.map((p) => [p.id, p]));
  const csrs = new Map(model.csrs.map((c) => [c.id, c]));
  if (key[0] === "d") {
    const d = model.docs[Number(key.slice(1))];
    if (!d) return "";
    const items = d.recs.map((i) => {
      const c = model.csrs[i];
      const n = model.edges.filter((e) => e.csr === c.id).length;
      return `<li>${hashLink("csr", c.id, `CSR ${c.number}`)} <span class="muted small">${esc(c.title)} · ${esc(c.info.label)} · `
        + `${n} project${n === 1 ? "" : "s"}</span></li>`;
    }).join("");
    const hidden = d.total - d.recs.length;
    return `<h3>${esc(`${model.country} ${d.year} · CSR document`)}</h3>`
      + `<p class="small">${fmtN(d.total)} recommendation${d.total === 1 ? "" : "s"}${hidden > 0 ? ` (${fmtN(hidden)} without a project not shown)` : ""}</p>`
      + `<ul class="ln-list">${items}</ul><p>${hashLink("csrdoc", d.id, "Open the document")}</p>`;
  }
  if (key[0] === "c") {
    const c = model.csrs[Number(key.slice(1))];
    if (!c) return "";
    const items = model.edges.filter((e) => e.csr === c.id).map((e) => {
      const p = projects.get(e.project) || {};
      return `<li>${badge(e.level)} ${hashLink("project", e.project, p.title || e.project)} <span class="muted small">${esc([p.programme, p.year].filter(Boolean).join(" "))}</span>`
        + `${lineageReviewParts(e, p.title)}</li>`;
    }).join("");
    return `<h3>${esc(`${model.country} ${c.year} · CSR ${c.number}`)}</h3><p>${esc(c.title)}</p>`
      + `<p class="small"><span class="label">Assessment:</span> ${esc(c.info.label)}${c.assessmentYear ? ` (${esc(c.assessmentYear)})` : ""}</p>`
      + (items ? `<ul class="ln-list">${items}</ul>` : `<p class="empty">No project linked.</p>`)
      + `<p>${hashLink("csr", c.id, "Open the recommendation")} · ${hashLink("csrdoc", docIdOf(model.country, c.year), `${model.country} ${c.year} document`)}</p>`;
  }
  // Project or outcome (same row k): the CSRs it supports, then its outcome.
  const id = model.projects.length ? lineageState?.layout?.order?.[Number(key.slice(1))] : null;
  const p = projects.get(id);
  if (!p) return "";
  const items = model.edges.filter((e) => e.project === p.id).map((e) => {
    const c = csrs.get(e.csr) || {};
    return `<li>${badge(e.level)} ${hashLink("csr", e.csr, `${model.country} ${c.year} · CSR ${c.number}`)} <span class="muted small">${esc(c.title)}</span>`
      + `${lineageReviewParts(e, p.title)}</li>`;
  }).join("");
  const page = p.url && sourceInfo(p.url) ? ` · ${sourceLink(sourceInfo(p.url))}` : "";
  const pid = encodeURIComponent(p.id);
  const impact = p.impact
    ? `${esc(impactLine(p.impact))} · <a href="#impact/${pid}">Impact analysis</a>`
    : `<span class="muted">No impact analysis yet</span> · <a href="#impact/${pid}">Run one</a>`;
  const outcome = `<h4>Outcome</h4><dl class="facts ln-facts">`
    + `<dt>Results</dt><dd><svg class="ln-key" width="14" height="14" aria-hidden="true">${outcomeMarker(p.outcome.key, 7, 7)}</svg>${esc(p.outcome.label)}</dd>`
    + `<dt>Deliverables</dt><dd>${esc(fmtN(p.deliverables))}${p.deliverables ? ` · <a href="#project/${pid}">listed on the project page</a>` : ""}</dd>`
    + `<dt>Latest impact</dt><dd>${impact}</dd></dl>`;
  return `<h3>${esc(p.title)}</h3><p class="small muted">${esc([p.programme, p.year].filter(Boolean).join(" · "))}</p>`
    + `<ul class="ln-list">${items}</ul>${outcome}<p>${hashLink("project", p.id, "Open the project")}${page}</p>`;
}

/** Sub-part links of a recommendation–project pair with their review chip and button (detail panel). */
function lineageReviewParts(edge, title) {
  if (feature.reviews !== true || !edge.parts.length) return "";
  return `<ul class="rv-parts">${edge.parts.map((x) => `<li><code>${esc(x.part)}</code> <span class="muted small">${esc(x.level)}</span>`
    + `${reviewControls(edge.project, x.part, title)}</li>`).join("")}</ul>`;
}

/** Tooltip lines for a node or an edge (plain strings, inserted with textContent). */
function lineageTipLines(model, target) {
  const order = lineageState?.layout?.order || [];
  const short = (s, n) => (String(s ?? "").length > n ? `${String(s).slice(0, n - 1)}…` : String(s ?? ""));
  if (target.node?.[0] === "d") {
    const d = model.docs[Number(target.node.slice(1))];
    if (!d) return null;
    const ids = new Set(d.recs.map((i) => model.csrs[i].id));
    const linked = d.recs.filter((i) => model.csrs[i].linked).length;
    const projects = new Set(model.edges.filter((e) => ids.has(e.csr)).map((e) => e.project)).size;
    return [`${model.country} ${d.year} · CSR document`, `${d.total} recommendation${d.total === 1 ? "" : "s"}`,
      `${linked} with a project`, `${projects} project${projects === 1 ? "" : "s"}`];
  }
  if (target.node?.[0] === "c") {
    const c = model.csrs[Number(target.node.slice(1))];
    if (!c) return null;
    const n = model.edges.filter((e) => e.csr === c.id).length;
    return [`${model.country} ${c.year} · CSR ${c.number}`, short(c.title || c.text, 180),
      `Assessment: ${c.info.label}${c.assessmentYear ? ` (${c.assessmentYear})` : ""}`, `${n} project${n === 1 ? "" : "s"}`];
  }
  if (target.band) return ["Initial request", LN_REQUEST_NOTE, "The country's request is not public; the lines pass through unchanged."];
  if (target.node?.[0] === "p" || target.node?.[0] === "o") {
    const p = model.projects.find((x) => x.id === order[Number(target.node.slice(1))]);
    if (!p) return null;
    const n = model.edges.filter((e) => e.project === p.id).length;
    const outcome = [p.outcome.label, `${fmtN(p.deliverables)} deliverable${p.deliverables === 1 ? "" : "s"}`,
      p.impact ? `Latest impact: ${impactLine(p.impact)}` : "No impact analysis"];
    return target.node[0] === "o" ? [`Outcome · ${short(p.title, 120)}`, ...outcome]
      : [short(p.title, 140), [p.programme, p.year].filter(Boolean).join(" · "), ...outcome, `${n} CSR${n === 1 ? "" : "s"}`];
  }
  const e = model.edges[Number(target.edge)];
  if (!e) return null;
  const c = model.csrs[model.csrIndex.get(e.csr)] || {};
  const p = model.projects.find((x) => x.id === e.project) || {};
  const parts = e.parts.slice(0, 4).map((pt) => `${pt.part} (${pt.level}): ${short(pt.rationale, 160)}`);
  if (e.parts.length > 4) parts.push(`+${e.parts.length - 4} more sub-parts`);
  return [`${e.level === "direct" ? "Direct" : "Partial"} link`, `CSR ${c.number} (${c.year}) → ${short(p.title, 80)}`, ...parts];
}

/** Nodes, links and lines lit by a node: its whole chain (document -> recommendations -> projects -> outcomes). */
function lineageChain(model, order, key) {
  const nodes = new Set([key]);
  const edges = new Set();
  const trees = new Set();
  const outs = new Set(); // rows k whose project -> outcome line is lit
  const recs = new Set();
  if (key[0] === "d") for (const i of model.docs[Number(key.slice(1))]?.recs || []) recs.add(i);
  else if (key[0] === "c") recs.add(Number(key.slice(1)));
  const project = key[0] === "p" || key[0] === "o";
  const pkey = project ? `p${key.slice(1)}` : "";
  model.edges.forEach((e, i) => {
    const ci = model.csrIndex.get(e.csr);
    const k = order.indexOf(e.project);
    if (project ? `p${k}` === pkey : recs.has(ci)) {
      edges.add(i);
      nodes.add(`p${k}`);
      nodes.add(`o${k}`);
      outs.add(k);
      if (project) recs.add(ci);
    }
  });
  for (const i of recs) {
    nodes.add(`c${i}`);
    trees.add(i);
    if (model.csrs[i]?.doc !== undefined) nodes.add(`d${model.csrs[i].doc}`);
  }
  return { nodes, edges, trees, outs };
}

function lineageHighlight(key) {
  const root = $("ln-graph");
  const s = lineageState;
  if (!root || !s) return;
  root.querySelectorAll(".on").forEach((el) => el.classList.remove("on"));
  root.classList.toggle("has-focus", Boolean(key));
  if (!key) return;
  const chain = lineageChain(s.model, s.layout.order, key);
  const on = (sel) => root.querySelector(sel)?.classList.add("on");
  for (const n of chain.nodes) on(`[data-node="${n}"]`);
  for (const i of chain.edges) on(`[data-e="${i}"]`);
  for (const i of chain.trees) on(`[data-t="${i}"]`);
  for (const k of chain.outs) on(`[data-ol="${k}"]`);
}

function lineagePin(key) {
  const s = lineageState;
  if (!s) return;
  s.pinned = key && s.pinned !== key ? key : null;
  $("ln-graph")?.querySelectorAll(".ln-node").forEach((n) => n.setAttribute("aria-pressed", String(n.dataset.node === s.pinned)));
  lineageHighlight(s.pinned);
  const panel = $("ln-detail");
  if (panel) panel.innerHTML = lineageDetail(s.model, s.pinned);
}

function hideLineageTip() {
  const tip = $("ln-tip");
  if (tip) tip.hidden = true;
}

function showLineageTip(lines, x, y) {
  const tip = $("ln-tip");
  const root = $("ln-wrap");
  if (!tip || !root || !lines) return;
  const [first, ...rest] = lines;
  const strong = document.createElement("strong");
  strong.textContent = first;
  tip.replaceChildren(strong, ...rest.filter(Boolean).map((t) => {
    const d = document.createElement("div");
    d.textContent = t;
    return d;
  }));
  tip.hidden = false;
  const box = root.getBoundingClientRect();
  const tw = tip.offsetWidth;
  const th = tip.offsetHeight;
  let left = x - box.left + 12;
  if (left + tw > box.width) left = Math.max(0, x - box.left - tw - 12);
  let top = y - box.top - th - 10;
  if (top < 0) top = y - box.top + 18;
  tip.style.left = `${Math.round(left)}px`;
  tip.style.top = `${Math.round(top)}px`;
}

function paintLineageGraph() {
  const slot = $("ln-graph");
  const s = lineageState;
  if (!slot || !s) return;
  const { svg, layout } = lineageSvg(s.model, slot.clientWidth);
  s.layout = layout;
  s.width = $("ln-wrap")?.clientWidth || 0;
  if (!layout.readable) {
    slot.innerHTML = `<p class="muted small">The screen is too narrow for the network: the list of links is shown instead.</p>${lineageTable(s.model)}`;
    return;
  }
  const left = slot.scrollLeft; // keep the sideways scroll of the compact layout across redraws
  slot.innerHTML = svg;
  slot.scrollLeft = left;
  const keep = s.pinned; // re-apply the pinned selection on the new drawing
  s.pinned = null;
  if (keep && slot.querySelector(`[data-node="${keep}"]`)) lineagePin(keep);
}

function bindLineage() {
  const slot = $("ln-graph");
  if (!slot) return;
  const targetOf = (el) => {
    if (!(el instanceof Element)) return null;
    const node = el.closest("[data-node]");
    if (node) return { node: node.dataset.node, el: node };
    const edge = el.closest("[data-e]");
    if (edge) return { edge: edge.dataset.e, el: edge };
    const band = el.closest("[data-band]");
    return band ? { band: true, el: band } : null;
  };
  slot.addEventListener("pointermove", (ev) => {
    const s = lineageState;
    const t = targetOf(ev.target);
    if (!s || !t) {
      hideLineageTip();
      if (s && !s.pinned) lineageHighlight(null);
      return;
    }
    if (t.node && !s.pinned) lineageHighlight(t.node);
    showLineageTip(lineageTipLines(s.model, t), ev.clientX, ev.clientY);
  });
  slot.addEventListener("pointerleave", () => {
    hideLineageTip();
    if (lineageState && !lineageState.pinned) lineageHighlight(null);
  });
  slot.addEventListener("focusin", (ev) => {
    const s = lineageState;
    const t = targetOf(ev.target);
    if (!s || !t?.node) return;
    if (!s.pinned) lineageHighlight(t.node);
    const r = t.el.getBoundingClientRect();
    showLineageTip(lineageTipLines(s.model, t), r.left + Math.min(r.width / 2, 120), r.top);
  });
  slot.addEventListener("focusout", () => {
    hideLineageTip();
    if (lineageState && !lineageState.pinned) lineageHighlight(null);
  });
  slot.addEventListener("click", (ev) => {
    const t = targetOf(ev.target);
    if (t?.node) lineagePin(t.node);
  });
  slot.addEventListener("keydown", (ev) => {
    const t = targetOf(ev.target);
    if (t?.node && (ev.key === "Enter" || ev.key === " ")) {
      ev.preventDefault();
      lineagePin(t.node);
    }
  });
}

/** Nested query (existing foreign keys); if it fails, the same data from simple queries. */
async function fetchLineage(country, from, to) {
  const key = `${currentUid}|${country}|${from}|${to}`;
  if (lineageCache.has(key)) return lineageCache.get(key);
  let csrs;
  const nested = await sb.from("csrs")
    .select("csr_id,year,number,title,text,assessment,assessment_year,csr_parts(part_id,text,project_csr_links(level,rationale,basis,projects(project_id,title,programme,year,results,deliverables,url)))")
    .eq("country", country).gte("year", from).lte("year", to);
  if (!nested.error) {
    csrs = nested.data;
  } else {
    const base = await sb.from("csrs").select("csr_id,year,number,title,text,assessment,assessment_year").eq("country", country).gte("year", from).lte("year", to);
    if (base.error) return { error: base.error };
    const ids = base.data.map((c) => c.csr_id);
    const parts = ids.length ? await sb.from("csr_parts").select("part_id,csr_id,text").in("csr_id", ids) : { data: [] };
    if (parts.error) return { error: parts.error };
    const partIds = parts.data.map((p) => p.part_id);
    const links = partIds.length ? await sb.from("project_csr_links").select("part_id,project_id,level,rationale,basis").in("part_id", partIds) : { data: [] };
    if (links.error) return { error: links.error };
    const pids = [...new Set(links.data.map((l) => l.project_id))];
    const projs = pids.length ? await sb.from("projects").select("project_id,title,programme,year,results,deliverables,url").in("project_id", pids) : { data: [] };
    if (projs.error) return { error: projs.error };
    const pmap = new Map(projs.data.map((p) => [p.project_id, p]));
    csrs = base.data.map((c) => ({ ...c, csr_parts: parts.data.filter((p) => p.csr_id === c.csr_id).map((p) => ({
      ...p, project_csr_links: links.data.filter((l) => l.part_id === p.part_id).map((l) => ({ ...l, projects: pmap.get(l.project_id) || null })),
    })) }));
  }
  const pids = [...new Set(arr(csrs).flatMap((c) => arr(c.csr_parts).flatMap((p) => arr(p.project_csr_links).map((l) => l.projects?.project_id))).filter(Boolean))];
  // Latest impact analysis per project (the latest scored one when there is one).
  const scores = new Map();
  if (pids.length) {
    const res = await sb.from("impact_reports").select("project_id,score,level,confidence,created_at").in("project_id", pids)
      .order("created_at", { ascending: false }).order("id", { ascending: false });
    if (!res.error) {
      for (const r of res.data) {
        const had = scores.get(r.project_id);
        if (!had || (!Number.isFinite(had.score) && Number.isFinite(r.score))) scores.set(r.project_id, r);
      }
    }
  }
  const entry = { csrs, scores };
  lineageCache.set(key, entry);
  return entry;
}

/** Country with the most CSRs supported by a project (from the dashboard figures), for #lineage without a country. */
async function defaultLineageCountry() {
  if (!dashCache || dashCache.uid !== currentUid) {
    const res = await fetchDashboard().catch(() => ({ error: true }));
    if (res.error) return "";
    dashCache = { uid: currentUid, model: res.model, at: new Date() };
  }
  const best = [...dashCache.model.coverage].filter((c) => Object.hasOwn(COUNTRIES, c.key)).sort((a, b) => b.covered - a.covered)[0];
  return best?.key || "";
}

async function renderLineage(p, seq) {
  ovMounted?.stop();
  ovMounted = null;
  // Sub-tab: ?view=sankey|network|sunburst|country; links with a country or a selection (project, CSR, document pages,
  // dashboard, guide) open By country, a bare #lineage the Sankey.
  const kind = pick(p.get("view") || "", OV_VIEWS) || (p.has("country") || p.has("sel") ? "country" : "sankey");
  if (kind !== "country") return renderOverview(kind, p, seq);
  const raw = p.get("country") || "";
  let country = pick(raw, COUNTRIES);
  let from = yearOf(p, "from") ?? LN_FROM;
  let to = yearOf(p, "to") ?? LN_TO;
  if (from > to) [from, to] = [to, from];
  const unlinked = p.get("unlinked") === "1";
  const sel = p.get("sel") || "";
  if (!country && !raw) {
    const best = await defaultLineageCountry();
    if (stale(seq)) return;
    if (best) {
      const q = new URLSearchParams(p);
      q.set("country", best);
      return location.replace(`#lineage?${q.toString()}`);
    }
  }
  if (country) ovState.countryHash = location.hash;
  const tabs = lineageTabs("country");
  const form = filterForm("lineage", `<input type="hidden" name="view" value="country">${countriesSelect(country)}`
    + yearField("from", "From year", from) + yearField("to", "To year", to)
    + checkField("unlinked", "Show CSRs without a project", unlinked)).replace('href="#lineage"', 'href="#lineage?view=country"');
  const intro = `<p class="muted">From the European Semester to the results: CSR documents (one per year), their recommendations, `
    + `the country's initial request (pending the JIRA export), the projects that support them and their outcome. `
    + `Links assessed by AI from the project text: verify before use. ${infoButton("links", "About the links")}</p>`;
  if (!country) {
    view(`${heading("Lineage")}${tabs}${intro}${form}`
      + (raw ? `<p class="error" role="alert">Unknown country code: choose a country.</p>` : `<p class="empty">Choose a country.</p>`));
    bindFilters();
    return;
  }
  const res = await fetchLineage(country, from, to);
  if (stale(seq)) return;
  if (res.error) return showError(res.error, "Could not load the lineage network");
  const model = lineageModel(res.csrs, res.scores, { unlinked, country });
  const head = `<div class="title-row title-row-actions">${heading(`Lineage · ${countryName(country)}`)}<span class="title-actions">${briefingButton(country)}</span></div>${tabs}${intro}${form}`;
  if (!model.edges.length) {
    view(`${head}<p class="muted small">${esc(lineageSummary(model.summary, { country, from, to }))}</p>`
      + `<div class="card"><p class="empty">No project is linked to a CSR of ${esc(countryName(country))} between ${from} and ${to}.</p>`
      + `<p><a href="${esc(hrefFor("projects", { country }))}">See the projects of ${esc(countryName(country))}</a></p></div>`);
    bindFilters();
    lineageState = null;
    return;
  }
  lineageState = { model, country, pinned: null, layout: null, width: 0 };
  view(`${head}<p class="ln-summary"><strong>${esc(lineageSummary(model.summary, { country, from, to }))}</strong></p>`
    + `<div class="card ln-card">${lineageLegend()}`
    + `<div id="ln-wrap" class="ln-wrap">`
    + `<div id="ln-graph" class="ln-graph"></div><div id="ln-tip" class="viz-tip ln-tip" role="tooltip" hidden></div></div>`
    + `<div id="ln-detail" class="ln-detail" aria-live="polite"></div>`
    + `<details class="viz-table"><summary>Table of links</summary>`
    + `<div class="toolbar"><span class="muted small">One row per sub-part and project.</span>`
    + `<button type="button" class="btn btn-secondary" id="ln-csv">Download CSV</button></div>${lineageTable(model)}</details></div>`);
  bindFilters();
  bindLineage();
  $("ln-csv").addEventListener("click", () => saveCsv(`lineage-${country}-${from}-${to}.csv`, LN_CSV_COLS, lineageRows(model)));
  paintLineageGraph();
  $("ln-detail").innerHTML = lineageDetail(model, null);
  // Review chips of the detail panel: loaded once the graph is drawn, then the pinned panel is refreshed.
  const reviewKeys = model.edges.flatMap((e) => e.parts.map((x) => ({ project_id: e.project, part_id: x.part })));
  loadReviews(reviewKeys).then((ok) => {
    const panel = $("ln-detail");
    if (ok && lineageState?.model === model && lineageState.pinned && panel && !panel.contains(document.activeElement)) {
      panel.innerHTML = lineageDetail(model, lineageState.pinned);
    }
  });
  // Selection passed by the document, CSR or project page (#lineage?...&sel=doc:ID, csr:ID, project:ID or outcome:ID).
  const m = /^(doc|csr|project|outcome):(.+)$/.exec(sel);
  if (m) {
    const row = lineageState.layout?.order.indexOf(m[2]) ?? -1;
    const key = m[1] === "doc" ? (model.docIndex.has(m[2]) ? `d${model.docIndex.get(m[2])}` : null)
      : m[1] === "csr" ? (model.csrIndex.has(m[2]) ? `c${model.csrIndex.get(m[2])}` : null)
        : (row >= 0 ? `${m[1] === "outcome" ? "o" : "p"}${row}` : null);
    if (key) {
      lineagePin(key);
      const graph = $("ln-graph");
      graph.querySelector(`[data-node="${key}"]`)?.scrollIntoView({ block: "center" });
      // Compact layout (scrolls sideways): bring a project's row into view up to its outcome.
      const far = graph.querySelector(`[data-node="${/^[po]/.test(key) ? `o${key.slice(1)}` : key}"]`);
      if (far && graph.scrollWidth > graph.clientWidth) {
        const r = far.getBoundingClientRect();
        const box = graph.getBoundingClientRect();
        if (r.right > box.right) graph.scrollLeft += r.right - box.right + 4;
      }
    }
  }
}

/** #lineage link that opens the network on a document, a recommendation or a project (years widened to include it). */
function lineageHref(country, years, sel, unlinked = false) {
  const ys = years.filter((y) => Number.isFinite(y) && y > 1900);
  return hrefFor("lineage", { country, from: Math.min(LN_FROM, ...ys), to: Math.max(LN_TO, ...ys), unlinked: unlinked ? "1" : "", sel });
}

// ---------- lineage overviews: Sankey, network and sunburst of all countries (d3, loaded on first use) ----------

const OV_VIEWS = ["sankey", "network", "sunburst", "country"];
const OV_LABELS = { sankey: "Sankey", network: "Network", sunburst: "Sunburst", country: "By country" };
const OV_LEVELS = ["all", "direct", "partial"];
const OV_LIB = "./lineage-overview.js"; // imports d3@7.9.0 and d3-sankey@0.12.3 (jsDelivr ESM builds)
const OV_TABLES = [ // [table, columns, stable order for the pagination]
  ["project_csr_links", "project_id,part_id,level", ["project_id", "part_id"]],
  ["csr_parts", "part_id,csr_id", ["part_id"]],
  ["csrs", "csr_id,country,year,number,assessment", ["csr_id"]],
  ["projects", "project_id,title,programme,year,countries,policy_area,results,deliverables", ["project_id"]],
  ["impact_reports", "id,project_id,score,level,confidence,created_at", ["created_at", "id"]],
];
let ovCache = null; // { uid, model } for the session; cleared on sign-out and by the dashboard Refresh
let ovLoading = null; // { uid, promise } while the tables load
let ovLib = null; // the lineage-overview.js module once loaded
let ovMounted = null; // { kind, redraw, stop, hideTip, width } of the chart on screen
let lnTabFocus = null; // sub-tab clicked: it gets the focus back after the render
const ovState = { from: null, to: null, level: "all", cc: { sankey: "", network: "", sunburst: "" }, network: { all: true, q: "" }, countryHash: "", years: null };

/** Every row of a table, 1,000 per request at most (PostgREST cap): first page with the exact count, then the others. */
async function fetchAllRows(table, columns, order) {
  const build = (opts) => order.reduce((q, col) => q.order(col, { ascending: table !== "impact_reports" }), sb.from(table).select(columns, opts));
  const first = await build({ count: "exact" }).range(0, 999);
  if (first.error) throw first.error;
  const rows = [...first.data];
  const size = first.data.length;
  const total = first.count ?? size;
  if (!size || rows.length >= total) return rows;
  const pages = [];
  for (let off = size; off < total; off += size) pages.push(build().range(off, off + size - 1));
  for (const res of await Promise.all(pages)) {
    if (res.error) throw res.error;
    rows.push(...res.data);
  }
  return rows;
}

/**
 * Recommendation–project pairs over all countries: sub-part links grouped by recommendation (direct wins),
 * with the recommendation (country, year) and the project (programme, policy area, results, latest impact).
 */
function overviewModel({ links, parts, csrs, projects, impacts }) {
  const partCsr = new Map(parts.map((p) => [p.part_id, p.csr_id]));
  const csrById = new Map();
  for (const c of csrs) if (Object.hasOwn(COUNTRIES, c.country)) csrById.set(c.csr_id, c);
  const projById = new Map(projects.map((p) => [p.project_id, p]));
  const pairLevel = new Map();
  for (const l of links) {
    const cid = partCsr.get(l.part_id);
    if (!csrById.has(cid) || !projById.has(l.project_id)) continue;
    const k = `${cid}\u0000${l.project_id}`;
    if (pairLevel.get(k) !== "direct") pairLevel.set(k, l.level === "direct" ? "direct" : "partial");
  }
  const latest = new Map(); // latest analysis per project (the latest scored one when there is one)
  for (const r of impacts) {
    const had = latest.get(r.project_id);
    if (!had || (!Number.isFinite(had.score) && Number.isFinite(r.score))) latest.set(r.project_id, r);
  }
  const C = new Map();
  const P = new Map();
  const pairs = [];
  for (const [k, level] of pairLevel) {
    const [cid, pid] = k.split("\u0000");
    if (!C.has(cid)) {
      const c = csrById.get(cid);
      C.set(cid, { i: C.size, id: cid, cc: c.country, year: num(c.year), num: c.number });
    }
    if (!P.has(pid)) {
      const p = projById.get(pid);
      const res = resultsInfo(p.results).key;
      const imp = latest.get(pid);
      P.set(pid, { i: P.size, id: pid, title: p.title || pid, prog: p.programme || "Other", year: p.year ?? "", res: res === "achieved" ? 0 : res === "expected" ? 1 : 2,
        deliv: num(p.deliverables), pa: p.policy_area || "none", countries: arr(p.countries),
        imp: imp ? { score: Number.isFinite(imp.score) ? imp.score : null, level: imp.level ?? "", confidence: imp.confidence ?? "" } : null });
    }
    pairs.push({ c: C.get(cid), p: P.get(pid), d: level === "direct" });
  }
  const ys = [...C.values()].map((c) => c.year).filter((y) => y > 1900);
  const years = ys.length ? { min: Math.min(...ys), max: Math.max(...ys) } : { min: LN_FROM, max: LN_TO };
  return { pairs, years, csrCount: C.size, projectCount: P.size };
}

async function fetchOverview() {
  const uid = currentUid;
  if (ovCache?.uid === uid) return ovCache.model;
  if (ovLoading?.uid !== uid) {
    const promise = Promise.all(OV_TABLES.map(([t, cols, order]) => fetchAllRows(t, cols, order)))
      .then(([links, parts, csrs, projects, impacts]) => {
        const model = overviewModel({ links, parts, csrs, projects, impacts });
        if (currentUid === uid) ovCache = { uid, model };
        return model;
      })
      .finally(() => { if (ovLoading?.promise === promise) ovLoading = null; });
    ovLoading = { uid, promise };
  }
  return ovLoading.promise;
}

/** The d3 views module (and d3 itself); null if the CDN cannot be reached (a later call retries). */
async function loadOverviewLib() {
  if (ovLib) return ovLib;
  try {
    ovLib = await import(OV_LIB);
  } catch {
    ovLib = null;
  }
  return ovLib;
}

function ovYearsDefault() {
  return ovState.years || { min: null, max: null };
}

/** Address of a lineage sub-tab, with the filters (and the drilled country) it had last. */
function ovHref(kind) {
  if (kind === "country") return ovState.countryHash || hrefFor("lineage", { view: "country" });
  const y = ovYearsDefault();
  return hrefFor("lineage", { view: kind, from: ovState.from !== y.min ? ovState.from : "", to: ovState.to !== y.max ? ovState.to : "",
    level: ovState.level !== "all" ? ovState.level : "", cc: ovState.cc[kind] });
}

/** By country address for a country, with the overview years when they were narrowed. */
function ovCountryHref(cc) {
  const y = ovYearsDefault();
  const narrowed = ovState.from !== null && (ovState.from !== y.min || ovState.to !== y.max);
  return hrefFor("lineage", { view: "country", country: cc, from: narrowed ? ovState.from : "", to: narrowed ? ovState.to : "" });
}

function lineageTabs(current) {
  const a = (kind) => `<a class="btn btn-secondary seg" data-lnview="${kind}" href="${esc(ovHref(kind))}"`
    + `${kind === current ? ' aria-current="page"' : ""}>${OV_LABELS[kind]}</a>`;
  return `<nav class="seg-group ln-views" aria-label="Lineage views">${OV_VIEWS.map(a).join("")}</nav>`;
}

/** After the render: the clicked sub-tab keeps the focus. */
function focusLineageTab() {
  if (!lnTabFocus) return;
  $("view").querySelector(`[data-lnview="${lnTabFocus}"]`)?.focus();
  lnTabFocus = null;
}

function ovIntro(kind) {
  const how = {
    sankey: `Columns: country of the recommendation → policy area of the project → programme → stated outcome. Ribbon width is the number of `
      + `CSR Recommendations. Hover a ribbon or a block for its CSR Recommendations, distinct projects and share of direct links. Click a country `
      + `to split it by CSR year (the other countries merge into one block); click again or use <em>All countries</em> to return.`,
    network: `Dark discs are countries (size = CSR Recommendations), small dots are projects (fill = programme, ring = outcome). `
      + `All projects are shown; choose <em>Linked to several countries only</em> in <em>Projects</em> to keep only the multi-country projects, which bridge countries. Click a `
      + `country (or focus it and press Enter) to highlight its projects; click it again to clear. Drag nodes, scroll to zoom, search by title or country.`,
    sunburst: `Centre EU → ring 1 countries → ring 2 policy areas of the projects → ring 3 projects, coloured by outcome. Angle is proportional `
      + `to the CSR Recommendations. Click a segment to make it the centre, click the centre to zoom back out; hover for details.`,
  }[kind];
  return `<p class="muted">All countries at once: each <strong>CSR Recommendation</strong> counted is one recommendation linked to one project (direct if at least one `
    + `of its sub-part links is direct); the country is that of the recommendation. ${how} Links assessed by AI: verify before use. `
    + `${infoButton("overview", "About the lineage views")}</p>`;
}

function ovLegend(kind) {
  const sw = (color, label, cls = "") => `<li><span class="viz-key${cls}" style="background:${color}" aria-hidden="true"></span>${esc(label)}</li>`;
  const ring = (color, label) => `<li><span class="viz-key ov-ring" style="border-color:${color}" aria-hidden="true"></span>${esc(label)}</li>`;
  const present = new Set(ovCache?.model.pairs.map((x) => x.p.prog) || PROGRAMMES);
  const shown = PROGRAMMES.filter((p) => present.has(p));
  const progs = shown.map((p) => sw(ovLib.PROG_COLOR[p], p)).join("");
  const res = ovLib.RES.map((r, i) => sw(ovLib.RES_COLOR[i], r)).join("");
  if (kind === "sankey") {
    return `<div class="ln-legend"><ul class="viz-legend"><li class="muted">Ribbons take the colour of the block they flow into.</li></ul>`
      + `<ul class="viz-legend"><li class="muted">Programme:</li>${progs}</ul>`
      + `<ul class="viz-legend"><li class="muted">Outcome ${infoButton("results", "About the results status")}</li>${res}</ul></div>`;
  }
  if (kind === "sunburst") {
    const pa = ovLib.PA_KEYS.filter((k) => k !== "migration-management").map((k) => sw(ovLib.paColor(k), ovPaName(k))).join("");
    return `<div class="ln-legend"><ul class="viz-legend"><li class="muted">Ring 1:</li>${sw("var(--ov-c1)", "Country (alternating shades)")}</ul>`
      + `<ul class="viz-legend"><li class="muted">Ring 2, policy area:</li>${pa}</ul>`
      + `<ul class="viz-legend"><li class="muted">Ring 3, project outcome ${infoButton("results", "About the results status")}</li>${res}</ul></div>`;
  }
  return `<div class="ln-legend"><ul class="viz-legend"><li class="muted">Nodes:</li>${sw("var(--ov-cnode)", "Country", " ov-dot")}`
    + shown.map((p) => sw(ovLib.PROG_COLOR[p], `${p} project`, " ov-dot")).join("") + `</ul>`
    + `<ul class="viz-legend"><li class="muted">Ring = outcome ${infoButton("results", "About the results status")}</li>`
    + ovLib.RES.map((r, i) => ring(ovLib.RES_COLOR[i], r)).join("") + `</ul></div>`;
}

function ovPaName(k) {
  if (k === "none" || !k) return "Policy area not stated";
  if (k === "recovery-and-resilience-plans") return "Recovery and resilience plans";
  return POLICY_AREAS[k] || deslug(k);
}

function ovFiltered(model) {
  return model.pairs.filter((x) => x.c.year >= ovState.from && x.c.year <= ovState.to && (ovState.level === "all" || (ovState.level === "direct") === x.d));
}

function ovSummaryHtml(ps) {
  const proj = new Set(ps.map((x) => x.p.i)).size;
  const recs = new Set(ps.map((x) => x.c.i)).size;
  const cs = new Set(ps.map((x) => x.c.cc)).size;
  const dir = ps.filter((x) => x.d).length;
  const years = ovState.from === ovState.to ? String(ovState.from) : `${ovState.from}–${ovState.to}`;
  const n = (v, word) => `<strong>${fmtN(v)}</strong> ${word}${v === 1 ? "" : "s"}`;
  return `CSRs ${years}: ${n(ps.length, "CSR Recommendation")} · ${n(recs, "distinct recommendation")} · `
    + `${n(proj, "project")} · ${n(cs, "country").replace(/countrys$/, "countries")} · <strong>${share(dir, ps.length) || "–"}</strong> direct`;
}

function ovFiltersHtml(years) {
  const opts = (v) => Array.from({ length: years.max - years.min + 1 }, (_, i) => years.min + i)
    .map((y) => `<option${y === v ? " selected" : ""}>${y}</option>`).join("");
  const lv = OV_LEVELS.map((l) => `<button type="button" class="btn btn-secondary seg" data-level="${l}" aria-pressed="${l === ovState.level}">${deslug(l)}</button>`).join("");
  return `<div class="card filters ov-filters" role="group" aria-label="Filters of the Sankey, Network and Sunburst views">`
    + `<label class="field field-half"><span>CSR years from</span><select id="ov-from">${opts(ovState.from)}</select></label>`
    + `<label class="field field-half"><span>to</span><select id="ov-to">${opts(ovState.to)}</select></label>`
    + `<div class="field"><span><span id="ov-level-l">Link level</span> ${infoButton("linklevel", "About link levels")}</span><div class="seg-group ov-level" role="group" aria-labelledby="ov-level-l">${lv}</div></div></div>`;
}

/** Keeps the address in step with the view (no new history entry, no re-render) and the sub-tab links up to date. */
function ovSaveHash(kind) {
  if (!$("ov-chart")) return;
  try { history.replaceState(null, "", `${location.pathname}${location.search}${ovHref(kind)}`); } catch { /* ignore */ }
  document.querySelectorAll("#view [data-lnview]").forEach((a) => a.setAttribute("href", ovHref(a.dataset.lnview)));
}

function ovRedraw(model) {
  const ps = ovFiltered(model);
  $("ov-summary").innerHTML = ovSummaryHtml(ps);
  ovMounted.redraw(ps);
  ovMounted.width = $("ov-chart")?.clientWidth || 0;
}

async function renderOverview(kind, p, seq) {
  lineageState = null;
  const head = `${heading("Lineage")}${lineageTabs(kind)}`;
  view(`${head}<p class="loading" role="status">Loading the links of all countries…</p>`);
  const [lib, data] = await Promise.all([loadOverviewLib(), fetchOverview().then((model) => ({ model }), (error) => ({ error }))]);
  if (stale(seq)) return;
  if (!lib) {
    view(`${head}<div class="error" role="alert"><strong>Could not load the chart library.</strong> d3 is loaded from cdn.jsdelivr.net: `
      + `check your network connection (or a blocker) and reload the page. The <a href="${esc(ovHref("country"))}">By country</a> view works without it.</div>`);
    return;
  }
  if (data.error) return showError(data.error, "Could not load the lineage links");
  const model = data.model;
  const years = model.years;
  ovState.years = years;
  const clamp = (y, d) => (y === null ? d : Math.min(years.max, Math.max(years.min, y)));
  ovState.from = clamp(yearOf(p, "from"), years.min);
  ovState.to = clamp(yearOf(p, "to"), years.max);
  if (ovState.from > ovState.to) [ovState.from, ovState.to] = [ovState.to, ovState.from];
  ovState.level = pick(p.get("level"), OV_LEVELS) || "all";
  ovState.cc[kind] = pick(p.get("cc") || "", COUNTRIES);
  view(`${heading("Lineage")}${lineageTabs(kind)}${ovIntro(kind)}${ovFiltersHtml(years)}`
    + `<p class="ln-summary" id="ov-summary" aria-live="polite"></p>`
    + `<div class="card ov-card">${lib.toolbarHtml(kind)}<div id="ov-chart" class="ov-chart ov-${kind}"></div>${ovLegend(kind)}`
    + `<div id="ov-detail" class="ln-detail" aria-live="polite"></div></div>`
    + `<div id="ov-tip" class="viz-tip ov-tip" role="tooltip" hidden></div>`);
  const ctx = {
    esc, cname: countryName, paName: ovPaName,
    countries: Object.keys(COUNTRIES).sort((a, b) => countryName(a).localeCompare(countryName(b))),
    getCc: () => ovState.cc[kind],
    setCc: (cc) => { ovState.cc[kind] = pick(cc || "", COUNTRIES); ovSaveHash(kind); },
    network: ovState.network,
    countryHref: ovCountryHref,
    projectHref: (id) => `#project/${encodeURIComponent(id)}`,
    projectsHref: ({ country, area }) => hrefFor("projects", { country, area: area && Object.hasOwn(POLICY_AREAS, area) ? area : "" }),
  };
  ovMounted = { kind, ...lib.mount(kind, $("view"), ctx), width: 0 };
  const changed = () => { ovRedraw(model); ovSaveHash(kind); };
  $("ov-from").addEventListener("change", (e) => {
    ovState.from = Number(e.target.value);
    if (ovState.from > ovState.to) { ovState.to = ovState.from; $("ov-to").value = String(ovState.to); }
    changed();
  });
  $("ov-to").addEventListener("change", (e) => {
    ovState.to = Number(e.target.value);
    if (ovState.to < ovState.from) { ovState.from = ovState.to; $("ov-from").value = String(ovState.from); }
    changed();
  });
  $("view").querySelectorAll(".ov-level [data-level]").forEach((b) => b.addEventListener("click", () => {
    ovState.level = b.dataset.level;
    $("view").querySelectorAll(".ov-level [data-level]").forEach((x) => x.setAttribute("aria-pressed", String(x === b)));
    changed();
  }));
  ovRedraw(model);
  ovSaveHash(kind);
}

// ---------- country briefing (Word document: data of one country and period, optional AI summary) ----------

const BRIEFING_PAGE = 1000; // PostgREST max rows per request
const BRIEFING_IN = 100; // ids per in.() filter (URL length)
const BRIEFING_AI_TIMEOUT_MS = 90000; // one model call without tools (the function allows 60 s)
const BRIEFING_PROJECT_COLS = "project_id,title,programme,year,countries,policy_area,results,deliverables,url,detail_page";
const BRIEFING_STEPS = [["data", "Collecting data…"], ["ai", "Writing the summary (AI)…"], ["doc", "Building the document…"]];
let briefingRun = null; // { controller } while a briefing is being generated
let briefingOpener = null;

/** Every row of a filtered query, BRIEFING_PAGE at a time (build() must have a stable order). */
async function briefingRows(build, signal) {
  const rows = [];
  for (let from = 0; ; from += BRIEFING_PAGE) {
    const { data, error } = await build().range(from, from + BRIEFING_PAGE - 1).abortSignal(signal);
    if (error) throw error;
    rows.push(...arr(data));
    if (arr(data).length < BRIEFING_PAGE) return rows;
  }
}

/** Rows whose `column` is in `ids`, in chunks. */
async function briefingIn(table, columns, column, ids, signal, order = column) {
  const out = [];
  for (let i = 0; i < ids.length; i += BRIEFING_IN) {
    const chunk = ids.slice(i, i + BRIEFING_IN);
    out.push(...await briefingRows(() => sb.from(table).select(columns).in(column, chunk).order(order, { ascending: order !== "created_at" }).order("project_id"), signal));
  }
  return out;
}

/** Projects, CSR documents, recommendations (sub-parts, links), linked projects, impact analyses and anomalies of a country. */
async function fetchBriefingData(country, from, to, signal) {
  const csrCols = (doc) => `csr_id,country,year,number,title,text,policy_areas,assessment,assessment_year,${doc ? "doc_id," : ""}source_url,`
    + "csr_parts(part_id,policy_areas,project_csr_links(project_id,level,basis))";
  const csrQuery = (doc) => () => sb.from("csrs").select(csrCols(doc)).eq("country", country).gte("year", from).lte("year", to).order("csr_id");
  const [projects, csrs, docs, anomalies] = await Promise.all([
    briefingRows(() => sb.from("projects").select(BRIEFING_PROJECT_COLS).contains("countries", [country]).gte("year", from).lte("year", to).order("project_id"), signal),
    briefingRows(csrQuery(!csrDocsMissing), signal).catch((e) => {
      if (!docsTableMissing(e)) throw e;
      csrDocsMissing = true;
      return briefingRows(csrQuery(false), signal);
    }),
    csrDocsMissing ? Promise.resolve([]) : briefingRows(() => sb.from("csr_documents").select("doc_id,country,year,title,stage,n_recommendations,document_url")
      .eq("country", country).gte("year", from).lte("year", to).order("doc_id"), signal).catch((e) => {
      if (docsTableMissing(e)) return []; // documents rebuilt from the recommendations
      throw e;
    }),
    briefingRows(() => sb.from("anomalies").select("id,project_id,title,programme,year,countries,type,severity").ilike("countries", `%${country}%`).order("id"), signal),
  ]);
  const known = new Set(projects.map((p) => p.project_id));
  const linkedIds = [...new Set(csrs.flatMap((c) => arr(c.csr_parts).flatMap((p) => arr(p?.project_csr_links).map((l) => l?.project_id))).filter(Boolean))];
  const linked = await briefingIn("projects", BRIEFING_PROJECT_COLS, "project_id", linkedIds.filter((id) => !known.has(id)), signal);
  const ids = [...new Set([...known, ...linkedIds])];
  const impacts = await briefingIn("impact_reports", "id,project_id,score,level,confidence,created_at,verdict", "project_id", ids, signal, "created_at")
    .catch(() => []); // impact analyses are extra context: the briefing stands without them
  impacts.sort((a, b) => String(b.created_at).localeCompare(String(a.created_at)) || num(b.id) - num(a.id));
  // Past projects suggested by AI for the country's 2026 recommendations (left out when the table is missing or empty).
  const recent = csrs.filter((c) => num(c.year) >= RELATED_YEAR);
  const relatedRows = recent.length ? await loadRelated().catch(() => null) : null;
  if (signal.aborted) throw new DOMException("Cancelled", "AbortError");
  const related = relatedRows ? recent.flatMap((c) => relatedRows.byCsr.get(c.csr_id) || []) : [];
  return { projects, linked, docs, csrs, impacts, anomalies, related };
}

/** POST {service: "briefing", ...} to tsi-assistant; returns { key_messages, comments } or throws a readable Error. */
async function callBriefing(payload, signal) {
  const { data } = await sb.auth.getSession();
  const token = data?.session?.access_token;
  if (!token) throw new Error("Your session has expired: sign in again.");
  const controller = new AbortController();
  const abort = () => controller.abort();
  signal.addEventListener("abort", abort);
  const timer = setTimeout(abort, BRIEFING_AI_TIMEOUT_MS);
  let res;
  let body;
  try {
    res = await fetch(PROXY_URL, {
      method: "POST",
      headers: { Authorization: `Bearer ${token}`, apikey: SUPABASE_ANON_KEY, "Content-Type": "application/json" },
      body: JSON.stringify({ service: "briefing", ...payload }),
      signal: controller.signal,
    });
    body = await res.json().catch(() => ({}));
  } catch (e) {
    if (signal.aborted) throw e;
    throw new Error(e?.name === "AbortError" ? "The AI summary took too long." : "The AI service could not be reached.");
  } finally {
    clearTimeout(timer);
    signal.removeEventListener("abort", abort);
  }
  const b = body?.briefing;
  if (res.ok && b && Array.isArray(b.key_messages)) {
    const text = (v) => (typeof v === "string" ? v : "");
    return { key_messages: b.key_messages.filter((x) => typeof x === "string" && x.trim()).slice(0, 7),
      comments: Object.fromEntries(["figures", "csrs", "lineage", "results", "gaps"].map((k) => [k, text(b.comments?.[k])])) };
  }
  if (res.status === 413) throw new Error("The figures were too large for the AI summary.");
  throw new Error(proxyMessage(res.status, body).replace("analysis service", "AI service"));
}

function briefingDefaults() {
  const y = new Date().getFullYear();
  return { from: y - 5, to: y };
}

function briefingFormHtml(country) {
  const d = briefingDefaults();
  const countries = Object.fromEntries(Object.entries(COUNTRIES).sort((a, b) => a[1].localeCompare(b[1])).map(([code, name]) => [code, `${name} (${code})`]));
  const steps = BRIEFING_STEPS.map(([k, label]) => `<li data-step="${k}">${esc(label)}</li>`).join("");
  return `<form id="briefing-form" class="info-inner" novalidate>`
    + `<div class="info-head"><div class="title-row"><h2 id="briefing-title">Country briefing</h2>${infoButton("briefing", "About the country briefing")}</div></div>`
    + `<div class="info-body briefing-body">`
    + `<p class="muted" id="briefing-intro">A Word document on one country: key figures, country-specific recommendations, lineage, `
    + `results and impact, gaps and data quality, with an optional AI summary.</p>`
    + `<div class="briefing-fields">${selectField("country", "Country", countries, country, null)}`
    + `${yearField("from", "From year", d.from)}${yearField("to", "To year", d.to)}`
    + `${selectField("topic", "Policy area (optional)", POLICY_AREAS, "", "All policy areas")}</div>`
    + `${checkField("ai", "Include AI summary", true)}`
    + `<p class="muted small briefing-ai-note">One call to Claude (Anthropic), written only from the figures of the document: about 10 to 30 seconds, with a small cost. AI-generated parts are marked in the document.</p>`
    + `<ol class="briefing-steps" id="briefing-steps" hidden>${steps}</ol>`
    + `<p id="briefing-status" class="briefing-status" role="status" aria-live="polite"></p></div>`
    + `<div class="briefing-actions"><button type="button" class="btn btn-secondary" id="briefing-cancel">Close</button>`
    + `<button type="submit" class="btn" id="briefing-generate">Generate</button></div></form>`;
}

function briefingStatus(text, { error = false } = {}) {
  const el = $("briefing-status");
  if (!el) return;
  el.textContent = text;
  el.classList.toggle("note-error", error);
}

function briefingStep(key) {
  const list = $("briefing-steps");
  if (!list) return;
  list.hidden = key === null;
  let past = true;
  for (const li of list.querySelectorAll("li")) {
    const on = li.dataset.step === key;
    if (on) past = false;
    li.classList.toggle("is-done", past && key !== null);
    li.classList.toggle("is-active", on);
    if (on) li.setAttribute("aria-current", "step"); else li.removeAttribute("aria-current");
  }
}

function briefingBusy(busy) {
  const form = $("briefing-form");
  if (!form) return;
  for (const el of form.querySelectorAll("select, input")) el.disabled = busy;
  $("briefing-generate").disabled = busy;
  $("briefing-cancel").textContent = busy ? "Cancel" : "Close";
}

function openBriefing(country, opener) {
  const dialog = $("briefing-dialog");
  if (!dialog || briefingRun) {
    if (dialog && !dialog.open) dialog.showModal();
    return;
  }
  dialog.innerHTML = briefingFormHtml(pick(country, COUNTRIES) || "");
  briefingOpener = opener || null;
  $("briefing-form").addEventListener("submit", (e) => { e.preventDefault(); generateBriefing(); });
  $("briefing-cancel").addEventListener("click", () => (briefingRun ? cancelBriefing() : dialog.close()));
  if (typeof dialog.showModal === "function") dialog.showModal(); else dialog.setAttribute("open", "");
  $("briefing-form").elements.country.focus();
}

function cancelBriefing() {
  briefingRun?.controller.abort();
}

function bindBriefing() {
  const dialog = $("briefing-dialog");
  if (!dialog) return;
  document.addEventListener("click", (e) => {
    const button = e.target instanceof Element ? e.target.closest("[data-briefing]") : null;
    if (button) openBriefing(button.dataset.briefing, button);
  });
  dialog.addEventListener("cancel", (e) => { // Escape: cancels a running generation first
    if (briefingRun) { e.preventDefault(); cancelBriefing(); }
  });
  dialog.addEventListener("close", () => {
    if (briefingOpener?.isConnected) briefingOpener.focus();
    briefingOpener = null;
  });
  window.addEventListener("hashchange", () => { if (!briefingRun && dialog.open) dialog.close(); });
}

async function generateBriefing() {
  if (briefingRun) return;
  const form = $("briefing-form");
  const country = pick(form.elements.country.value, COUNTRIES);
  let from = parseInt(form.elements.from.value, 10);
  let to = parseInt(form.elements.to.value, 10);
  const topic = pick(form.elements.topic.value, POLICY_AREAS);
  const withAi = form.elements.ai.checked;
  if (!country) return briefingStatus("Choose a country.", { error: true });
  if (!(from >= 2010 && from <= 2100) || !(to >= 2010 && to <= 2100)) return briefingStatus("Enter the years of the period (for example 2021 and 2026).", { error: true });
  if (from > to) [from, to] = [to, from];
  const epoch = aiEpoch;
  const controller = new AbortController();
  const signal = controller.signal;
  briefingRun = { controller };
  briefingBusy(true);
  const aiStep = $("briefing-steps")?.querySelector('[data-step="ai"]');
  if (aiStep) aiStep.hidden = !withAi;
  let aiError = "";
  try {
    briefingStep("data");
    briefingStatus(`Collecting data for ${countryName(country)}, ${from}–${to}…`);
    const [raw, lib] = await Promise.all([
      fetchBriefingData(country, from, to, signal),
      loadWord().catch((e) => { throw new Error(`could not load the Word library (${e?.message || "network error"}). Check your connection and try again.`); }),
    ]);
    if (signal.aborted) throw new DOMException("Cancelled", "AbortError");
    const model = B.briefingModel(raw, { country, countryName: countryName(country), from, to, topic, topicLabel: areaLabel(topic), areaLabel: ovPaName, date: new Date() });
    let ai = null;
    if (withAi) {
      briefingStep("ai");
      briefingStatus("Writing the summary (AI)… about 10 to 30 seconds.");
      try {
        const facts = B.briefingFacts(model);
        if (JSON.stringify(facts).length > B.FACTS_MAX) throw new Error("The figures were too large for the AI summary.");
        ai = await callBriefing({ country, period: { from, to }, topic: topic ? areaLabel(topic) : null, facts }, signal);
      } catch (e) {
        if (signal.aborted) throw e;
        aiError = e?.message || String(e);
      }
    }
    briefingStep("doc");
    briefingStatus("Building the document…");
    const doc = lib.W.buildBriefingDocument(lib.D, { model, ai, aiError, aiSkipped: !withAi, email: currentEmail, date: model.date,
      labels: { anomaly: (t) => ANOMALY_TYPES[t] || t } });
    const blob = await lib.D.Packer.toBlob(doc);
    if (signal.aborted) throw new DOMException("Cancelled", "AbortError");
    if (epoch !== aiEpoch) return;
    saveBlob(B.briefingFileName(country, from, to, topic), blob);
    briefingStep(null);
    briefingStatus(withAi && !ai ? `Briefing downloaded without the AI summary: ${aiError}` : "Briefing downloaded.", { error: withAi && !ai });
  } catch (e) {
    if (epoch !== aiEpoch) return;
    briefingStep(null);
    if (signal.aborted) briefingStatus("Cancelled: no document was created.");
    else briefingStatus(`Could not create the briefing: ${e?.message || e}`, { error: true });
  } finally {
    if (briefingRun?.controller === controller) briefingRun = null;
    briefingBusy(false);
  }
}

function briefingButton(country) {
  return Object.hasOwn(COUNTRIES, country)
    ? `<button type="button" class="btn btn-secondary" data-briefing="${esc(country)}" aria-haspopup="dialog">Country briefing</button>` : "";
}

// ---------- usage tracking (public.page_views, migration 007; logic in tracking.js) ----------

const TRACK_SESSION_KEY = "tsi-track-session";
const TRACK_OFF_KEY = "tsi-track-off"; // "1" once the table is known to be missing (this browser session)
let trackToken = null; // access token of the signed-in user, kept for the keepalive request sent when the page is hidden
let trackOn = false; // a tester is signed in and the explorer is shown

function trackSessionId() {
  let id = storageGet(TRACK_SESSION_KEY);
  if (!id || !/^[0-9a-f]{8,64}$/.test(id)) {
    id = T.newSessionId();
    storageSet(TRACK_SESSION_KEY, id);
  }
  return id;
}

/** One INSERT per finished view, always with keepalive (the request survives the page being hidden or closed): the
 * supabase-js client cannot set keepalive. Errors are ignored; a missing table switches tracking off for the session. */
function trackSend(row) {
  if (!trackToken || storageGet(TRACK_OFF_KEY) === "1") return;
  fetch(`${SUPABASE_URL}/rest/v1/page_views`, {
    method: "POST", keepalive: true,
    headers: { apikey: SUPABASE_ANON_KEY, Authorization: `Bearer ${trackToken}`, "Content-Type": "application/json", Prefer: "return=minimal" },
    body: JSON.stringify(row),
  }).then(async (res) => {
    if (res.ok) return;
    let code = "";
    try { code = (await res.json())?.code || ""; } catch { /* no body */ }
    if (res.status === 404 || MISSING_CODES.has(code)) storageSet(TRACK_OFF_KEY, "1");
  }, () => { /* offline: the view is lost, never retried */ });
}

const pageTracker = T.createTracker({ send: trackSend, sessionId: trackSessionId });
const chatTracker = T.createTracker({ send: trackSend, sessionId: trackSessionId });

/** The view on screen (hash route and its parameters, never a typed text). */
function trackRoute() {
  if (!trackOn) return;
  const { view, detail } = T.routeView(location.hash);
  pageTracker.start(view, detail);
}

/** Signed out, another screen or another user: the current views are ended (sent with the last token). */
function trackStop() {
  trackOn = false;
  pageTracker.end();
  chatTracker.end();
}

function bindTracking() {
  document.addEventListener("visibilitychange", () => {
    if (document.visibilityState === "hidden") {
      pageTracker.hide();
      chatTracker.hide();
    } else if (trackOn) {
      pageTracker.show();
      chatTracker.show();
    }
  });
  window.addEventListener("pagehide", () => { pageTracker.hide(); chatTracker.hide(); });
  let last = 0;
  const active = () => {
    const t = Date.now();
    if (t - last < 1000) return;
    last = t;
    pageTracker.activity();
    chatTracker.activity();
  };
  for (const type of ["pointerdown", "pointermove", "keydown", "wheel", "touchstart", "scroll"]) {
    window.addEventListener(type, active, { passive: true, capture: true });
  }
}

// ---------- admin (#admin: users and roles through the tsi-admin function; #admin/usage: usage monitoring) ----------

const ADMIN_URL = SUPABASE_URL + "/functions/v1/tsi-admin";
const ADMIN_TIMEOUT_MS = 30000;
const ROLE_MISSING_CODES = new Set(["PGRST202", "42883"]); // my_role() not created yet (migration 007)
const LOG_COLS_DB = "email,view,detail,started_at,duration_s,session_id";
let roleCache = null; // { userId, role: "admin" | "user" | null }
let adminUsers = null; // { uid, users, fallback } of the Users view
let adminUsage = null; // { model, period } of the Usage view on screen (repaint on resize)
let adminDialogOpener = null;

const isAdmin = () => Boolean(roleCache && roleCache.userId === currentUid && roleCache.role === "admin");

/** The caller's role ('admin' | 'user' | null) from my_role(); a missing function means no administrator yet. */
async function fetchRole(session) {
  if (roleCache && roleCache.userId === session.user.id) return roleCache.role;
  const { data, error } = await sb.rpc("my_role");
  if (error && !ROLE_MISSING_CODES.has(error.code) && !/could not find|does not exist/i.test(error.message || "")) return null; // retried later
  roleCache = { userId: session.user.id, role: !error && (data === "admin" || data === "user") ? data : null };
  return roleCache.role;
}

function paintAdminTab() {
  const tab = $("admin-tab");
  if (tab) tab.hidden = !isAdmin();
}

/** Menu sous l'adresse e-mail (en-tete) : Admin (admins seulement), Change password, Sign out. */
function setupUserMenu() {
  const btn = $("user-menu-btn"), menu = $("user-menu"), wrap = $("user-menu-wrap");
  const items = () => [...menu.querySelectorAll(".user-menu-item")].filter((el) => !el.hidden);
  const close = (focusBtn = false) => {
    if (menu.hidden) return;
    menu.hidden = true;
    btn.setAttribute("aria-expanded", "false");
    if (focusBtn) btn.focus();
  };
  const open = (focusFirst = false) => {
    $("admin-tab").setAttribute("aria-current", location.hash.startsWith("#admin") ? "page" : "false");
    menu.hidden = false;
    btn.setAttribute("aria-expanded", "true");
    if (focusFirst) items()[0]?.focus();
  };
  btn.addEventListener("click", () => (menu.hidden ? open() : close()));
  btn.addEventListener("keydown", (e) => {
    if (e.key === "ArrowDown") { e.preventDefault(); open(true); }
  });
  menu.addEventListener("keydown", (e) => {
    const list = items(), i = list.indexOf(document.activeElement);
    if (e.key === "Escape") { e.preventDefault(); close(true); }
    else if (e.key === "ArrowDown") { e.preventDefault(); list[(i + 1) % list.length]?.focus(); }
    else if (e.key === "ArrowUp") { e.preventDefault(); list[(i - 1 + list.length) % list.length]?.focus(); }
    else if (e.key === "Tab") close();
  });
  menu.addEventListener("click", (e) => { if (e.target.closest(".user-menu-item")) close(); });
  document.addEventListener("click", (e) => { if (!wrap.contains(e.target)) close(); });
  addEventListener("hashchange", () => close());
}

/** POST {action, ...} to tsi-admin with the session token; returns the JSON answer or throws Error(message). */
async function callAdmin(action, payload = {}) {
  const { data } = await sb.auth.getSession();
  const token = data?.session?.access_token;
  if (!token) throw new Error("Your session has expired. Please sign in again.");
  const controller = new AbortController();
  const timer = setTimeout(() => controller.abort(), ADMIN_TIMEOUT_MS);
  let res;
  try {
    res = await fetch(ADMIN_URL, {
      method: "POST", signal: controller.signal,
      headers: { "Content-Type": "application/json", apikey: SUPABASE_ANON_KEY, Authorization: `Bearer ${token}` },
      body: JSON.stringify({ action, ...payload }),
    });
  } catch (e) {
    throw new Error(e?.name === "AbortError" ? "the administration service did not answer in time"
      : "the administration service is unreachable (not deployed yet, or network error)");
  } finally {
    clearTimeout(timer);
  }
  let body = null;
  try { body = await res.json(); } catch { /* no JSON */ }
  if (!res.ok) {
    const err = new Error(body?.error || body?.message || (res.status === 404 ? "the administration function is not deployed yet" : `HTTP ${res.status}`));
    err.status = res.status;
    throw err;
  }
  return body || {};
}

function adminTabs(current) {
  const a = (kind, href, label) => `<a class="btn btn-secondary seg" href="${href}"${kind === current ? ' aria-current="page"' : ""}>${label}</a>`;
  return `<nav class="seg-group" aria-label="Admin views">${a("users", "#admin", "Users")}${a("usage", "#admin/usage", "Usage monitoring")}</nav>`;
}

async function renderAdmin(id, p, seq) {
  if (!isAdmin()) {
    view(heading("Admin") + `<div class="card notice notice-error" role="alert"><h3>Not authorised</h3>`
      + `<p>The Admin page is reserved for administrators of the explorer.</p></div>`);
    return;
  }
  if (id === "usage") return renderAdminUsage(p, seq);
  return renderAdminUsers(seq);
}

// Users: invitation form, temporary password shown once, users table with role / reset / revoke.

function inviteFormHtml() {
  return `<section class="card adm-invite" aria-labelledby="adm-invite-h"><h3 id="adm-invite-h">Invite a user</h3>`
    + `<form id="adm-invite-form" class="adm-form" novalidate>`
    + `<label class="field field-grow"><span>E-mail address</span><input type="email" name="email" required autocomplete="off" spellcheck="false" placeholder="name@example.org"></label>`
    + `<label class="field"><span>Role</span><select name="role">${options(A.ROLES, "user", null)}</select></label>`
    + `<label class="check"><input type="checkbox" name="send_link" value="1"> Also e-mail a sign-in link</label>`
    + `<button type="submit" class="btn" id="adm-invite-submit">Invite</button></form>`
    + `<p class="muted small">A new address gets an account with a temporary password, to be replaced at the first sign-in. An address that`
    + ` already has an account in the shared sign-in service (for example another application) only receives access to the explorer: it`
    + ` keeps its password, or signs in with an e-mailed link. Administrators can also invite users and see the usage monitoring.</p>`
    + `<p id="adm-invite-status" class="briefing-status" role="status" aria-live="polite"></p></section>`
    + `<div id="adm-secret" class="adm-secret-slot"></div>`;
}

/** Temporary password panel: shown once, never stored; "Hide" or any navigation removes it. */
function showSecret(email, password, intro) {
  const slot = $("adm-secret");
  if (!slot) return;
  slot.innerHTML = `<section class="card adm-secret" aria-labelledby="adm-secret-h"><h3 id="adm-secret-h" tabindex="-1">Temporary password for ${esc(email)}</h3>`
    + `<p class="small">${esc(intro)}</p>`
    + `<div class="adm-secret-row"><code id="adm-secret-value" class="adm-secret-value">${esc(password)}</code>`
    + `<button type="button" class="btn btn-secondary" id="adm-copy">Copy</button>`
    + `<button type="button" class="btn btn-secondary" id="adm-secret-hide">Hide</button></div>`
    + `<p class="small adm-warning"><strong>Transmit it through a separate channel</strong> (not in the same message as the address);`
    + ` it is not stored and will not be shown again.</p><p id="adm-copy-status" class="muted small" role="status" aria-live="polite"></p></section>`;
  $("adm-copy").addEventListener("click", async () => {
    let ok = false;
    try { await navigator.clipboard.writeText(password); ok = true; } catch { /* clipboard refused */ }
    if (!ok) {
      const range = document.createRange();
      range.selectNodeContents($("adm-secret-value"));
      const sel = getSelection();
      sel.removeAllRanges();
      sel.addRange(range);
    }
    $("adm-copy-status").textContent = ok ? "Copied to the clipboard." : "Copy refused by the browser: the password is selected, press Ctrl+C.";
  });
  $("adm-secret-hide").addEventListener("click", () => { slot.innerHTML = ""; $("adm-invite-form")?.elements.email.focus(); });
  $("adm-secret-h").focus();
}

function usersTableHtml(state) {
  const users = A.sortUsers(state.users);
  const me = currentEmail.toLowerCase();
  const rows = users.map((u) => {
    const st = A.userStatus(u);
    const other = u.role === "admin" ? "user" : "admin";
    const lastAdmin = A.wouldRemoveLastAdmin(users, u.email, other);
    const self = u.email === me;
    const act = (action, label, disabled, why = "") => `<button type="button" class="btn btn-secondary adm-act" data-adm-action="${action}"`
      + ` data-email="${esc(u.email)}"${action === "role" ? ` data-role="${other}"` : ""}${disabled ? ` disabled title="${esc(why)}"` : ""}>${esc(label)}</button>`;
    const actions = state.fallback ? `<span class="muted small">—</span>`
      : `<div class="adm-actions">${act("role", other === "admin" ? "Make admin" : "Make user", lastAdmin, "The last administrator cannot be demoted")}`
        + act("reset", "Reset password", !u.account, "No account for this e-mail")
        + act("revoke", "Revoke", self, "You cannot revoke your own access") + `</div>`;
    return [
      td(`${esc(u.email)}${self ? ` <span class="muted small">(you)</span>` : ""}`, "adm-email"),
      td(`<span class="adm-role adm-role-${esc(u.role)}">${esc(A.ROLES[u.role] || u.role)}</span>`),
      td(state.fallback ? `<span class="muted">unknown</span>` : `<span class="adm-status adm-status-${st.key}">${esc(st.label)}</span>`),
      td(esc(u.last_sign_in_at ? fmtDateTime(u.last_sign_in_at) : "—"), "nowrap"),
      td(esc(u.invited_at ? fmtDate(u.invited_at) : "—") + (u.invited_by ? `<br><span class="muted small">by ${esc(u.invited_by)}</span>` : ""), "nowrap"),
      td(actions),
    ];
  });
  const note = state.fallback
    ? `<p class="notice-few" role="note">Account status and actions are unavailable: ${esc(state.fallback)}. The list comes from the testers table.</p>` : "";
  return note + table(["E-mail", "Role", "Status", "Last sign-in", "Invited", "Actions"], rows, "No user yet.");
}

function paintAdminUsers() {
  const box = $("adm-users");
  if (!box || !adminUsers) return;
  $("adm-users-count").textContent = `(${fmtN(adminUsers.users.length)})`;
  box.innerHTML = usersTableHtml(adminUsers);
  // Phones: each row becomes a card (CSS); the cells carry their column name.
  const heads = [...box.querySelectorAll("thead th")].map((th) => th.textContent);
  box.querySelectorAll("tbody td").forEach((cell) => { cell.dataset.label = heads[cell.cellIndex] || ""; });
}

/** Users from tsi-admin (accounts' status); if the function fails, the testers table (admin RLS) without status. */
async function loadAdminUsers() {
  try {
    const res = await callAdmin("list");
    return { uid: currentUid, users: arr(res.users), fallback: null };
  } catch (e) {
    const { data, error } = await sb.from("testers").select("email,role,invited_at,invited_by").order("email");
    if (error) throw e;
    return { uid: currentUid, users: arr(data).map((t) => ({ ...t, account: null })), fallback: e.message || String(e) };
  }
}

async function renderAdminUsers(seq) {
  view(heading("Admin") + adminTabs("users") + inviteFormHtml()
    + `<section class="adm-users" aria-labelledby="adm-users-h"><div class="toolbar"><h3 id="adm-users-h" class="adm-h">Users <span id="adm-users-count" class="muted"></span></h3>`
    + `<span class="toolbar-actions"><span id="adm-users-status" class="muted small" role="status" aria-live="polite"></span>`
    + `<button type="button" class="btn btn-secondary" id="adm-refresh">Refresh</button></span></div>`
    + `<div id="adm-users" class="adm-stack"><p class="loading" role="status">Loading…</p></div></section>`);
  $("adm-invite-form").addEventListener("submit", (e) => { e.preventDefault(); submitInvite(); });
  $("adm-refresh").addEventListener("click", () => refreshAdminUsers("Users reloaded."));
  $("adm-users").addEventListener("click", (e) => {
    const b = e.target instanceof Element ? e.target.closest("[data-adm-action]") : null;
    if (b && !b.disabled) adminAction(b);
  });
  if (!adminUsers || adminUsers.uid !== currentUid) {
    try {
      adminUsers = await loadAdminUsers();
    } catch (e) {
      if (stale(seq)) return;
      $("adm-users").innerHTML = `<div class="error" role="alert"><strong>Could not load the users.</strong> ${esc(e.message || e)}</div>`;
      return;
    }
    if (stale(seq)) return;
  }
  paintAdminUsers();
}

async function refreshAdminUsers(message = "") {
  const status = $("adm-users-status");
  if (status) status.textContent = "Loading…";
  try {
    adminUsers = await loadAdminUsers();
  } catch (e) {
    if (status) status.textContent = `Could not reload the users: ${e.message || e}`;
    return;
  }
  paintAdminUsers();
  if ($("adm-users-status")) $("adm-users-status").textContent = message;
}

async function submitInvite() {
  const form = $("adm-invite-form");
  const say = (text, error = false) => briefingStatusIn("adm-invite-status", text, error);
  const email = A.cleanEmail(form.elements.email.value);
  if (!email) {
    say("Enter a valid e-mail address.", true);
    form.elements.email.focus();
    return;
  }
  const role = form.elements.role.value === "admin" ? "admin" : "user";
  const sendLink = form.elements.send_link.checked;
  if (role === "admin" && !(await confirmAdmin({ title: "Invite an administrator?", confirm: "Invite as Admin",
    html: `<p><strong>${esc(email)}</strong> will be able to invite and revoke users, change roles, reset passwords and see the usage monitoring.</p>` }, $("adm-invite-submit")))) return;
  $("adm-invite-submit").disabled = true;
  say("Inviting…");
  let res;
  try {
    res = await callAdmin("invite", { email, role, send_link: sendLink });
  } catch (e) {
    if (form.isConnected) { $("adm-invite-submit").disabled = false; say(`Could not invite ${email}: ${e.message || e}.`, true); }
    return;
  }
  if (!form.isConnected) return;
  $("adm-invite-submit").disabled = false;
  form.reset();
  const link = res.link_sent === true ? " A sign-in link has been e-mailed." : res.link_sent === false ? " The sign-in link could not be sent (e-mail rate limit?): they can request one from the sign-in page." : "";
  if (res.existing) {
    say(`${email} already had an account in the shared sign-in service: access granted as ${A.ROLES[res.role] || res.role}. They keep their current password (or use “Send me a sign-in link”).${link}`);
  } else {
    say(`${email} invited as ${A.ROLES[res.role] || res.role}.${link}`);
    if (res.password) showSecret(email, res.password, "Give this password to the user: they must replace it with their own at the first sign-in.");
  }
  refreshAdminUsers();
}

async function adminAction(button) {
  const email = button.dataset.email;
  const action = button.dataset.admAction;
  const say = (text) => { if ($("adm-users-status")) $("adm-users-status").textContent = text; };
  let confirmText;
  if (action === "role") {
    const role = button.dataset.role;
    confirmText = { title: role === "admin" ? "Make this user an administrator?" : "Remove the administrator role?",
      confirm: role === "admin" ? "Make admin" : "Make user",
      html: role === "admin"
        ? `<p><strong>${esc(email)}</strong> will be able to invite and revoke users, change roles, reset passwords and see the usage monitoring.</p>`
        : `<p><strong>${esc(email)}</strong> keeps access to the explorer as a User, without the Admin page.</p>` };
  } else if (action === "reset") {
    confirmText = { title: "Reset the password?", confirm: "Reset password", danger: true,
      html: `<p>A new temporary password is generated for <strong>${esc(email)}</strong>; they must replace it at the next sign-in.</p>`
        + `<p class="adm-warning"><strong>Warning:</strong> this also changes their password for any other application using the same account`
        + ` (shared sign-in service). Prefer the e-mailed sign-in link when the user only forgot it.</p>` };
  } else {
    confirmText = { title: "Revoke the access?", confirm: "Revoke access", danger: true,
      html: `<p><strong>${esc(email)}</strong> will no longer be able to open the explorer. Their account is kept (it may be used by another`
        + ` application); you can invite them again later.</p>` };
  }
  if (!(await confirmAdmin(confirmText, button))) return;
  say("Saving…");
  try {
    if (action === "role") {
      await callAdmin("set_role", { email, role: button.dataset.role });
      if (email === currentEmail.toLowerCase()) roleCache = null; // demoted yourself: the Admin tab goes
      say(`${email} is now ${A.ROLES[button.dataset.role]}.`);
    } else if (action === "reset") {
      const res = await callAdmin("reset_password", { email });
      say(`New temporary password for ${email}.`);
      showSecret(email, res.password, "Give this password to the user: they must replace it with their own at the next sign-in.");
    } else {
      await callAdmin("revoke", { email });
      say(`Access revoked for ${email}.`);
    }
  } catch (e) {
    say(`Could not save: ${e.message || e}.`);
    return;
  }
  if (roleCache === null) return render();
  await refreshAdminUsers($("adm-users-status")?.textContent || "");
}

/** In-page confirmation (<dialog>): resolves true on Confirm, false on Cancel, Escape or a click on the backdrop. */
function confirmAdmin({ title, html, confirm, danger = false }, opener) {
  const dialog = $("admin-dialog");
  if (!dialog || typeof dialog.showModal !== "function") return Promise.resolve(window.confirm(title));
  adminDialogOpener = opener || null;
  dialog.innerHTML = `<form method="dialog" class="info-inner"><div class="info-head"><h2 id="admin-dialog-title">${esc(title)}</h2></div>`
    + `<div class="info-body" id="admin-dialog-body">${html}</div>`
    + `<div class="briefing-actions"><button type="submit" value="cancel" class="btn btn-secondary" id="admin-dialog-cancel">Cancel</button>`
    + `<button type="submit" value="ok" class="btn${danger ? " btn-danger" : ""}">${esc(confirm)}</button></div></form>`;
  dialog.returnValue = "";
  return new Promise((resolve) => {
    const onClick = (e) => { if (e.target === dialog) dialog.close("cancel"); };
    dialog.addEventListener("click", onClick);
    dialog.addEventListener("close", () => {
      dialog.removeEventListener("click", onClick);
      if (adminDialogOpener?.isConnected) adminDialogOpener.focus();
      adminDialogOpener = null;
      resolve(dialog.returnValue === "ok");
    }, { once: true });
    dialog.showModal();
    $("admin-dialog-cancel").focus();
  });
}

// Usage monitoring: KPIs, visits per day, time by section, per-user table, visit log (filters, pages, exports).

function fmtDateTime(value) {
  const d = new Date(value);
  return Number.isNaN(d.getTime()) ? "" : d.toLocaleString("en-GB", { day: "numeric", month: "short", year: "numeric", hour: "2-digit", minute: "2-digit" });
}

function usageFilters(p) {
  return { period: p.get("period") || "", from: p.get("from") || "", to: p.get("to") || "", user: p.get("user") || "", section: p.get("section") || "" };
}

function usageHref(f, extra = {}) {
  const v = { ...f, ...extra };
  return hrefFor("admin/usage", { period: v.period, from: v.period === "custom" ? v.from : "", to: v.period === "custom" ? v.to : "", user: v.user, section: v.section });
}

function usageLogQuery(period, f, opts = {}) {
  let q = sb.from("page_views").select(LOG_COLS_DB, opts)
    .gte("started_at", period.from.toISOString()).lt("started_at", period.to.toISOString());
  if (f.user) q = q.eq("email", f.user);
  if (f.section) q = q.eq("view", f.section);
  return q.order("started_at", { ascending: false }).order("id", { ascending: false });
}

function usageMissing(error) {
  return Boolean(error) && (MISSING_CODES.has(error.code) || ROLE_MISSING_CODES.has(error.code) || /schema cache|does not exist|could not find/i.test(error.message || ""));
}

const ADMIN_CHARTS = {
  days: (m, w) => vbarChart(m.perDay.map((d) => ({ label: new Date(`${d.day}T00:00:00`).toLocaleDateString("en-GB", { day: "numeric", month: "short" }),
    segs: [{ v: d.sessions, key: "s1", tip: { label: new Date(`${d.day}T00:00:00`).toLocaleDateString("en-GB", { weekday: "short", day: "numeric", month: "short", year: "numeric" }),
      value: `${fmtN(d.sessions)} session${d.sessions === 1 ? "" : "s"}`, share: `${fmtN(d.users)} user${d.users === 1 ? "" : "s"} · ${fmtN(d.views)} views · ${A.fmtDuration(d.total_s)}` } }] })),
  w, { title: "Sessions per day" }),
  sections: (m, w) => hbarChart(m.perView.map((v) => ({ label: A.viewLabel(v.view), display: A.fmtDuration(v.total_s),
    segs: [{ v: v.total_s, key: "s1", href: usageHref(adminUsage.filters, { section: v.view }),
      tip: { label: A.viewLabel(v.view), value: A.fmtDuration(v.total_s), share: `${fmtN(v.views)} views · ${fmtN(v.users)} user${v.users === 1 ? "" : "s"}` } }] })),
  w, { title: "Time spent by section" }),
};

function paintAdminCharts() {
  const root = $("adm-usage");
  if (!root || !adminUsage) return;
  adminUsage.width = root.clientWidth;
  root.querySelectorAll("[data-adm-chart]").forEach((slot) => { slot.innerHTML = ADMIN_CHARTS[slot.dataset.admChart](adminUsage.model, slot.clientWidth); });
}

function usageFormHtml(f, period, model, sections) {
  const periodField = selectField("period", "Period", A.PERIODS, period.key, null);
  const custom = period.key === "custom" || f.period === "custom";
  const dates = custom ? `<label class="field"><span>From</span><input type="date" name="from" value="${esc(A.isoDate(period.from))}"></label>`
    + `<label class="field"><span>To</span><input type="date" name="to" value="${esc(A.isoDate(new Date(period.to.getTime() - 86400000)))}"></label>` : "";
  const users = Object.fromEntries([...new Set([...model.perUser.map((u) => u.email), ...(f.user ? [f.user] : [])])].sort().map((e) => [e, e]));
  return filterForm("admin/usage", periodField + dates + selectField("user", "User", users, f.user, "All users")
    + selectField("section", "Section", sections, f.section, "All sections"));
}

async function renderAdminUsage(p, seq) {
  const f = usageFilters(p);
  const period = A.periodOf(f);
  const page = pageOf(p);
  const tz = Intl.DateTimeFormat().resolvedOptions().timeZone || "UTC";
  const [usage, log] = await Promise.all([
    sb.rpc("admin_usage", { p_from: period.from.toISOString(), p_to: period.to.toISOString(), p_tz: tz }),
    fetchPage((opts) => usageLogQuery(period, f, opts), page),
  ]);
  if (stale(seq)) return;
  const intro = heading("Admin") + adminTabs("usage");
  if (usageMissing(usage.error) || usageMissing(log.error)) {
    view(intro + `<div class="card notice"><h3>Usage monitoring is not available yet</h3><p>The database has no usage table yet: run`
      + ` <code>supabase/migrations/007_admin_monitoring.sql</code> in the Supabase SQL editor. Visits are recorded from then on.</p></div>`);
    return;
  }
  if (usage.error) return showError(usage.error, "Could not load the usage figures");
  if (log.redirectPage) return redirectToPage("admin/usage", p, log.redirectPage);
  if (log.error) return showError(log.error, "Could not load the visit log");
  let raw = usage.data;
  if (typeof raw === "string") { try { raw = JSON.parse(raw); } catch { raw = null; } }
  const model = A.usageModel(raw, period);
  adminUsage = { model, period, filters: f, width: 0 };
  const sections = Object.fromEntries([...new Set([...Object.keys(A.VIEW_LABELS), ...model.perView.map((v) => v.view), ...(f.section ? [f.section] : [])])]
    .map((k) => [k, A.viewLabel(k)]));
  const t = model.totals;
  const kpis = `<div class="kpis">`
    + tile("Active users", fmtN(t.users), "with at least one recorded view")
    + tile("Sessions", fmtN(t.sessions), "browser tabs with activity")
    + tile("Time spent", A.fmtDuration(t.total_s), `${fmtN(t.views)} page views`)
    + tile("Average session", t.sessions ? A.fmtDuration(t.avg_session_s) : "—", "visible, active time") + `</div>`;
  const userRows = model.perUser.map((u) => [
    td(`<a href="${esc(usageHref(f, { user: u.email }))}">${esc(u.email)}</a>`, "adm-email"),
    td(esc(u.last_seen ? fmtDateTime(u.last_seen) : "—"), "nowrap"), td(esc(fmtN(u.sessions)), "num"), td(esc(fmtN(u.views)), "num"),
    td(esc(A.fmtDuration(u.total_s)), "num"), td(esc(u.top_view ? A.viewLabel(u.top_view) : "—")),
  ]);
  const logRows = arr(log.data).map((r) => [
    td(esc(r.email), "adm-email"), td(esc(fmtDateTime(r.started_at)), "nowrap"), td(esc(A.viewLabel(r.view))),
    td(r.detail ? `<code class="adm-detail">${esc(r.detail)}</code>` : `<span class="muted">—</span>`), td(esc(A.fmtDuration(r.duration_s)), "num"),
  ]);
  const filtered = [f.user && `user ${f.user}`, f.section && `section ${A.viewLabel(f.section)}`].filter(Boolean).join(", ");
  view(intro + `<div id="adm-usage" class="dash adm-usage">`
    + `<div class="title-row"><p class="muted small">${period.key === "custom" ? "" : `${esc(period.label)} · `}${esc(A.isoDate(period.from))} to ${esc(A.isoDate(new Date(period.to.getTime() - 86400000)))} (${esc(tz)})`
    + `${f.user || f.section ? " · the user and section filters apply to the visit log; the figures and charts cover everyone" : ""}</p>`
    + infoButton("monitoring", "About the usage monitoring") + `</div>`
    + usageFormHtml(f, period, model, sections) + kpis
    + (t.views ? "" : `<p class="empty">No visit recorded in this period.</p>`)
    + `<div class="dash-grid">`
    + `<figure class="viz viz-wide"><figcaption><div class="title-row"><h4 class="viz-title">Sessions per day</h4>${infoButton("monitoring", "About the usage monitoring")}</div>`
    + `<p class="viz-sub">A session is one browser tab of one user; hover a bar for users, views and time.</p></figcaption>`
    + `<div class="viz-slot" data-adm-chart="days"></div>`
    + dataTable(["Day", "Users", "Sessions", "Views", "Time"], model.perDay.map((d) => [d.day, fmtN(d.users), fmtN(d.sessions), fmtN(d.views), A.fmtDuration(d.total_s)])) + `</figure>`
    + `<figure class="viz viz-wide"><figcaption><h4 class="viz-title">Time spent by section</h4><p class="viz-sub">Click a bar to filter the visit log.</p></figcaption>`
    + `<div class="viz-slot" data-adm-chart="sections"></div>`
    + dataTable(["Section", "Views", "Users", "Time"], model.perView.map((v) => [A.viewLabel(v.view), fmtN(v.views), fmtN(v.users), A.fmtDuration(v.total_s)])) + `</figure>`
    + `<section class="viz viz-wide adm-peruser" aria-labelledby="adm-peruser-h"><h4 class="viz-title" id="adm-peruser-h">Users</h4>`
    + `<p class="viz-sub">Click an e-mail to filter the visit log.</p>`
    + table(["User", "Last seen", "Sessions", "Views", "Time", "Most used section"], userRows, "No visit in this period.") + `</section></div>`
    + `<section class="dash-section adm-log" aria-labelledby="adm-log-h"><div class="toolbar"><h3 id="adm-log-h" class="adm-h">Visit log${filtered ? ` <span class="muted small">(${esc(filtered)})</span>` : ""}</h3>`
    + `<span class="toolbar-actions"><span id="adm-export-note" class="muted small" role="status" aria-live="polite"></span>`
    + `<button type="button" class="btn btn-secondary" id="adm-csv">Download CSV</button>`
    + `<button type="button" class="btn btn-secondary" id="adm-xlsx">Export to Excel</button></span></div>`
    + table(["User", "When", "Section", "Detail", "Duration"], logRows, "No visit matches these filters.")
    + (log.count ? pager("admin/usage", p, page, log.count) : "") + `</section>`
    + `<div id="dash-tip" class="viz-tip" role="tooltip" hidden></div></div>`);
  bindFilters();
  bindDashTips($("adm-usage"));
  paintAdminCharts();
  $("adm-csv").addEventListener("click", (e) => exportVisits(e.currentTarget, "csv", period, f));
  $("adm-xlsx").addEventListener("click", (e) => exportVisits(e.currentTarget, "xlsx", period, f));
}

/** The visit log with the current filters (up to EXPORT_MAX rows) as CSV or Excel (SheetJS, loaded on first use). */
async function exportVisits(button, kind, period, f) {
  if (button.dataset.busy) return;
  button.dataset.busy = "1";
  button.disabled = true;
  const note = $("adm-export-note");
  const say = (text, error = false) => { if (note?.isConnected) { note.textContent = text; note.classList.toggle("note-error", error); } };
  try {
    say("Collecting the visits…");
    const rows = [];
    for (let from = 0; from < A.EXPORT_MAX; from += CSV_PAGE) {
      const { data, error } = await usageLogQuery(period, f).range(from, Math.min(from + CSV_PAGE, A.EXPORT_MAX) - 1);
      if (error) throw error;
      rows.push(...arr(data));
      if (arr(data).length < CSV_PAGE) break;
    }
    const out = A.logExportRows(rows);
    if (kind === "csv") {
      saveCsv(A.logFileName("csv"), A.LOG_COLS, out);
    } else {
      say("Loading the Excel library…");
      const X = await loadSheetJs();
      const wb = X.utils.book_new();
      X.utils.book_append_sheet(wb, X.utils.json_to_sheet(out, { header: A.LOG_COLS }), "Visits");
      saveBlob(A.logFileName("xlsx"), new Blob([X.write(wb, { type: "array", bookType: "xlsx" })], { type: "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet" }));
    }
    say(`Downloaded ${fmtN(out.length)} visit${out.length === 1 ? "" : "s"}${out.length >= A.EXPORT_MAX ? ` (limited to the first ${fmtN(A.EXPORT_MAX)})` : ""}.`);
  } catch (e) {
    say(`Could not export: ${e?.message || e}`, true);
  } finally {
    delete button.dataset.busy;
    if (button.isConnected) button.disabled = false;
  }
}

// ---------- auth and routing ----------

function showScreen(name) {
  for (const id of ["config-error", "login", "change-password", "denied"]) $(id).hidden = id !== name;
  $("tabs").hidden = name !== "app";
  $("view").hidden = name !== "app";
  // The assistant exists only in the explorer (not on the sign-in, password or access-denied screens).
  if (name !== "app") {
    trackStop();
    $("assistant").hidden = true;
    document.body.classList.remove("assistant-modal");
  }
  $("assistant-launcher").hidden = name !== "app" || assistantOpen();
}

function showSession(session) {
  $("session").hidden = !session;
  currentEmail = session?.user?.email || "";
  $("session-email").textContent = currentEmail;
}

async function isTester(session) {
  if (testerCache && testerCache.userId === session.user.id) return testerCache;
  // getUser() asks the server: detects a session revoked server-side and gives fresh user metadata.
  const { data: userData, error: userError } = await sb.auth.getUser();
  if (userError || !userData?.user) return { expired: true };
  const { data, error } = await sb.rpc("is_tester");
  if (error) return { error };
  testerCache = { userId: session.user.id, ok: data === true,
                  mustChange: userData.user.user_metadata?.must_change_password === true };
  return testerCache;
}

// ---------- CSR progress ----------
// Implementation of the recommendations (Commission assessment, ECFIN database): supported by a project or not.

const PROGRESS_TABLES = [ // [table, columns, stable order for the pagination]
  ["csrs", "csr_id,country,year,assessment,assessment_year,policy_areas", ["csr_id"]],
  ["csr_parts", "part_id,csr_id", ["part_id"]],
  ["project_csr_links", "project_id,part_id,level", ["project_id", "part_id"]],
  ["projects", "project_id,year", ["project_id"]],
];
const PROGRESS_LEVELS = { all: "All links", direct: "Direct links only" };
let progressCache = null; // { uid, data } for the session; cleared on sign-out and by the dashboard Refresh
let progressView = null; // { model, width } of the page on screen (repaint on resize)

async function fetchProgressData() {
  const uid = currentUid;
  if (progressCache?.uid === uid) return progressCache.data;
  const [csrs, parts, links, projects] = await Promise.all(PROGRESS_TABLES.map(([t, cols, order]) => fetchAllRows(t, cols, order)));
  const data = { csrs, parts, links, projects };
  if (currentUid === uid) progressCache = { uid, data };
  return data;
}

const pctOf = (x) => (x === null ? "–" : `${Math.round(x * 100)}%`);
const signedPts = (g) => (g === null ? "–" : `${g > 0 ? "+" : ""}${g.toFixed(1)} pts`);

/** One 100 % bar per group, segments from full implementation to no progress. */
function progressSplitRows(m) {
  const scale = [...PG.PROGRESS_SCALE].reverse();
  return [["s", "Supported by a project"], ["u", "Not supported"]].map(([side, label]) => {
    const t = m[side];
    return { label, display: t.n ? `${pctOf(PG.okShare(t))} progressing` : "none",
      segs: scale.map(([, name, key, rank]) => ({ v: t.n ? (100 * t.counts[rank]) / t.n : 0, key,
        tip: { series: name, label, value: `${fmtN(t.counts[rank])} recommendations`, share: `${share(t.counts[rank], t.n)} of ${fmtN(t.n)}`, key } })) };
  });
}

/** Breakdown table: supported and other recommendations with their share progressing, and the gap. */
function progressTable(rows, head, labelHtml) {
  const small = (r) => r.s.n < PG.FEW || r.u.n < PG.FEW;
  const body = rows.map((r) => {
    const cls = small(r) ? "num muted" : "num";
    return [td(labelHtml(r.key), small(r) ? "muted" : ""), td(fmtN(r.s.n), cls), td(pctOf(PG.okShare(r.s)), cls),
      td(fmtN(r.u.n), cls), td(pctOf(PG.okShare(r.u)), cls), td(esc(signedPts(PG.gapPoints(r.s, r.u))), cls)];
  });
  return table([head, "Supported", "Progressing", "Not supported", "Progressing", "Gap"], body, "No assessed recommendation.");
}

async function renderProgress(p, seq) {
  let data;
  try { data = await fetchProgressData(); } catch (error) {
    if (stale(seq)) return;
    return showError(error, "Could not load the CSR assessments");
  }
  if (stale(seq)) return;
  const years = PG.assessedYears(data.csrs);
  const title = `<div class="title-row">${heading("CSR progress and project support")}${infoButton("progress", "About this comparison")}</div>`;
  if (!years.length) return view(`${title}<p class="empty">No assessed recommendation.</p>`);
  const clamp = (y) => Math.min(years[years.length - 1], Math.max(years[0], y));
  const level = p.get("level") === "direct" ? "direct" : "all";
  const before = p.get("before") === "1";
  // Default end: the last CSR year with a supported recommendation (later ones have no linked project yet).
  const lastYear = PG.lastSupportedYear(PG.progressModel(data, { level, before })) ?? years[years.length - 1];
  let from = clamp(yearOf(p, "from") ?? years[0]);
  let to = clamp(yearOf(p, "to") ?? lastYear);
  if (from > to) [from, to] = [to, from];
  const m = PG.progressModel(data, { from, to, level, before });
  const yearMap = Object.fromEntries(years.map((y) => [String(y), String(y)]));
  const form = filterForm("progress",
    selectField("from", "CSR years from", yearMap, String(from), null) + selectField("to", "to", yearMap, String(to), null)
    + selectField("level", "Project links", PROGRESS_LEVELS, level, null)
    + checkField("before", "Only projects started by the assessment year", before));
  const tiles = `<div class="kpis">`
    + tile("Supported recommendations", pctOf(PG.okShare(m.s)), `progressing · ${fmtN(m.s.n)} recommendations, ${fmtN(m.projects)} projects`, "progress")
    + tile("Not supported", pctOf(PG.okShare(m.u)), `progressing · ${fmtN(m.u.n)} recommendations`)
    + tile("Gap", signedPts(m.gap), `supported minus not supported · ${signedPts(m.sameYearGap)} within the same CSR year`) + `</div>`;
  const few = m.s.n < PG.FEW || m.u.n < PG.FEW ? `<p class="notice-few" role="note">Few recommendations on one side: the comparison is not meaningful.</p>` : "";
  const legendHtml = legend([...PG.PROGRESS_SCALE].reverse().map(([, label, key, rank]) => ({ key, label, n: m.s.counts[rank] + m.u.counts[rank] })));
  const split = figure("split", "Latest assessment of the recommendations", { wide: true, legendHtml,
    sub: `CSRs ${from === to ? from : `${from}–${to}`}, share of each group. Progressing = some progress, substantial progress or full implementation.`,
    tableHtml: dataTable(["Group", ...PG.PROGRESS_SCALE.map(([, l]) => l), "Total"],
      [["Supported", m.s], ["Not supported", m.u]].map(([l, t]) => [l, ...t.counts.map(fmtN), fmtN(t.n)])) });
  const countryLink = (cc) => `<a href="${esc(hrefFor("lineage", { view: "country", country: cc }))}">${esc(countryName(cc))}</a>`;
  const section = (id, h, sub, html) => `<section class="dash-section" aria-labelledby="${id}"><h3 id="${id}">${h}</h3><p class="muted small">${sub}</p>`
    + `${html}</section>`;
  const greyed = `Greyed rows have fewer than ${PG.FEW} recommendations on one side: too few to compare.`;
  view(`${title}<p class="muted">Does a recommendation progress more when a TSI or SRSP project supports it? The Commission assesses
      every year how far each country-specific recommendation has been implemented (ECFIN database, latest assessment). This page compares
      the recommendations with at least one linked project and those without. A correlation, not proof of impact: countries committed to
      a reform may also ask for more support. Links assessed by AI: verify before use.</p>`
    + form + `<div id="prog" class="dash">${tiles}${few}`
    + `<section class="dash-section"><div class="dash-grid">${split}</div></section>`
    + section("prog-year", "By CSR year", greyed, progressTable(m.byYear, "CSR year", (y) => esc(y)))
    + section("prog-area", "By policy area", `A recommendation counts in each of its policy areas (ECFIN classification). ${greyed}`,
      progressTable(m.byArea, "Policy area", (a) => esc(a)))
    + section("prog-country", "By country", `Click a country for its lineage. ${greyed}`, progressTable(m.byCountry, "Country", countryLink))
    + `<p class="muted small">${fmtN(m.unassessed)} recommendation${m.unassessed === 1 ? "" : "s"} of these years without a progress assessment `
    + `(marked not assessed, or not yet assessed) are left out.</p>`
    + `<div id="dash-tip" class="viz-tip" role="tooltip" hidden></div></div>`);
  bindFilters();
  progressView = { model: m, width: 0 };
  paintProgressCharts();
  bindDashTips($("prog"));
}

function paintProgressCharts() {
  const root = $("prog");
  if (!root || !progressView) return;
  progressView.width = root.clientWidth;
  const slot = root.querySelector('[data-chart="split"]');
  if (slot) slot.innerHTML = hbarChart(progressSplitRows(progressView.model), slot.clientWidth, { max: 100, title: "Latest assessment, supported and not supported recommendations" });
}

async function route(seq) {
  let { route: r, id, params: p } = params();
  let focusAssistant = false;
  if (r === "chat") {
    // Former Chat tab: the assistant now opens over the default view (old links keep working).
    history.replaceState(null, "", `${location.pathname}${location.search}#projects`);
    ({ route: r, id, params: p } = params());
    openAssistant({ focus: false });
    focusAssistant = true;
  }
  assistantContext = null;
  const viewKey = ["project", "csr", "csrdoc", "impact"].includes(r) && id ? `${r}/${id}`
    : (["dashboard", "lineage", "progress", "csrs", "anomalies", "impact", "guide", "reviews"].includes(r) ? r
      : r === "admin" ? (id === "usage" ? "admin/usage" : "admin") : "projects");
  setActiveTab(r);
  trackRoute();
  loading();
  try {
    if (r === "project" && id) await renderProject(id, seq);
    else if (r === "csr" && id) await renderCsr(id, seq);
    else if (r === "csrdoc" && id) await renderCsrDoc(id, seq);
    else if (r === "dashboard") await renderDashboard(seq);
    else if (r === "lineage") await renderLineage(p, seq);
    else if (r === "progress") await renderProgress(p, seq);
    else if (r === "csrs") await renderCsrs(p, seq);
    else if (r === "anomalies") await renderAnomalies(p, seq);
    else if (r === "reviews") await renderReviews(p, seq);
    else if (r === "guide") renderGuide(id);
    else if (r === "admin") await renderAdmin(id, p, seq);
    else if (r === "impact") await renderImpact(id, p, seq);
    else { setActiveTab("projects"); await renderProjects(p, seq); }
  } catch (e) {
    if (!stale(seq)) showError(e, "Unexpected error");
  }
  if (stale(seq)) return;
  restoreFocus(viewKey);
  if (r === "lineage") focusLineageTab();
  else lnTabFocus = null;
  focusGuideSection();
  paintAssistantContext();
  if (focusAssistant) $("chat-input")?.focus();
}

async function render() {
  const seq = ++renderSeq;
  if (!sb) return;
  const { data, error } = await sb.auth.getSession();
  if (stale(seq)) return;
  const session = data?.session;
  const uid = session?.user?.id ?? null;
  if (currentUid && uid !== currentUid) clearAiState();
  currentUid = uid;
  if (session?.access_token) trackToken = session.access_token;
  showSession(session);
  if (error || !session) {
    showScreen("login");
    const msg = error?.message || authError;
    if (msg) setLoginMessage(msg, true);
    return;
  }
  const tester = await isTester(session);
  if (stale(seq)) return;
  if (tester.expired) {
    testerCache = null;
    await sb.auth.signOut({ scope: "local" });
    authError = "Your session has expired. Please sign in again.";
    return render();
  }
  if (tester.error) {
    showScreen("app");
    $("tabs").hidden = true;
    return showError(tester.error, "Could not check access rights");
  }
  if (!tester.ok) return showScreen("denied");
  // Temporary password set by the administrator (user_metadata.must_change_password): change it before anything else.
  const mustChange = tester.mustChange === true;
  if (mustChange || wantsPasswordChange) {
    $("change-password-intro").textContent = mustChange
      ? "For your first sign-in, replace the temporary password with your own (at least 12 characters)."
      : "Choose a new password (at least 12 characters).";
    $("change-password-cancel-row").hidden = mustChange;
    showScreen("change-password");
    $("change-password-title").focus();
    return;
  }
  await fetchRole(session);
  if (stale(seq)) return;
  showScreen("app");
  paintAdminTab();
  trackOn = true;
  if (storageGet(ASSISTANT_KEY) === "1" && !assistantOpen()) openAssistant({ focus: false }); // still open in this session
  await route(seq);
}

let wantsPasswordChange = false;

async function changePassword(event) {
  event.preventDefault();
  const password = $("new-password").value;
  const confirm = $("new-password-confirm").value;
  const msg = (text, isError) => {
    const el = $("change-password-message");
    el.textContent = text;
    el.className = `message ${isError ? "err" : "ok"}`;
  };
  if (password.length < 12) return msg("The password must have at least 12 characters.", true);
  if (password !== confirm) return msg("The two passwords do not match.", true);
  const button = event.submitter || $("change-password-form").querySelector("button[type=submit]");
  button.disabled = true;
  msg("Saving…", false);
  const { error } = await sb.auth.updateUser({ password, data: { must_change_password: false } });
  button.disabled = false;
  if (error && (error.name === "AuthSessionMissingError" || /session (missing|not found|expired)|refresh token/i.test(error.message || ""))) {
    // Session stored in the browser but no longer valid on the server: sign in again.
    msg("", false);
    wantsPasswordChange = false;
    testerCache = null;
    await sb.auth.signOut({ scope: "local" });
    authError = "Your session has expired. Please sign in again.";
    return render();
  }
  if (error) return msg(`Could not change the password: ${error.message}`, true);
  $("new-password").value = "";
  $("new-password-confirm").value = "";
  msg("", false);
  wantsPasswordChange = false;
  testerCache = null; // re-read the user metadata (must_change_password is now false)
  await sb.auth.refreshSession();
  render();
}

function setLoginMessage(text, isError) {
  const el = $("login-message");
  el.textContent = text;
  el.className = `message ${isError ? "err" : "ok"}`;
}

async function signInWithPassword(event) {
  event.preventDefault();
  const email = $("login-email").value.trim().toLowerCase();
  const password = $("login-password").value;
  if (!email) return;
  if (!password) {
    setLoginMessage("Enter your password, or use the sign-in link below.", true);
    $("login-password").focus();
    return;
  }
  const button = event.submitter || $("login-form").querySelector("button[type=submit]");
  button.disabled = true;
  setLoginMessage("Signing in…", false);
  const { error } = await sb.auth.signInWithPassword({ email, password });
  button.disabled = false;
  if (error) {
    // Same message for unknown e-mail and wrong password (no account enumeration).
    setLoginMessage(/invalid login credentials/i.test(error.message || "")
      ? "Wrong e-mail or password." : `Could not sign in: ${error.message}`, true);
    return;
  }
  $("login-password").value = "";
  authError = null;
  setLoginMessage("", false); // onAuthStateChange (new user id) renders the explorer
}

async function sendMagicLink(event) {
  event.preventDefault();
  const email = $("login-email").value.trim().toLowerCase();
  if (!email) {
    setLoginMessage("Enter your e-mail address first.", true);
    $("login-email").focus();
    return;
  }
  const button = event.currentTarget || $("login-magic");
  button.disabled = true;
  setLoginMessage("Sending…", false);
  // Invitation only: no account is created here (testers are invited from the Supabase dashboard).
  const { error } = await sb.auth.signInWithOtp({
    email, options: { shouldCreateUser: false, emailRedirectTo: new URL("./", location.href).href },
  });
  button.disabled = false;
  if (error && /signups? not allowed|user not found|not invited/i.test(error.message || "")) {
    setLoginMessage("This e-mail is not invited. Ask the administrator for an invitation.", true);
  } else if (error) setLoginMessage(`Could not send the link: ${error.message}`, true);
  else setLoginMessage("Check your mailbox: a sign-in link has been sent.", false);
}

/** A magic-link error comes back in the hash (#error=...&error_description=...): keep it, restore a route. */
function captureAuthError() {
  const raw = location.hash.replace(/^#/, "");
  if (!/(^|&)error(_description)?=/.test(raw)) return;
  const p = new URLSearchParams(raw);
  authError = `Sign-in failed: ${p.get("error_description") || p.get("error")}`;
  history.replaceState(null, "", location.pathname + location.search + "#projects");
}

async function init() {
  if (!SUPABASE_URL || !SUPABASE_ANON_KEY || SUPABASE_ANON_KEY === "REMPLACER_PAR_LA_CLE_ANON") {
    showScreen("config-error");
    return;
  }
  captureAuthError();
  try {
    const { createClient } = await import(SUPABASE_JS);
    sb = createClient(SUPABASE_URL, SUPABASE_ANON_KEY);
  } catch (e) {
    showScreen("config-error");
    $("config-error").innerHTML = `<h2>Could not load the Supabase client</h2><p>${esc(e.message || e)}. Check your network connection and reload the page.</p>`;
    return;
  }
  $("login-form").addEventListener("submit", signInWithPassword);
  $("login-magic").addEventListener("click", sendMagicLink);
  $("change-password-form").addEventListener("submit", changePassword);
  $("change-password-open").addEventListener("click", () => { wantsPasswordChange = true; render(); });
  setupUserMenu();
  $("change-password-cancel").addEventListener("click", () => { wantsPasswordChange = false; render(); });
  $("logout").addEventListener("click", async () => {
    testerCache = null;
    roleCache = null;
    adminUsers = null;
    trackStop(); // ends the current views while the token is still known
    wantsPasswordChange = false;
    await sb.auth.signOut({ scope: "local" }); // SIGNED_OUT re-renders (user id changes)
    history.replaceState(null, "", location.pathname + location.search + "#projects");
  });
  // One Escape handler for the whole page: hides the chart tooltips and releases a pinned lineage node.
  document.addEventListener("keydown", (e) => {
    if (e.key !== "Escape" || $("info-dialog")?.open || $("briefing-dialog")?.open || $("review-dialog")?.open || $("admin-dialog")?.open) return;
    const tip = $("dash-tip");
    if (tip) tip.hidden = true;
    hideLineageTip();
    ovMounted?.hideTip();
    if (lineageState?.pinned && $("ln-graph")) lineagePin(lineageState.pinned);
  });
  // A lineage sub-tab gets the focus back after its render (the view keeps the same route).
  document.addEventListener("click", (e) => {
    const a = e.target instanceof Element ? e.target.closest("#view [data-lnview]") : null;
    if (a) lnTabFocus = a.getAttribute("href") === location.hash ? null : a.dataset.lnview;
  });
  bindInfoDialog();
  bindReviewDialog();
  bindBriefing();
  bindAssistant();
  bindTracking();
  let resizeTimer = 0;
  window.addEventListener("resize", () => {
    clearTimeout(resizeTimer);
    resizeTimer = setTimeout(() => {
      syncAssistantMode();
      const root = $("dash");
      if (root && root.clientWidth !== dashWidth) paintDashCharts();
      const prog = $("prog");
      if (prog && progressView && prog.clientWidth !== progressView.width) paintProgressCharts();
      const adm = $("adm-usage");
      if (adm && adminUsage && adm.clientWidth !== adminUsage.width) paintAdminCharts();
      const wrap = $("ln-wrap");
      if (wrap && lineageState && wrap.clientWidth !== lineageState.width) paintLineageGraph();
      const chart = $("ov-chart");
      if (chart && ovMounted && ovCache && Math.abs(chart.clientWidth - ovMounted.width) >= 24) ovRedraw(ovCache.model);
    }, 150);
  });
  window.addEventListener("hashchange", () => {
    if (/^#?access_token=/.test(location.hash)) return; // handled by supabase-js
    render();
  });
  // Re-render only when the signed-in user changes (or is updated): SIGNED_IN is also emitted when the tab
  // regains focus, and must not rebuild the view. The callback must not await Supabase calls (auth lock).
  sb.auth.onAuthStateChange((event, session) => {
    trackToken = session?.access_token || null; // refreshed tokens too (keepalive requests read it synchronously)
    if (event === "INITIAL_SESSION") return; // covered by the first render() below
    const uid = session?.user?.id ?? null;
    if (uid === currentUid && event !== "USER_UPDATED") return;
    if (currentUid && uid !== currentUid) clearAiState(); // sign-out or another user: drop chat and reports
    currentUid = uid;
    testerCache = null;
    roleCache = null;
    adminUsers = null;
    setTimeout(render, 0);
  });
  await render();
}

init();
