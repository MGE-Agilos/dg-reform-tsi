import json

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
    assert list(rows) == list(TABLES) == ["projects", "csrs", "csr_parts", "project_csr_links", "anomalies"]
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
    rows = {"projects": [{"project_id": f"p{i}"} for i in range(1201)], "csrs": [], "csr_parts": [],
            "project_csr_links": [{"project_id": "p1", "part_id": "x"}], "anomalies": [{"type": "t"}]}
    calls, client = recorder()
    summary = push(rows, "https://abc.supabase.co", "service-key", client=client, backup=tmp_path / "rows.json")
    methods = [(m, p.removeprefix("/rest/v1/")) for m, p, *_ in calls]
    assert methods[:5] == [("DELETE", "project_csr_links"), ("DELETE", "csr_parts"), ("DELETE", "csrs"),
                           ("DELETE", "projects"), ("DELETE", "anomalies")]
    assert methods[5:] == [("POST", "projects")] * 3 + [("POST", "project_csr_links"), ("POST", "anomalies")]
    assert [c[5] for c in calls[5:8]] == [500, 500, 201]
    assert calls[0][2] == "project_id=not.is.null"
    assert all(c[3] == "service-key" and c[4] == "Bearer service-key" for c in calls)
    assert summary == {"projects": 1201, "csrs": 0, "csr_parts": 0, "project_csr_links": 1, "anomalies": 1}
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
