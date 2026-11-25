import json

import pytest

from tsi_agent.db_sync import BATCH, check, database_settings, load
from tsi_agent.supabase_sync import SyncError

URL = "postgresql://u:secret@db:5432/tsi_explorer?sslmode=require"
KEYS = ("TSI_DATABASE_URL", "TSI_DB_SCHEMA", "TSI_DB_ROLE")


@pytest.fixture
def clean_env(monkeypatch):
    for key in KEYS:
        monkeypatch.delenv(key, raising=False)
    return monkeypatch


def test_settings_from_env_then_dotenv(tmp_path, clean_env):
    env = tmp_path / ".env"
    env.write_text(f"TSI_DATABASE_URL={URL}\nTSI_DB_SCHEMA=tsi_explorer\nTSI_DB_ROLE=service_role\n", encoding="utf-8")
    assert database_settings(env) == (URL, "tsi_explorer", "service_role")
    clean_env.setenv("TSI_DB_SCHEMA", "other")
    assert database_settings(env)[1] == "other"


def test_settings_defaults_and_errors(tmp_path, clean_env):
    with pytest.raises(SyncError, match="TSI_DATABASE_URL"):
        database_settings(tmp_path / ".env")
    clean_env.setenv("TSI_DATABASE_URL", URL)
    assert database_settings(tmp_path / ".env") == (URL, "public", "")
    clean_env.setenv("TSI_DB_SCHEMA", "Bad-Name")
    with pytest.raises(SyncError, match="TSI_DB_SCHEMA") as exc:
        database_settings(tmp_path / ".env")
    assert "secret" not in str(exc.value)
    clean_env.setenv("TSI_DB_SCHEMA", "public")
    clean_env.setenv("TSI_DB_ROLE", "x; drop")
    with pytest.raises(SyncError, match="TSI_DB_ROLE"):
        database_settings(tmp_path / ".env")


class FakeCursor:
    def __init__(self, conn):
        self.conn = conn
        self._one = None

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def execute(self, stmt, params=None):
        text = stmt if isinstance(stmt, str) else stmt.as_string()
        self.conn.log.append((text, params))
        if self.conn.fail_on and text.startswith(self.conn.fail_on):
            raise RuntimeError("boom")
        if text.startswith("select to_regclass"):
            self._one = (None if params[0].split(".")[1] in self.conn.missing else params[0],)
        elif text.startswith("select has_table_privilege"):
            self._one = (params[1] not in self.conn.readonly,)

    def fetchone(self):
        return self._one


class FakeConn:
    def __init__(self, missing=(), fail_on=None, readonly=()):
        self.log, self.missing, self.fail_on, self.readonly = [], set(missing), fail_on, set(readonly)
        self.commits = self.rollbacks = 0
        self.closed = False

    def cursor(self):
        return FakeCursor(self)

    def commit(self):
        self.commits += 1

    def rollback(self):
        self.rollbacks += 1

    def close(self):
        self.closed = True


def rows_fixture(n_projects=3):
    return {"projects": [{"project_id": f"p{i}", "title": "T", "countries": ["HR"]} for i in range(n_projects)],
            "csr_documents": [], "csrs": [], "csr_parts": [], "project_csr_links": [],
            "anomalies": [{"project_id": None, "type": "x", "severity": "low"}],
            "impact_reports": [{"project_id": "p0", "score": 5, "report": "r", "source": "local"}],
            "csr_related_projects": [], "csr_recitals": [{"recital_id": "rec-HR-2026-1"}],
            "country_reports": [], "country_report_passages": []}


def statements(conn):
    return [s for s, _ in conn.log]


def test_load_deletes_then_inserts_in_one_transaction_with_role():
    conn = FakeConn()
    summary = load(rows_fixture(), URL, "tsi_explorer", "service_role", connect=lambda url: conn)
    s = statements(conn)
    assert s[0] == 'set local role "service_role"'
    deletes = [x for x in s if x.startswith("delete from")]
    assert deletes[:6] == [f'delete from "tsi_explorer"."{t}"' for t in
                           ("project_csr_links", "csr_parts", "csrs", "csr_documents", "projects", "anomalies")]
    assert 'delete from "tsi_explorer"."impact_reports" where source = \'local\'' in s
    first_insert = next(x for x in s if x.startswith("insert into"))
    assert first_insert.startswith('insert into "tsi_explorer"."projects" ("project_id", "title", "countries")')
    assert 'json_populate_recordset(null::"tsi_explorer"."projects", %s::json)' in first_insert
    assert conn.commits >= 1 and conn.rollbacks == 0 and conn.closed
    assert summary["projects"] == 3 and summary["impact_reports"] == 1 and summary["csr_recitals"] == 1
    assert "warnings" not in summary
    for table in ("testers", "link_reviews", "page_views", "user_passwords"):
        assert not any(f'"{table}"' in x for x in s)


def test_load_batches_and_json_parameter():
    conn = FakeConn()
    load(rows_fixture(BATCH + 1), URL, "public", "", connect=lambda url: conn)
    inserts = [p for x, p in conn.log if x.startswith('insert into "public"."projects"')]
    assert [len(json.loads(p[0])) for p in inserts] == [BATCH, 1]
    assert not any(x.startswith("set local role") for x in statements(conn))


def test_load_skips_missing_optional_table_with_warning():
    conn = FakeConn(missing={"csr_recitals"})
    summary = load(rows_fixture(), URL, "public", "", connect=lambda url: conn)
    assert summary["csr_recitals"] == 0
    assert any("csr_recitals" in w and "009_csr_recitals.sql" in w for w in summary["warnings"])
    assert not any(x.startswith('delete from "public"."csr_recitals"') for x in statements(conn))


def test_load_rolls_back_on_error():
    conn = FakeConn(fail_on='insert into "public"."anomalies"')
    with pytest.raises(SyncError, match="insert anomalies"):
        load(rows_fixture(), URL, "public", "", connect=lambda url: conn)
    assert conn.rollbacks == 1 and conn.commits == 0 and conn.closed


def test_load_refuses_protected_tables():
    rows = rows_fixture()
    rows["testers"] = [{"email": "a@b"}]
    with pytest.raises(SyncError, match="protegees"):
        load(rows, URL, "public", "", connect=lambda url: FakeConn())


def test_load_connection_error_hides_nothing_secret_and_writes_backup(tmp_path):
    def refuse(url):
        raise RuntimeError("connection refused")
    backup = tmp_path / "state" / "db_rows.json"
    with pytest.raises(SyncError, match="connexion") as exc:
        load(rows_fixture(), URL, "public", "", connect=refuse, backup=backup)
    assert "secret" not in str(exc.value)
    assert json.loads(backup.read_text(encoding="utf-8"))["projects"][0]["project_id"] == "p0"


def test_check_reports_missing_and_not_writable_and_writes_nothing():
    conn = FakeConn(missing={"csrs", "csr_recitals"}, readonly={"projects"})
    report = check(URL, "public", "service_role", connect=lambda url: conn)
    assert report["missing"] == ["csrs"]
    assert report["missing_optional"] == ["csr_recitals"]
    assert report["not_writable"] == ["projects"]
    assert conn.rollbacks == 1 and conn.commits == 0 and conn.closed
    assert not any(s.startswith(("delete", "insert")) for s in statements(conn))
