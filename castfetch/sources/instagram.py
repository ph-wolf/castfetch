"""Filtered public Instagram video discovery, without cookies or login."""

import hashlib
import json
import re
import subprocess
import unicodedata
from datetime import UTC, datetime
from html.parser import HTMLParser
from pathlib import Path

from ..network import fetch


class ScriptParser(HTMLParser):
    def __init__(self):
        super().__init__()
        self.active = False
        self.scripts = []

    def handle_starttag(self, tag, attrs):
        if tag == "script":
            self.active = True

    def handle_endtag(self, tag):
        if tag == "script":
            self.active = False

    def handle_data(self, text):
        if self.active:
            self.scripts.append(text)


def folded(text):
    return " ".join(
        "".join(c for c in unicodedata.normalize("NFKD", text) if not unicodedata.combining(c))
        .casefold()
        .split()
    )


def parse_profile(data, source):
    """Read JSON payloads, including nested JSON strings in Instagram's public embed."""
    parser = ScriptParser()
    parser.feed(data.decode("utf-8"))
    nodes = {}
    username = source["username"].casefold()

    def walk(value, depth=0):
        if depth > 40:
            return
        if isinstance(value, dict):
            if value.get("shortcode") and "taken_at_timestamp" in value:
                if value.get("owner", {}).get("username", "").casefold() != username:
                    raise ValueError("Instagram post owner does not match configured account")
                nodes[value["shortcode"]] = value
            for child in value.values():
                walk(child, depth + 1)
        elif isinstance(value, list):
            for child in value:
                walk(child, depth + 1)
        elif isinstance(value, str) and "shortcode" in value:
            try:
                decoded = json.loads(value)
            except ValueError:
                return
            walk(decoded, depth + 1)

    decoder = json.JSONDecoder()
    for script in parser.scripts:
        try:
            document = json.loads(script)
        except ValueError:
            document = None
        if document is not None:
            walk(document)
        for match in re.finditer(r"\.handle\(", script):
            try:
                document, _ = decoder.raw_decode(script, match.end())
            except ValueError:
                continue
            walk(document)
    if not nodes:
        raise ValueError(
            "Instagram public embed returned no dated posts; discovery unavailable (no login attempted)"
        )

    episodes = []
    dates = []
    required = folded(source.get("caption_contains", ""))
    for shortcode, node in nodes.items():
        if not re.fullmatch(r"[A-Za-z0-9_-]+", shortcode):
            raise ValueError("Invalid Instagram post identifier")
        published = (
            datetime.fromtimestamp(float(node["taken_at_timestamp"]), UTC).replace(microsecond=0).isoformat()
        )
        dates.append(published)
        caption = "\n".join(
            edge["node"].get("text", "") for edge in node.get("edge_media_to_caption", {}).get("edges", [])
        )
        if (
            not node.get("is_video")
            or node.get("__typename") == "GraphSidecar"
            or node.get("edge_sidecar_to_children")
            or (required and required not in folded(caption))
        ):
            continue
        url = f"https://www.instagram.com/p/{shortcode}/"
        guid = f"instagram:{source['username'].casefold()}:{shortcode}"
        episodes.append(
            {
                "episode_key": hashlib.sha256(guid.encode()).hexdigest()[:12],
                "episode_number": shortcode,
                "title": caption.splitlines()[0] if caption else shortcode,
                "caption": caption,
                "published": published,
                "published_raw": str(node["taken_at_timestamp"]),
                "episode_url": url,
                "guid": guid,
                "audio_url": url,
                "transcripts": [],
                "source_type": "instagram",
                "instagram_username": source["username"],
                "instagram_shortcode": shortcode,
            }
        )
    return sorted(episodes, key=lambda e: (e["published"], e["episode_key"])), {
        "visible_posts": len(nodes),
        "oldest_visible": min(dates),
        "latest_visible": max(dates),
    }


def download_audio(url, directory, settings):
    directory = Path(directory)
    limit = int(settings.get("max_audio_bytes", 1073741824))
    subprocess.run(
        [
            settings.get("yt_dlp", "yt-dlp"),
            "--ignore-config",
            "--no-cache-dir",
            "--no-playlist",
            "--socket-timeout",
            "30",
            "--retries",
            "1",
            "--no-progress",
            "--max-filesize",
            str(limit),
            "--format",
            "bestaudio/best",
            "--output",
            str(directory / "media.%(ext)s"),
            url,
        ],
        check=True,
        capture_output=True,
        text=True,
    )
    media = [p for p in directory.glob("media.*") if p.is_file() and p.suffix not in {".part", ".ytdl"}]
    if len(media) != 1 or not 0 < media[0].stat().st_size <= limit:
        raise ValueError("Instagram audio download missing, ambiguous or exceeds size limit")
    return media[0]


def discover(source, config):
    data, _ = fetch(f"https://www.instagram.com/{source.username}/embed/", config.max_feed_bytes, "text/html")
    episodes, details = parse_profile(
        data, {"username": source.username, "caption_contains": source.caption_contains}
    )
    return episodes, details, None
