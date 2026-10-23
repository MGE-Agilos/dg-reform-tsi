// Tests of the explorer's Admin helpers and usage tracking (pure modules, no DOM).
// Run: node --test tests/js/admin-tracking.test.mjs
import { test } from "node:test";
import assert from "node:assert/strict";
import { routeView, createTracker, newSessionId, IDLE_MS } from "../../web/tracking.js";
import * as A from "../../web/admin.js";

test("routeView: section and route parameters only, never the free-text search", () => {
  assert.deepEqual(routeView(""), { view: "projects", detail: null });
  assert.deepEqual(routeView("#project/abc%20def"), { view: "project", detail: "abc def" });
  assert.deepEqual(routeView("#projects?q=pension%20reform&country=LT&page=2"), { view: "projects", detail: "country=LT" });
  assert.deepEqual(routeView("#lineage?view=sankey&from=2019&to=2025&level=direct"), { view: "lineage", detail: "view=sankey&from=2019&to=2025&level=direct" });
  assert.deepEqual(routeView("#impact?q=secret"), { view: "impact", detail: null });
  assert.deepEqual(routeView("#admin/usage?user=a%40b.c&period=30"), { view: "admin", detail: "usage" });
  assert.equal(routeView("#csr/" + "x".repeat(500)).detail.length, 200);
  assert.equal(routeView("#Évil!/x").view, "other");
  assert.equal(routeView("#" + "a".repeat(60)).view.length, 40);
});

function clock(t0 = Date.UTC(2026, 9, 23, 8, 0, 0)) {
  let t = t0;
  return { now: () => t, tick: (ms) => { t += ms; } };
}

test("tracker: one row per finished view, same view ignored, short views dropped", () => {
  const c = clock();
  const sent = [];
  const tr = createTracker({ send: (row, o) => sent.push({ row, o }), sessionId: () => "s1234567", now: c.now });
  tr.start("projects", null);
  c.tick(10_000);
  tr.start("projects", null); // re-render of the same view: nothing
  c.tick(5_000);
  tr.start("project", "p1");
  assert.equal(sent.length, 1);
  assert.deepEqual(sent[0].row, { session_id: "s1234567", view: "projects", detail: null, started_at: "2026-10-23T08:00:00.000Z", duration_s: 15 });
  assert.equal(sent[0].o.keepalive, false);
  c.tick(1_000);
  tr.start("csrs", null); // 1 s: dropped
  assert.equal(sent.length, 1);
  c.tick(3_000);
  assert.equal(tr.end().duration_s, 3);
  assert.equal(tr.current(), null);
});

test("tracker: idle after 5 minutes stops the clock until the next input", () => {
  const c = clock();
  const sent = [];
  const tr = createTracker({ send: (row) => sent.push(row), sessionId: () => "s1234567", now: c.now });
  tr.start("dashboard", null);
  c.tick(60_000);
  tr.activity();
  c.tick(30 * 60_000); // away 30 min: only 5 min after the last input count
  tr.activity(); // back
  c.tick(20_000);
  tr.end();
  assert.equal(sent[0].duration_s, 60 + IDLE_MS / 1000 + 20);
});

test("tracker: hide sends with keepalive, show restarts the same view with a new start time", () => {
  const c = clock();
  const sent = [];
  const tr = createTracker({ send: (row, o) => sent.push({ row, o }), sessionId: () => "s1234567", now: c.now });
  tr.start("lineage", "view=sankey");
  c.tick(8_000);
  tr.hide();
  assert.equal(sent.length, 1);
  assert.equal(sent[0].o.keepalive, true);
  assert.equal(sent[0].row.duration_s, 8);
  c.tick(600_000); // hidden: not counted
  tr.show();
  assert.deepEqual(tr.current(), { view: "lineage", detail: "view=sankey" });
  c.tick(4_000);
  tr.end();
  assert.equal(sent[1].row.duration_s, 4);
  assert.equal(sent[1].row.started_at, new Date(c.now() - 4_000).toISOString());
  tr.show(); // nothing hidden any more
  assert.equal(tr.current(), null);
});

test("tracker: a failing sender never throws; session ids are hex", () => {
  const c = clock();
  const tr = createTracker({ send: () => { throw new Error("offline"); }, sessionId: () => "s1234567", now: c.now });
  tr.start("guide", null);
  c.tick(5_000);
  assert.doesNotThrow(() => tr.end());
  assert.match(newSessionId(), /^[0-9a-f]{24}$/);
});

test("admin helpers: e-mail, durations, statuses, last admin", () => {
  assert.equal(A.cleanEmail("  A.B@Example.ORG "), "a.b@example.org");
  assert.equal(A.cleanEmail("nope"), null);
  assert.equal(A.fmtDuration(45), "45 s");
  assert.equal(A.fmtDuration(720), "12 min");
  assert.equal(A.fmtDuration(3 * 3600 + 5 * 60), "3 h 05");
  assert.equal(A.fmtDuration(52 * 3600), "2 d 4 h");
  assert.equal(A.userStatus({ account: false }).key, "none");
  assert.equal(A.userStatus({ account: true, email_confirmed_at: null }).key, "unconfirmed");
  assert.equal(A.userStatus({ account: true, email_confirmed_at: "x", must_change_password: true }).key, "pending");
  assert.equal(A.userStatus({ account: true, email_confirmed_at: "x" }).key, "active");
  const users = [{ email: "b@x.y", role: "user" }, { email: "a@x.y", role: "admin" }];
  assert.deepEqual(A.sortUsers(users).map((u) => u.email), ["a@x.y", "b@x.y"]);
  assert.equal(A.wouldRemoveLastAdmin(users, "a@x.y", "user"), true);
  assert.equal(A.wouldRemoveLastAdmin([...users, { email: "c@x.y", role: "admin" }], "a@x.y", "user"), false);
  assert.equal(A.wouldRemoveLastAdmin(users, "b@x.y", "admin"), false);
});

test("admin periods and usage model (empty days filled, sorted sections, average session)", () => {
  const today = new Date(2026, 9, 23, 15, 0);
  const p7 = A.periodOf({ period: "7" }, today);
  assert.equal(A.isoDate(p7.from), "2026-10-17");
  assert.equal(A.isoDate(p7.to), "2026-10-24");
  assert.equal(A.periodOf({ period: "bogus" }, today).days, 30);
  const c = A.periodOf({ period: "custom", from: "2026-10-01", to: "2026-10-03" }, today);
  assert.equal(c.days, 3);
  assert.equal(A.periodOf({ period: "custom", from: "2026-10-05", to: "2026-10-01" }, today).days, 30);
  assert.equal(A.periodOf({ period: "custom", from: "2024-01-01", to: "2026-10-01" }, today).days, 30);
  const m = A.usageModel({ totals: { users: 2, sessions: 4, views: 10, total_s: 1200 },
    per_view: [{ view: "guide", views: 2, total_s: 100, users: 1 }, { view: "projects", views: 8, total_s: 1100, users: 2 }],
    per_day: [{ day: "2026-10-02", users: 2, sessions: 4, views: 10, total_s: 1200 }] }, c);
  assert.equal(m.totals.avg_session_s, 300);
  assert.deepEqual(m.perView.map((v) => v.view), ["projects", "guide"]);
  assert.deepEqual(m.perDay.map((d) => [d.day, d.sessions]), [["2026-10-01", 0], ["2026-10-02", 4], ["2026-10-03", 0]]);
  assert.deepEqual(A.usageModel(null, null), { totals: { users: 0, sessions: 0, views: 0, total_s: 0, avg_session_s: 0 }, perUser: [], perView: [], perDay: [] });
  assert.deepEqual(A.logExportRows([{ email: "a@x.y", view: "chat", detail: null, started_at: "t", duration_s: "7", session_id: "s" }]),
    [{ email: "a@x.y", view: "chat", section: "Assistant (chat)", detail: "", started_at: "t", duration_s: 7, session_id: "s" }]);
  assert.equal(A.logFileName("csv", today), "tsi-visits-2026-10-23.csv");
});
