"""deploy/onprem/set_schema.py: copies of the database scripts for a schema other than public."""
import importlib.util
from pathlib import Path

_PATH = Path(__file__).resolve().parents[1] / "deploy" / "onprem" / "set_schema.py"
_spec = importlib.util.spec_from_file_location("set_schema", _PATH)
set_schema = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(set_schema)


def test_schema_references_change_public_role_stays():
    sql = ("create table if not exists public.projects (id int);\n"
           "create or replace function public.f() returns int language sql set search_path = public as $$ select 1 $$;\n"
           "revoke all on function public.f() from public;\n"
           "grant usage on schema public to anon;\n"
           "alter default privileges in schema public grant all on tables to anon;\n"
           "select 1 from pg_namespace where nspname = 'public';\n"
           "-- comment about the public site\n")
    out, unknown = set_schema.convert(sql, "tsi_explorer")
    assert unknown == []
    assert "public.projects" not in out and "tsi_explorer.projects" in out
    assert "search_path = tsi_explorer" in out
    assert "revoke all on function tsi_explorer.f() from public;" in out
    assert "on schema tsi_explorer" in out and "in schema tsi_explorer" in out
    assert "'tsi_explorer'" in out


def test_unknown_reference_is_reported():
    _, unknown = set_schema.convert("set role public_reader;\nalter schema public rename to x;\n", "s")
    assert unknown == ["2: alter schema public rename to x;"]


def test_every_repository_script_converts_without_unknown_reference():
    for src in set_schema.SOURCES:
        _, unknown = set_schema.convert(src.read_text(encoding="utf-8"), "tsi_explorer")
        assert unknown == [], src.name


def test_data_file_keeps_the_texts():
    data = ("truncate table public.projects, public.csrs;\n"
            "insert into public.projects (\"id\", \"summary\")\n"
            "  select \"id\", \"summary\" from json_populate_recordset(null::public.projects, "
            "$tsi$[{\"id\": 1, \"summary\": \"for business and the public. See public.example\"}]$tsi$);\n"
            "analyze public.projects;\n")
    out = set_schema.convert_data(data, "tsi_explorer")
    assert "truncate table tsi_explorer.projects, tsi_explorer.csrs;" in out
    assert "insert into tsi_explorer.projects" in out
    assert "null::tsi_explorer.projects" in out
    assert "the public. See public.example" in out
    assert "analyze tsi_explorer.projects;" in out
