"""CLI : `python -m tsi_agent run ...` et `python -m tsi_agent export-yaml pipeline.yaml`."""

import argparse
import json
import logging
import sys
import traceback
from pathlib import Path

from tsi_agent.config import load_settings, load_sources
from tsi_agent.countries import COUNTRIES
from tsi_agent.index_pipeline import build_index_pipeline
from tsi_agent.pipeline import build_pipeline
from tsi_agent.runner import run_extraction, run_index


def _countries(value: str) -> list[str]:
    isos = [c.strip().upper() for c in value.split(",") if c.strip()]
    unknown = [c for c in isos if c not in COUNTRIES]
    if unknown:
        raise argparse.ArgumentTypeError(f"codes pays inconnus : {', '.join(unknown)}")
    return isos


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="tsi_agent")
    parser.add_argument("--settings", default="config/settings.yaml")
    parser.add_argument("--sources", default="config/sources.yaml")
    sub = parser.add_subparsers(dest="command", required=True)
    run = sub.add_parser("run", help="extraire les projets nouveaux ou modifiés")
    run.add_argument("--full", action="store_true", help="ré-extraire toutes les pages détail")
    run.add_argument("--countries", type=_countries, help="codes ISO, ex. FR,HR (exclut le PDF SRSP)")
    run.add_argument("--limit", type=int,
                     help="nombre maximal de projets envoyés au LLM (projets détaillés)")
    run.add_argument("--chunk-size", type=int)
    run.add_argument("--dry-run", action="store_true", help="découverte seule, sans LLM ni écriture")
    run.add_argument("--model")
    index = sub.add_parser("index", help="vectoriser les fichiers extraits nouveaux ou modifiés (couche RAG)")
    index.add_argument("--full", action="store_true", help="revectoriser tous les fichiers")
    export = sub.add_parser("export-yaml", help="exporter le pipeline en YAML (import deepset)")
    export.add_argument("path")
    export.add_argument("--index", action="store_true", help="exporter le pipeline d'indexation (couche vectorisée)")
    args = parser.parse_args(argv)
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")

    logging.basicConfig(level=logging.WARNING)
    if not Path(args.sources).exists():
        parser.error(f"fichier introuvable : {args.sources}")
    settings = load_settings(args.settings)
    sources = load_sources(args.sources)
    if args.command == "export-yaml":
        try:
            pipe = build_index_pipeline(settings) if args.index else build_pipeline(settings, sources)
            Path(args.path).write_text(pipe.dumps(), encoding="utf-8")
        except Exception as exc:
            print(f"ERREUR : {exc!r}", file=sys.stderr)
            traceback.print_exc(file=sys.stderr)
            return 2
        print(f"Pipeline exporté vers {args.path}")
        return 0
    if args.command == "index":
        try:
            summary = run_index(settings, full=args.full)
        except Exception as exc:
            print(f"ERREUR : {exc!r} — voir le journal du run dans {settings.logs_dir}/runs/", file=sys.stderr)
            traceback.print_exc(file=sys.stderr)
            return 2
        print(json.dumps(summary, indent=2, ensure_ascii=False))
        return 0
    try:
        summary = run_extraction(settings, sources, full=args.full, countries=args.countries, limit=args.limit,
                                 chunk_size=args.chunk_size, dry_run=args.dry_run, model=args.model)
    except Exception as exc:
        print(f"ERREUR : {exc!r} — voir le journal du run dans {settings.logs_dir}/runs/", file=sys.stderr)
        traceback.print_exc(file=sys.stderr)
        return 2
    print(json.dumps(summary, indent=2, ensure_ascii=False))
    return 1 if summary.get("quota_exceeded") else 0


if __name__ == "__main__":
    sys.exit(main())
