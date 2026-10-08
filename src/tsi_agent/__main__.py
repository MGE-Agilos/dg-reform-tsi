"""CLI : `python -m tsi_agent run ...` et `python -m tsi_agent export-yaml pipeline.yaml`."""

import argparse
import json
import logging
import sys
import traceback
from pathlib import Path

from tsi_agent.config import load_settings, load_sources
from tsi_agent.countries import COUNTRIES
from tsi_agent.deliverables_pipeline import build_deliverables_pipeline
from tsi_agent.index_pipeline import build_index_pipeline
from tsi_agent.pipeline import build_pipeline
from tsi_agent.runner import refresh_deliverables_metadata, run_deliverables, run_extraction, run_index


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
    deliv = sub.add_parser("deliverables", help="livrables des projets (publications PDF) : texte et résumé IA")
    deliv.add_argument("--full", action="store_true", help="retraiter toutes les publications")
    deliv.add_argument("--limit", type=int, help="nombre maximal de publications envoyées au LLM")
    deliv.add_argument("--chunk-size", type=int)
    deliv.add_argument("--model")
    deliv.add_argument("--refresh-metadata", action="store_true",
                       help="mettre à jour pays, année, programme et domaine des textes intégraux (ni LLM ni réseau)")
    chat = sub.add_parser("chat", help="chatbot local sur la couche vectorisée (page web)")
    chat.add_argument("--port", type=int, help="port local (défaut : chat_port des réglages)")
    chat.add_argument("--no-browser", action="store_true", help="ne pas ouvrir le navigateur")
    rescore = sub.add_parser("impact-rescore",
                             help="recalculer le score d'impact (0-10) des rapports existants, sans nouvelle analyse")
    rescore.add_argument("project_ids", nargs="*", help="identifiants des projets (défaut : tous les rapports)")
    ev = sub.add_parser("eval-retrieval",
                        help="banc de recherche : la recherche retrouve-t-elle le bon passage ? (chatbot arrêté)")
    ev.add_argument("--questions-file", help="jeu de questions (défaut : <out-dir>/questions.jsonl, réutilisé)")
    ev.add_argument("--regenerate", action="store_true", help="rédiger un nouveau jeu de questions (LLM)")
    ev.add_argument("--skip-chunk-sizes", action="store_true",
                    help="ne pas mesurer les tailles de morceaux (~30 min de vectorisation)")
    ev.add_argument("--n-project", type=int, default=60)
    ev.add_argument("--n-deliverable", type=int, default=60)
    ev.add_argument("--n-other", type=int, default=30)
    ev.add_argument("--out-dir", default="output/eval")
    agg = sub.add_parser("eval-aggregation",
                         help="banc d'agrégation : le chatbot trouve-t-il tous les pays / projets ? (chatbot arrêté)")
    agg.add_argument("--regenerate", action="store_true",
                     help="refaire la vérité des thèmes libres (juge LLM) au lieu de la réutiliser")
    only = agg.add_mutually_exclusive_group()
    only.add_argument("--topics-only", action="store_true", help="questions sur les thèmes libres seulement")
    only.add_argument("--metadata-only", action="store_true", help="questions sur les métadonnées seulement")
    agg.add_argument("--out-dir", default="output/eval")
    export = sub.add_parser("export-yaml", help="exporter le pipeline en YAML (import deepset)")
    export.add_argument("path")
    export.add_argument("--index", action="store_true", help="exporter le pipeline d'indexation (couche vectorisée)")
    export.add_argument("--chat", action="store_true", help="exporter le pipeline du chatbot")
    export.add_argument("--impact", action="store_true", help="exporter le pipeline d'analyse d'impact")
    export.add_argument("--deliverables", action="store_true", help="exporter le pipeline des livrables")
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
            if args.deliverables:
                pipe = build_deliverables_pipeline(settings)
            elif args.impact:
                from tsi_agent.impact.pipeline import build_impact_pipeline
                pipe = build_impact_pipeline(settings)
            elif args.chat:
                from tsi_agent.chat import pipeline as chat_pipeline
                pipe = chat_pipeline.build_chat_pipeline(settings, sources.get("policy_areas") or [])
            elif args.index:
                pipe = build_index_pipeline(settings)
            else:
                pipe = build_pipeline(settings, sources)
            Path(args.path).write_text(pipe.dumps(), encoding="utf-8")
        except Exception as exc:
            print(f"ERREUR : {exc!r}", file=sys.stderr)
            traceback.print_exc(file=sys.stderr)
            return 2
        print(f"Pipeline exporté vers {args.path}")
        return 0
    if args.command == "impact-rescore":
        from tsi_agent.impact import scoring
        try:
            out = scoring.rescore_reports(settings, scoring.default_scorer(settings), args.project_ids)
        except Exception as exc:
            print(f"ERREUR : {exc!r} — voir le journal du run dans {settings.logs_dir}/runs/", file=sys.stderr)
            traceback.print_exc(file=sys.stderr)
            return 2
        errors = 0
        for r in out["results"]:
            if "error" in r:
                errors += 1
                print(f"{r['project_id']} ERREUR : {r['error']}", file=sys.stderr)
            else:
                print(f"{r['project_id']} {r['impact_score']}/10 {r['impact_level']}")
        print(f"Coût total : {out['cost_usd']:.4f} $ — run {out['run_id']}")
        return 2 if errors else 0
    if args.command == "eval-retrieval":
        from tsi_agent.eval import runner as eval_runner
        try:
            out = eval_runner.run_eval(settings, out_dir=args.out_dir, questions_file=args.questions_file,
                                       regenerate=args.regenerate, skip_chunk_sizes=args.skip_chunk_sizes,
                                       n_project=args.n_project, n_deliverable=args.n_deliverable,
                                       n_other=args.n_other)
        except Exception as exc:
            print(f"ERREUR : {exc} — voir le journal du run dans {settings.logs_dir}/runs/", file=sys.stderr)
            traceback.print_exc(file=sys.stderr)
            return 2
        print(f"Rapport : {out['report']}")
        print(f"Détail par question : {out['csv']}")
        print(f"Questions ({out['questions']}) : {out['questions_file']}")
        print(f"Coût total : {out['cost_usd']:.4f} $ — run {out['run_id']}")
        return 0
    if args.command == "eval-aggregation":
        from tsi_agent.eval import aggregation
        try:
            out = aggregation.run_eval_aggregation(settings, out_dir=args.out_dir, regenerate=args.regenerate,
                                                   topics_only=args.topics_only, metadata_only=args.metadata_only,
                                                   policy_areas=sources.get("policy_areas") or [])
        except Exception as exc:
            print(f"ERREUR : {exc} — voir le journal du run dans {settings.logs_dir}/runs/", file=sys.stderr)
            traceback.print_exc(file=sys.stderr)
            return 2
        print(f"Rapport : {out['report']}")
        print(f"Détail par question : {out['csv']}")
        if out.get("truth_file"):
            print(f"Vérité des thèmes libres : {out['truth_file']}")
        print(f"Questions : {out['questions']}")
        print(f"Coût total : {out['cost_usd']:.4f} $ — run {out['run_id']}")
        return 0
    if args.command == "chat":
        import webbrowser

        from tsi_agent.chat import web
        port = args.port or settings.chat_port
        try:
            app = web.create_app(settings, policy_areas=sources.get("policy_areas") or [])
        except Exception as exc:
            print(f"ERREUR : {exc}", file=sys.stderr)
            traceback.print_exc(file=sys.stderr)
            return 2
        url = f"http://127.0.0.1:{port}/"
        print(f"Chatbot : {url}  (Ctrl+C pour arrêter)")
        if not args.no_browser:
            webbrowser.open(url)
        app.run(host="127.0.0.1", port=port, threaded=True)
        return 0
    if args.command == "deliverables":
        try:
            if args.refresh_metadata:
                print(json.dumps(refresh_deliverables_metadata(settings), indent=2, ensure_ascii=False))
                return 0
            summary = run_deliverables(settings, full=args.full, limit=args.limit, chunk_size=args.chunk_size,
                                       model=args.model)
        except Exception as exc:
            print(f"ERREUR : {exc!r} — voir le journal du run dans {settings.logs_dir}/runs/", file=sys.stderr)
            traceback.print_exc(file=sys.stderr)
            return 2
        print(json.dumps(summary, indent=2, ensure_ascii=False))
        return 1 if summary.get("quota_exceeded") else 0
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
