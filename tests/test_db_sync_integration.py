"""db-sync against a real PostgreSQL (schema installed with the migrations), run by hand:

    TSI_TEST_DATABASE_URL=postgresql://... [TSI_TEST_DB_SCHEMA=tsi_explorer] [TSI_TEST_DB_ROLE=service_role] \
        pytest -m integration tests/test_db_sync_integration.py

Nothing is changed in the database: the load below fails on purpose after its deletes, and must roll back.
"""
import os

import pytest

from tsi_agent.db_sync import check, load
from tsi_agent.supabase_sync import SyncError

pytestmark = pytest.mark.integration
URL = os.environ.get("TSI_TEST_DATABASE_URL", "")
SCHEMA = os.environ.get("TSI_TEST_DB_SCHEMA", "public")
ROLE = os.environ.get("TSI_TEST_DB_ROLE", "")


@pytest.fixture
def counts():
    if not URL:
        pytest.skip("TSI_TEST_DATABASE_URL not set")
    import psycopg
    from psycopg import sql

    def read():
        with psycopg.connect(URL) as conn, conn.cursor() as cur:
            if ROLE:
                cur.execute(sql.SQL("set role {}").format(sql.Identifier(ROLE)))
            out = {}
            for table in ("projects", "csrs", "csr_parts", "project_csr_links", "anomalies", "impact_reports"):
                cur.execute(sql.SQL("select count(*) from {}.{}").format(sql.Identifier(SCHEMA), sql.Identifier(table)))
                out[table] = cur.fetchone()[0]
            return out
    return read


def test_check_finds_every_table_writable(counts):
    report = check(URL, SCHEMA, ROLE)
    assert report["missing"] == [] and report["not_writable"] == []


def test_failed_load_rolls_back_after_its_deletes(counts):
    before = counts()
    rows = {"projects": [{"project_id": "it-p1", "title": "T"}], "csr_documents": [], "csrs": [], "csr_parts": [],
            "project_csr_links": [{"project_id": "it-p1", "part_id": "csr-XX-1999-1", "level": "direct",
                                   "rationale": "r", "basis": "full_text"}],   # unknown part: foreign key violation
            "anomalies": []}
    with pytest.raises(SyncError, match="insert project_csr_links"):
        load(rows, URL, SCHEMA, ROLE, force=True)   # past the size safeguard, up to the failing insert
    assert counts() == before


def test_much_smaller_load_is_refused_before_any_change(counts):
    before = counts()
    rows = {"projects": [{"project_id": "it-p1", "title": "T"}], "csrs": [{"csr_id": "csr-XX-1999-1", "country": "HR",
            "year": 1999, "number": "1", "title": "T", "text": "T"}]}
    if before["projects"] < 4:
        pytest.skip("database almost empty")
    with pytest.raises(SyncError, match="chargement refuse"):
        load(rows, URL, SCHEMA, ROLE)
    assert counts() == before
