"""Public CLI: structured outputs, explicit writes, offline retrieval."""

import argparse
import json
import shutil
import subprocess
import sys
from dataclasses import asdict

from . import __version__, config, reports, search, storage, sync


def integer(value):
    number = int(value)
    if number < 1:
        raise argparse.ArgumentTypeError("must be a positive integer")
    return number


def offset(value):
    number = int(value)
    if number < 0:
        raise argparse.ArgumentTypeError("must be nonnegative")
    return number


def common(parser):
    parser.add_argument("--config", default=argparse.SUPPRESS, help="YAML configuration path")
    parser.add_argument("--json", action="store_true", default=argparse.SUPPRESS, help="Emit structured JSON")


def parser():
    p = argparse.ArgumentParser(
        prog="castfetch", description="Make your favorite content searchable. Keep it local."
    )
    p.add_argument("--version", action="version", version=f"castfetch {__version__}")
    common(p)
    commands = p.add_subparsers(dest="command", required=True)
    for name in ("run", "backfill"):
        c = commands.add_parser(
            name,
            help="Synchronize enabled sources" if name == "run" else "Explicitly expand archived history",
        )
        common(c)
        c.add_argument("--source", action="append", default=[], help="Source ID; repeat to select several")
        c.add_argument(
            "--dry-run",
            action="store_true",
            help="Inspect availability without any writes, media downloads or models",
        )
        if name == "backfill":
            group = c.add_mutually_exclusive_group(required=True)
            group.add_argument("--days", type=integer)
            group.add_argument("--all", action="store_true")
    for name in ("sources", "doctor"):
        common(commands.add_parser(name))
    c = commands.add_parser("config")
    common(c)
    check = c.add_subparsers(dest="action", required=True).add_parser("check")
    common(check)
    c = commands.add_parser("search", help="Read-only, case-insensitive phrase search in local transcripts")
    common(c)
    c.add_argument("query")
    filters(c)
    c = commands.add_parser("episodes")
    common(c)
    sub = c.add_subparsers(dest="action", required=True).add_parser("list")
    common(sub)
    filters(sub)
    sub.add_argument("--title", help="Case-insensitive substring of episode title")
    c = commands.add_parser("transcript")
    common(c)
    sub = c.add_subparsers(dest="action", required=True).add_parser("show")
    common(sub)
    sub.add_argument("--source", required=True)
    sub.add_argument("--episode", required=True, help="Stable episode key from episodes list or search")
    sub.add_argument("--from", dest="start", help="Seconds or HH:MM:SS")
    sub.add_argument("--to", dest="end", help="Seconds or HH:MM:SS")
    c = commands.add_parser("runs")
    common(c)
    sub = c.add_subparsers(dest="action", required=True)
    listing = sub.add_parser("list")
    common(listing)
    listing.add_argument("--limit", type=integer, default=10)
    showing = sub.add_parser("show")
    common(showing)
    showing.add_argument("run_id", help="Run ID or latest")
    return p


def filters(c):
    c.add_argument("--source", action="append", default=[])
    c.add_argument("--since", help="Inclusive publication date YYYY-MM-DD")
    c.add_argument("--until", help="Inclusive publication date YYYY-MM-DD")
    c.add_argument("--limit", type=integer, default=20)
    c.add_argument("--offset", type=offset, default=0)


def doctor(cfg):
    checks = [{"name": "configuration", "ok": True, "detail": str(cfg.path)}]
    local_sources = [s for s in cfg.sources if s.enabled and s.transcription == "local"]
    programs = ["ffmpeg", "ffprobe"] if local_sources else []
    if any(s.type == "instagram" for s in local_sources):
        programs.append("yt-dlp")
    for program in programs:
        checks.append({"name": program, "ok": bool(shutil.which(program)), "detail": shutil.which(program)})
    needs_local = bool(local_sources)
    if needs_local:
        python = cfg.transcription.get("python")
        try:
            if not python:
                raise ValueError("Set transcription.python to an environment with mlx-whisper")
            subprocess.run([python, "-c", "import mlx_whisper"], check=True, capture_output=True, timeout=60)
            checks.append({"name": "mlx_whisper", "ok": True, "detail": python})
        except (OSError, ValueError, subprocess.SubprocessError) as exc:
            checks.append({"name": "mlx_whisper", "ok": False, "detail": str(exc)})
    return {"schema_version": 1, "ok": all(c["ok"] for c in checks), "checks": checks}


def productive(args, cfg):
    selected = [s for s in cfg.sources if s.enabled and (not args.source or s.id in args.source)]
    unknown = set(args.source) - {s.id for s in selected}
    if unknown or not selected:
        raise config.ConfigError(
            "Select at least one enabled source; unknown or disabled: " + ", ".join(sorted(unknown))
        )
    backfill = ("all" if args.all else args.days) if args.command == "backfill" else None
    if args.dry_run:
        results = [sync.synchronize(s, cfg, True, backfill) for s in selected]
        return {"schema_version": 1, "dry_run": True, "sources": reports.redact(results)}, int(
            any(r["failed"] for r in results)
        )
    results = []
    # The report is created only after acquiring the archive lock.
    with storage.archive_lock(cfg.archive_root):
        report = reports.Report(cfg, args.command)
        try:
            for source in selected:
                print(f"Checking {source.name}…", file=sys.stderr, flush=True)
                result = sync.synchronize(source, cfg, report=report, backfill=backfill)
                results.append(result)
                report.result["sources"] = reports.redact(results)
                storage.json_write(report.path / "result.json", report.result)
            failed = any(r["failed"] for r in results)
            status = (
                "partial"
                if failed and any(not r["failed"] for r in results)
                else "failed"
                if failed
                else "success"
            )
            return report.finish(status, results), int(failed)
        except KeyboardInterrupt:
            return report.finish("interrupted", results, "Interrupted by user"), 130
        except Exception as exc:
            return report.finish("failed", results, exc), 1


def execute(args, cfg):
    if args.command in ("run", "backfill"):
        return productive(args, cfg)
    if args.command == "config":
        return {"schema_version": 1, "ok": True, "config_path": str(cfg.path), "sources": len(cfg.sources)}, 0
    if args.command == "sources":
        return {"schema_version": 1, "sources": [asdict(s) for s in cfg.sources]}, 0
    if args.command == "doctor":
        result = doctor(cfg)
        return result, 0 if result["ok"] else 1
    if args.command == "runs":
        return (
            {"schema_version": 1, "runs": reports.list_runs(cfg.logging_directory, args.limit)}
            if args.action == "list"
            else reports.read(cfg.logging_directory, args.run_id)
        ), 0
    if args.command == "search":
        result = search.search(cfg, args.query, args.source, args.since, args.until, args.limit, args.offset)
        return result, 0
    if args.command == "episodes":
        rows = search.records(cfg, args.source, args.since, args.until)
        if args.title:
            rows = [r for r in rows if search.normalized(args.title) in search.normalized(r.get("title", ""))]
        return {
            "schema_version": 1,
            "total": len(rows),
            "offset": args.offset,
            "limit": args.limit,
            "has_more": args.offset + args.limit < len(rows),
            "episodes": [search.metadata(r) for r in rows[args.offset : args.offset + args.limit]],
        }, 0
    if args.command == "transcript":
        result = search.show(cfg, args.source, args.episode, args.start, args.end)
        return result, 0
    raise ValueError("Unknown command")


def human(result):
    if "sources" in result and all("downloaded" in s for s in result["sources"]):
        for s in result["sources"]:
            print(
                f"{s['name']}: {s['episodes']} discovered; {s['downloaded']} "
                f"{'would download' if result.get('dry_run') else 'downloaded'}; {s['unchanged']} unchanged; "
                f"{s['local_candidates']} local candidates; {s['locally_transcribed']} transcribed; {s['failed']} errors"
            )
            for warning in s["warnings"]:
                print(f"  Warning: {warning}")
            for error in s["errors"]:
                print(f"  Error: {error['message']}")
        if result.get("result_path"):
            print("Report:", result["result_path"])
    elif "hits" in result:
        print(f"{result['total']} matches (offset {result['offset']})")
        for hit in result["hits"]:
            print(
                f"\n{hit['source_id']} / {hit['episode_key']} — {hit['title']} [{hit['start']}–{hit['end']}s]\n{hit['text']}"
            )
        for warning in result["warnings"]:
            print("Warning:", warning, file=sys.stderr)
    elif "segments" in result:
        print(result["title"])
        for s in result["segments"]:
            print(f"[{s['start']}–{s['end']}] {s['text']}")
        for warning in result["warnings"]:
            print("Warning:", warning, file=sys.stderr)
    else:
        print(json.dumps(result, ensure_ascii=False, indent=2))


def main(argv=None):
    args = parser().parse_args(argv)
    try:
        cfg = config.load(getattr(args, "config", None))
        result, code = execute(args, cfg)
    except (OSError, ValueError, KeyError) as exc:
        result = {"schema_version": 1, "status": "error", "error": reports.redact(str(exc))}
        code = 2 if isinstance(exc, config.ConfigError) else 1
        if not getattr(args, "json", False):
            print(result["error"], file=sys.stderr)
            return code
    if getattr(args, "json", False):
        print(json.dumps(result, ensure_ascii=False, indent=2))
    else:
        human(result)
    return code
