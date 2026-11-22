"""Versioned, private run reports; no media bodies or credential-bearing URLs."""

import json
import re
import uuid
from datetime import UTC, datetime

from . import storage


def redact(value):
    if isinstance(value, dict):
        return {
            k: redact(v)
            for k, v in value.items()
            if k not in ("audio_source_url", "audio_url", "caption", "segments")
        }
    if isinstance(value, list):
        return [redact(v) for v in value]
    if isinstance(value, str):
        # Query strings, fragments and URL userinfo are never needed in operational logs.
        value = re.sub(r"(https?://)[^\s/]+@", r"\1[redacted]@", value)
        return re.sub(r"(https?://[^\s?#]+)[?#][^\s]*", r"\1?[redacted]", value)
    return value


class Report:
    def __init__(self, config, command):
        self.directory = config.logging_directory
        run_id = datetime.now(UTC).strftime("%Y-%m-%dT%H%M%SZ_") + uuid.uuid4().hex[:12]
        self.path = self.directory / run_id
        self.path.mkdir(parents=True, exist_ok=False)
        self.result = {
            "schema_version": 1,
            "run_id": run_id,
            "command": command,
            "status": "running",
            "started_at": storage.now(),
            "finished_at": None,
            "config_path": str(config.path),
            "archive_root": str(config.archive_root),
            "sources": [],
            "errors": [],
            "result_path": str(self.path / "result.json"),
            "events_path": str(self.path / "events.jsonl"),
        }
        storage.json_write(self.path / "result.json", self.result)
        storage.json_write(self.directory / "latest.json", {"schema_version": 1, "run_id": run_id})
        self.event("info", "run_started")

    def event(self, level, event, **fields):
        row = redact(
            {
                "schema_version": 1,
                "run_id": self.result["run_id"],
                "time": storage.now(),
                "level": level,
                "event": event,
                **fields,
            }
        )
        with (self.path / "events.jsonl").open("a") as f:
            f.write(json.dumps(row, ensure_ascii=False) + "\n")
            f.flush()

    def finish(self, status, sources=None, error=None):
        self.result.update(status=status, finished_at=storage.now(), sources=sources or [])
        if error:
            self.result["errors"].append(redact(str(error)))
        counts = (
            "episodes",
            "advertised",
            "downloaded",
            "unchanged",
            "local_candidates",
            "locally_transcribed",
            "failed",
        )
        self.result["totals"] = {k: sum(r.get(k, 0) for r in self.result["sources"]) for k in counts}
        self.result = redact(self.result)
        storage.json_write(self.path / "result.json", self.result)
        self.event("info" if status == "success" else "error", "run_finished", status=status)
        return self.result


def read(directory, run_id):
    if run_id == "latest":
        pointer = storage.json_read(directory / "latest.json")
        if not pointer:
            raise ValueError("No saved runs")
        run_id = pointer["run_id"]
    if not re.fullmatch(r"\d{4}-\d\d-\d\dT\d{6}Z_[a-f0-9]{12}", run_id):
        raise ValueError("Invalid run ID")
    path = storage.inside(directory, f"{run_id}/result.json")
    if not path.is_file():
        raise ValueError(f"Run not found: {run_id}")
    result = storage.json_read(path)
    if result["status"] == "running":
        result["completion"] = "No completion recorded; the run may be active or interrupted"
    return result


def list_runs(directory, limit):
    if not directory.is_dir():
        return []
    paths = sorted(directory.glob("*/result.json"), reverse=True)
    return [read(directory, p.parent.name) for p in paths[:limit]]
