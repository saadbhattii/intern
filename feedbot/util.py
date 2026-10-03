"""Small, dependency-free helpers shared across the package."""

from __future__ import annotations

import hashlib
import html
import re
from dataclasses import dataclass
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

# Query parameters that identify the *visitor*, not the *article*. Stripping them
# means the same post shared with different tracking tags is recognised as one item.
_TRACKING_KEYS = {
    "fbclid", "gclid", "dclid", "msclkid", "mc_cid", "mc_eid", "_hsenc", "_hsmi",
    "ref_src", "igshid", "yclid", "spm", "s_cid",
}

_TAG_RE = re.compile(r"<[^>]+>")
_WS_RE = re.compile(r"\s+")


@dataclass
class Item:
    """One article: the only things we ever post are its title and link."""

    title: str
    link: str
    guid: str = ""
    published: datetime | None = None

    def keys(self) -> list[str]:
        """Stable identifiers used for 'have we already posted this?' checks.

        Two keys are kept on purpose: some sites change GUIDs but keep URLs, others
        change URL formatting but keep GUIDs. Matching *either* counts as seen.
        """
        out = [hash_key(normalize_url(self.link))]
        if self.guid and self.guid != self.link:
            out.append(hash_key("guid:" + self.guid.strip()))
        return out


def hash_key(value: str) -> str:
    """Short, stable fingerprint (keeps the committed state file small)."""
    return hashlib.sha256(value.encode("utf-8", "replace")).hexdigest()[:20]


def normalize_url(url: str) -> str:
    """Canonical form of a URL for de-duplication (never used for posting)."""
    url = (url or "").strip()
    try:
        parts = urlsplit(url)
    except ValueError:
        return url
    scheme = (parts.scheme or "https").lower()
    if scheme == "http":
        scheme = "https"
    netloc = parts.netloc.lower()
    if netloc.startswith("www."):
        netloc = netloc[4:]
    if netloc.endswith(":443") or netloc.endswith(":80"):
        netloc = netloc.rsplit(":", 1)[0]
    path = parts.path or "/"
    if len(path) > 1:
        path = path.rstrip("/") or "/"
    query = [
        (k, v)
        for k, v in parse_qsl(parts.query, keep_blank_values=True)
        if not k.lower().startswith("utm_") and k.lower() not in _TRACKING_KEYS
    ]
    return urlunsplit((scheme, netloc, path, urlencode(sorted(query)), ""))


def clean_text(value: str | None) -> str:
    """Strip tags, decode entities (twice, for double-escaped feeds), collapse spaces."""
    if not value:
        return ""
    text = html.unescape(_TAG_RE.sub(" ", value))
    text = html.unescape(text)
    return _WS_RE.sub(" ", text).strip()


def parse_date(value: str | None) -> datetime | None:
    """Parse RFC 822 (RSS) or ISO 8601 (Atom) dates. Always returns aware UTC or None."""
    if not value:
        return None
    value = value.strip()
    if not value:
        return None
    dt: datetime | None = None
    try:
        dt = parsedate_to_datetime(value)
    except (TypeError, ValueError, IndexError):
        dt = None
    if dt is None:
        iso = value.replace("Z", "+00:00").replace("z", "+00:00")
        # Some feeds use a space instead of 'T', or append fractional seconds oddly.
        for candidate in (iso, iso.replace(" ", "T", 1), iso[:25], iso[:19], iso[:10]):
            try:
                dt = datetime.fromisoformat(candidate)
                break
            except ValueError:
                continue
    if dt is None:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    dt = dt.astimezone(timezone.utc)
    # Reject obviously broken dates (epoch placeholders, far future typos).
    if dt.year < 1995 or dt.year > 2100:
        return None
    return dt


def format_date(dt: datetime | None) -> str:
    if dt is None:
        return ""
    return f"{dt.strftime('%b')} {dt.day}, {dt.year}"


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


def truncate(text: str, limit: int) -> str:
    if len(text) <= limit:
        return text
    return text[: max(0, limit - 1)].rstrip() + "…"
