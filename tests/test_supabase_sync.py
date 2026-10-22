import json
from datetime import datetime

import httpx
import openpyxl
import pytest

from tsi_agent.csr import COLUMNS
from tsi_agent.supabase_sync import TABLES, SyncError, build_rows, credentials, push

PROJECT = """---
project_id: p1
title: Public administration reform
programme: TSI
year: 2024
countries:
- HR
policy_area: public-administration-and-governance
results: achieved
deliverables: 2
detail_page: true
source_urls:
- https://example.eu/p1
csrs:
- csr-HR-2022-1.1
---

# Public administration reform

Intro text.

## Summary (AI-generated)

The project reformed things.

## Sources

- https://example.eu/p1
"""

THIN = """---
project_id: p2
title: Thin project
programme: SRSP
year: 2019
countries:
- GR
detail_page: false
---

# Thin project
"""


def make_output(tmp_path):
    out = tmp_path / "output"
    (out / "projects").mkdir(parents=True)
    (out / "projects" / "p1.md").write_text(PROJECT, encoding="utf-8")
    (out / "projects" / "p2.md").write_text(THIN, encoding="utf-8")
    (out / "csr").mkdir()
    wb = openpyxl.Workbook()
    wb.active.append(list(COLUMNS))
    for nbr, text in (("1", "Reform the administration. Cut red tape."), ("1.1", "Reform the administration."),
                      ("1.2", "Cut red tape.")):
        wb.active.append(["Croatia", "2022", nbr, "2023 - Semester Cycle (May)", text, "Public administration",
                          "-", "No", "Annual", "Some Progress"])
    wb.save(out / "csr" / "csr_full.xlsx")
    (out / "csr_links").mkdir()
    (out / "csr_links" / "p1.json").write_text(json.dumps({"project_id": "p1", "status": "ok", "basis": "full_text",
        "links": [{"csr_id": "csr-HR-2022-1.1", "level": "direct", "rationale": "R."},
                  {"csr_id": "csr-XX-2000-9.9", "level": "partial", "rationale": "unknown part"}]}), encoding="utf-8")
    (out / "csr_links" / "ghost.json").write_text(json.dumps({"project_id": "ghost", "status": "ok",
        "basis": "full_text", "links": [{"csr_id": "csr-HR-2022-1.2", "level": "partial", "rationale": "x"}]}),
        encoding="utf-8")
    return out


def test_build_rows_tables_and_projects(tmp_path):
    rows = build_rows(make_output(tmp_path), current_year=2026)
    assert list(TABLES) == ["projects", "csr_documents", "csrs", "csr_parts", "project_csr_links", "anomalies"]
    assert list(rows) == [*TABLES, "impact_reports", "csr_related_projects"] and rows["impact_reports"] == []
    assert rows["csr_related_projects"] == []
    p1, p2 = sorted(rows["projects"], key=lambda r: r["project_id"])
    assert p1 == {"project_id": "p1", "title": "Public administration reform", "programme": "TSI", "year": 2024,
                  "countries": ["HR"], "policy_area": "public-administration-and-governance", "results": "achieved",
                  "detail_page": True, "url": "https://example.eu/p1", "summary": "The project reformed things.",
                  "deliverables": 2, "csrs": ["csr-HR-2022-1.1"]}
    assert p2["summary"] is None and p2["csrs"] == [] and p2["deliverables"] == 0 and p2["url"] is None


def test_build_rows_csrs_parts_links_anomalies(tmp_path):
    rows = build_rows(make_output(tmp_path), current_year=2026)
    (csr,) = rows["csrs"]
    assert csr["csr_id"] == "csr-HR-2022-1" and csr["assessment"] == "Some Progress" and csr["assessment_year"] == 2023
    assert csr["policy_areas"] == ["Public administration"] and csr["title"] == "Croatia 2022 CSR 1"
    assert csr["doc_id"] == "csrdoc-HR-2022" and csr["source_url"].startswith("https://ec.europa.eu/")
    assert rows["csr_documents"] == [{"doc_id": "csrdoc-HR-2022", "country": "HR", "year": 2022,
                                      "title": "Croatia — 2022 country-specific recommendations",
                                      "stage": "Council recommendation", "n_recommendations": 1,
                                      "document_url": csr["source_url"]}]
    assert [p["part_id"] for p in rows["csr_parts"]] == ["csr-HR-2022-1.1", "csr-HR-2022-1.2"]
    assert rows["csr_parts"][0]["csr_id"] == "csr-HR-2022-1"
    assert rows["project_csr_links"] == [{"project_id": "p1", "part_id": "csr-HR-2022-1.1", "level": "direct",
                                          "rationale": "R.", "basis": "full_text"}]
    types = {a["type"] for a in rows["anomalies"]}
    assert "srsp_title_only" in types
    assert all(set(a) == {"project_id", "title", "programme", "year", "countries", "type", "severity", "detail",
                          "url"} for a in rows["anomalies"])
    assert all(a["project_id"] is None or isinstance(a["project_id"], str) for a in rows["anomalies"])


def test_summary_falls_back_to_intro_and_is_capped(tmp_path):
    out = make_output(tmp_path)
    (out / "projects" / "p3.md").write_text(PROJECT.replace("p1", "p3").replace(
        "## Summary (AI-generated)\n\nThe project reformed things.\n\n", "").replace("Intro text.", "x" * 2000),
        encoding="utf-8")
    p3 = next(r for r in build_rows(out, current_year=2026)["projects"] if r["project_id"] == "p3")
    assert p3["summary"] == "x" * 1500


def recorder(fail_on=None):
    calls = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append((request.method, request.url.path, request.url.query.decode(), request.headers.get("apikey"),
                      request.headers.get("authorization"), len(json.loads(request.content)) if request.content else 0))
        if fail_on and fail_on == (request.method, request.url.path):
            return httpx.Response(400, json={"message": "bad"})
        return httpx.Response(204 if request.method == "DELETE" else 201)

    return calls, httpx.Client(transport=httpx.MockTransport(handler))


def test_push_deletes_then_inserts_in_batches(tmp_path):
    rows = {"projects": [{"project_id": f"p{i}"} for i in range(1201)], "csr_documents": [{"doc_id": "d"}],
            "csrs": [], "csr_parts": [],
            "project_csr_links": [{"project_id": "p1", "part_id": "x"}], "anomalies": [{"type": "t"}]}
    calls, client = recorder()
    summary = push(rows, "https://abc.supabase.co", "service-key", client=client, backup=tmp_path / "rows.json")
    methods = [(m, p.removeprefix("/rest/v1/")) for m, p, *_ in calls]
    assert methods[:6] == [("DELETE", "project_csr_links"), ("DELETE", "csr_parts"), ("DELETE", "csrs"),
                           ("DELETE", "csr_documents"), ("DELETE", "projects"), ("DELETE", "anomalies")]
    assert methods[6:] == [("POST", "projects")] * 3 + [("POST", "csr_documents"), ("POST", "project_csr_links"),
                                                        ("POST", "anomalies")]
    assert calls[3][2] == "doc_id=not.is.null"
    assert [c[5] for c in calls[6:9]] == [500, 500, 201]
    assert calls[0][2] == "project_id=not.is.null"
    assert all(c[3] == "service-key" and c[4] == "Bearer service-key" for c in calls)
    assert summary == {"projects": 1201, "csr_documents": 1, "csrs": 0, "csr_parts": 0, "project_csr_links": 1,
                       "anomalies": 1}
    assert json.loads((tmp_path / "rows.json").read_text(encoding="utf-8"))["projects"][0] == {"project_id": "p0"}


def test_push_http_error_is_a_sync_error(tmp_path):
    calls, client = recorder(fail_on=("POST", "/rest/v1/projects"))
    with pytest.raises(SyncError, match="projects.*400.*bad"):
        push({"projects": [{"project_id": "p"}], "csrs": [], "csr_parts": [], "project_csr_links": [],
              "anomalies": []}, "https://abc.supabase.co", "k", client=client, backup=tmp_path / "r.json")


def test_credentials_from_env_or_dotenv(tmp_path, monkeypatch):
    monkeypatch.delenv("SUPABASE_URL", raising=False)
    monkeypatch.delenv("SUPABASE_SERVICE_KEY", raising=False)
    with pytest.raises(SyncError, match="SUPABASE_SERVICE_KEY"):
        credentials(tmp_path / ".env")
    (tmp_path / ".env").write_text("# c\nSUPABASE_URL=https://abc.supabase.co/\nSUPABASE_SERVICE_KEY='k1'\n",
                                   encoding="utf-8")
    assert credentials(tmp_path / ".env") == ("https://abc.supabase.co", "k1")
    monkeypatch.setenv("SUPABASE_SERVICE_KEY", "k2")
    assert credentials(tmp_path / ".env") == ("https://abc.supabase.co", "k2")


def test_links_skip_bad_level_and_duplicates(tmp_path):
    out = make_output(tmp_path)
    (out / "csr_links" / "p1.json").write_text(json.dumps({"project_id": "p1", "status": "ok", "basis": "full_text",
        "links": [{"csr_id": "csr-HR-2022-1.1", "level": "direct", "rationale": "R."},
                  {"csr_id": "csr-HR-2022-1.1", "level": "partial", "rationale": "dup"},
                  {"csr_id": "csr-HR-2022-1.2", "level": "weak", "rationale": "bad"}]}), encoding="utf-8")
    links = build_rows(out, current_year=2026)["project_csr_links"]
    assert [(l["part_id"], l["level"]) for l in links] == [("csr-HR-2022-1.1", "direct")]


def test_new_format_secret_key_uses_apikey_only(tmp_path):
    calls, client = recorder()
    push({t: [] for t in ("projects", "csrs", "csr_parts", "project_csr_links", "anomalies")},
         "https://abc.supabase.co", "sb_secret_xyz", client=client, backup=tmp_path / "r.json")
    assert calls and all(c[3] == "sb_secret_xyz" and c[4] is None for c in calls)


IMPACT = """---
project_id: p1
title: 'Public administration reform'
generated: '2026-10-07'
model: opus
overall_verdict: The reform was adopted.
confidence: Medium
impact_score: 8
impact_level: strong
---

# Impact analysis — Public administration reform

Body of the report.
"""


def make_impact(out):
    (out / "impact").mkdir(parents=True, exist_ok=True)
    (out / "impact" / "p1.md").write_text(IMPACT, encoding="utf-8")
    (out / "impact" / "noscore.md").write_text(IMPACT.replace("impact_score: 8\n", "").replace("p1", "p9"),
                                               encoding="utf-8")
    (out / "impact" / "broken.md").write_text("no front matter at all", encoding="utf-8")
    (out / "impact" / "odd.md").write_text(IMPACT.replace("p1", "p2").replace("impact_level: strong", "impact_level: huge")
                                           .replace("generated: '2026-10-07'\n", ""), encoding="utf-8")
    return out


def test_impact_rows_reads_local_reports(tmp_path):
    from tsi_agent.supabase_sync import impact_rows
    out = make_impact(tmp_path / "output")
    rows = sorted(impact_rows(out), key=lambda r: r["project_id"])
    assert [r["project_id"] for r in rows] == ["p1", "p2"]
    p1 = rows[0]
    assert p1 == {"project_id": "p1", "title": "Public administration reform", "score": 8, "level": "strong",
                  "confidence": "medium", "verdict": "The reform was adopted.",
                  "report": "# Impact analysis — Public administration reform\n\nBody of the report.\n",
                  "model": "opus", "source": "local", "created_at": "2026-10-07"}
    assert rows[1]["level"] is None and rows[1]["created_at"]  # niveau inconnu -> null ; date de fichier a defaut
    assert all(set(r) == set(p1) for r in rows)  # memes cles pour l'insertion groupee PostgREST
    assert impact_rows(tmp_path / "missing") == []


def test_impact_rows_generated_parsed_as_date_or_datetime_else_file_time(tmp_path):
    from tsi_agent.supabase_sync import impact_rows
    out = tmp_path / "output"
    (out / "impact").mkdir(parents=True)
    cases = {"a": "generated: 2026-10-07", "b": "generated: '2026-10-07T14:54:46+02:00'",
             "c": "generated: 2026-10-07 14:54:46", "d": "generated: 'last tuesday'", "e": "generated: 12"}
    for name, line in cases.items():
        (out / "impact" / f"{name}.md").write_text(IMPACT.replace("p1", name).replace("generated: '2026-10-07'", line),
                                                   encoding="utf-8")
    got = {r["project_id"]: r["created_at"] for r in impact_rows(out)}
    assert got["a"] == "2026-10-07"
    assert got["b"] == "2026-10-07T14:54:46+02:00"
    assert got["c"].startswith("2026-10-07T14:54:46")
    for name in ("d", "e"):  # texte non date : date du fichier (ISO, UTC), jamais le texte brut
        assert got[name] not in ("last tuesday", "12")
        datetime.fromisoformat(got[name])
        assert got[name].endswith("+00:00")


def test_build_rows_includes_impact_reports(tmp_path):
    out = make_impact(make_output(tmp_path))
    rows = build_rows(out, current_year=2026)
    assert [r["project_id"] for r in sorted(rows["impact_reports"], key=lambda r: r["project_id"])] == ["p1", "p2"]


def test_push_replaces_only_local_impact_reports_after_other_tables(tmp_path):
    rows = {t: [] for t in TABLES}
    rows["projects"] = [{"project_id": "p1"}]
    rows["impact_reports"] = [{"project_id": "p1", "source": "local"}]
    calls, client = recorder()
    summary = push(rows, "https://abc.supabase.co", "service-key", client=client, backup=tmp_path / "rows.json")
    methods = [(m, p.removeprefix("/rest/v1/"), q) for m, p, q, *_ in calls]
    assert methods[-2:] == [("DELETE", "impact_reports", "source=eq.local"), ("POST", "impact_reports", "")]
    assert [m for m in methods if m[1] == "impact_reports"] == methods[-2:]
    assert not any("web" in q for _, _, q in methods)
    assert calls[-1][5] == 1
    assert summary["impact_reports"] == 1
    assert json.loads((tmp_path / "rows.json").read_text(encoding="utf-8"))["impact_reports"] == rows["impact_reports"]


def test_push_without_impact_key_leaves_impact_reports_alone(tmp_path):
    calls, client = recorder()
    push({t: [] for t in TABLES}, "https://abc.supabase.co", "k", client=client, backup=tmp_path / "r.json")
    assert not any(p.endswith("impact_reports") for _, p, *_ in calls)


def test_build_rows_include_commission_recommendations_and_their_documents(tmp_path):
    from tsi_agent.csr_commission import PUBLICATION_ID
    out = make_output(tmp_path)
    (out / "csr" / "publications").mkdir()
    (out / "csr" / "publications" / f"{PUBLICATION_ID}.md").write_text(
        "## Recommendation - Croatia\n\nSource: https://ec.example/HR.pdf\n\n--- page 1 ---\n"
        "HEREBY RECOMMENDS that Croatia take action in 2026 and 2027 to:\n1. Adhere to the path.\n"
        "2. Ensure continuity.\nDone at Brussels,\n", encoding="utf-8")
    rows = build_rows(out, current_year=2026)
    assert [d["doc_id"] for d in rows["csr_documents"]] == ["csrdoc-HR-2022", "csrdoc-HR-2026"]
    hr26 = rows["csr_documents"][1]
    assert hr26["stage"] == "Commission recommendation" and hr26["n_recommendations"] == 2
    assert hr26["document_url"] == "https://ec.example/HR.pdf"
    new = [c for c in rows["csrs"] if c["year"] == 2026]
    assert [c["csr_id"] for c in new] == ["csr-HR-2026-1", "csr-HR-2026-2"]
    assert all(c["assessment"] is None and c["doc_id"] == "csrdoc-HR-2026" for c in new)
    assert [p["part_id"] for p in rows["csr_parts"] if p["year"] == 2026] == ["csr-HR-2026-1", "csr-HR-2026-2"]
    assert all(set(r) == set(rows["csrs"][0]) for r in rows["csrs"])  # memes cles (insertion groupee)


def test_build_rows_csr_related_only_known_csrs_and_projects(tmp_path):
    out = make_output(tmp_path)
    d = out / "csr_related"
    d.mkdir()
    related = [{"project_id": "p1", "relevance": "high", "scope": "same_country", "rationale": "Same reform."},
               {"project_id": "ghost", "relevance": "medium", "scope": "other_country", "rationale": "x"}]
    (d / "csr-HR-2022-1.json").write_text(json.dumps({"csr_id": "csr-HR-2022-1", "status": "ok",
                                                      "related": related}), encoding="utf-8")
    (d / "csr-XX-2026-1.json").write_text(json.dumps({"csr_id": "csr-XX-2026-1", "status": "ok",
                                                      "related": related[:1]}), encoding="utf-8")
    rows = build_rows(out, current_year=2026)
    assert rows["csr_related_projects"] == [{"csr_id": "csr-HR-2022-1", "project_id": "p1", "relevance": "high",
                                             "scope": "same_country", "rationale": "Same reform."}]


def test_push_replaces_csr_related_last_and_never_touches_reviews(tmp_path):
    rows = {t: [] for t in TABLES}
    rows["impact_reports"] = []
    rows["csr_related_projects"] = [{"csr_id": "c", "project_id": "p", "relevance": "high",
                                     "scope": "same_country", "rationale": "r"}]
    rows["link_reviews"] = [{"project_id": "p", "part_id": "x", "status": "confirmed"}]  # ignore : jamais envoye
    calls, client = recorder()
    summary = push(rows, "https://abc.supabase.co", "k", client=client, backup=tmp_path / "r.json")
    methods = [(m, p.removeprefix("/rest/v1/"), q) for m, p, q, *_ in calls]
    assert methods[-2:] == [("DELETE", "csr_related_projects", "csr_id=not.is.null"),
                            ("POST", "csr_related_projects", "")]
    assert not any(p in ("link_reviews", "testers", "link_review_latest") for _, p, _ in methods)
    assert summary["csr_related_projects"] == 1 and "warnings" not in summary


def test_push_skips_missing_csr_related_table_with_a_warning(tmp_path):
    calls = []

    def handler(request):
        calls.append((request.method, request.url.path))
        if request.url.path.endswith("csr_related_projects"):
            return httpx.Response(404, json={"code": "PGRST205", "message": "Could not find the table"})
        return httpx.Response(204 if request.method == "DELETE" else 201)
    rows = {t: [] for t in TABLES}
    rows["projects"] = [{"project_id": "p"}]
    rows["csr_related_projects"] = [{"csr_id": "c", "project_id": "p"}]
    summary = push(rows, "https://abc.supabase.co", "k", client=httpx.Client(transport=httpx.MockTransport(handler)),
                   backup=tmp_path / "r.json")
    assert ("POST", "/rest/v1/projects") in calls
    assert [c for c in calls if c[1].endswith("csr_related_projects")] == [("DELETE", "/rest/v1/csr_related_projects")]
    assert summary["csr_related_projects"] == 0 and "006_reviews_related.sql" in summary["warnings"][0]


def test_push_other_csr_related_errors_still_fail(tmp_path):
    calls, client = recorder(fail_on=("POST", "/rest/v1/csr_related_projects"))
    rows = {t: [] for t in TABLES}
    rows["csr_related_projects"] = [{"csr_id": "c", "project_id": "p"}]
    with pytest.raises(SyncError, match="csr_related_projects.*400"):
        push(rows, "https://abc.supabase.co", "k", client=client, backup=tmp_path / "r.json")


def test_protected_tables_are_not_in_the_sync_lists():
    from tsi_agent.supabase_sync import DELETE_ORDER, INSERT_ORDER, PROTECTED_TABLES
    assert set(PROTECTED_TABLES) == {"testers", "link_reviews"}
    assert not set(PROTECTED_TABLES) & {*DELETE_ORDER, *INSERT_ORDER, "impact_reports", "csr_related_projects"}
