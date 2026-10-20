// TSI x CSR Explorer: static page over Supabase (RLS: signed-in testers only).
// Every database string goes through esc() before being injected; only https:// URLs become links.
// Chat and Impact answers (Markdown) are inserted only after marked + DOMPurify; without them, as escaped text.
import { SUPABASE_URL, SUPABASE_ANON_KEY, PROXY_URL } from "./config.js";

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
const CSR_YEARS = Array.from({ length: 10 }, (_, i) => 2025 - i);

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
  const tab = { project: "projects", csr: "csrs" }[route] || route;
  document.querySelectorAll("#tabs a").forEach((a) => {
    if (a.dataset.tab === tab) a.setAttribute("aria-current", "page"); else a.removeAttribute("aria-current");
  });
}

// ---------- projects ----------

function projectsQuery(f, opts) {
  let q = sb.from("projects").select("project_id,title,programme,year,countries,policy_area,detail_page,csrs", opts)
    .order("year", { ascending: false }).order("title").order("project_id");
  if (f.q) q = q.ilike("title", likePattern(f.q));
  if (f.country) q = q.contains("countries", [f.country]);
  if (f.programme) q = q.eq("programme", f.programme);
  if (f.from) q = q.gte("year", f.from);
  if (f.to) q = q.lte("year", f.to);
  if (f.area) q = q.eq("policy_area", f.area);
  if (f.linked) q = q.not("csrs", "eq", "{}");
  if (f.detail) q = q.eq("detail_page", true);
  return q;
}

async function renderProjects(p, seq) {
  const page = pageOf(p);
  const f = {
    q: p.get("q") || "", country: pick(p.get("country"), COUNTRIES), programme: pick(p.get("programme"), PROGRAMMES),
    from: yearOf(p, "from"), to: yearOf(p, "to"), area: pick(p.get("area"), POLICY_AREAS),
    linked: p.get("linked") === "1", detail: p.get("detail") === "1",
  };
  const res = await fetchPage((opts) => projectsQuery(f, opts), page);
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
    + checkField("linked", "Linked to a CSR", f.linked)
    + checkField("detail", "Has a detail page", f.detail));
  const rows = data.map((r) => [
    td(hashLink("project", r.project_id, r.title), "title-cell"),
    td(esc(r.programme)), td(esc(r.year), "nowrap"), td(esc(countriesText(r.countries))),
    td(esc(areaLabel(r.policy_area))), td(esc((r.csrs || []).length), "num"),
  ]);
  view(`${heading("Projects")}${form}${pager("projects", p, page, count)}`
    + table(["Title", "Programme", "Year", "Countries", "Policy area", "CSRs"], rows, "No project matches these filters.")
    + (data.length ? pager("projects", p, page, count) : ""));
  bindFilters();
}

function linkItem(link) {
  const part = link.csr_parts || {};
  const csr = part.csrs || {};
  const partLabel = part.csr_id ? hashLink("csr", part.csr_id, link.part_id) : esc(link.part_id);
  const basis = link.basis === "title_only" ? `<span class="basis-note">based on the project title only</span>` : "";
  return `<li class="link-item"><div class="link-head">${badge(link.level)}<span class="part-id">${partLabel}</span>`
    + `<span>${esc(csr.title)}${csr.year ? ` (${esc(csr.year)})` : ""}</span>${basis}</div>`
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
  if (stale(seq)) return;
  if (proj.error) return showError(proj.error, proj.error.code === "PGRST116" ? "Project not found" : "Could not load the project");
  if (links.error) return showError(links.error, "Could not load the CSR links");

  const r = proj.data;
  impactProject = r;
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
  const linksHtml = sorted.length ? `<ul class="link-list">${sorted.map(linkItem).join("")}</ul>` : `<p class="empty">No CSR link assessed.</p>`;
  view(`<a class="back" href="#projects">← Projects</a><div class="card"><h2 class="detail-title" tabindex="-1">${esc(r.title)}</h2>`
    + `<dl class="facts">${facts}</dl>${page}`
    + `${latest.error ? "" : `<div id="impact-latest">${latestImpactHtml(latest.data)}</div>`}</div>${summary}`
    + `<div class="card"><h3 class="section-label">Supported CSRs (AI-assessed) <span class="muted small">${sorted.length}</span></h3>${linksHtml}</div>`);
  bindReportToggles($("impact-latest"));
}

// ---------- CSRs ----------

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

async function renderCsrs(p, seq) {
  const page = pageOf(p);
  const f = { country: pick(p.get("country"), COUNTRIES), year: yearOf(p, "year"), q: p.get("q") || "" };
  const res = await fetchPage((opts) => csrsQuery(f, opts), page);
  if (stale(seq)) return;
  if (res.redirectPage) return redirectToPage("csrs", p, res.redirectPage);
  const { data, error, count } = res;
  if (error) return showError(error, "Could not load CSRs");

  const years = Object.fromEntries(CSR_YEARS.map((y) => [String(y), String(y)]));
  const form = filterForm("csrs",
    textField("q", "Search CSR text", f.q, "e.g. pension, energy")
    + countriesSelect(f.country)
    + selectField("year", "Year", years, f.year ? String(f.year) : "", "All years"));
  // Server sorts "number" as text; re-sort the page numerically (10 after 9).
  const rows = [...data].sort(compareCsr).map((r) => [
    td(hashLink("csr", r.csr_id, r.csr_id), "nowrap"), td(esc(countryName(r.country))), td(esc(r.year), "nowrap"),
    td(esc(r.number), "num"), td(esc(r.title), "title-cell"),
    td(r.assessment ? `${esc(r.assessment)}${r.assessment_year ? ` <span class="muted">(${esc(r.assessment_year)})</span>` : ""}` : `<span class="muted">—</span>`),
  ]);
  view(`${heading("Country-specific recommendations")}${form}${pager("csrs", p, page, count)}`
    + table(["CSR", "Country", "Year", "No.", "Title", "Latest assessment"], rows, "No CSR matches these filters.")
    + (data.length ? pager("csrs", p, page, count) : ""));
  bindFilters();
}

function partBlock(part) {
  const rank = { direct: 0, partial: 1 };
  const links = [...(part.project_csr_links || [])].sort((a, b) => (rank[a.level] ?? 2) - (rank[b.level] ?? 2));
  const items = links.map((l) => {
    const pr = l.projects || {};
    const meta = [pr.programme, pr.year].filter(Boolean).map(esc).join(" · ");
    return `<li class="link-item"><div class="link-head">${badge(l.level)}`
      + `${pr.project_id ? hashLink("project", pr.project_id, pr.title || pr.project_id) : esc(pr.title)}`
      + `${meta ? `<span class="muted small">${meta}</span>` : ""}</div>`
      + `<p><span class="label">Rationale:</span> ${esc(l.rationale)}</p></li>`;
  }).join("");
  const areas = (part.policy_areas || []).map(areaLabel).join(", ");
  return `<div class="part"><h3>${esc(part.part_id)}</h3><p class="prose">${esc(part.text)}</p>`
    + (areas ? `<p class="small"><span class="label">Policy areas:</span> ${esc(areas)}</p>` : "")
    + (part.assessment ? `<p><span class="label">Assessment:</span> ${esc(part.assessment)}</p>` : "")
    + `<p class="label">Linked projects (${links.length})</p>`
    + (items ? `<ul class="link-list">${items}</ul>` : `<p class="empty">No project linked.</p>`) + `</div>`;
}

async function renderCsr(id, seq) {
  const [csr, parts] = await Promise.all([
    sb.from("csrs").select("csr_id,country,year,number,title,text,policy_areas,assessment,assessment_year,source_url").eq("csr_id", id).single(),
    sb.from("csr_parts").select("part_id,text,policy_areas,assessment,project_csr_links(level,rationale,projects(project_id,title,programme,year))")
      .eq("csr_id", id).order("part_id"),
  ]);
  if (stale(seq)) return;
  if (csr.error) return showError(csr.error, csr.error.code === "PGRST116" ? "CSR not found" : "Could not load the CSR");
  if (parts.error) return showError(parts.error, "Could not load the CSR sub-parts");

  const r = csr.data;
  const projectIds = new Set(parts.data.flatMap((pt) => (pt.project_csr_links || []).map((l) => l.projects?.project_id).filter(Boolean)));
  const n = projectIds.size;
  const areas = (r.policy_areas || []).map(areaLabel).join(", ");
  const assessment = r.assessment
    ? `<p><span class="label">Latest assessment${r.assessment_year ? ` (${esc(r.assessment_year)})` : ""}:</span> ${esc(r.assessment)}</p>` : "";
  const source = safeUrl(r.source_url) ? `<p class="small">${extLink(r.source_url, "ECFIN CSR database")} ↗</p>` : "";
  const lineage = Object.hasOwn(COUNTRIES, r.country)
    ? `<p><a class="btn btn-secondary" href="${esc(lineageHref(r.country, [num(r.year)], `csr:${r.csr_id}`, n === 0))}">Show in lineage network</a></p>` : "";
  view(`<a class="back" href="#csrs">← CSRs</a>`
    + `<div class="card"><p class="muted small">${esc(countryName(r.country))} · ${esc(r.year)} · CSR ${esc(r.number)} · <code>${esc(r.csr_id)}</code></p>`
    + `<h2 class="detail-title" tabindex="-1">${esc(r.title)}</h2>`
    + `<p><strong>${n} project${n === 1 ? "" : "s"} support${n === 1 ? "s" : ""} this CSR</strong> <span class="ai-note">AI-assessed</span></p>`
    + `<p class="prose">${esc(r.text)}</p>`
    + (areas ? `<p class="small"><span class="label">Policy areas:</span> ${esc(areas)}</p>` : "")
    + `${assessment}${source}${lineage}</div>`
    + `<div class="card"><h3>Sub-parts</h3>${parts.data.length ? parts.data.map(partBlock).join("") : `<p class="empty">No sub-part.</p>`}</div>`);
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
  dashCache = null;
  lineageCache.clear();
  lineageState = null;
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

async function renderChat(seq) {
  await loadMarkdown();
  if (stale(seq)) return;
  view(`${heading("Chat")}`
    + `<p class="muted">Ask about TSI projects and the country-specific recommendations. `
    + `Answers use project records, AI summaries, CSRs and AI-assessed links; deliverable full texts are not searched here. `
    + `Answers are generated by AI (Claude, Anthropic): verify the cited sources.</p>`
    + `<div class="card chat"><div id="chat-log" class="chat-log"></div>`
    + `<p id="chat-status" class="chat-status" role="status"></p>`
    + `<form id="chat-form" class="chat-form"><label for="chat-input">Your question</label>`
    + `<div class="chat-row"><textarea id="chat-input" rows="3" maxlength="${MAX_CONTENT}" `
    + `placeholder="e.g. Which projects support pension reforms in Portugal?"></textarea>`
    + `<button type="submit" class="btn" id="chat-send">Send</button></div>`
    + `<div class="chat-actions"><span class="muted small">Enter to send · Shift+Enter for a new line</span>`
    + `<span class="toolbar-actions"><button type="button" class="btn btn-secondary" id="chat-word" `
    + `aria-label="Export this conversation to Word">Export to Word</button>`
    + `<button type="button" class="btn btn-secondary" id="chat-new">New conversation</button></span></div></form></div>`);
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
  assessments: () => ({
    title: "CSR assessment",
    html: `<p>The Commission's latest yearly assessment of the implementation of each country-specific recommendation, from the
      ECFIN CSR database.</p><ul>
      <li>From <strong>Full implementation</strong> through <strong>Substantial progress</strong>, <strong>Some progress</strong>
        and <strong>Limited progress</strong> to <strong>No progress</strong>.</li>
      <li><strong>Not assessed</strong>: the Commission did not assess the recommendation that year.</li>
      <li><strong>None</strong>: no assessment yet.</li></ul>`,
  }),
  links: () => ({
    title: "Project–CSR links",
    html: `<p>Links assessed by AI from the project text: verify before use.</p><ul>
      <li><strong>Direct</strong>: the project's objective is to design or implement what the CSR sub-part recommends.</li>
      <li><strong>Partial</strong>: the project contributes to one aspect of the recommendation, or to a precondition.</li></ul>
      <p>So far only projects with a detail page are linked; projects known only by their title are not yet linked.</p>`,
  }),
};

function infoButton(key, label = "About this chart") {
  return `<button type="button" class="info-btn" data-info="${key}" aria-label="${esc(label)}" aria-haspopup="dialog">ⓘ</button>`;
}

let infoOpener = null;

/** Opens the shared <dialog>: Escape, the Close button or a click on the backdrop close it; focus returns to the button. */
function openInfo(key, opener) {
  const dialog = $("info-dialog");
  const content = INFO[key]?.();
  if (!dialog || !content) return;
  $("info-title").textContent = content.title;
  $("info-body").innerHTML = content.html;
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
  dialog.addEventListener("click", (e) => { if (e.target === dialog) dialog.close(); }); // backdrop
  dialog.addEventListener("close", () => {
    if (infoOpener?.isConnected) infoOpener.focus();
    infoOpener = null;
  });
}

// ---------- dashboard (hand-made SVG; method of the dataviz skill) ----------

const CH = 7; // average glyph width (px) of the 12 px chart font, used to fit labels
const DASH_MIN_W = 280;
const PROGRAMME_KEYS = { TSI: "s1", SRSP: "s2", SRSS: "s3" }; // fixed categorical order, never by rank
const SEVERITY_KEYS = [["high", "High"], ["medium", "Medium"], ["low", "Low"], ["info", "Info"]];
const ASSESSMENT_SCALE = [
  ["full implementation", "Full implementation", "o1"], ["substantial progress", "Substantial progress", "o2"],
  ["some progress", "Some progress", "o3"], ["limited progress", "Limited progress", "o4"],
  ["no progress", "No progress", "o5"], ["not assessed", "Not assessed", "muted"], ["none", "No assessment", "muted"],
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
  const csrYears = arr(d.csrs_by_year).map((x) => ({ year: num(x.year), n: num(x.n) })).filter((x) => x.year).sort((a, b) => a.year - b.year);
  const scores = new Map(arr(d.impact_scores).map((x) => [num(x.score), num(x.n)]));
  const analysed = num(t.impact_projects) || [...scores.values()].reduce((s, n) => s + n, 0);
  return {
    totals: {
      projects, detail: num(t.projects_with_detail), deliverables: num(t.deliverables), csrs: num(t.csrs),
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
    coverage: arr(d.csr_coverage_by_country).map((x) => ({ key: String(x.country ?? x.key ?? ""), csrs: num(x.csrs), covered: num(x.csrs_with_project) }))
      .filter((x) => x.key && x.csrs > 0),
    linksByLevel: kv(d.links_by_level),
    scores: Array.from({ length: 11 }, (_, s) => ({ score: s, n: scores.get(s) || 0 })),
    byLevel: kv(d.impact_by_level),
    byConfidence: kv(d.impact_by_confidence),
    meanArea: arr(d.impact_mean_by_policy_area).map((x) => ({ key: String(x.key ?? "none"), mean: num(x.mean), n: num(x.n) })),
    meanCountry: arr(d.impact_mean_by_country).map((x) => ({ key: String(x.key ?? ""), mean: num(x.mean), n: num(x.n) })),
    recent: arr(d.impact_recent).filter((x) => x && typeof x === "object"),
    anomalies: arr(d.anomalies_by_type_severity).map((x) => ({ type: String(x.type ?? ""), severity: String(x.severity ?? ""), n: num(x.n) })),
  };
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
    segs: [{ v: x.n, key: x.color, href: null, tip: { label: x.label, value: `${fmtN(x.n)} CSRs`, share: `${share(x.n, total)} of CSRs`, key: x.color } }] }));
}

function coverageRows(m) {
  return [...m.coverage].map((x) => ({ ...x, pct: x.csrs ? (100 * x.covered) / x.csrs : 0 }))
    .sort((a, b) => b.pct - a.pct || b.csrs - a.csrs).map((x) => ({
      label: countryName(x.key), display: `${Math.round(x.pct)}% (${x.covered}/${x.csrs})`, csrs: x.csrs, covered: x.covered,
      segs: [{ v: x.pct, key: "s1", href: Object.hasOwn(COUNTRIES, x.key) ? hrefFor("lineage", { country: x.key }) : null,
        tip: { label: countryName(x.key), value: `${Math.round(x.pct)}% of CSRs supported`, share: `${fmtN(x.covered)} of ${fmtN(x.csrs)} CSRs have a linked project` } }],
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
  csrYear: (m, w) => vbarChart(m.csrYears.map((x) => ({
    label: String(x.year),
    segs: [{ v: x.n, key: "s1", href: hrefFor("csrs", { year: x.year }), tip: { label: String(x.year), value: `${fmtN(x.n)} CSRs`, share: `${share(x.n, m.totals.csrs)} of CSRs` } }],
  })), w, { height: 180, title: "CSRs by year" }),
  assessments: (m, w) => hbarChart(assessmentRows(m), w, { title: "Latest CSR assessment" }),
  coverage: (m, w) => hbarChart(coverageRows(m), w, { max: 100, title: "CSRs supported by at least one project, by country" }),
  links: (m, w) => hbarChart(simpleRows(inOrder(m.linksByLevel, ["direct", "partial"]), {
    label: (x) => LINK_LABELS[x.key] || x.key, total: m.totals.links, unit: " links",
  }), w, { title: "Project–CSR links by level" }),
  anomalies: (m, w) => hbarChart(anomalyRows(m), w, { title: "Anomalies by type and severity" }),
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

function tile(label, value, sub) {
  return `<div class="kpi"><p class="kpi-label">${esc(label)}</p><p class="kpi-value">${esc(value)}</p>`
    + (sub ? `<p class="kpi-sub">${esc(sub)}</p>` : "") + `</div>`;
}

function dashboardHtml(m, at) {
  const t = m.totals;
  const years = m.csrYears.length ? `${m.csrYears[0].year}–${m.csrYears[m.csrYears.length - 1].year}` : "2016–2025";
  const tiles = `<div class="kpis">`
    + tile("Projects", fmtN(t.projects), t.projects ? `${share(t.detail, t.projects)} with a detail page` : "")
    + tile("Deliverables", fmtN(t.deliverables), "")
    + tile(`CSRs (${years})`, fmtN(t.csrs), t.csrParts ? `${fmtN(t.csrParts)} sub-parts` : "")
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
    + figure("csrYear", "CSRs by year", { sub: "Click a bar to list that year's CSRs.", tableHtml: dataTable(["Year", "CSRs"], m.csrYears.map((x) => [String(x.year), fmtN(x.n)])) })
    + figure("assessments", "Latest CSR assessment", { info: "assessments", sub: "From full implementation to no progress.",
      tableHtml: dataTable(["Assessment", "CSRs"], assessmentRows(m).map((r) => [r.label, r.display])) })
    + figure("coverage", "CSRs supported by a project, by country", { wide: true, sub: "Share of the country's CSRs with at least one linked project (AI-assessed links). Click to open the lineage network.",
      tableHtml: dataTable(["Country", "CSRs", "With a project", "Share"], coverageRows(m).map((r) => [r.label, fmtN(r.csrs), fmtN(r.covered), share(r.covered, r.csrs)])) })
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
    + tiles + projects + impact + csrs + quality
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

// ---------- lineage: bipartite network CSR -> projects, per country (hand-made SVG) ----------

const LN_FROM = 2019;
const LN_TO = 2025;
const LN_NODE_H = 30; // two text lines
const LN_STEP = 36; // vertical pitch of a node
const LN_YEAR_H = 26; // year sub-heading in the CSR column
const LN_MIN_COL = 96; // below this column width the labels are unreadable: list view instead
const lineageCache = new Map(); // `${uid}|${country}|${from}|${to}` -> { csrs, scores }
let lineageState = null; // { model, country, width, pinned }

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
          projects.set(pid, { id: pid, title: p.title ?? pid, programme: p.programme ?? "", year: p.year ?? "", results: p.results ?? "",
            url: p.url ?? "", score: Number.isFinite(scores?.get?.(pid)) ? scores.get(pid) : null });
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
  return {
    country,
    csrs: shown.map((c) => ({ ...c, linked: linkedCsrs.has(c.id), info: assessmentInfo(c.assessment) })),
    projects: projectList,
    edges: visibleEdges,
    csrIndex,
    summary: {
      csrs: csrList.length, csrsLinked: linkedCsrs.size, projects: projectList.length,
      links: edgeList.length, direct: edgeList.filter((e) => e.level === "direct").length,
      subLinks: edgeList.reduce((n, e) => n + e.parts.length, 0),
    },
  };
}

/** Positions: CSRs by year then number; projects in barycentric order (mean centre of their CSRs), pulled towards it. */
function lineageLayout(model, width) {
  const W = Math.max(280, Math.floor(width) || 0);
  const narrow = W < 600;
  const leftW = Math.round(narrow ? W * 0.4 : Math.min(300, W * 0.34));
  const rightW = Math.round(narrow ? W * 0.42 : Math.min(380, W * 0.4));
  const rightX = W - rightW;
  const csrPos = [];
  const headers = [];
  let y = 0;
  let year = null;
  model.csrs.forEach((c, i) => {
    if (c.year !== year) {
      year = c.year;
      headers.push({ year, y });
      y += LN_YEAR_H;
    }
    csrPos[i] = y;
    y += LN_STEP;
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
  const top = LN_YEAR_H;
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
  return { W, narrow, leftW, rightW, rightX, csrPos, headers, projPos, order: order.map((o) => o.p.id), height,
    readable: leftW >= LN_MIN_COL && rightW >= LN_MIN_COL };
}

function lnAria(parts) {
  return esc(parts.filter(Boolean).join(", "));
}

/** The network as an SVG string (pure: also used by the Node render test). */
function lineageSvg(model, width) {
  const L = lineageLayout(model, width);
  const cc = model.country;
  const x1 = L.leftW;
  const x2 = L.rightX;
  const dx = (x2 - x1) / 2;
  const csrIdx = model.csrIndex;
  let edges = "";
  model.edges.forEach((e, i) => {
    const ci = csrIdx.get(e.csr);
    const py = L.projPos.get(e.project);
    if (ci === undefined || py === undefined) return;
    const ya = L.csrPos[ci] + LN_NODE_H / 2;
    const yb = py + LN_NODE_H / 2;
    const d = `M${px(x1)} ${px(ya)}C${px(x1 + dx)} ${px(ya)} ${px(x2 - dx)} ${px(yb)} ${px(x2)} ${px(yb)}`;
    edges += `<g class="ln-edge ln-${e.level}" data-e="${i}"><path class="ln-line" d="${d}"/><path class="ln-hit" d="${d}"/></g>`;
  });
  let nodes = "";
  for (const h of L.headers) {
    nodes += `<text class="ln-year" x="2" y="${h.y + 17}">${esc(h.year)}</text>`;
  }
  const textW1 = (w) => w - 20 - 6; // marker + padding
  model.csrs.forEach((c, i) => {
    const y = L.csrPos[i];
    const full = `${cc} ${c.year} · CSR ${c.number}`;
    const label = full.length * CH <= textW1(L.leftW) ? full : fitText(`CSR ${c.number}`, textW1(L.leftW));
    const n = model.edges.filter((e) => e.csr === c.id).length;
    nodes += `<g class="ln-node ln-csr${c.linked ? "" : " ln-unlinked"}" data-node="c${i}" tabindex="0" role="button" aria-pressed="false"`
      + ` aria-label="${lnAria([full, c.info.label, `${n} project${n === 1 ? "" : "s"}`])}">`
      + `<rect class="ln-box" x="0.5" y="${y}" width="${L.leftW - 1}" height="${LN_NODE_H}" rx="4"/>`
      + `<rect class="viz-fill-${esc(c.info.key)}" x="5" y="${y + 5}" width="8" height="${LN_NODE_H - 10}" rx="2"/>`
      + `<text class="ln-t1" x="20" y="${y + 13}">${esc(label)}</text>`
      + `<text class="ln-t2" x="20" y="${y + 25}">${esc(fitText(c.info.label, textW1(L.leftW), 6.4))}</text></g>`;
  });
  model.projects.forEach((p) => {
    const y = L.projPos.get(p.id);
    if (y === undefined) return;
    const k = L.order.indexOf(p.id);
    const x = L.rightX;
    const hasScore = Number.isFinite(p.score);
    const pill = hasScore ? 38 : 0;
    const meta = [p.programme, p.year].filter(Boolean).join(" ");
    const n = model.edges.filter((e) => e.project === p.id).length;
    nodes += `<g class="ln-node ln-project" data-node="p${k}" data-id="${esc(p.id)}" tabindex="0" role="button" aria-pressed="false"`
      + ` aria-label="${lnAria([p.title, meta, hasScore ? `impact score ${p.score}/10` : "", `${n} CSR${n === 1 ? "" : "s"}`])}">`
      + `<rect class="ln-box" x="${x + 0.5}" y="${y}" width="${L.rightW - 1}" height="${LN_NODE_H}" rx="4"/>`
      + `<rect class="viz-fill-${programmeKey(p.programme)}" x="${x + 5}" y="${y + 5}" width="8" height="${LN_NODE_H - 10}" rx="2"/>`
      + `<text class="ln-t1" x="${x + 20}" y="${y + 13}">${esc(fitText(p.title, textW1(L.rightW)))}</text>`
      + `<text class="ln-t2" x="${x + 20}" y="${y + 25}">${esc(fitText(meta, textW1(L.rightW) - pill, 6.4))}</text>`
      + (hasScore ? `<rect class="ln-pill" x="${x + L.rightW - 6 - 34}" y="${y + 16}" width="34" height="12" rx="6"/>`
        + `<text class="ln-pill-t" x="${x + L.rightW - 6 - 17}" y="${y + 25.5}" text-anchor="middle">${esc(`${p.score}/10`)}</text>` : "")
      + `</g>`;
  });
  const svg = `<svg class="ln-svg" viewBox="0 0 ${L.W} ${L.height}" width="100%" height="${L.height}" role="group"`
    + ` aria-label="${esc(`Network of CSRs and supporting projects, ${countryName(cc)}`)}">`
    + `<g class="ln-edges">${edges}</g><g class="ln-nodes">${nodes}</g></svg>`;
  return { svg, layout: L };
}

/** One row per link (CSR sub-part -> project), for the table view and the CSV export. */
function lineageRows(model) {
  const projects = new Map(model.projects.map((p) => [p.id, p]));
  const csrs = new Map(model.csrs.map((c) => [c.id, c]));
  return model.edges.flatMap((e) => e.parts.map((pt) => {
    const c = csrs.get(e.csr) || {};
    const p = projects.get(e.project) || {};
    return { csr_id: e.csr, csr_year: c.year, csr_number: c.number, part_id: pt.part, project_id: e.project, project_title: p.title,
      programme: p.programme, project_year: p.year, level: pt.level, basis: pt.basis, rationale: pt.rationale };
  }));
}

const LN_CSV_COLS = ["csr_id", "csr_year", "csr_number", "part_id", "project_id", "project_title", "programme", "project_year", "level", "basis", "rationale"];

function lineageTable(model) {
  const rows = lineageRows(model).map((r) => [
    td(hashLink("csr", r.csr_id, r.csr_id), "nowrap"), td(esc(r.part_id), "nowrap"),
    td(hashLink("project", r.project_id, r.project_title || r.project_id), "title-cell"), td(badge(r.level)), td(esc(r.rationale)),
  ]);
  return table(["CSR", "Sub-part", "Project", "Level", "Rationale"], rows, "No link.");
}

function lineageLegend() {
  const line = (cls) => `<svg class="ln-key" width="26" height="10" aria-hidden="true"><line class="ln-line ${cls}" x1="1" y1="5" x2="25" y2="5"/></svg>`;
  const sw = (key, label) => `<li><span class="viz-key viz-key-${key}" aria-hidden="true"></span>${esc(label)}</li>`;
  return `<div class="ln-legend">`
    + `<ul class="viz-legend"><li>${line("ln-direct-key")}Direct link</li><li>${line("ln-partial-key")}Partial link</li>`
    + `<li><span class="ln-pill-key" aria-hidden="true">6/10</span>Latest impact score</li>`
    + `<li>${infoButton("links", "About the links")}</li></ul>`
    + `<ul class="viz-legend"><li class="muted">CSR assessment ${infoButton("assessments", "About the CSR assessment")}</li>${ASSESSMENT_SCALE.filter(([k]) => k !== "none").map(([, label, key]) => sw(key, label)).join("")}${sw("muted", "None")}</ul>`
    + `<ul class="viz-legend"><li class="muted">Programme:</li>${PROGRAMMES.map((p) => sw(programmeKey(p), p)).join("")}</ul></div>`;
}

function lineageSummary(s, { country = "", from = null, to = null } = {}) {
  const plural = (n, word) => `${fmtN(n)} ${word}${n === 1 ? "" : "s"}`;
  const where = [country ? countryName(country) : "", from && to ? (from === to ? String(from) : `${from}–${to}`) : ""].filter(Boolean).join(", ");
  return `${where ? `${where}: ` : ""}${plural(s.csrs, "CSR")} (${fmtN(s.csrsLinked)} with at least one project) · `
    + `${plural(s.projects, "project")} · ${plural(s.links, "CSR–project pair")} (${fmtN(s.direct)} direct)`
    + ` from ${plural(s.subLinks ?? s.links, "sub-part link")}`;
}

/** Detail panel of the pinned node (links to the CSR or project page). */
function lineageDetail(model, key) {
  if (!key) return `<p class="muted small">Click a CSR or a project to pin its links and see the details here.</p>`;
  const projects = new Map(model.projects.map((p) => [p.id, p]));
  const csrs = new Map(model.csrs.map((c) => [c.id, c]));
  if (key[0] === "c") {
    const c = model.csrs[Number(key.slice(1))];
    if (!c) return "";
    const items = model.edges.filter((e) => e.csr === c.id).map((e) => {
      const p = projects.get(e.project) || {};
      return `<li>${badge(e.level)} ${hashLink("project", e.project, p.title || e.project)} <span class="muted small">${esc([p.programme, p.year].filter(Boolean).join(" "))}</span></li>`;
    }).join("");
    return `<h3>${esc(`${model.country} ${c.year} · CSR ${c.number}`)}</h3><p>${esc(c.title)}</p>`
      + `<p class="small"><span class="label">Assessment:</span> ${esc(c.info.label)}${c.assessmentYear ? ` (${esc(c.assessmentYear)})` : ""}</p>`
      + (items ? `<ul class="ln-list">${items}</ul>` : `<p class="empty">No project linked.</p>`)
      + `<p>${hashLink("csr", c.id, "Open the CSR")}</p>`;
  }
  const id = model.projects.length ? lineageState?.layout?.order?.[Number(key.slice(1))] : null;
  const p = projects.get(id);
  if (!p) return "";
  const items = model.edges.filter((e) => e.project === p.id).map((e) => {
    const c = csrs.get(e.csr) || {};
    return `<li>${badge(e.level)} ${hashLink("csr", e.csr, `${model.country} ${c.year} · CSR ${c.number}`)} <span class="muted small">${esc(c.title)}</span></li>`;
  }).join("");
  const page = p.url && sourceInfo(p.url) ? ` · ${sourceLink(sourceInfo(p.url))}` : "";
  return `<h3>${esc(p.title)}</h3><p class="small muted">${esc([p.programme, p.year, p.results ? `results ${p.results}` : "",
    Number.isFinite(p.score) ? `impact score ${p.score}/10` : ""].filter(Boolean).join(" · "))}</p>`
    + `<ul class="ln-list">${items}</ul><p>${hashLink("project", p.id, "Open the project")}${page}</p>`;
}

/** Tooltip lines for a node or an edge (plain strings, inserted with textContent). */
function lineageTipLines(model, target) {
  const order = lineageState?.layout?.order || [];
  const short = (s, n) => (String(s ?? "").length > n ? `${String(s).slice(0, n - 1)}…` : String(s ?? ""));
  if (target.node?.[0] === "c") {
    const c = model.csrs[Number(target.node.slice(1))];
    if (!c) return null;
    const n = model.edges.filter((e) => e.csr === c.id).length;
    return [`${model.country} ${c.year} · CSR ${c.number}`, short(c.title || c.text, 180),
      `Assessment: ${c.info.label}${c.assessmentYear ? ` (${c.assessmentYear})` : ""}`, `${n} project${n === 1 ? "" : "s"}`];
  }
  if (target.node?.[0] === "p") {
    const p = model.projects.find((x) => x.id === order[Number(target.node.slice(1))]);
    if (!p) return null;
    const n = model.edges.filter((e) => e.project === p.id).length;
    return [short(p.title, 140), [p.programme, p.year].filter(Boolean).join(" · "), `Results: ${p.results || "not stated"}`,
      Number.isFinite(p.score) ? `Impact score: ${p.score}/10` : "No impact analysis", `${n} CSR${n === 1 ? "" : "s"}`];
  }
  const e = model.edges[Number(target.edge)];
  if (!e) return null;
  const c = model.csrs[model.csrIndex.get(e.csr)] || {};
  const p = model.projects.find((x) => x.id === e.project) || {};
  const parts = e.parts.slice(0, 4).map((pt) => `${pt.part} (${pt.level}): ${short(pt.rationale, 160)}`);
  if (e.parts.length > 4) parts.push(`+${e.parts.length - 4} more sub-parts`);
  return [`${e.level === "direct" ? "Direct" : "Partial"} link`, `CSR ${c.number} (${c.year}) → ${short(p.title, 80)}`, ...parts];
}

function lineageHighlight(key) {
  const root = $("ln-graph");
  const s = lineageState;
  if (!root || !s) return;
  root.querySelectorAll(".on").forEach((el) => el.classList.remove("on"));
  root.classList.toggle("has-focus", Boolean(key));
  if (!key) return;
  const order = s.layout.order;
  const on = (sel) => root.querySelector(sel)?.classList.add("on");
  on(`[data-node="${key}"]`);
  s.model.edges.forEach((e, i) => {
    const ck = `c${s.model.csrIndex.get(e.csr)}`;
    const pk = `p${order.indexOf(e.project)}`;
    if (ck === key || pk === key) {
      on(`[data-e="${i}"]`);
      on(`[data-node="${ck === key ? pk : ck}"]`);
    }
  });
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
  slot.innerHTML = svg;
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
    return edge ? { edge: edge.dataset.e, el: edge } : null;
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
    .select("csr_id,year,number,title,text,assessment,assessment_year,csr_parts(part_id,text,project_csr_links(level,rationale,basis,projects(project_id,title,programme,year,results,url)))")
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
    const projs = pids.length ? await sb.from("projects").select("project_id,title,programme,year,results,url").in("project_id", pids) : { data: [] };
    if (projs.error) return { error: projs.error };
    const pmap = new Map(projs.data.map((p) => [p.project_id, p]));
    csrs = base.data.map((c) => ({ ...c, csr_parts: parts.data.filter((p) => p.csr_id === c.csr_id).map((p) => ({
      ...p, project_csr_links: links.data.filter((l) => l.part_id === p.part_id).map((l) => ({ ...l, projects: pmap.get(l.project_id) || null })),
    })) }));
  }
  const pids = [...new Set(arr(csrs).flatMap((c) => arr(c.csr_parts).flatMap((p) => arr(p.project_csr_links).map((l) => l.projects?.project_id))).filter(Boolean))];
  const scores = new Map();
  if (pids.length) {
    const res = await sb.from("impact_reports").select("project_id,score,created_at").in("project_id", pids)
      .order("created_at", { ascending: false }).order("id", { ascending: false });
    if (!res.error) for (const r of res.data) if (!scores.has(r.project_id) && Number.isFinite(r.score)) scores.set(r.project_id, r.score);
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
  const form = filterForm("lineage", countriesSelect(country)
    + yearField("from", "From year", from) + yearField("to", "To year", to)
    + checkField("unlinked", "Show CSRs without a project", unlinked));
  const intro = `<p class="muted">Country-specific recommendations (left, by year) and the projects that support them (right). `
    + `Links assessed by AI from the project text: verify before use. ${infoButton("links", "About the links")}</p>`;
  if (!country) {
    view(`${heading("Lineage")}${intro}${form}`
      + (raw ? `<p class="error" role="alert">Unknown country code: choose a country.</p>` : `<p class="empty">Choose a country.</p>`));
    bindFilters();
    return;
  }
  const res = await fetchLineage(country, from, to);
  if (stale(seq)) return;
  if (res.error) return showError(res.error, "Could not load the lineage network");
  const model = lineageModel(res.csrs, res.scores, { unlinked, country });
  const head = `${heading(`Lineage · ${countryName(country)}`)}${intro}${form}`;
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
    + `<div id="ln-wrap" class="ln-wrap"><div class="ln-cols" aria-hidden="true"><span>CSRs</span><span>Projects</span></div>`
    + `<div id="ln-graph" class="ln-graph"></div><div id="ln-tip" class="viz-tip ln-tip" role="tooltip" hidden></div></div>`
    + `<div id="ln-detail" class="ln-detail" aria-live="polite"></div>`
    + `<details class="viz-table"><summary>Table of links</summary>`
    + `<div class="toolbar"><span class="muted small">One row per CSR sub-part and project.</span>`
    + `<button type="button" class="btn btn-secondary" id="ln-csv">Download CSV</button></div>${lineageTable(model)}</details></div>`);
  bindFilters();
  bindLineage();
  $("ln-csv").addEventListener("click", () => saveCsv(`lineage-${country}-${from}-${to}.csv`, LN_CSV_COLS, lineageRows(model)));
  paintLineageGraph();
  $("ln-detail").innerHTML = lineageDetail(model, null);
  // Selection passed by the CSR or project page (#lineage?...&sel=csr:ID or sel=project:ID).
  const m = /^(csr|project):(.+)$/.exec(sel);
  if (m) {
    const key = m[1] === "csr"
      ? (model.csrIndex.has(m[2]) ? `c${model.csrIndex.get(m[2])}` : null)
      : (lineageState.layout?.order.includes(m[2]) ? `p${lineageState.layout.order.indexOf(m[2])}` : null);
    if (key) {
      lineagePin(key);
      $("ln-graph").querySelector(`[data-node="${key}"]`)?.scrollIntoView({ block: "center" });
    }
  }
}

/** #lineage link that opens the network on a CSR or a project (years widened to include it). */
function lineageHref(country, years, sel, unlinked = false) {
  const ys = years.filter((y) => Number.isFinite(y) && y > 1900);
  return hrefFor("lineage", { country, from: Math.min(LN_FROM, ...ys), to: Math.max(LN_TO, ...ys), unlinked: unlinked ? "1" : "", sel });
}

// ---------- auth and routing ----------

function showScreen(name) {
  for (const id of ["config-error", "login", "change-password", "denied"]) $(id).hidden = id !== name;
  $("tabs").hidden = name !== "app";
  $("view").hidden = name !== "app";
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

async function route(seq) {
  const { route: r, id, params: p } = params();
  const viewKey = ["project", "csr", "impact"].includes(r) && id ? `${r}/${id}`
    : (["dashboard", "lineage", "csrs", "anomalies", "chat", "impact"].includes(r) ? r : "projects");
  setActiveTab(r);
  loading();
  try {
    if (r === "project" && id) await renderProject(id, seq);
    else if (r === "csr" && id) await renderCsr(id, seq);
    else if (r === "dashboard") await renderDashboard(seq);
    else if (r === "lineage") await renderLineage(p, seq);
    else if (r === "csrs") await renderCsrs(p, seq);
    else if (r === "anomalies") await renderAnomalies(p, seq);
    else if (r === "chat") await renderChat(seq);
    else if (r === "impact") await renderImpact(id, p, seq);
    else { setActiveTab("projects"); await renderProjects(p, seq); }
  } catch (e) {
    if (!stale(seq)) showError(e, "Unexpected error");
  }
  if (!stale(seq)) restoreFocus(viewKey);
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
  showScreen("app");
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
  $("change-password-cancel").addEventListener("click", () => { wantsPasswordChange = false; render(); });
  $("logout").addEventListener("click", async () => {
    testerCache = null;
    wantsPasswordChange = false;
    await sb.auth.signOut({ scope: "local" }); // SIGNED_OUT re-renders (user id changes)
    history.replaceState(null, "", location.pathname + location.search + "#projects");
  });
  // One Escape handler for the whole page: hides the chart tooltips and releases a pinned lineage node.
  document.addEventListener("keydown", (e) => {
    if (e.key !== "Escape" || $("info-dialog")?.open) return;
    const tip = $("dash-tip");
    if (tip) tip.hidden = true;
    hideLineageTip();
    if (lineageState?.pinned && $("ln-graph")) lineagePin(lineageState.pinned);
  });
  bindInfoDialog();
  let resizeTimer = 0;
  window.addEventListener("resize", () => {
    clearTimeout(resizeTimer);
    resizeTimer = setTimeout(() => {
      const root = $("dash");
      if (root && root.clientWidth !== dashWidth) paintDashCharts();
      const wrap = $("ln-wrap");
      if (wrap && lineageState && wrap.clientWidth !== lineageState.width) paintLineageGraph();
    }, 150);
  });
  window.addEventListener("hashchange", () => {
    if (/^#?access_token=/.test(location.hash)) return; // handled by supabase-js
    render();
  });
  // Re-render only when the signed-in user changes (or is updated): SIGNED_IN is also emitted when the tab
  // regains focus, and must not rebuild the view. The callback must not await Supabase calls (auth lock).
  sb.auth.onAuthStateChange((event, session) => {
    if (event === "INITIAL_SESSION") return; // covered by the first render() below
    const uid = session?.user?.id ?? null;
    if (uid === currentUid && event !== "USER_UPDATED") return;
    if (currentUid && uid !== currentUid) clearAiState(); // sign-out or another user: drop chat and reports
    currentUid = uid;
    testerCache = null;
    setTimeout(render, 0);
  });
  await render();
}

init();
