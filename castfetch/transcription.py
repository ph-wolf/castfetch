"""External MLX Whisper integration, verified timestamps and deterministic exports."""

import json
import math
import re
import subprocess
import tempfile
from pathlib import Path

from . import network, storage
from .sources.instagram import download_audio


class TimestampError(ValueError):
    pass


def validate(result):
    if not isinstance(result, dict) or not result.get("segments") or not result.get("text", "").strip():
        raise ValueError("Transcription returned no speech")
    duration = float(result["audio_duration"])
    if not math.isfinite(duration) or duration <= 0:
        raise ValueError("Invalid audio duration")
    previous = -1
    for segment in result["segments"]:
        start, end = float(segment["start"]), float(segment["end"])
        if (
            not math.isfinite(start)
            or not math.isfinite(end)
            or not 0 <= start <= end <= duration + 1
            or start < previous
        ):
            raise TimestampError("Invalid or out-of-order transcript timestamps")
        if not isinstance(segment["text"], str):
            raise ValueError("Invalid segment text")
        previous = start


def timestamp(seconds):
    ms = round(seconds * 1000)
    return f"{ms // 3600000:02}:{ms // 60000 % 60:02}:{ms // 1000 % 60:02}.{ms % 1000:03}"


def stem(episode):
    date = episode.get("published", "")[:10]
    if not re.fullmatch(r"\d{4}-\d{2}-\d{2}", date):
        date = "unknown-date"
    number = (
        re.sub(r"[^A-Za-z0-9._-]+", "-", episode.get("episode_number", "")).strip("-._")[:80] or "episode"
    )
    return f"{date}_{number}_{episode['episode_key']}"


def generate(source, episode, settings):
    """Return payloads after validation; temporary media never enters the archive."""
    python = settings.get("python")
    if not python or not Path(python).is_file():
        raise ValueError(
            "Configure transcription.python with an environment containing mlx_whisper; run castfetch doctor"
        )
    with tempfile.TemporaryDirectory(prefix="castfetch-") as directory:
        directory = Path(directory)
        if source.type == "instagram":
            audio = download_audio(episode["audio_url"], directory, settings)
        else:
            audio = directory / "audio"
            network.download(episode["audio_url"], audio, settings["max_audio_bytes"])
        audio_hash = storage.digest(audio)
        output = directory / "result.json"
        command = [
            python,
            str(Path(__file__).with_name("worker.py")),
            str(audio),
            str(output),
            "--model",
            settings["model"],
        ]
        if settings.get("cache_dir"):
            command += ["--cache-dir", settings["cache_dir"]]
        if source.language:
            command += ["--language", source.language]
        subprocess.run(command, check=True, capture_output=True, text=True)
        result = json.loads(output.read_text())
        retried = False
        try:
            validate(result)
        except TimestampError:
            subprocess.run([*command, "--align-words"], check=True, capture_output=True, text=True)
            result = json.loads(output.read_text())
            validate(result)
            retried = True
    provenance = {
        "origin": "locally_generated",
        "model": settings["model"],
        "audio_source_url": episode["audio_url"],
        "audio_sha256": audio_hash,
        "created_at": storage.now(),
    }
    if retried:
        provenance["timestamp_retry"] = "word_alignment_without_previous_text"
    result["provenance"] = provenance
    vtt = (
        "WEBVTT\n\n"
        + "\n\n".join(
            f"{timestamp(s['start'])} --> {timestamp(s['end'])}\n{s['text'].strip()}"
            for s in result["segments"]
        )
        + "\n"
    )
    markdown = (
        f"# {episode['title']}\n\nPublished: {episode['published']}\nSource: {episode['episode_url']}\n\n"
    )
    markdown += f"Automatically generated locally with {settings['model']}; not manually corrected.\n\n"
    markdown += (
        "\n\n".join(f"[{timestamp(s['start'])}] {s['text'].strip()}" for s in result["segments"]) + "\n"
    )
    return {
        ".local.vtt": vtt.encode(),
        ".local.json": (json.dumps(result, ensure_ascii=False, indent=2) + "\n").encode(),
        ".local.md": markdown.encode(),
    }, provenance
