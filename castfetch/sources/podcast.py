"""Direct RSS/Atom podcast feeds and publisher transcript discovery."""

import hashlib
import xml.etree.ElementTree as ET
from datetime import UTC, datetime
from email.utils import parsedate_to_datetime
from typing import Any

from ..network import fetch


def local_name(tag: str) -> str:
    return tag.rsplit("}", 1)[-1].lower()


def clean_text(value: str | None) -> str:
    return (value or "").strip()


def child_text(element: ET.Element, names: set[str]) -> str:
    for child in element:
        if local_name(child.tag) in names and clean_text(child.text):
            return clean_text(child.text)
    return ""


def publication_date(value: str) -> str:
    if not value:
        return ""
    try:
        parsed = parsedate_to_datetime(value)
    except (TypeError, ValueError):
        try:
            parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        except ValueError:
            return value
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=UTC)
    return parsed.astimezone(UTC).replace(microsecond=0).isoformat()


def episode_link(item: ET.Element) -> str:
    for child in item:
        if local_name(child.tag) != "link":
            continue
        href = clean_text(child.attrib.get("href"))
        rel = clean_text(child.attrib.get("rel")).lower()
        if href and rel in {"", "alternate"}:
            return href
        if clean_text(child.text):
            return clean_text(child.text)
    return ""


def parse_feed(data: bytes, allowed_formats: set[str]) -> list[dict[str, Any]]:
    root = ET.fromstring(data)
    items = [node for node in root.iter() if local_name(node.tag) in {"entry", "item"}]
    episodes: list[dict[str, Any]] = []
    for item in items:
        title = child_text(item, {"title"})
        published_raw = child_text(item, {"pubdate", "published", "updated"})
        published = publication_date(published_raw)
        link = episode_link(item)
        guid = child_text(item, {"guid", "id"})
        episode_number = child_text(item, {"episode"})
        enclosure = ""
        for child in item:
            if local_name(child.tag) == "enclosure":
                enclosure = clean_text(child.attrib.get("url"))
                break
            if local_name(child.tag) == "link" and child.attrib.get("rel") == "enclosure":
                enclosure = clean_text(child.attrib.get("href"))
                break

        identity = guid or link or enclosure or f"{published}|{title}"
        digest = hashlib.sha256(identity.encode("utf-8")).hexdigest()[:12]
        transcript_entries: list[dict[str, str]] = []
        seen_urls: set[str] = set()
        for child in item.iter():
            if local_name(child.tag) != "transcript":
                continue
            url = clean_text(child.attrib.get("url") or child.attrib.get("href"))
            mime = clean_text(child.attrib.get("type")).lower().split(";", 1)[0]
            if not url or url in seen_urls or (allowed_formats and mime not in allowed_formats):
                continue
            seen_urls.add(url)
            transcript_entries.append({"source_url": url, "mime_type": mime})

        episodes.append(
            {
                "episode_key": digest,
                "episode_number": episode_number,
                "title": title,
                "published": published,
                "published_raw": published_raw,
                "episode_url": link,
                "guid": guid,
                "audio_url": enclosure,
                # Detect publisher support even for formats not selected for download.
                "publisher_transcripts_advertised": any(
                    local_name(child.tag) == "transcript" for child in item.iter()
                ),
                "transcripts": transcript_entries,
            }
        )
    return episodes


def discover(source, config):
    data, url = fetch(
        source.feed_url,
        config.max_feed_bytes,
        "application/rss+xml, application/atom+xml, application/xml, text/xml",
    )
    try:
        episodes = parse_feed(data, set(config.formats))
    except ET.ParseError as exc:
        raise ValueError("Podcast response is not a valid XML feed") from exc
    if local_name(ET.fromstring(data).tag) not in ("rss", "feed", "rdf"):
        raise ValueError("Expected a direct RSS/Atom feed, not a web page")
    return episodes, {"feed_url": url}, data
