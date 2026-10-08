"""Find an article's preview image (og:image) for branded cards.

Only used when a feed did not supply an image and `preview_image` is not "none".
One quick request per posted article (a handful per run), never more: a slow or
broken page simply means the card has no picture.
"""

from __future__ import annotations

import re
from typing import Callable
from urllib.parse import urljoin

from .http import FetchError, fetch as http_fetch

_META = re.compile(r"<meta\b[^>]*>", re.I)
_ATTR = re.compile(r'([a-zA-Z:_-]+)\s*=\s*("([^"]*)"|\'([^\']*)\')')
_WANTED = ("og:image:secure_url", "og:image", "twitter:image", "twitter:image:src")


def find_image(page_url: str, fetch: Callable = http_fetch) -> str:
    try:
        resp = fetch(page_url, timeout=8, attempts=1)
    except (FetchError, Exception):  # noqa: BLE001 - a missing picture is never an error
        return ""
    if resp.status != 200:
        return ""
    head = resp.body[:300_000].decode("utf-8", "replace")
    found: dict[str, str] = {}
    for tag in _META.findall(head):
        attrs = {m.group(1).lower(): (m.group(3) if m.group(3) is not None else m.group(4)) for m in _ATTR.finditer(tag)}
        key = (attrs.get("property") or attrs.get("name") or "").lower()
        if key in _WANTED and attrs.get("content") and key not in found:
            found[key] = attrs["content"].strip()
    for key in _WANTED:
        if key in found:
            url = urljoin(resp.url, found[key])
            if url.startswith("https://") and len(url) <= 1000:
                return url
    return ""
