"""Chargement des donnees de l'explorateur dans la base PostgreSQL d'une installation auto-hebergee (db-sync).

Connexion directe (psycopg 3), UNE transaction : en cas d'erreur, la base garde son contenu precedent. Memes lignes
que supabase-sync (`build_rows`). Reglages, depuis l'environnement, sinon `.env` :
- TSI_DATABASE_URL : URL libpq (jamais affichee : elle contient le mot de passe), par exemple le meme compte que
  PostgREST, `...?sslmode=require` ;
- TSI_DB_SCHEMA : schema des tables (defaut public) ;
- TSI_DB_ROLE : optionnel, `set local role` (par exemple service_role, qui ecrit malgre la securite par ligne grace aux
  regles de deploy/onprem/sql/900_service_role.sql ; le compte de connexion doit en etre membre).
Jamais touchees : testers, link_reviews, page_views, user_passwords, et les analyses d'impact lancees depuis la page
(impact_reports.source = 'web').
"""
from __future__ import annotations

import json
import os
import re
from collections.abc import Callable
from pathlib import Path
from typing import Any

from tsi_agent.llm import read_dotenv
from tsi_agent.supabase_sync import (DELETE_ORDER, IMPACT_TABLE, INSERT_ORDER, OPTIONAL_TABLES, PROTECTED_TABLES,
                                     SyncError)

BATCH = 500
NEVER_WRITTEN = (*PROTECTED_TABLES, "page_views", "user_passwords")
MANDATORY = (*DELETE_ORDER, IMPACT_TABLE)
MIN_SHARE = 0.5  # garde-fou : moins de la moitie des projets de la base -> refus (output/ vide ou mal monte), sauf force
_NAME = re.compile(r"[a-z_][a-z0-9_]{0,62}")
# Le role effectif peut-il inserer et supprimer toutes les lignes ? Droits, puis securite par ligne : table sans RLS,
# proprietaire (non force), BYPASSRLS, ou regle pour ce role (900_service_role.sql : service_role_all).
_WRITABLE = """select has_table_privilege(current_user, c.oid, 'INSERT') and has_table_privilege(current_user, c.oid, 'DELETE')
  and (not c.relrowsecurity or pg_get_userbyid(c.relowner) = current_user
       or coalesce((select r.rolbypassrls from pg_roles r where r.rolname = current_user), false)
       or exists (select 1 from pg_policies p where p.schemaname = n.nspname and p.tablename = c.relname
                  and p.permissive = 'PERMISSIVE' and p.cmd in ('ALL', 'DELETE')
                  and exists (select 1 from unnest(p.roles) r(name)
                              where r.name = 'public' or pg_has_role(current_user, r.name, 'USAGE'))))
from pg_class c join pg_namespace n on n.oid = c.relnamespace where n.nspname = %s and c.relname = %s"""


def database_settings(dotenv: Path = Path(".env")) -> tuple[str, str, str]:
    """(url, schema, role) : environnement, sinon `.env` ; SyncError claire (sans l'URL) si absent ou invalide."""
    file = read_dotenv(dotenv)

    def get(key: str) -> str:
        return (os.environ.get(key) or file.get(key) or "").strip()

    url, schema, role = get("TSI_DATABASE_URL"), get("TSI_DB_SCHEMA") or "public", get("TSI_DB_ROLE")
    if not url:
        raise SyncError(f"TSI_DATABASE_URL requis (environnement ou {dotenv.resolve()}, jamais dans git)")
    if not _NAME.fullmatch(schema):
        raise SyncError("TSI_DB_SCHEMA : lettres minuscules, chiffres et _ seulement")
    if role and not _NAME.fullmatch(role):
        raise SyncError("TSI_DB_ROLE : lettres minuscules, chiffres et _ seulement")
    return url, schema, role


def _table(schema: str, table: str) -> Any:
    from psycopg import sql
    return sql.SQL("{}.{}").format(sql.Identifier(schema), sql.Identifier(table))


def _insert(cur: Any, schema: str, table: str, data: list[dict[str, Any]]) -> None:
    """Insertion par lots : un parametre JSON par lot ; colonnes = cles des lignes (identiques pour toute la table),
    donc les colonnes calculees (fts) et d'identite (id) restent a la base."""
    from psycopg import sql
    if not data:
        return
    cols = sql.SQL(", ").join(sql.Identifier(c) for c in data[0])
    stmt = sql.SQL("insert into {t} ({c}) select {c} from json_populate_recordset(null::{t}, %s::json)").format(
        t=_table(schema, table), c=cols)
    for start in range(0, len(data), BATCH):
        cur.execute(stmt, (json.dumps(data[start:start + BATCH], ensure_ascii=False),))


def _exists(cur: Any, schema: str, table: str) -> bool:
    cur.execute("select to_regclass(%s)", (f"{schema}.{table}",))
    return cur.fetchone()[0] is not None


def _set_role(cur: Any, role: str) -> None:
    from psycopg import sql
    if role:
        cur.execute(sql.SQL("set local role {}").format(sql.Identifier(role)))


def _connect(url: str) -> Any:
    import psycopg
    return psycopg.connect(url)


def _open(connect: Callable[[str], Any], url: str) -> Any:
    try:
        return connect(url)
    except Exception as exc:  # message libpq : hote, compte, raison ; jamais le mot de passe
        raise SyncError(f"connexion a la base impossible : {type(exc).__name__}: {exc}") from exc


def load(rows: dict[str, list[dict[str, Any]]], url: str, schema: str, role: str = "", *,
         connect: Callable[[str], Any] = _connect, backup: Path | None = None, force: bool = False) -> dict[str, Any]:
    """Remplace les donnees publiques dans UNE transaction ; resume {table: lignes, warnings?}. SyncError sinon.

    Ordre de supabase-sync : suppressions (liens avant les tables referencees), insertions, analyses d'impact locales
    (source='local' seulement), tables optionnelles (absentes : sautees avec un avertissement). Puis ANALYZE hors
    transaction (seul le proprietaire le peut ; sinon PostgreSQL passe la table, autovacuum le fera).

    Garde-fou : sans projet ou sans CSR, ou avec moins de MIN_SHARE des projets deja en base, refus (SyncError) avant
    toute ecriture, sauf `force` : un output/ vide ou mal monte ne doit pas vider l'explorateur.
    """
    from psycopg import sql
    written = {*DELETE_ORDER, *INSERT_ORDER, *(t for t in (IMPACT_TABLE, *(o[0] for o in OPTIONAL_TABLES)) if t in rows)}
    forbidden = (written | set(rows)) & set(NEVER_WRITTEN)
    if forbidden:
        raise SyncError(f"tables protegees dans la synchronisation : {sorted(forbidden)}")
    if not force and not (rows.get("projects") and rows.get("csrs")):
        raise SyncError("aucun projet ou aucune CSR dans output/ (etat non restaure ? mauvais volume ?) : "
                        "chargement refuse, la base est inchangee (--force pour charger quand meme)")
    if backup is not None:
        backup.parent.mkdir(parents=True, exist_ok=True)
        backup.write_text(json.dumps(rows, ensure_ascii=False), encoding="utf-8")
    conn = _open(connect, url)
    summary: dict[str, Any] = {}
    warnings: list[str] = []
    done: list[str] = []
    step = ("set role", role)
    try:
        with conn.cursor() as cur:
            _set_role(cur, role)
            step = ("check", "projects")
            cur.execute(sql.SQL("select count(*) from {}").format(_table(schema, "projects")))
            current = cur.fetchone()[0] or 0
            new = len(rows.get("projects") or [])
            if not force and current and new < MIN_SHARE * current:
                raise SyncError(f"{new} projets a charger pour {current} dans la base : chargement refuse "
                                "(output/ incomplet ?), --force pour charger quand meme")
            for table in DELETE_ORDER:
                step = ("delete", table)
                cur.execute(sql.SQL("delete from {}").format(_table(schema, table)))
            for table in INSERT_ORDER:
                step = ("insert", table)
                _insert(cur, schema, table, rows.get(table) or [])
                summary[table] = len(rows.get(table) or [])
            done.extend(DELETE_ORDER)
            if IMPACT_TABLE in rows:
                step = ("delete", IMPACT_TABLE)
                cur.execute(sql.SQL("delete from {} where source = 'local'").format(_table(schema, IMPACT_TABLE)))
                step = ("insert", IMPACT_TABLE)
                _insert(cur, schema, IMPACT_TABLE, rows.get(IMPACT_TABLE) or [])
                summary[IMPACT_TABLE] = len(rows.get(IMPACT_TABLE) or [])
                done.append(IMPACT_TABLE)
            for table, _, migration in OPTIONAL_TABLES:
                if table not in rows:
                    continue
                step = ("check", table)
                if not _exists(cur, schema, table):
                    warnings.append(f"table {table} absente (appliquer supabase/migrations/{migration}) : non synchronisee")
                    summary[table] = 0
                    continue
                step = ("delete", table)
                cur.execute(sql.SQL("delete from {}").format(_table(schema, table)))
                step = ("insert", table)
                _insert(cur, schema, table, rows.get(table) or [])
                summary[table] = len(rows.get(table) or [])
                done.append(table)
        conn.commit()
    except Exception as exc:
        try:
            conn.rollback()
        finally:
            conn.close()
        if isinstance(exc, SyncError):
            raise
        raise SyncError(f"{step[0]} {step[1]}: {type(exc).__name__}: {exc}") from exc
    try:
        with conn.cursor() as cur:
            for table in done:
                cur.execute(sql.SQL("analyze {}").format(_table(schema, table)))
        conn.commit()
    except Exception:
        conn.rollback()
    finally:
        conn.close()
    if warnings:
        summary["warnings"] = warnings
    return summary


def check(url: str, schema: str, role: str = "", *, connect: Callable[[str], Any] = _connect) -> dict[str, Any]:
    """Verification sans ecriture (transaction annulee) : tables presentes et modifiables par le role effectif."""
    conn = _open(connect, url)
    report: dict[str, Any] = {"schema": schema, "role": role or "(compte de connexion)", "missing": [],
                              "missing_optional": [], "not_writable": []}
    try:
        with conn.cursor() as cur:
            _set_role(cur, role)
            for table in (*MANDATORY, *(o[0] for o in OPTIONAL_TABLES)):
                if not _exists(cur, schema, table):
                    report["missing" if table in MANDATORY else "missing_optional"].append(table)
                    continue
                cur.execute(_WRITABLE, (schema, table))
                if not cur.fetchone()[0]:
                    report["not_writable"].append(table)
    except Exception as exc:
        raise SyncError(f"verification : {type(exc).__name__}: {exc}") from exc
    finally:
        conn.rollback()
        conn.close()
    return report
