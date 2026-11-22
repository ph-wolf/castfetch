"""Incremental sync with fixed history boundaries and create-only transcripts."""

import hashlib
import json
import subprocess
from datetime import UTC, datetime, timedelta

from . import network, storage, transcription
from .sources import instagram, podcast

EXTENSIONS = {
    "text/vtt": ".vtt",
    "application/json": ".json",
    "text/plain": ".txt",
    "application/srt": ".srt",
    "application/x-subrip": ".srt",
    "text/srt": ".srt",
}


def initial_policy(source, backfill=None):
    days = source.initial_days if backfill is None else backfill
    cutoff = (
        None
        if days == "all" or days is None
        else (datetime.now(UTC) - timedelta(days=days)).replace(microsecond=0).isoformat()
    )
    return {"schema_version": 1, "since": cutoff, "initial_days": source.initial_days}


def eligible(episode, policy):
    try:
        date = datetime.fromisoformat(episode.get("published", "").replace("Z", "+00:00"))
        if date.tzinfo is None:
            return False
    except ValueError:
        return False
    return policy["since"] is None or date >= datetime.fromisoformat(policy["since"])


def publisher_source(episodes, records):
    return any(
        e.get("publisher_transcripts_advertised")
        or any(
            t.get("origin") != "locally_generated"
            and t.get("source_url", "").startswith(("http://", "https://"))
            for t in e.get("transcripts", [])
        )
        for e in [*episodes, *records.values()]
    )


def synchronize(source, config, dry_run=False, backfill=None, report=None):
    root = config.archive_root
    result = dict(
        id=source.id,
        name=source.name,
        source_type=source.type,
        episodes=0,
        episodes_with_transcripts=0,
        advertised=0,
        downloaded=0,
        unchanged=0,
        local_candidates=0,
        locally_transcribed=0,
        failed=0,
        errors=[],
        warnings=[],
    )

    def event(level, kind, **fields):
        if report:
            report.event(level, kind, source_id=source.id, **fields)

    def fail(exc, episode=None, stage="discovery"):
        # Subprocess exceptions can contain arguments; do not record captured media/Whisper output.
        message = (
            f"External command failed (exit {exc.returncode})"
            if isinstance(exc, subprocess.CalledProcessError)
            else str(exc)
        )
        result["failed"] += 1
        result["errors"].append({"episode_id": episode, "stage": stage, "message": message})
        event("error", "processing_failed", episode_id=episode, stage=stage, message=message)

    try:
        adapter = podcast if source.type == "podcast" else instagram
        episodes, discovery, feed = adapter.discover(source, config)
        result["episodes"] = len(episodes)
        result["episodes_with_transcripts"] = sum(bool(e["transcripts"]) for e in episodes)
        result["advertised"] = sum(len(e["transcripts"]) for e in episodes)
        result["discovery"] = discovery
        records = storage.index_read(root, source.id)
        state_path = storage.inside(root, f"{source.id}/state.json")
        policy = storage.json_read(state_path)
        if policy is None:
            policy = initial_policy(source, backfill)
        else:
            if policy.get("initial_days") != source.initial_days:
                result["warnings"].append(
                    "initial_sync changed; stored coverage is unchanged. Use backfill to expand it."
                )
            if backfill is not None:
                new_since = initial_policy(source, backfill)["since"]
                if policy["since"] is not None and (new_since is None or new_since < policy["since"]):
                    policy["since"] = new_since
        official = publisher_source(episodes, records)
        result["publisher_transcript_source"] = official
        result["local_transcription_enabled"] = source.transcription == "local" and not official
        result["coverage_since"] = policy["since"]
        # Include persisted metadata for failed items that have left the public window/feed.
        current = {e["episode_key"]: e for e in episodes}
        merged = {k: {**e, "transcripts": e.get("offered_transcripts", [])} for k, e in records.items()}
        merged.update(current)
        if source.type == "instagram":
            last = policy.get("last_discovery_latest")
            if last and discovery["oldest_visible"] > last:
                policy["coverage_gap"] = True
            if policy.get("coverage_gap"):
                result["warnings"].append(
                    "Possible missed Instagram posts: public discovery windows did not overlap. Coverage is incomplete."
                )
            policy["last_discovery_latest"] = discovery["latest_visible"]
        # Warn about modified/damaged local files but never replace them.
        for record in records.values():
            for entry in record.get("transcripts", []):
                path = storage.inside(root, entry["local_path"])
                if path.exists() and (
                    not path.is_file()
                    or not path.stat().st_size
                    or (entry.get("sha256") and storage.digest(path) != entry["sha256"])
                ):
                    result["warnings"].append(
                        f"Local file changed or damaged; preserved: {entry['local_path']}"
                    )
        if not dry_run:
            storage.json_write(state_path, policy)
            if feed is not None:
                storage.write(storage.inside(root, f"{source.id}/feed.xml"), feed)
            else:
                storage.json_write(
                    storage.inside(root, f"{source.id}/discovery.json"),
                    {"discovery": discovery, "episodes": episodes},
                )
        for key, episode in sorted(merged.items(), key=lambda item: (item[1].get("published", ""), item[0])):
            if not eligible(episode, policy):
                if not episode.get("published") or not _dated(episode.get("published")):
                    result["warnings"].append(f"Undated episode skipped: {key}")
                continue
            previous = records.get(key, {})
            offered = episode.get("transcripts", [])
            record = {
                **previous,
                **{k: v for k, v in episode.items() if k != "transcripts"},
                "source_id": source.id,
                "source_name": source.name,
                "source_type": source.type,
                "offered_transcripts": offered,
                "transcripts": previous.get("transcripts", []).copy(),
            }
            try:
                for item in offered:
                    try:
                        extension = EXTENSIONS.get(item["mime_type"])
                        if not extension:
                            continue
                        relative = f"{source.id}/transcripts/{transcription.stem(episode)}{extension}"
                        old = next(
                            (t for t in record["transcripts"] if t.get("source_url") == item["source_url"]),
                            None,
                        )
                        if old:
                            relative = old["local_path"]
                        elif any(t["local_path"] == relative for t in record["transcripts"]):
                            # Keep distinct publisher/local versions and distinct publisher URLs.
                            suffix = hashlib.sha256(item["source_url"].encode()).hexdigest()[:12]
                            relative = f"{source.id}/transcripts/{transcription.stem(episode)}.publisher-{suffix}{extension}"
                        path = storage.inside(root, relative)
                        if path.exists():
                            if not path.is_file():
                                raise ValueError(f"Output path is not a file; preserved: {relative}")
                            if not path.stat().st_size:
                                result["warnings"].append(f"Empty local file preserved: {relative}")
                            result["unchanged"] += 1
                            if not old:
                                record["transcripts"].append(_entry(path, root, item, "publisher"))
                            continue
                        if dry_run:
                            result["downloaded"] += 1
                            continue
                        data, resolved = network.fetch(
                            item["source_url"], config.max_transcript_bytes, item["mime_type"]
                        )
                        if not data.strip():
                            raise ValueError("Empty publisher transcript")
                        _validate_publisher(data, item["mime_type"])
                        storage.write(path, data, replace=False)
                        entry = _entry(path, root, {**item, "resolved_url": resolved}, "publisher")
                        record["transcripts"] = [
                            t for t in record["transcripts"] if t.get("source_url") != item["source_url"]
                        ] + [entry]
                        result["downloaded"] += 1
                        event("info", "transcript_downloaded", episode_id=key, path=relative)
                    except (OSError, ValueError, KeyError) as exc:
                        fail(exc, key, "download")
                existing = any(storage.inside(root, t["local_path"]).exists() for t in record["transcripts"])
                if record.get("local_incomplete"):
                    raise ValueError(
                        "Incomplete local output from an earlier failure; files preserved for manual review"
                    )
                if (
                    result["local_transcription_enabled"]
                    and not offered
                    and not existing
                    and episode.get("audio_url")
                ):
                    if any(
                        storage.inside(
                            root, f"{source.id}/transcripts/{transcription.stem(episode)}.local.{ext}"
                        ).exists()
                        for ext in ("vtt", "json", "md")
                    ):
                        raise ValueError(
                            "Unindexed local transcript output already exists; preserved without replacement"
                        )
                    result["local_candidates"] += 1
                    if not dry_run:
                        event("info", "transcription_started", episode_id=key)
                        payloads, provenance = transcription.generate(source, episode, config.transcription)
                        relatives = {
                            ext: f"{source.id}/transcripts/{transcription.stem(episode)}{ext}"
                            for ext in payloads
                        }
                        if any(storage.inside(root, rel).exists() for rel in relatives.values()):
                            raise ValueError(
                                "Local transcript output already exists; preserved without replacement"
                            )
                        record["local_incomplete"] = True
                        for ext, data in payloads.items():
                            path = storage.inside(root, relatives[ext])
                            storage.write(path, data, replace=False)
                            entry = {
                                "source_url": f"urn:local-transcript:{provenance['audio_sha256']}:{ext.rsplit('.', 1)[-1]}",
                                "mime_type": {
                                    ".local.vtt": "text/vtt",
                                    ".local.json": "application/json",
                                    ".local.md": "text/markdown",
                                }[ext],
                            }
                            record["transcripts"].append(
                                _entry(path, root, {**entry, **provenance}, "locally_generated")
                            )
                        record.pop("local_incomplete", None)
                        result["locally_transcribed"] += 1
                        event("info", "transcription_completed", episode_id=key)
            except (OSError, ValueError, KeyError, subprocess.CalledProcessError) as exc:
                fail(exc, key, "transcripts")
            finally:
                if not dry_run:
                    records[key] = record
                    storage.index_write(root, source.id, records)
        for warning in result["warnings"]:
            event("warning", "coverage_or_archive_warning", message=warning)
        event(
            "info",
            "source_finished",
            counts={k: result[k] for k in ("downloaded", "unchanged", "locally_transcribed", "failed")},
        )
    except (OSError, ValueError, KeyError) as exc:
        fail(exc)
    return result


def _dated(value):
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00")).tzinfo is not None
    except ValueError:
        return False


def _entry(path, root, details, origin):
    return {
        **details,
        "origin": origin,
        "local_path": str(path.relative_to(root)),
        "sha256": storage.digest(path),
        "size": path.stat().st_size,
        "retrieved_at": storage.now(),
    }


def _validate_publisher(data, mime):
    text = data.decode("utf-8-sig")
    if mime == "application/json":
        if not json.loads(text):
            raise ValueError("Empty publisher JSON")
    elif mime != "text/plain":
        from .search import parse_vtt

        parse_vtt(text)
    elif text.lstrip().lower().startswith(("<!doctype html", "<html")):
        raise ValueError("Publisher returned an HTML page instead of a transcript")
