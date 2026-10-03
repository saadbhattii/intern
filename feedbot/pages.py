"""Feed autodiscovery and article extraction from HTML listing pages.

Many company sites (IBM, Quantinuum, Riverlane, ...) publish no RSS feed. For
those, the bot reads the blog/news *listing page* and extracts article links.

How links are chosen (no per-site code required)
1. If the source sets `link_pattern`, only absolute URLs matching that regex count.
2. Otherwise, links *under the listing path* count: for https://x.com/news the
   articles are https://x.com/news/<something>. This matches most CMSs.
3. If (2) finds nothing, a slug heuristic is used: same host, and the last path
   segment looks like an article slug (several hyphenated words).
Anchors inside <nav> and <footer> are ignored, as are pagination, tag, category
and author pages.

Why imperfect extraction is safe: the first time a source is seen, everything
currently on the page is recorded as "already seen" and nothing is posted. Only
links that *appear later* are posted, so stray menu links never spam a channel.
"""

from __future__ import annotations

import re
from html.parser import HTMLParser
from urllib.parse import urljoin, urlsplit

from .util import Item, clean_text, normalize_url

MAX_HTML_ITEMS = 60

_FEED_TYPES = ("application/rss+xml", "application/atom+xml", "application/rdf+xml")
_SKIP_SEGMENTS = re.compile(
    r"/(page|category|categories|tag|tags|topic|topics|author|authors|search|feed|rss|login|"
    r"signup|subscribe|wp-content|wp-json|cdn-cgi|events?|careers?|jobs?|contact|about|privacy|"
    r"legal|terms|cookie[s-]?\w*)(/|$)",
    re.I,
)
_FILE_EXT = re.compile(r"\.(pdf|jpe?g|png|gif|svg|webp|mp4|mp3|zip|xml|json|css|js|ico)$", re.I)
_GENERIC_TEXT = {
    "read more", "learn more", "continue reading", "read", "more", "read article",
    "read the blog", "read post", "view", "view more", "see more", "details", "here",
    "click here", "read story", "full story", "→", "»", "read now", "watch", "listen",
}
_SLUG_RE = re.compile(r"^[a-z0-9]+(?:[-_][a-z0-9]+){2,}$", re.I)


class _PageParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.feed_links: list[tuple[str, str]] = []  # (href, title)
        self.anchors: list[tuple[str, str, str]] = []  # (href, text, heading-context)
        self._skip_depth = 0
        self._ignored_depth = 0
        self._anchor: dict | None = None
        self._heading_depth = 0
        self._heading_text: list[str] = []
        self.last_heading = ""
        self._anchors_since_heading = 99

    def handle_starttag(self, tag, attrs):
        a = {k.lower(): (v or "") for k, v in attrs}
        if tag in ("script", "style", "noscript", "template", "svg"):
            self._skip_depth += 1
            return
        if tag in ("nav", "footer"):
            self._ignored_depth += 1
        if tag == "link":
            rel = a.get("rel", "").lower()
            ctype = a.get("type", "").lower()
            if "alternate" in rel and any(t in ctype for t in _FEED_TYPES) and a.get("href"):
                self.feed_links.append((a["href"], a.get("title", "")))
            return
        if tag in ("h1", "h2", "h3", "h4"):
            self._heading_depth += 1
            self._heading_text = []
        if tag == "a":
            if self._anchor is not None:  # unclosed <a>: close it implicitly
                self._finish_anchor()
            self._anchor = {
                "href": a.get("href", ""),
                "label": a.get("aria-label", "") or a.get("title", ""),
                "text": [],
                "ignored": self._ignored_depth > 0,
            }

    def handle_endtag(self, tag):
        if tag in ("script", "style", "noscript", "template", "svg"):
            self._skip_depth = max(0, self._skip_depth - 1)
            return
        if tag in ("nav", "footer"):
            self._ignored_depth = max(0, self._ignored_depth - 1)
        if tag in ("h1", "h2", "h3", "h4") and self._heading_depth:
            self._heading_depth -= 1
            text = clean_text(" ".join(self._heading_text))
            if text:
                self.last_heading = text
                self._anchors_since_heading = 0
        if tag == "a" and self._anchor is not None:
            self._finish_anchor()

    def handle_data(self, data):
        if self._skip_depth:
            return
        if self._anchor is not None:
            self._anchor["text"].append(data)
        if self._heading_depth:
            self._heading_text.append(data)

    def _finish_anchor(self):
        anchor, self._anchor = self._anchor, None
        if anchor is None or anchor["ignored"] or not anchor["href"]:
            return
        text = clean_text(" ".join(anchor["text"])) or clean_text(anchor["label"])
        context = self.last_heading if self._anchors_since_heading <= 2 else ""
        self._anchors_since_heading += 1
        self.anchors.append((anchor["href"], text, context))


def _parse(page_html: str) -> _PageParser:
    parser = _PageParser()
    try:
        parser.feed(page_html)
        parser.close()
    except Exception:  # noqa: BLE001 - HTMLParser is lenient; keep whatever was parsed
        pass
    return parser


def discover_feeds(page_html: str, page_url: str) -> list[str]:
    """Feed URLs advertised via <link rel="alternate">, comment feeds excluded."""
    parser = _parse(page_html)
    found: list[str] = []
    for href, title in parser.feed_links:
        url = urljoin(page_url, href.strip())
        lowered = (url + " " + title).lower()
        if "comment" in lowered:
            continue
        if url.startswith(("http://", "https://")) and url not in found:
            found.append(url)
    return found


def _host(url: str) -> str:
    host = urlsplit(url).netloc.lower()
    return host[4:] if host.startswith("www.") else host


def _slug_from_url(url: str) -> str:
    seg = [s for s in urlsplit(url).path.split("/") if s]
    if not seg:
        return url
    words = re.split(r"[-_]+", seg[-1])
    return " ".join(w for w in words if w).capitalize()


def _pick_title(texts: list[str], url: str) -> str:
    good = [t for t in texts if t and t.lower().strip(" .:›»→") not in _GENERIC_TEXT and len(t) >= 6]
    if good:
        # Card layouts often have several anchors per article (image, title, "read more").
        # The descriptive title is almost always the longest sensible one, but avoid
        # paragraphs of teaser text (we never post summaries).
        short_enough = [t for t in good if len(t) <= 200] or good
        return max(short_enough, key=len)
    return _slug_from_url(url)


def extract_links(page_html: str, page_url: str, link_pattern: str | None = None) -> list[Item]:
    parser = _parse(page_html)
    page_norm = normalize_url(page_url)
    page_host = _host(page_url)
    base_path = urlsplit(page_url).path.rstrip("/")
    custom = re.compile(link_pattern) if link_pattern else None

    grouped: dict[str, dict] = {}
    order: list[str] = []
    for href, text, context in parser.anchors:
        href = href.strip()
        if not href or href.startswith(("#", "mailto:", "tel:", "javascript:")):
            continue
        url = urljoin(page_url, href).split("#", 1)[0]
        if not url.startswith(("http://", "https://")):
            continue
        key = normalize_url(url)
        if key == page_norm:
            continue
        if key not in grouped:
            grouped[key] = {"url": url, "texts": []}
            order.append(key)
        grouped[key]["texts"].append(text)
        if context:
            grouped[key]["texts"].append(context)

    def accepted(url: str, rule: str) -> bool:
        parts = urlsplit(url)
        path = parts.path
        if _FILE_EXT.search(path) or "page=" in parts.query.lower():
            return False
        if rule == "custom":
            return bool(custom.search(url))
        if _host(url) != page_host:
            return False
        rest = path[len(base_path):] if path.startswith(base_path + "/") else None
        if rule == "under-listing":
            if rest is None or not rest.strip("/"):
                return False
            return not _SKIP_SEGMENTS.search("/" + rest.strip("/"))
        if rule == "slug":
            if _SKIP_SEGMENTS.search(path):
                return False
            segments = [s for s in path.split("/") if s]
            return bool(segments) and bool(_SLUG_RE.match(segments[-1]))
        return False

    rules = ["custom"] if custom else ["under-listing", "slug"]
    for rule in rules:
        items = []
        for key in order:
            entry = grouped[key]
            if accepted(entry["url"], rule):
                items.append(Item(title=_pick_title(entry["texts"], entry["url"]), link=entry["url"]))
        if items:
            return items[:MAX_HTML_ITEMS]
    return []
