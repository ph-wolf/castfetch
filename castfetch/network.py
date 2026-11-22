"""Bounded public HTTP access."""

from urllib.parse import urlparse
from urllib.request import Request, urlopen


def request(url, accept="*/*"):
    parsed = urlparse(url)
    if parsed.scheme not in ("http", "https") or not parsed.netloc:
        raise ValueError("Only public HTTP(S) URLs are supported")
    return Request(url, headers={"User-Agent": "castfetch/0.1", "Accept": accept})


def fetch(url, limit, accept="*/*"):
    with urlopen(request(url, accept), timeout=60) as response:
        data = response.read(limit + 1)
        if len(data) > limit:
            raise ValueError("Response exceeds configured byte limit")
        return data, response.geturl()


def download(url, destination, limit):
    size = 0
    with urlopen(request(url), timeout=60) as response, destination.open("xb") as f:
        while chunk := response.read(1024 * 1024):
            size += len(chunk)
            if size > limit:
                raise ValueError("Audio exceeds configured byte limit")
            f.write(chunk)
    if not size:
        raise ValueError("Empty audio download")
