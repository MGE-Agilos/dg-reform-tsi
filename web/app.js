// TSI x CSR Explorer: static page over Supabase (RLS: signed-in testers only).
// Every database string goes through esc() before being injected; only https:// URLs become links.
import { SUPABASE_URL, SUPABASE_ANON_KEY } from "./config.js";

const SUPABASE_JS = "https://cdn.jsdelivr.net/npm/@supabase/supabase-js@2.45.4/+esm";
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
  const [proj, links] = await Promise.all([
    sb.from("projects").select("*").eq("project_id", id).single(),
    sb.from("project_csr_links").select("level,rationale,basis,part_id,csr_parts(text,assessment,csr_id,csrs(title,year))").eq("project_id", id),
  ]);
  if (stale(seq)) return;
  if (proj.error) return showError(proj.error, proj.error.code === "PGRST116" ? "Project not found" : "Could not load the project");
  if (links.error) return showError(links.error, "Could not load the CSR links");

  const r = proj.data;
  const rank = { direct: 0, partial: 1 };
  const sorted = [...links.data].sort((a, b) => (rank[a.level] ?? 2) - (rank[b.level] ?? 2) || a.part_id.localeCompare(b.part_id));
  const facts = [
    ["Programme", esc(r.programme)], ["Year", esc(r.year)], ["Countries", esc(countriesText(r.countries))],
    ["Policy area", esc(areaLabel(r.policy_area))], ["Results", esc(r.results || "not stated")],
    ["Deliverables", esc(r.deliverables)], ["Detail page", r.detail_page ? "yes" : "no"], ["Project ID", `<code>${esc(r.project_id)}</code>`],
  ].map(([k, v]) => `<dt>${k}</dt><dd>${v}</dd>`).join("");
  const page = safeUrl(r.url) ? `<p>${extLink(r.url, "Project page")} ↗</p>` : "";
  const summary = r.summary
    ? `<div class="card"><h3 class="section-label">Summary <span class="ai-note">AI-generated summary</span></h3><p class="prose">${esc(r.summary)}</p></div>`
    : "";
  const linksHtml = sorted.length ? `<ul class="link-list">${sorted.map(linkItem).join("")}</ul>` : `<p class="empty">No CSR link assessed.</p>`;
  view(`<a class="back" href="#projects">← Projects</a><div class="card"><h2 class="detail-title" tabindex="-1">${esc(r.title)}</h2>`
    + `<dl class="facts">${facts}</dl>${page}</div>${summary}`
    + `<div class="card"><h3 class="section-label">Supported CSRs (AI-assessed) <span class="muted small">${sorted.length}</span></h3>${linksHtml}</div>`);
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
    sb.from("csrs").select("*").eq("csr_id", id).single(),
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
  view(`<a class="back" href="#csrs">← CSRs</a>`
    + `<div class="card"><p class="muted small">${esc(countryName(r.country))} · ${esc(r.year)} · CSR ${esc(r.number)} · <code>${esc(r.csr_id)}</code></p>`
    + `<h2 class="detail-title" tabindex="-1">${esc(r.title)}</h2>`
    + `<p><strong>${n} project${n === 1 ? "" : "s"} support${n === 1 ? "s" : ""} this CSR</strong> <span class="ai-note">AI-assessed</span></p>`
    + `<p class="prose">${esc(r.text)}</p>`
    + (areas ? `<p class="small"><span class="label">Policy areas:</span> ${esc(areas)}</p>` : "")
    + `${assessment}${source}</div>`
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
    td(r.project_id ? hashLink("project", r.project_id, r.title || r.project_id) : extLink(r.url, r.title || r.url || "—"), "title-cell"),
    td(esc(ANOMALY_TYPES[r.type] || r.type)), td(badge(r.severity)), td(esc(r.programme)), td(esc(r.year), "nowrap"),
    td(esc(r.countries)), td(esc(r.detail)),
  ]);
  const order = f.severity === "all" ? "Sorted by severity (alphabetical: high, info, low, medium), then type." : "Sorted by severity, then type.";
  view(`${heading("Publication anomalies")}${form}`
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
  // Separator ";" for Excel with Belgian/French regional settings; UTF-8 BOM and CRLF.
  const lines = [cols.map(csvCell).join(";"), ...rows.map((r) => cols.map((c) => csvCell(r[c])).join(";"))];
  const blob = new Blob(["\uFEFF" + lines.join("\r\n") + "\r\n"], { type: "text/csv;charset=utf-8" });
  const url = URL.createObjectURL(blob);
  const a = Object.assign(document.createElement("a"), { href: url, download: "anomalies.csv" });
  document.body.appendChild(a);
  a.click();
  a.remove();
  setTimeout(() => URL.revokeObjectURL(url), 1000);
}

// ---------- auth and routing ----------

function showScreen(name) {
  for (const id of ["config-error", "login", "denied"]) $(id).hidden = id !== name;
  $("tabs").hidden = name !== "app";
  $("view").hidden = name !== "app";
}

function showSession(session) {
  $("session").hidden = !session;
  $("session-email").textContent = session?.user?.email || "";
}

async function isTester(session) {
  if (testerCache && testerCache.userId === session.user.id) return testerCache;
  const { data, error } = await sb.rpc("is_tester");
  if (error) return { error };
  testerCache = { userId: session.user.id, ok: data === true };
  return testerCache;
}

async function route(seq) {
  const { route: r, id, params: p } = params();
  const viewKey = ["project", "csr"].includes(r) && id ? `${r}/${id}` : (["csrs", "anomalies"].includes(r) ? r : "projects");
  setActiveTab(r);
  loading();
  try {
    if (r === "project" && id) await renderProject(id, seq);
    else if (r === "csr" && id) await renderCsr(id, seq);
    else if (r === "csrs") await renderCsrs(p, seq);
    else if (r === "anomalies") await renderAnomalies(p, seq);
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
  currentUid = session?.user?.id ?? null;
  showSession(session);
  if (error || !session) {
    showScreen("login");
    const msg = error?.message || authError;
    if (msg) setLoginMessage(msg, true);
    return;
  }
  const tester = await isTester(session);
  if (stale(seq)) return;
  if (tester.error) {
    showScreen("app");
    $("tabs").hidden = true;
    return showError(tester.error, "Could not check access rights");
  }
  if (!tester.ok) return showScreen("denied");
  showScreen("app");
  await route(seq);
}

function setLoginMessage(text, isError) {
  const el = $("login-message");
  el.textContent = text;
  el.className = `message ${isError ? "err" : "ok"}`;
}

async function sendMagicLink(event) {
  event.preventDefault();
  const email = $("login-email").value.trim().toLowerCase();
  if (!email) return;
  const button = event.submitter || $("login-form").querySelector("button");
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
  $("login-form").addEventListener("submit", sendMagicLink);
  $("logout").addEventListener("click", async () => {
    testerCache = null;
    await sb.auth.signOut({ scope: "local" }); // SIGNED_OUT re-renders (user id changes)
    history.replaceState(null, "", location.pathname + location.search + "#projects");
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
    currentUid = uid;
    testerCache = null;
    setTimeout(render, 0);
  });
  await render();
}

init();
