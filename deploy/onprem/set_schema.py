"""Copies of the database scripts for a schema other than `public` (deploy/onprem/README.md, step 4).

The scripts in deploy/onprem/sql and supabase/migrations are written for the schema `public`. When the explorer's
tables must live in another schema (for example `tsi_explorer`), this script writes adapted copies:
    python3 deploy/onprem/set_schema.py tsi_explorer --out /tmp/tsi-sql
    python3 deploy/onprem/set_schema.py tsi_explorer --out /tmp/tsi-sql --data tsi-explorer-data.sql
Only the schema references change: `public.<object>`, `search_path = public`, `on schema public`,
`in schema public` and the literal 'public'. `from public` / `to public` (the PUBLIC pseudo-role of the
grants) are kept. The script refuses to write if it finds a reference to `public` it does not know.
In the data file, only the statements change, never the data: the texts of the projects contain "public." too.
"""
from __future__ import annotations

import argparse
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
SOURCES = [*sorted((ROOT / "deploy/onprem/sql").glob("0*.sql")), *sorted((ROOT / "supabase/migrations").glob("0*.sql")),
           *sorted((ROOT / "deploy/onprem/sql").glob("9*.sql"))]

RULES = [
    (re.compile(r"\bpublic\.", re.I), "{s}."),
    (re.compile(r"\b(search_path\s*(?:=|to)\s*)public\b", re.I), r"\g<1>{s}"),
    (re.compile(r"\b((?:on|in)\s+schema\s+)public\b", re.I), r"\g<1>{s}"),
    (re.compile(r"'public'", re.I), "'{s}'"),
]
ROLE = re.compile(r"\b(?:from|to)\s+public\b(?!\.)", re.I)  # the PUBLIC pseudo-role: unchanged
WORD = re.compile(r"\bpublic\b", re.I)


def convert(text: str, schema: str) -> tuple[str, list[str]]:
    """Returns the adapted text and the lines that still name `public` other than as the PUBLIC role."""
    for pattern, repl in RULES:
        text = pattern.sub(repl.replace("{s}", schema), text)
    unknown = []
    for n, line in enumerate(text.splitlines(), 1):
        code = line.split("--", 1)[0]
        if WORD.search(ROLE.sub("", code)):
            unknown.append(f"{n}: {line.strip()}")
    return text, unknown


def convert_data(text: str, schema: str) -> str:
    """Data file of export_data.py: the rows are JSON on the `  select ... json_populate_recordset(...)` lines, where
    only the first `null::public.` is the schema; every other line is a short statement without data."""
    lines = []
    for line in text.split("\n"):
        if line.startswith("  select ") and "json_populate_recordset(" in line:
            line = line.replace("json_populate_recordset(null::public.", f"json_populate_recordset(null::{schema}.", 1)
        else:
            line = RULES[0][0].sub(f"{schema}.", line)
        lines.append(line)
    return "\n".join(lines)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("schema", help="target schema, for example tsi_explorer")
    parser.add_argument("--out", required=True, help="folder for the adapted copies (created if needed)")
    parser.add_argument("--data", help="also adapt this data file (tsi-explorer-data.sql)")
    args = parser.parse_args()
    if not re.fullmatch(r"[a-z_][a-z0-9_]{0,62}", args.schema):
        sys.exit("The schema name must be lower case letters, digits and _")
    out = Path(args.out)
    out.mkdir(parents=True, exist_ok=True)
    failed = False
    for src in SOURCES:
        text, unknown = convert(src.read_text(encoding="utf-8"), args.schema)
        if unknown:
            failed = True
            print(f"{src.name}: unknown reference to public:\n  " + "\n  ".join(unknown[:10]), file=sys.stderr)
            continue
        (out / src.name).write_text(text, encoding="utf-8", newline="\n")
        print(f"written {out / src.name}")
    if args.data:
        src = Path(args.data)
        (out / src.name).write_text(convert_data(src.read_text(encoding="utf-8"), args.schema), encoding="utf-8", newline="\n")
        print(f"written {out / src.name}")
    if failed:
        sys.exit(1)


if __name__ == "__main__":
    main()
