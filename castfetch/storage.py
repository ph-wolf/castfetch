"""Native archive IO: replace machine state, never replace transcript files."""

import fcntl
import hashlib
import json
import os
import tempfile
from contextlib import contextmanager
from datetime import UTC, datetime
from pathlib import Path


def now():
    return datetime.now(UTC).replace(microsecond=0).isoformat()


def digest(path):
    h = hashlib.sha256()
    with Path(path).open("rb") as f:
        for chunk in iter(lambda: f.read(1024 * 1024), b""):
            h.update(chunk)
    return h.hexdigest()


def inside(root, relative):
    root = Path(root).resolve()
    path = (root / relative).resolve()
    if not path.is_relative_to(root) or path == root:
        raise ValueError(f"Path escapes archive: {relative}")
    return path


def write(path, data, *, replace=True):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary = tempfile.mkstemp(prefix=".castfetch-", dir=path.parent)
    try:
        with os.fdopen(fd, "wb") as f:
            f.write(data)
            f.flush()
            os.fsync(f.fileno())
        if replace:
            os.replace(temporary, path)
        else:
            os.link(temporary, path)
    finally:
        Path(temporary).unlink(missing_ok=True)


def json_write(path, data):
    write(path, (json.dumps(data, ensure_ascii=False, indent=2) + "\n").encode())


def json_read(path, default=None):
    return json.loads(Path(path).read_text()) if Path(path).exists() else default


def index_read(root, sid):
    path = inside(root, f"{sid}/index.jsonl")
    rows = {}
    if path.exists():
        for line in path.read_text().splitlines():
            if line.strip():
                row = json.loads(line)
                if row["episode_key"] in rows:
                    raise ValueError(f"Duplicate episode in index: {sid}/{row['episode_key']}")
                rows[row["episode_key"]] = row
    return rows


def index_write(root, sid, rows):
    ordered = sorted(rows.values(), key=lambda r: (r.get("published", ""), r["episode_key"]))
    write(
        inside(root, f"{sid}/index.jsonl"),
        "".join(json.dumps(r, ensure_ascii=False) + "\n" for r in ordered).encode(),
    )


@contextmanager
def archive_lock(root):
    root = Path(root)
    root.mkdir(parents=True, exist_ok=True)
    with (root / ".castfetch.lock").open("a") as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            raise ValueError("Another castfetch process is writing this archive") from exc
        try:
            yield
        finally:
            fcntl.flock(lock, fcntl.LOCK_UN)
