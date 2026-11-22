"""Read-only, timestamped text retrieval from native archive indexes."""

import html
import json
import math
import re
import unicodedata
from datetime import date

from . import storage


def seconds(value):
    if isinstance(value, (int, float)):
        result = float(value)
    else:
        parts = str(value).replace(",", ".").split(":")
        if len(parts) > 3:
            raise ValueError("Time must be seconds or HH:MM:SS")
        result = sum(float(n) * 60**i for i, n in enumerate(reversed(parts)))
    if not math.isfinite(result) or result < 0:
        raise ValueError("Time must be finite and nonnegative")
    return result


def day(value):
    return date.fromisoformat(value).isoformat() if value else None


def records(config, sources=(), since=None, until=None):
    since, until = day(since), day(until)
    if since and until and since > until:
        raise ValueError("since must not be later than until")
    root = config.archive_root
    available = {p.parent.name for p in root.glob("*/index.jsonl")} if root.is_dir() else set()
    if set(sources) - available:
        raise ValueError("No archived index for source(s): " + ", ".join(sorted(set(sources) - available)))
    rows = []
    for sid in sorted(set(sources) if sources else available):
        for row in storage.index_read(root, sid).values():
            published = row.get("published", "")[:10]
            if since and published < since or until and published > until:
                continue
            rows.append({**row, "source_id": sid})
    return sorted(
        rows, key=lambda r: (r.get("published", ""), r["source_id"], r["episode_key"]), reverse=True
    )


def metadata(row):
    return {
        k: row.get(k)
        for k in ("source_id", "source_name", "episode_key", "title", "published", "episode_url")
    }


def parse_vtt(text):
    cues = []
    for block in re.split(r"\n\s*\n", text.replace("\r\n", "\n")):
        lines = block.splitlines()
        for i, line in enumerate(lines):
            if "-->" not in line:
                continue
            start, end = line.split("-->", 1)
            cue = {
                "start": seconds(start.strip()),
                "end": seconds(end.strip().split()[0]),
                "text": html.unescape(re.sub(r"<[^>]+>", "", " ".join(lines[i + 1 :]))),
            }
            if cue["end"] < cue["start"]:
                raise ValueError("Reversed transcript timestamps")
            if cue["text"].strip():
                cues.append(cue)
            break
    if not cues:
        raise ValueError("No timed transcript cues")
    return cues


def read_transcript(config, row):
    warnings = []
    candidates = sorted(
        row.get("transcripts", []),
        key=lambda t: (
            0
            if t.get("mime_type") in ("text/vtt", "application/srt", "application/x-subrip", "text/srt")
            else 1
            if t.get("mime_type") == "application/json"
            else 2,
            t.get("origin") == "locally_generated",
            t.get("local_path", ""),
        ),
    )
    for entry in candidates:
        relative = entry["local_path"]
        try:
            path = storage.inside(config.archive_root, relative)
            content = path.read_text(encoding="utf-8-sig")
            mime = entry.get("mime_type")
            if mime in ("text/vtt", "application/srt", "application/x-subrip", "text/srt") or path.suffix in (
                ".vtt",
                ".srt",
            ):
                segments = parse_vtt(content)
            elif mime == "application/json" or path.suffix == ".json":
                data = json.loads(content)
                data = data.get("segments") if isinstance(data, dict) else data
                if not isinstance(data, list) or not data:
                    raise ValueError("Unsupported JSON transcript structure")
                segments = []
                for cue in data:
                    start, end = seconds(cue["start"]), seconds(cue["end"])
                    if end < start:
                        raise ValueError("Reversed transcript timestamps")
                    segments.append({"start": start, "end": end, "text": str(cue["text"])})
            elif mime == "text/plain":
                if not content.strip():
                    raise ValueError("Empty transcript")
                segments = [{"start": None, "end": None, "text": content.strip()}]
            else:
                continue
            return (
                segments,
                {"transcript_path": str(path), "origin": entry.get("origin", "publisher"), "mime_type": mime},
                warnings,
            )
        except (OSError, ValueError, KeyError, TypeError) as exc:
            warnings.append(f"{row['source_id']}/{row['episode_key']}: cannot read {relative}: {exc}")
    warnings.append(f"{row['source_id']}/{row['episode_key']}: no readable transcript")
    return [], {}, warnings


def normalized(text):
    return " ".join(unicodedata.normalize("NFKC", text).casefold().split())


def search(config, query, sources=(), since=None, until=None, limit=20, offset=0):
    query = normalized(query)
    if not query:
        raise ValueError("Search query must not be empty")
    hits, warnings = [], []
    for row in records(config, sources, since, until):
        segments, provenance, issues = read_transcript(config, row)
        warnings.extend(issues)
        # Search a rolling three-cue window so phrases may cross subtitle boundaries.
        last_hit_end = -1
        for i, segment in enumerate(segments):
            end = min(len(segments), i + 3)
            window = segments[i:end]
            if i <= last_hit_end or query not in normalized(" ".join(s["text"] for s in window)):
                continue
            # Avoid attributing a match in the next cue to this cue if it already matches alone.
            if (
                query not in normalized(segment["text"])
                and len(window) > 1
                and query in normalized(" ".join(s["text"] for s in window[1:]))
            ):
                continue
            context = segments[max(0, i - 1) : min(len(segments), end + 1)]
            hits.append(
                {
                    **metadata(row),
                    **provenance,
                    "start": segment["start"],
                    "end": window[-1]["end"],
                    "text": " ".join(s["text"] for s in window),
                    "context": context,
                }
            )
            last_hit_end = end - 1
    return {
        "schema_version": 1,
        "query": query,
        "total": len(hits),
        "limit": limit,
        "offset": offset,
        "has_more": offset + limit < len(hits),
        "hits": hits[offset : offset + limit],
        "warnings": warnings,
    }


def show(config, sid, episode_id, start=None, end=None):
    rows = records(config, [sid])
    row = next((r for r in rows if r["episode_key"] == episode_id), None)
    if not row:
        raise ValueError("Episode not found")
    segments, provenance, warnings = read_transcript(config, row)
    left, right = (
        seconds(start) if start is not None else 0,
        seconds(end) if end is not None else float("inf"),
    )
    if right < left:
        raise ValueError("to must not be earlier than from")
    if (start is not None or end is not None) and any(s["start"] is None for s in segments):
        raise ValueError("This transcript has no timestamps; omit from/to")
    return {
        "schema_version": 1,
        **metadata(row),
        **provenance,
        "segments": [s for s in segments if s["start"] is None or s["end"] >= left and s["start"] <= right],
        "warnings": warnings,
    }
