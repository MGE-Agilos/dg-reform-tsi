"""CLI : `python -m tsi_agent run ...` et `python -m tsi_agent export-yaml pipeline.yaml`."""

import argparse
import json
import logging
import sys
import traceback
from pathlib import Path

from tsi_agent.config import Settings, load_settings, load_sources
from tsi_agent.countries import COUNTRIES
from tsi_agent.deliverables_pipeline import build_deliverables_pipeline
from tsi_agent.index_pipeline import build_index_pipeline
from tsi_agent.llm import load_dotenv
from tsi_agent.pipeline import build_pipeline
from tsi_agent.runner import (format_discovery, refresh_deliverables_metadata, run_deliverables, run_extraction,
                              run_index, seed_sitemap_lastmod)


def _countries(value: str) -> list[str]:
    isos = [c.strip().upper() for c in value.split(",") if c.strip()]
    unknown = [c for c in isos if c not in COUNTRIES]
    if unknown:
        raise argparse.ArgumentTypeError(f"codes pays inconnus : {', '.join(unknown)}")
    return isos


def _uses_llm(args: argparse.Namespace) -> bool:
    """Commandes de mise a jour qui appellent le fournisseur LLM configure (hors modes sans LLM)."""
    if args.command == "deliverables":
        return not args.refresh_metadata
    if args.command == "csr-link":
        return not args.sample
    if args.command == "run":
        # --dry-run garde la verification : l'arbitre du sitemap (SitemapMergeJudge) appelle le LLM meme en dry-run.
        return not args.seed_sitemap_lastmod
    return args.command == "llm-check"


def _check_llm(settings: Settings) -> bool:
    """Fournisseur verifie avant tout travail (cle ; en mode openai, un appel minimal) : message clair sinon."""
    from tsi_agent import llm
    from tsi_agent.components.claude_cli import LLMCallError
    try:
        llm.check_provider(settings)
        if settings.llm_provider == "openai":
            result = llm.ping(settings)
            print(f"Fournisseur LLM : {result.get('model')} ({result.get('model_returned') or '-'}) OK",
                  file=sys.stderr)
    except (ValueError, LLMCallError) as exc:  # LLMCallError couvre LLMQuotaExceeded et LLMConfigError
        print(f"ERREUR (fournisseur LLM) : {exc}", file=sys.stderr)
        return False
    return True


def main(argv: list[str] | None = None) -> int:
    load_dotenv()  # cles (OPENAI_API_KEY...) depuis .env, sans ecraser l'environnement
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
    run.add_argument("--dry-run", "--discover-only", dest="dry_run", action="store_true",
                     help="découverte seule (sitemap compris), sans LLM ni écriture de projets ; bilan des fusions "
                          "dans logs/runs/<run_id>.discovery.json")
    run.add_argument("--model")
    run.add_argument("--recheck-all", action="store_true",
                     help="relire toutes les pages détail sans tenir compte des dates du sitemap ; seules celles dont "
                          "le texte a changé partent au LLM (contrairement à --full)")
    run.add_argument("--seed-sitemap-lastmod", action="store_true",
                     help="initialiser le mode incrémental : enregistrer la date <lastmod> actuelle du sitemap des "
                          "pages détail connues, sans les télécharger ni appeler le LLM (corpus supposé à jour)")
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
    bundle = sub.add_parser("trial-bundle",
                            help="regrouper projets et programme en fichiers par pays (trial Haystack Enterprise)")
    bundle.add_argument("--out", default="deploy/trial/build", help="dossier de sortie (vidé des tsi-* d'abord)")
    prod = sub.add_parser("prod-export",
                          help="un fichier par projet, programme et livrable + métadonnées (production Haystack Enterprise)")
    prod.add_argument("--out", default="deploy/prod/build/files",
                      help="dossier à téléverser (vidé des tsi-* d'abord) ; manifest.json écrit dans le dossier parent")
    rel = sub.add_parser("release-data", help="préparer les données de production pour une release GitHub "
                                              "(zips dans deploy/release/<tag>), et la publier avec --publish")
    rel.add_argument("--tag", help="tag de la release (défaut : data-AAAA-MM-JJ du jour)")
    rel.add_argument("--publish", action="store_true", help="publier la release avec `gh release create`")
    rel.add_argument("--repo", default="MGE-Agilos/dg-reform-tsi", help="dépôt GitHub (owner/nom)")
    rel.add_argument("--build", default="deploy/prod/build", help="dossier de prod-export (manifest.json et files/)")
    rel.add_argument("--out", default="deploy/release", help="dossier racine des releases préparées")
    anom = sub.add_parser("anomalies",
                          help="rapport d'anomalies de publication (CSV + Markdown), sans LLM ni réseau")
    anom.add_argument("--out", help="dossier du rapport (défaut : <output_dir>/reports)")
    anom.add_argument("--year", type=int, help="année de référence pour la gravité (défaut : année courante)")
    sub.add_parser("csr-fetch", help="télécharger la base CSR d'ECFIN (export Qlik complet, xlsx)")
    csrb = sub.add_parser("csr-build", help="fiches Markdown des CSR (output/csr) depuis l'export ECFIN")
    csrb.add_argument("--year-from", type=int, default=2016)
    csrb.add_argument("--year-to", type=int, default=2025)
    link = sub.add_parser("csr-link", help="lier les projets aux CSR (LLM du fournisseur configuré ; cache, reprise sur quota)")
    link.add_argument("--limit", type=int, help="nombre maximal de projets envoyés au LLM")
    link.add_argument("--project", action="append", dest="projects", help="id de projet (répétable)")
    link.add_argument("--detail-only", action="store_true",
                      help="n'envoyer au LLM que les projets avec page détail (les autres plus tard)")
    link.add_argument("--sample", type=int, help="écrire un échantillon de N projets liés (relecture métier)")
    link.add_argument("--model", help="modèle du CLI Claude (défaut : réglage model ; ignoré en mode openai)")
    check = sub.add_parser("llm-check", help="refaire l'extraction de N projets en cache avec le fournisseur "
                                            "configuré et comparer (output/reports/llm_check.md)")
    check.add_argument("--n", type=int, default=3, help="nombre de projets (défaut : 3)")
    sub.add_parser("supabase-sync", help="publier projets, CSR, liens et anomalies dans Supabase (explorateur web)")
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
    if _uses_llm(args) and not _check_llm(settings):
        return 2
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
    if args.command == "trial-bundle":
        from tsi_agent.trial_bundle import build
        print(json.dumps(build(Path(settings.output_dir), Path(args.out)), indent=2, ensure_ascii=False))
        return 0
    if args.command == "prod-export":
        from tsi_agent.prod_export import export
        manifest = export(Path(settings.output_dir), Path(args.out))
        print(json.dumps(manifest, indent=2, ensure_ascii=False))
        return 1 if manifest["meta_type_problems"] or manifest["files_over_10mb"] else 0
    if args.command == "release-data":
        from tsi_agent import release
        tag = args.tag or release.default_tag()
        try:
            summary = release.build_release(Path(settings.output_dir), Path(args.build), Path(args.out), tag)
            print(json.dumps(summary, indent=2, ensure_ascii=False))
            if args.publish:
                print(json.dumps(release.publish(tag, Path(summary["dir"]), repo=args.repo), indent=2,
                                 ensure_ascii=False))
        except (release.ReleaseError, OSError) as exc:
            print(f"ERREUR : {exc}", file=sys.stderr)
            return 1
        return 0
    if args.command == "anomalies":
        from tsi_agent.anomalies import run as run_anomalies
        out = Path(args.out) if args.out else Path(settings.output_dir) / "reports"
        print(json.dumps(run_anomalies(Path(settings.output_dir), out, args.year), indent=2, ensure_ascii=False))
        print(f"Rapport : {out / 'anomalies.csv'} et {out / 'anomalies.md'}")
        return 0
    if args.command == "csr-fetch":
        import httpx
        import websockets.exceptions
        from tsi_agent.csr_fetch import fetch
        try:
            result = fetch(Path(settings.output_dir) / "csr" / "csr_full.xlsx")
        except (httpx.HTTPError, websockets.exceptions.WebSocketException, OSError, TimeoutError, RuntimeError,
                KeyError) as exc:
            print(f"ERREUR : csr-fetch a échoué : {exc!r}", file=sys.stderr)
            return 1
        print(json.dumps(result, indent=2))
        return 0
    if args.command == "csr-build":
        from tsi_agent.csr import build_csrs, read_rows, write_csrs
        source = Path(settings.output_dir) / "csr" / "csr_full.xlsx"
        if not source.exists():
            print(f"ERREUR : {source} introuvable — lancer d'abord `python -m tsi_agent csr-fetch`", file=sys.stderr)
            return 1
        rows = read_rows(source)
        written = write_csrs(build_csrs(rows, args.year_from, args.year_to), source.parent)
        print(json.dumps({"rows": len(rows), "csr_files": written, "dir": str(source.parent)}, indent=2))
        return 0
    if args.command == "csr-link":
        from tsi_agent import runlog
        from tsi_agent.chat.generator import LoggedChatGenerator
        from tsi_agent.csr import build_csrs, read_rows
        from tsi_agent.csr_link import LINK_SCHEMA, run_linking, write_sample
        from tsi_agent.llm import make_json_generator
        source = Path(settings.output_dir) / "csr" / "csr_full.xlsx"
        if not source.exists():
            print(f"ERREUR : {source} introuvable — lancer d'abord `python -m tsi_agent csr-fetch`", file=sys.stderr)
            return 1
        csrs = build_csrs(read_rows(source))
        if args.sample:
            out = Path(settings.output_dir) / "reports" / "csr_links_sample.csv"
            print(f"Échantillon : {write_sample(settings, csrs, args.sample, out)}")
            return 0
        try:
            generator = LoggedChatGenerator(chat_generator=make_json_generator(
                settings, LINK_SCHEMA, timeout_s=settings.llm_timeout_s, model=args.model), pricing=settings.pricing)
        except ValueError as exc:
            print(f"ERREUR : {exc}", file=sys.stderr)
            return 2
        with runlog.open_run(settings.logs_dir) as run:
            summary = run_linking(settings, csrs, generator, limit=args.limit, project_ids=args.projects,
                                  detail_only=args.detail_only)
            run.extra.update({"mode": "csr_link", **{k: v for k, v in summary.items() if k != "failed"},
                              "failed": summary["failed"][:50]})
        print(json.dumps({**summary, "run": run.final_summary.get("run_id") if run.final_summary else None,
                          "cost_usd": (run.final_summary or {}).get("cost_usd")}, indent=2, ensure_ascii=False))
        return 1 if summary["quota_exceeded"] else 0
    if args.command == "llm-check":
        from tsi_agent import llm_check
        try:
            result = llm_check.run_check(settings, n=args.n)
        except (llm_check.LLMCheckError, ValueError) as exc:
            print(f"ERREUR : {exc}", file=sys.stderr)
            return 1
        print(json.dumps(result, indent=2, ensure_ascii=False))
        print(f"Rapport : {result['report']}")
        ok = any(p["error"] is None for p in result["projects"])
        return 0 if ok and not result["quota_exceeded"] else 1
    if args.command == "supabase-sync":
        import httpx
        from tsi_agent.supabase_sync import SyncError, build_rows, credentials, push
        try:
            url, key = credentials()
            rows = build_rows(Path(settings.output_dir))
            summary = push(rows, url, key, backup=Path(settings.output_dir) / "state" / "supabase_rows.json")
        except (SyncError, httpx.HTTPError, OSError) as exc:
            print(f"ERREUR : {exc} — relancer supabase-sync (les tables peuvent etre partielles)", file=sys.stderr)
            return 1
        print(json.dumps(summary, indent=2))
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
    if args.seed_sitemap_lastmod:
        try:
            summary = seed_sitemap_lastmod(settings, sources)
        except (RuntimeError, OSError) as exc:
            print(f"ERREUR : {exc}", file=sys.stderr)
            return 1
        print(json.dumps(summary, indent=2, ensure_ascii=False))
        return 0
    try:
        summary = run_extraction(settings, sources, full=args.full, countries=args.countries, limit=args.limit,
                                 chunk_size=args.chunk_size, dry_run=args.dry_run, model=args.model,
                                 recheck_all=args.recheck_all)
    except Exception as exc:
        print(f"ERREUR : {exc!r} — voir le journal du run dans {settings.logs_dir}/runs/", file=sys.stderr)
        traceback.print_exc(file=sys.stderr)
        return 2
    discovery = (summary.get("dry_run") or {}).get("discovery")
    if discovery and Path(discovery["file"]).exists():
        report = json.loads(Path(discovery["file"]).read_text(encoding="utf-8"))
        print(format_discovery(report, discovery["file"]))
    print(json.dumps(summary, indent=2, ensure_ascii=False))
    return 1 if summary.get("quota_exceeded") else 0


if __name__ == "__main__":
    sys.exit(main())
