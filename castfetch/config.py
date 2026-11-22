"""One validated YAML configuration; no archive-specific setup or conversion."""

import os
import re
from dataclasses import dataclass
from pathlib import Path

import yaml


class ConfigError(ValueError):
    pass


class UniqueLoader(yaml.SafeLoader):
    pass


def mapping(loader, node):
    result = {}
    for key_node, value_node in node.value:
        key = loader.construct_object(key_node)
        if not isinstance(key, str):
            raise ConfigError("YAML keys must be strings")
        if key in result:
            raise ConfigError(f"Duplicate YAML key: {key}")
        result[key] = loader.construct_object(value_node)
    return result


UniqueLoader.add_constructor(yaml.resolver.BaseResolver.DEFAULT_MAPPING_TAG, mapping)


def section(value, allowed, label):
    if not isinstance(value, dict):
        raise ConfigError(f"{label} must be a mapping")
    unknown = set(value) - set(allowed)
    if unknown:
        raise ConfigError(f"Unknown {label} keys: {', '.join(sorted(unknown))}")
    return value


def text(value, label):
    if not isinstance(value, str) or not value.strip():
        raise ConfigError(f"{label} must be a non-empty string")
    return value.strip()


def positive(value, label):
    if type(value) is not int or value <= 0:
        raise ConfigError(f"{label} must be a positive integer")
    return value


def resolve_path(value, base, label, *, preserve_symlink=False):
    path = Path(text(value, label)).expanduser()
    path = path if path.is_absolute() else base / path
    # Invoking a venv's symlink is essential: its location selects pyvenv.cfg.
    return Path(os.path.abspath(path)) if preserve_symlink else path.resolve()


@dataclass(frozen=True)
class Source:
    id: str
    name: str
    type: str
    enabled: bool
    transcription: str
    initial_days: int | None
    feed_url: str = ""
    username: str = ""
    caption_contains: str = ""
    language: str | None = None


@dataclass(frozen=True)
class Config:
    path: Path
    archive_root: Path
    logging_directory: Path
    sources: tuple[Source, ...]
    formats: tuple[str, ...]
    transcription: dict
    max_feed_bytes: int = 52428800
    max_transcript_bytes: int = 52428800


def locate(explicit=None):
    if explicit:
        path = Path(explicit).expanduser().resolve()
        if not path.is_file():
            raise ConfigError(f"Configuration not found: {path}")
        return path
    for path in (Path.cwd() / "config.yaml", Path.home() / ".castfetch/config.yaml"):
        if path.is_file():
            return path.resolve()
    raise ConfigError(
        "No config.yaml found. Copy the example:\n"
        "  mkdir -p ~/.castfetch\n"
        '  cp "$(brew --prefix castfetch)/share/castfetch/config.example.yaml" ~/.castfetch/config.yaml\n'
        "Edit it, then run: castfetch config check"
    )


def load(explicit=None):
    path = locate(explicit)
    try:
        data = yaml.load(path.read_text(), Loader=UniqueLoader)
    except yaml.YAMLError as exc:
        raise ConfigError(f"Invalid YAML: {exc}") from exc
    data = section(
        data,
        [
            "version",
            "archive_root",
            "formats",
            "transcription",
            "logging",
            "sources",
            "max_feed_bytes",
            "max_transcript_bytes",
        ],
        "configuration",
    )
    if type(data.get("version")) is not int or data["version"] != 1:
        raise ConfigError("version must be 1")
    base = path.parent
    archive = resolve_path(data.get("archive_root"), base, "archive_root")
    logs = section(data.get("logging", {}), ["directory"], "logging")
    log_dir = resolve_path(logs.get("directory", "~/.castfetch/runs"), base, "logging.directory")
    raw = section(
        data.get("transcription", {}), ["python", "model", "cache_dir", "max_audio_bytes"], "transcription"
    )
    settings = {
        "model": text(raw.get("model", "mlx-community/whisper-large-v3-turbo"), "model"),
        "max_audio_bytes": positive(raw.get("max_audio_bytes", 1073741824), "max_audio_bytes"),
    }
    for key in ("python", "cache_dir"):
        if key in raw:
            settings[key] = str(
                resolve_path(raw[key], base, f"transcription.{key}", preserve_symlink=key == "python")
            )
    formats = data.get("formats", ["text/vtt", "application/json"])
    supported = {
        "text/vtt",
        "application/json",
        "text/plain",
        "application/srt",
        "application/x-subrip",
        "text/srt",
    }
    if (
        not isinstance(formats, list)
        or not formats
        or any(not isinstance(f, str) or f not in supported for f in formats)
    ):
        raise ConfigError(f"formats must be a non-empty list drawn from {sorted(supported)}")
    sources = data.get("sources")
    if not isinstance(sources, list) or not sources:
        raise ConfigError("sources must be a non-empty list")
    result = []
    for source in sources:
        s = section(
            source,
            [
                "id",
                "name",
                "type",
                "enabled",
                "feed_url",
                "username",
                "caption_contains",
                "transcription",
                "initial_sync",
                "language",
            ],
            "source",
        )
        sid = text(s.get("id"), "source.id")
        if not re.fullmatch(r"[a-z0-9]+(?:-[a-z0-9]+)*", sid) or sid in {x.id for x in result}:
            raise ConfigError(f"Invalid or duplicate source id: {sid}")
        kind = s.get("type")
        if kind not in ("podcast", "instagram"):
            raise ConfigError(f"{sid}: type must be podcast or instagram")
        enabled = s.get("enabled", True)
        if type(enabled) is not bool:
            raise ConfigError(f"{sid}: enabled must be true or false")
        mode = s.get("transcription", "publisher")
        if mode not in ("publisher", "local"):
            raise ConfigError(f"{sid}: transcription must be publisher or local")
        initial = s.get("initial_sync", {"days": 30})
        if initial == "all":
            days = None
        else:
            section(initial, ["days"], f"{sid}.initial_sync")
            days = positive(initial.get("days"), f"{sid}.initial_sync.days")
        kwargs = {}
        if kind == "podcast":
            if "username" in s or "caption_contains" in s:
                raise ConfigError(f"{sid}: Instagram fields are not valid for podcasts")
            url = text(s.get("feed_url"), f"{sid}.feed_url")
            from urllib.parse import urlparse

            if urlparse(url).scheme not in ("https", "http") or not urlparse(url).netloc:
                raise ConfigError(f"{sid}: feed_url must be an HTTP(S) URL")
            kwargs["feed_url"] = url
        else:
            if "feed_url" in s:
                raise ConfigError(f"{sid}: feed_url is not valid for Instagram")
            username = text(s.get("username"), f"{sid}.username")
            if not re.fullmatch(r"[A-Za-z0-9_.]{1,30}", username):
                raise ConfigError(f"{sid}: invalid username (omit @)")
            kwargs.update(
                username=username, caption_contains=text(s.get("caption_contains"), f"{sid}.caption_contains")
            )
        language = text(s["language"], f"{sid}.language") if "language" in s else None
        result.append(
            Source(
                sid,
                text(s.get("name"), f"{sid}.name"),
                kind,
                enabled,
                mode,
                days,
                language=language,
                **kwargs,
            )
        )
    return Config(
        path,
        archive,
        log_dir,
        tuple(result),
        tuple(formats),
        settings,
        positive(data.get("max_feed_bytes", 52428800), "max_feed_bytes"),
        positive(data.get("max_transcript_bytes", 52428800), "max_transcript_bytes"),
    )
