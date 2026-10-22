// Lineage overviews of the TSI x CSR Explorer: Sankey, force-directed network and zoomable sunburst (d3), over the
// recommendation–project pairs of all countries. Loaded on first use by app.js (with d3 and d3-sankey from the CDN).
// Every database string goes through ctx.esc() before being injected as HTML; SVG text is set with .text().
import * as d3base from "https://cdn.jsdelivr.net/npm/d3@7.9.0/+esm";
import { sankey, sankeyLinkHorizontal, sankeyJustify } from "https://cdn.jsdelivr.net/npm/d3-sankey@0.12.3/+esm";

const d3 = d3base;
const REDUCED = typeof matchMedia === "function" && matchMedia("(prefers-reduced-motion: reduce)").matches;
const DUR = REDUCED ? 0 : 450;
const RES = ["Achieved", "Expected", "Not stated"];
const RES_COLOR = ["var(--direct)", "var(--partial)", "var(--viz-muted)"];
const PROG_COLOR = { TSI: "var(--viz-s1)", SRSP: "var(--viz-s2)", SRSS: "var(--viz-s3)" };
const resColor = (r) => RES_COLOR[r] ?? RES_COLOR[2];
const progColor = (p) => PROG_COLOR[p] || "var(--viz-muted)";
const fmt = d3.format(",");
const pct = (a, b) => (b ? `${Math.round((100 * a) / b)}%` : "–");
const truncChars = (s, n) => (s.length > n ? `${s.slice(0, Math.max(1, n - 1)).trimEnd()}…` : s);
const plural = (n, w) => `${fmt(n)} ${w}${n === 1 ? "" : "s"}`;

export const PA_KEYS = ["public-administration-and-governance", "revenue-administration-and-public-financial-management",
  "green-transition", "labour-market-and-social-protection", "competitiveness", "skills-education-and-training",
  "health-and-long-term-care", "financial-sector-and-access-finance", "digital-transition", "recovery-and-resilience-plans",
  "none", "migration-management"];
export const paColor = (k) => `var(--ov-pa${Math.max(0, PA_KEYS.indexOf(k))})`;
export { RES, RES_COLOR, PROG_COLOR };

function statLine(ps) {
  const proj = new Set(ps.map((x) => x.p.i)).size;
  const dir = ps.filter((x) => x.d).length;
  return `${plural(ps.length, "pair")} · ${plural(proj, "project")} · ${pct(dir, ps.length)} direct`;
}

/** Text fitted to a pixel width (measured on the rendered SVG text), with an ellipsis. */
function fitText(textSel, full, maxW) {
  const node = textSel.node();
  textSel.text(full);
  if (node.getComputedTextLength() <= maxW) return full;
  let lo = 0;
  let hi = full.length;
  while (lo < hi) {
    const mid = (lo + hi + 1) >> 1;
    textSel.text(`${full.slice(0, mid).trimEnd()}…`);
    if (node.getComputedTextLength() <= maxW) lo = mid; else hi = mid - 1;
  }
  const t = lo > 0 ? `${full.slice(0, lo).trimEnd()}…` : "";
  textSel.text(t);
  return t;
}

/** Toolbar of each view (static markup; the chart below is redrawn on every filter change). */
export function toolbarHtml(kind) {
  if (kind === "sankey") {
    return `<div class="ov-tools"><button type="button" class="btn btn-secondary" id="sk-back" hidden>← All countries</button>`
      + `<label class="ov-inline">Drill into <select id="sk-country" aria-label="Drill into a country"></select></label>`
      + `<span class="muted small" id="ov-info"></span></div>`;
  }
  if (kind === "sunburst") {
    return `<div class="ov-tools"><label class="ov-inline">Zoom to <select id="sb-country" aria-label="Zoom to a country"></select></label>`
      + `<span class="muted small" id="ov-info"></span></div>`;
  }
  return `<div class="ov-tools"><input type="search" id="nw-search" placeholder="Search a project or country" aria-label="Search a project or country">`
    + `<label class="check"><input type="checkbox" id="nw-all"> Show all projects</label>`
    + `<button type="button" class="btn btn-secondary" id="nw-reset">Reset</button>`
    + `<button type="button" class="btn btn-secondary" id="nw-fit">Fit to view</button>`
    + `<span class="muted small" id="ov-info"></span></div>`;
}

/**
 * Binds the toolbar of `kind` inside root and returns { redraw(pairs), stop() }.
 * ctx: { esc, cname, paName, countries (codes in name order), getCc(), setCc(cc), network: { all, q },
 *        countryHref(cc), projectHref(id), projectsHref({ country, area }) }
 */
export function mount(kind, root, ctx) {
  const $ = (id) => root.querySelector(`#${id}`);
  const tipEl = $("ov-tip");
  const moveTip = (ev) => {
    const pad = 14;
    const r = tipEl.getBoundingClientRect();
    let x = ev.clientX + pad;
    let y = ev.clientY + pad;
    if (x + r.width > innerWidth - 8) x = ev.clientX - r.width - pad;
    if (y + r.height > innerHeight - 8) y = ev.clientY - r.height - pad;
    tipEl.style.left = `${Math.max(8, x)}px`;
    tipEl.style.top = `${Math.max(8, y)}px`;
  };
  const showTip = (html, ev) => { tipEl.innerHTML = html; tipEl.hidden = false; moveTip(ev); };
  const tipAt = (el, html) => { const r = el.getBoundingClientRect(); showTip(html, { clientX: r.right, clientY: r.top }); };
  const hideTip = () => { tipEl.hidden = true; };
  const bindTip = (sel, htmlFn) => sel.on("mouseenter.tip", (e, d) => showTip(htmlFn(d), e)).on("mousemove.tip", moveTip).on("mouseleave.tip", hideTip);
  const onScroll = () => hideTip();
  addEventListener("scroll", onScroll, { passive: true });
  const chart = $("ov-chart");
  chart.addEventListener("scroll", onScroll, { passive: true });
  const detail = $("ov-detail");
  const info = $("ov-info");
  const { esc, cname, paName } = ctx;
  const countryLinks = (cc) => `<a href="${esc(ctx.countryHref(cc))}">Open the lineage of ${esc(cname(cc))}</a> (By country)`
    + ` · <a href="${esc(ctx.projectsHref({ country: cc }))}">See its projects</a>`;
  const projectHtml = (p, extra = "") => `<h3><a href="${esc(ctx.projectHref(p.id))}">${esc(p.title)}</a></h3>`
    + `<p class="small">${esc(p.prog)} ${esc(p.year ?? "")} · ${esc(paName(p.pa))} · ${RES[p.res]} · ${plural(p.deliv, "deliverable")}`
    + `${p.imp && Number.isFinite(p.imp.score) ? ` · latest impact score ${p.imp.score}/10` : ""}</p>${extra}`;
  const impText = (p) => (p.imp && Number.isFinite(p.imp.score) ? ` · impact ${p.imp.score}/10` : "");
  let impl;

  /* ---------------- Sankey ---------------- */
  if (kind === "sankey") {
    const sel = $("sk-country");
    const back = $("sk-back");
    let ps = [];
    sel.innerHTML = `<option value="">— none —</option>` + ctx.countries.map((cc) => `<option value="${esc(cc)}">${esc(cname(cc))}</option>`).join("");
    const drill = (cc) => { ctx.setCc(cc || ""); render(); };
    sel.addEventListener("change", () => drill(sel.value));
    back.addEventListener("click", () => { drill(""); sel.focus(); });
    function render() {
      const drilled = ctx.getCc() || null;
      chart.innerHTML = "";
      hideTip();
      sel.value = drilled || "";
      back.hidden = !drilled;
      detail.innerHTML = drilled
        ? `<p><strong>${esc(cname(drilled))}</strong>, split by CSR year (all other countries merged). ${countryLinks(drilled)}</p>`
        : `<p class="muted small">Click a country block (or choose it above) to split it by CSR year.</p>`;
      if (!ps.length) { chart.innerHTML = `<p class="empty ov-empty">No pair in the selected period.</p>`; info.textContent = ""; return; }
      const col0 = (x) => (drilled ? (x.c.cc === drilled ? `y:${x.c.year}` : "c:other") : `c:${x.c.cc}`);
      const keys = [col0, (x) => `pa:${x.p.pa}`, (x) => `pg:${x.p.prog}`, (x) => `o:${x.p.res}`];
      const nodeMeta = (id) => {
        const [t, v] = id.split(/:(.*)/s);
        if (t === "c") return v === "other" ? { name: "Other countries", short: "Others", color: "var(--viz-edge)", col: 0, click: "back" }
          : { name: cname(v), color: "var(--accent)", col: 0, click: "drill", cc: v };
        if (t === "y") return { name: `${cname(drilled)} ${v}`, color: "var(--accent)", col: 0, click: "back" };
        if (t === "pa") return { name: paName(v), color: paColor(v), col: 1 };
        if (t === "pg") return { name: v, color: progColor(v), col: 2 };
        return { name: RES[+v], color: resColor(+v), col: 3 };
      };
      const counts = new Map();
      ps.forEach((x) => keys.forEach((k) => { const id = k(x); counts.set(id, (counts.get(id) || 0) + 1); }));
      const ids = Array.from(counts.keys()).map((id) => ({ id, ...nodeMeta(id), n: counts.get(id) }));
      ids.sort((a, b) => {
        if (a.col !== b.col) return a.col - b.col;
        if (a.col === 0 && drilled) { if (a.id === "c:other") return 1; if (b.id === "c:other") return -1; return a.id.localeCompare(b.id); }
        if (a.col === 3) return a.id.localeCompare(b.id);
        return b.n - a.n;
      });
      const nodeIndex = new Map(ids.map((d, i) => [d.id, i]));
      const nodePairs = ids.map(() => []);
      const linkMap = new Map();
      ps.forEach((x) => {
        const k = keys.map((f) => f(x));
        k.forEach((id) => nodePairs[nodeIndex.get(id)].push(x));
        for (let i = 0; i < 3; i++) {
          const lk = `${k[i]}|${k[i + 1]}`;
          if (!linkMap.has(lk)) linkMap.set(lk, { source: k[i], target: k[i + 1], pairs: [] });
          linkMap.get(lk).pairs.push(x);
        }
      });
      const n0 = ids.filter((d) => d.col === 0).length;
      const W = Math.max(chart.clientWidth, 720);
      const H = Math.max(380, Math.min(760, n0 * 24 + 40));
      const ML = 124;
      const MR = 96;
      const sk = sankey().nodeId((d) => d.id).nodeWidth(12).nodePadding(n0 > 14 ? 5 : 12).nodeAlign(sankeyJustify).nodeSort(null)
        .extent([[ML, 8], [W - MR, H - 8]]);
      const graph = sk({ nodes: ids.map((d, i) => ({ ...d, pairs: nodePairs[i] })), links: Array.from(linkMap.values()).map((l) => ({ ...l, value: l.pairs.length })) });
      const svg = d3.select(chart).append("svg").attr("width", W).attr("height", H).attr("class", "sk")
        .attr("role", "img").attr("aria-label", `Sankey of ${ps.length} recommendation–project pairs: country, policy area, programme, outcome`);
      const lk = svg.append("g").selectAll("path").data(graph.links).join("path").attr("class", "lk")
        .attr("d", sankeyLinkHorizontal()).style("stroke", (d) => d.target.color).attr("stroke-width", (d) => Math.max(1, d.width));
      lk.on("mouseenter", function (e, d) { svg.classed("dim", true); d3.select(this).classed("hl", true); showTip(`<strong>${esc(d.source.name)} → ${esc(d.target.name)}</strong><div>${statLine(d.pairs)}</div>`, e); })
        .on("mousemove", moveTip).on("mouseleave", function () { svg.classed("dim", false); d3.select(this).classed("hl", false); hideTip(); });
      const nd = svg.append("g").selectAll("g").data(graph.nodes).join("g").attr("class", (d) => `nd-g${d.click ? " click" : ""}`);
      nd.append("rect").attr("class", "nd").attr("x", (d) => d.x0).attr("y", (d) => d.y0).attr("width", (d) => d.x1 - d.x0)
        .attr("height", (d) => Math.max(1, d.y1 - d.y0)).style("fill", (d) => d.color);
      const hl = (on, d) => { svg.classed("dim", on); lk.classed("hl", (l) => on && (l.source === d || l.target === d)); };
      const hint = (d) => (d.click === "drill" ? `<div class="muted">Click to split by CSR year</div>` : d.click === "back" ? `<div class="muted">Click to return to all countries</div>` : "");
      nd.on("mouseenter", (e, d) => { hl(true, d); showTip(`<strong>${esc(d.name)}</strong><div>${statLine(d.pairs)}</div>${hint(d)}`, e); })
        .on("mousemove", moveTip).on("mouseleave", (e, d) => { hl(false, d); hideTip(); });
      const act = (d) => { if (d.click === "drill") drill(d.cc); else if (d.click === "back") drill(""); };
      nd.filter((d) => d.click).attr("tabindex", 0).attr("role", "button")
        .attr("aria-label", (d) => (d.click === "drill" ? `Drill into ${d.name}, ${d.value} pairs` : `Back to all countries (${d.name})`))
        .on("click", (e, d) => { hideTip(); act(d); })
        .on("keydown", (e, d) => { if (e.key === "Enter" || e.key === " ") { e.preventDefault(); act(d); refocus(); } })
        .on("focus", (e, d) => { hl(true, d); tipAt(e.currentTarget, `<strong>${esc(d.name)}</strong><div>${statLine(d.pairs)}</div>${hint(d)}`); })
        .on("blur", (e, d) => { hl(false, d); hideTip(); });
      nd.each(function (d) {
        const t = d3.select(this).append("text").attr("class", "halo").attr("dy", "0.35em").attr("y", (d.y0 + d.y1) / 2).style("pointer-events", "none");
        const h = d.y1 - d.y0;
        if (d.col === 0) { t.attr("x", d.x0 - 6).attr("text-anchor", "end"); fitText(t, `${d.short || d.name} ${d.value}`, ML - 10); }
        else if (d.col === 3) { t.attr("x", d.x1 + 6); fitText(t, `${d.name} ${d.value}`, MR - 8); }
        else { t.attr("x", d.x1 + 6); fitText(t, `${d.name} · ${d.value}`, d.col === 1 ? 190 : 120); }
        if (h < 9 && d.col !== 0 && d.col !== 3) t.remove();
        else if (h < 7) t.style("font-size", "10.5px");
      });
      const lastY = {};
      nd.select("text").each(function (d) { // drop labels colliding with the one above (still on hover)
        const cy = (d.y0 + d.y1) / 2;
        if (lastY[d.col] !== undefined && cy - lastY[d.col] < 11.5) { this.remove(); return; }
        lastY[d.col] = cy;
      });
      const projN = new Set(ps.map((x) => x.p.i)).size;
      info.textContent = drilled ? `${cname(drilled)} split by CSR year` : `${n0} countries · ${fmt(ps.length)} pairs · ${fmt(projN)} projects`;
    }
    // After a keyboard drill the clicked block is gone: keep the focus in the chart.
    const refocus = () => { (chart.querySelector("g.click") || sel).focus(); };
    impl = { redraw(pairs) { ps = pairs; render(); }, stop() {} };
  }

  /* ---------------- Sunburst ---------------- */
  if (kind === "sunburst") {
    const sel = $("sb-country");
    let ps = [];
    function render() {
      chart.innerHTML = "";
      hideTip();
      if (!ps.length) { chart.innerHTML = `<p class="empty ov-empty">No pair in the selected period.</p>`; info.textContent = ""; detail.innerHTML = ""; sel.innerHTML = ""; return; }
      const byC = d3.group(ps, (x) => x.c.cc);
      const data = { name: "EU", kind: "root", ps, children: ctx.countries.filter((cc) => byC.has(cc)).map((cc) => {
        const cps = byC.get(cc);
        return { name: cname(cc), kind: "country", cc, ps: cps, children: Array.from(d3.group(cps, (x) => x.p.pa)).map(([pa, pps]) => ({
          name: paName(pa), kind: "pa", pa, cc, ps: pps, children: Array.from(d3.group(pps, (x) => x.p)).map(([p, xs]) => ({ name: p.title, kind: "proj", p, ps: xs, value: xs.length })),
        })) };
      }) };
      const hier = d3.hierarchy(data).sum((d) => d.value || 0).sort((a, b) => b.value - a.value);
      const root = d3.partition().size([2 * Math.PI, hier.height + 1])(hier);
      root.each((d) => { d.current = d; });
      const size = Math.max(300, Math.min(chart.clientWidth - 2, 700));
      const radius = size / 8;
      const arc = d3.arc().startAngle((d) => d.x0).endAngle((d) => d.x1).padAngle((d) => Math.min((d.x1 - d.x0) / 2, 0.004)).padRadius(radius * 1.5)
        .innerRadius((d) => d.y0 * radius).outerRadius((d) => Math.max(d.y0 * radius, d.y1 * radius - 1));
      const ci = new Map();
      root.children.forEach((c, i) => ci.set(c, i));
      const color = (d) => (d.depth === 1 ? (ci.get(d) % 2 ? "var(--ov-c2)" : "var(--ov-c1)") : d.depth === 2 ? paColor(d.data.pa) : resColor(d.data.p.res));
      const arcVisible = (d) => d.y1 <= 4 && d.y0 >= 1 && d.x1 > d.x0;
      const labelVisible = (d) => d.y1 <= 4 && d.y0 >= 1 && (d.x1 - d.x0) * ((d.y0 + d.y1) / 2 * radius) > 12.5;
      const labelTransform = (d) => { const x = (d.x0 + d.x1) / 2 * 180 / Math.PI; const y = (d.y0 + d.y1) / 2 * radius; return `rotate(${x - 90}) translate(${y},0) rotate(${x < 180 ? 0 : 180})`; };
      const svg = d3.select(chart).append("svg").attr("width", size).attr("height", size).attr("viewBox", [-size / 2, -size / 2, size, size]).attr("class", "sb")
        .attr("aria-label", "Sunburst of the pairs by country, policy area and project");
      const g = svg.append("g");
      const path = g.append("g").selectAll("path").data(root.descendants().slice(1)).join("path")
        .style("fill", color).attr("fill-opacity", (d) => (arcVisible(d.current) ? 1 : 0))
        .attr("pointer-events", (d) => (arcVisible(d.current) ? "auto" : "none")).attr("d", (d) => arc(d.current)).style("cursor", "pointer");
      path.on("click", (e, d) => { hideTip(); if (d.children) clicked(d); else showProject(d); });
      bindTip(path, (d) => {
        const trail = d.ancestors().reverse().slice(1).map((a) => esc(truncChars(a.data.name, 40)));
        if (d.data.kind === "proj") {
          const p = d.data.p;
          return `<strong>${esc(p.title)}</strong><div class="muted">${trail.slice(0, 2).join(" › ")}</div>`
            + `<div>${esc(p.prog)} ${esc(p.year ?? "")} · ${RES[p.res]} · ${plural(p.deliv, "deliverable")}${impText(p)}</div><div>${statLine(d.data.ps)}</div><div class="muted">Click for the project link</div>`;
        }
        return `<strong>${trail.join(" › ")}</strong><div>${statLine(d.data.ps)}</div><div class="muted">${pct(d.value, root.value)} of all pairs · click to zoom in</div>`;
      });
      const maxChars = Math.max(3, Math.floor((radius - 8) / 5.6));
      const label = g.append("g").attr("pointer-events", "none").attr("text-anchor", "middle").selectAll("text").data(root.descendants().slice(1)).join("text")
        .attr("class", "halo").attr("dy", "0.35em").attr("opacity", (d) => +labelVisible(d.current)).attr("transform", (d) => labelTransform(d.current))
        .text((d) => truncChars(d.data.name, maxChars));
      const center = svg.append("g").attr("class", "center").attr("tabindex", 0).attr("role", "button");
      center.append("circle").attr("r", radius).style("fill", "var(--card)").style("stroke", "var(--border)");
      const c1 = center.append("text").attr("text-anchor", "middle").attr("dy", "-0.2em").style("font-size", "13px").style("font-weight", "600");
      const c2 = center.append("text").attr("text-anchor", "middle").attr("dy", "1.2em").style("font-size", "11px").attr("class", "sb-sub");
      let focusNode = root;
      const ccOf = (p) => (p.depth === 1 ? p.data.cc : p.depth === 2 ? p.parent.data.cc : "");
      function setCenter(p) {
        fitText(c1, p.depth ? p.data.name : "EU", radius * 1.7);
        fitText(c2, p.depth && radius > 60 ? `${fmt(p.value)} pairs · zoom out` : `${fmt(p.value)} pairs`, radius * 1.75);
        center.attr("aria-label", p.depth ? `Zoom out from ${p.data.name}` : "EU, all countries");
        info.textContent = p.depth ? p.ancestors().reverse().map((a) => a.data.name).join(" › ") : `${root.children.length} countries · ${fmt(root.value)} pairs`;
        sel.value = ccOf(p);
        const cc = ccOf(p);
        detail.innerHTML = !cc ? `<p class="muted small">Click a country to zoom in, then a policy area; click the centre to zoom out. Click a project slice for its link.</p>`
          : `<p><strong>${esc(p.ancestors().reverse().slice(1).map((a) => a.data.name).join(" › "))}</strong>: ${statLine(p.data.ps)}. ${countryLinks(cc)}`
            + (p.depth === 2 && p.data.pa !== "none" ? ` · <a href="${esc(ctx.projectsHref({ country: cc, area: p.data.pa }))}">Projects of ${esc(cname(cc))} in this policy area</a>` : "") + `</p>`;
      }
      function showProject(d) {
        const p = d.data.p;
        detail.innerHTML = projectHtml(p, `<p class="small">Linked to ${plural(d.data.ps.length, "recommendation")} of ${esc(cname(d.parent.data.cc))}. ${countryLinks(d.parent.data.cc)}</p>`);
      }
      const up = () => { if (focusNode.parent) clicked(focusNode.parent); };
      center.on("click", up).on("keydown", (e) => { if (e.key === "Enter" || e.key === " ") { e.preventDefault(); up(); } });
      function clicked(p, animate = true) {
        focusNode = p;
        root.each((d) => {
          d.target = {
            x0: Math.max(0, Math.min(1, (d.x0 - p.x0) / (p.x1 - p.x0))) * 2 * Math.PI,
            x1: Math.max(0, Math.min(1, (d.x1 - p.x0) / (p.x1 - p.x0))) * 2 * Math.PI,
            y0: Math.max(0, d.y0 - p.depth), y1: Math.max(0, d.y1 - p.depth) };
        });
        const cc = ccOf(p);
        if (cc !== (ctx.getCc() || "")) ctx.setCc(cc);
        if (!animate || !DUR) {
          root.each((d) => { d.current = d.target; });
          path.attr("fill-opacity", (d) => (arcVisible(d.current) ? 1 : 0)).attr("pointer-events", (d) => (arcVisible(d.current) ? "auto" : "none")).attr("d", (d) => arc(d.current));
          label.attr("opacity", (d) => +labelVisible(d.current)).attr("transform", (d) => labelTransform(d.current));
          setCenter(p);
          return;
        }
        const t = svg.transition().duration(DUR);
        path.transition(t).tween("data", (d) => { const i = d3.interpolate(d.current, d.target); return (tt) => { d.current = i(tt); }; })
          .filter(function (d) { return +this.getAttribute("fill-opacity") || arcVisible(d.target); })
          .attr("fill-opacity", (d) => (arcVisible(d.target) ? 1 : 0)).attr("pointer-events", (d) => (arcVisible(d.target) ? "auto" : "none"))
          .attrTween("d", (d) => () => arc(d.current));
        label.filter(function (d) { return +this.getAttribute("opacity") || labelVisible(d.target); }).transition(t)
          .attr("opacity", (d) => +labelVisible(d.target)).attrTween("transform", (d) => () => labelTransform(d.current));
        setCenter(p);
      }
      sel.innerHTML = `<option value="">All countries</option>` + root.children.map((c) => `<option value="${esc(c.data.cc)}">${esc(c.data.name)}</option>`).join("");
      sel.onchange = () => { const c = root.children.find((d) => d.data.cc === sel.value); clicked(c || root); };
      const start = root.children.find((d) => d.data.cc === ctx.getCc());
      if (start) clicked(start, false); else { setCenter(root); if (ctx.getCc()) ctx.setCc(""); }
    }
    impl = { redraw(pairs) { ps = pairs; render(); }, stop() {} };
  }

  /* ---------------- Network ---------------- */
  if (kind === "network") {
    const expanded = new Set();
    const posCache = new Map();
    const st = ctx.network; // { all, q }, kept for the session
    let refit = false;
    let focus = null;
    let sim = null;
    let curNodes = [];
    let svg;
    let zoom;
    let W;
    let H;
    let ps = [];
    let apply = () => {};
    const search = $("nw-search");
    const all = $("nw-all");
    search.value = st.q || "";
    all.checked = st.all !== false;
    const cc0 = ctx.getCc();
    if (cc0) { expanded.add(cc0); focus = `c:${cc0}`; }
    let tm;
    search.addEventListener("input", () => { clearTimeout(tm); tm = setTimeout(() => { st.q = search.value.trim(); render(); }, 200); });
    all.addEventListener("change", () => { st.all = all.checked; render(); });
    $("nw-reset").addEventListener("click", () => {
      expanded.clear(); focus = null; st.q = ""; search.value = ""; st.all = true; all.checked = true; posCache.clear(); ctx.setCc(""); render();
    });
    $("nw-fit").addEventListener("click", () => fit(true));
    const query = () => (st.q || "").toLowerCase();
    const matches = (n) => {
      const q = query();
      if (!q) return false;
      return n.type === "c" ? (cname(n.cc).toLowerCase().includes(q) || n.cc.toLowerCase() === q) : n.p.title.toLowerCase().includes(q);
    };
    function fit(animated) {
      if (!curNodes.length || !svg) return;
      const x0 = d3.min(curNodes, (d) => d.x - d.r); const x1 = d3.max(curNodes, (d) => d.x + d.r);
      const y0 = d3.min(curNodes, (d) => d.y - d.r); const y1 = d3.max(curNodes, (d) => d.y + d.r);
      const k = Math.min(2, 0.92 * Math.min(W / (x1 - x0 || 1), H / (y1 - y0 || 1)));
      const t = d3.zoomIdentity.translate(W / 2 - k * (x0 + x1) / 2, H / 2 - k * (y0 + y1) / 2).scale(k);
      if (animated && DUR) svg.transition().duration(DUR).call(zoom.transform, t); else svg.call(zoom.transform, t);
    }
    function showDetail(nodes, links) {
      if (!focus) {
        detail.innerHTML = `<p class="muted small">Click a country disc to highlight its projects, or a project dot for its details and link.</p>`;
        return;
      }
      const n = nodes.find((x) => x.id === focus);
      if (!n) { detail.innerHTML = ""; return; }
      if (n.type === "c") {
        detail.innerHTML = `<p><strong>${esc(cname(n.cc))}</strong>: ${plural(n.pairs, "pair")} · ${plural(n.projects.size, "project")} · ${fmt(n.multi)} shared with other countries. ${countryLinks(n.cc)}</p>`;
        return;
      }
      const cs = [...n.countries].sort((a, b) => cname(a).localeCompare(cname(b)));
      detail.innerHTML = projectHtml(n.p, `<p class="small">Linked to CSRs of ${cs.map((cc) => `<a href="${esc(ctx.countryHref(cc))}">${esc(cname(cc))}</a>`).join(", ")} (${plural(n.pairs, "pair")}).</p>`);
    }
    function render() {
      curNodes.forEach((n) => posCache.set(n.id, { x: n.x, y: n.y }));
      const keepT = svg && svg.node().isConnected ? d3.zoomTransform(svg.node()) : null;
      if (sim) sim.stop();
      chart.innerHTML = "";
      hideTip();
      const showAll = st.all !== false;
      W = chart.clientWidth;
      H = W < 600 ? 560 : (showAll ? 820 : 620);
      chart.style.height = `${H + 2}px`;
      if (!ps.length) { chart.innerHTML = `<p class="empty ov-empty">No pair in the selected period.</p>`; info.textContent = ""; detail.innerHTML = ""; curNodes = []; svg = null; return; }
      const cN = new Map(); const pN = new Map(); const lM = new Map();
      ps.forEach((x) => {
        const cc = x.c.cc;
        if (!cN.has(cc)) cN.set(cc, { id: `c:${cc}`, type: "c", cc, pairs: 0, projects: new Set() });
        const c = cN.get(cc); c.pairs++; c.projects.add(x.p.i);
        if (!pN.has(x.p.i)) pN.set(x.p.i, { id: `p:${x.p.i}`, type: "p", p: x.p, pairs: 0, countries: new Set() });
        const p = pN.get(x.p.i); p.pairs++; p.countries.add(cc);
        const k = `${x.p.i}|${cc}`;
        if (!lM.has(k)) lM.set(k, { source: `p:${x.p.i}`, target: `c:${cc}`, n: 0 });
        lM.get(k).n++;
      });
      const countries = Array.from(cN.values());
      countries.forEach((c) => { c.r = 9 + Math.sqrt(c.pairs) * 1.7; c.multi = 0; });
      const projAll = Array.from(pN.values());
      projAll.forEach((p) => { p.r = Math.min(8, 3.2 + Math.sqrt(p.pairs) * 0.9); p.multi = p.countries.size > 1; if (p.multi) p.countries.forEach((cc) => cN.get(cc).multi++); });
      const visP = projAll.filter((p) => p.multi || showAll || [...p.countries].some((cc) => expanded.has(cc)) || matches(p));
      const nodes = countries.concat(visP);
      const vis = new Set(nodes.map((n) => n.id));
      if (focus && !vis.has(focus)) focus = null;
      const links = Array.from(lM.values()).filter((l) => vis.has(l.source)).map((l) => ({ ...l, multi: pN.get(+l.source.slice(2)).multi }));
      let fresh = true;
      const ring = Math.min(W, H) * 0.42;
      countries.sort((a, b) => a.cc.localeCompare(b.cc)).forEach((c, i) => {
        const pc = posCache.get(c.id);
        if (pc) { c.x = pc.x; c.y = pc.y; fresh = false; } else { const a = i / countries.length * 2 * Math.PI; c.x = Math.cos(a) * ring; c.y = Math.sin(a) * ring; }
      });
      visP.forEach((p) => {
        const pc = posCache.get(p.id);
        if (pc) { p.x = pc.x; p.y = pc.y; return; }
        const cs = [...p.countries].map((cc) => cN.get(cc));
        p.x = d3.mean(cs, (c) => c.x) + (Math.random() - 0.5) * 30; p.y = d3.mean(cs, (c) => c.y) + (Math.random() - 0.5) * 30;
      });
      curNodes = nodes;
      sim = d3.forceSimulation(nodes)
        .force("link", d3.forceLink(links).id((d) => d.id).distance((l) => (l.multi ? 70 : 14 + l.target.r)).strength((l) => (l.multi ? 0.12 : 0.7)))
        .force("charge", d3.forceManyBody().strength((d) => (d.type === "c" ? -320 : -14)).distanceMax(400))
        .force("collide", d3.forceCollide((d) => d.r + 1.5))
        .force("x", d3.forceX(0).strength(0.05)).force("y", d3.forceY(0).strength(0.05))
        .stop();
      svg = d3.select(chart).append("svg").attr("width", W).attr("height", H).attr("class", "nw")
        .attr("aria-label", `Network of ${countries.length} countries and ${visP.length} projects`);
      svg.append("rect").attr("class", "nw-bg").attr("width", W).attr("height", H);
      const gRoot = svg.append("g");
      zoom = d3.zoom().scaleExtent([0.2, 5]).on("zoom", (e) => { gRoot.attr("transform", e.transform); hideTip(); });
      svg.call(zoom).on("dblclick.zoom", null);
      svg.on("click", (e) => { if (e.target.classList?.contains("nw-bg") && focus) { const wasC = focus.startsWith("c:"); focus = null; if (wasC) ctx.setCc(""); apply(); } });
      const lk = gRoot.append("g").selectAll("line").data(links).join("line").attr("stroke-width", (l) => 0.6 + Math.sqrt(l.n) * 0.5);
      const pn = gRoot.append("g").selectAll("circle").data(visP).join("circle").attr("class", "pn")
        .attr("r", (d) => d.r).style("fill", (d) => progColor(d.p.prog)).style("stroke", (d) => resColor(d.p.res)).attr("stroke-width", 2);
      const cn = gRoot.append("g").selectAll("g").data(countries).join("g").attr("class", "cn").attr("tabindex", 0).attr("role", "button")
        .attr("aria-label", (d) => `${cname(d.cc)}: ${d.pairs} pairs, ${d.projects.size} projects. ${expanded.has(d.cc) ? "Press Enter to clear the highlight" : "Press Enter to highlight its projects"}`)
        .attr("aria-pressed", (d) => String(focus === d.id));
      cn.append("circle").attr("r", (d) => d.r).style("fill", "var(--ov-cnode)")
        .style("stroke", (d) => (expanded.has(d.cc) ? "var(--accent)" : "var(--card)")).attr("stroke-width", (d) => (expanded.has(d.cc) ? 3.5 : 1.5));
      cn.append("text").attr("text-anchor", "middle").attr("dy", "0.35em").text((d) => d.cc);
      const cTip = (d) => `<strong>${esc(cname(d.cc))}</strong><div>${fmt(d.pairs)} pairs · ${d.projects.size} projects · ${d.multi} shared with other countries</div>`
        + `<div class="muted">${expanded.has(d.cc) ? "Click to clear the highlight" : "Click to highlight its projects"}</div>`;
      const pTip = (d) => { const p = d.p; return `<strong>${esc(p.title)}</strong><div class="muted">${esc(p.prog)} ${esc(p.year ?? "")} · ${esc(paName(p.pa))}</div>`
        + `<div>${RES[p.res]} · ${plural(p.deliv, "deliverable")}${impText(p)}</div><div><span class="muted">Linked to CSRs of:</span> ${esc([...d.countries].map(cname).join(", "))} (${plural(d.pairs, "pair")})</div>`; };
      bindTip(cn, cTip); bindTip(pn, pTip);
      const toggleC = (d, key) => {
        hideTip();
        if (expanded.has(d.cc)) { expanded.delete(d.cc); if (focus === d.id) focus = null; } else { expanded.add(d.cc); focus = d.id; }
        ctx.setCc(focus?.startsWith("c:") ? focus.slice(2) : "");
        refit = !REDUCED && !key;
        render();
        if (key) chart.querySelector(`g.cn[data-cc="${d.cc}"]`)?.focus();
      };
      cn.attr("data-cc", (d) => d.cc)
        .on("click", (e, d) => { if (e.defaultPrevented) return; toggleC(d, false); })
        .on("keydown", (e, d) => { if (e.key === "Enter" || e.key === " ") { e.preventDefault(); toggleC(d, true); } })
        .on("focus", (e, d) => tipAt(e.currentTarget, cTip(d))).on("blur", hideTip);
      pn.on("click", (e, d) => {
        if (e.defaultPrevented) return;
        const wasC = focus?.startsWith("c:");
        focus = focus === d.id ? null : d.id;
        if (wasC) ctx.setCc("");
        apply();
      });
      const drag = d3.drag()
        .on("start", (e, d) => { if (!e.active) sim.alphaTarget(0.2).restart(); d.fx = d.x; d.fy = d.y; })
        .on("drag", (e, d) => { d.fx = e.x; d.fy = e.y; })
        .on("end", (e, d) => { if (!e.active) sim.alphaTarget(0); d.fx = null; d.fy = null; });
      cn.call(drag); pn.call(drag);
      function draw() {
        lk.attr("x1", (l) => l.source.x).attr("y1", (l) => l.source.y).attr("x2", (l) => l.target.x).attr("y2", (l) => l.target.y);
        pn.attr("cx", (d) => d.x).attr("cy", (d) => d.y);
        cn.attr("transform", (d) => `translate(${d.x},${d.y})`);
      }
      apply = function () {
        let keep = null;
        if (focus) {
          keep = new Set([focus]);
          links.forEach((l) => { if (l.source.id === focus) keep.add(l.target.id); if (l.target.id === focus) keep.add(l.source.id); });
        }
        cn.classed("faded", (d) => keep && !keep.has(d.id)); pn.classed("faded", (d) => keep && !keep.has(d.id));
        lk.classed("faded", (l) => keep && !(l.source.id === focus || l.target.id === focus));
        pn.classed("match", (d) => matches(d)); cn.select("circle").classed("match", (d) => matches(d));
        cn.attr("aria-pressed", (d) => String(focus === d.id));
        pn.classed("sel", (d) => focus === d.id);
        const nm = query() ? nodes.filter(matches).length : 0;
        info.textContent = `${countries.length} countries · ${visP.length} of ${projAll.length} projects shown`
          + (query() ? ` · ${nm} match${nm === 1 ? "" : "es"}` : "") + (focus ? " · click the background to clear the highlight" : "");
        showDetail(nodes, links);
      };
      sim.on("tick", draw).alphaMin(0.02);
      sim.on("end", () => { if (refit) { refit = false; fit(true); } });
      if (fresh || REDUCED) {
        sim.alpha(1);
        for (let i = 0; i < 320; i++) sim.tick();
        draw();
        if (fresh || !keepT || refit) { refit = false; fit(false); } else svg.call(zoom.transform, keepT);
      } else {
        for (let i = 0; i < 40; i++) sim.tick(); // settle the new nodes a little before the first paint
        draw();
        if (keepT) svg.call(zoom.transform, keepT); else fit(false);
        sim.alpha(0.5).restart();
      }
      apply();
    }
    impl = { redraw(pairs) { ps = pairs; render(); }, stop() { if (sim) sim.stop(); } };
  }

  return {
    redraw: (pairs) => impl.redraw(pairs),
    stop() { impl.stop(); hideTip(); removeEventListener("scroll", onScroll); },
    hideTip,
  };
}
