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

from .util import Item, clean_text, normalize_url, truncate

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
_VOID = {"area", "base", "br", "col", "embed", "hr", "img", "input", "link", "meta", "source", "track", "wbr"}
_SLUG_RE = re.compile(r"^[a-z0-9]+(?:[-_][a-z0-9]+){2,}$", re.I)


class _PageParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.feed_links: list[tuple[str, str]] = []  # (href, title)
        # (href, anchor text, nearby heading, heading inside the anchor, inside <main>/<article>)
        self.anchors: list[tuple[str, str, str, str, bool]] = []
        self._skip_depth = 0
        self._ignored_depth = 0
        self._main_depth = 0
        self._anchor: dict | None = None
        self._heading_depth = 0
        self._heading_text: list[str] = []
        self.last_heading = ""
        self._anchors_since_heading = 99
        self._stack: list[tuple[str, bool]] = []  # open elements: (tag, opened a main region)
        self.has_main = False

    def handle_starttag(self, tag, attrs):
        a = {k.lower(): (v or "") for k, v in attrs}
        if tag in ("script", "style", "noscript", "template", "svg"):
            self._skip_depth += 1
            return
        if tag in ("nav", "footer"):
            self._ignored_depth += 1
        is_main = tag in ("main", "article") or a.get("role", "").lower() == "main"
        if is_main:
            self._main_depth += 1
        if tag not in _VOID:
            self._stack.append((tag, is_main))
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
                "heading": [],
                "ignored": self._ignored_depth > 0,
                "in_main": self._main_depth > 0,
            }
            if self._main_depth > 0:
                self.has_main = True

    def handle_endtag(self, tag):
        if tag in ("script", "style", "noscript", "template", "svg"):
            self._skip_depth = max(0, self._skip_depth - 1)
            return
        if tag in ("nav", "footer"):
            self._ignored_depth = max(0, self._ignored_depth - 1)
        # Pop to the matching open element (tolerates unclosed children, ignores stray end tags).
        for i in range(len(self._stack) - 1, -1, -1):
            if self._stack[i][0] == tag:
                for _, opened_main in self._stack[i:]:
                    if opened_main:
                        self._main_depth = max(0, self._main_depth - 1)
                del self._stack[i:]
                break
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
                self._anchor["heading"].append(data)
        if self._heading_depth:
            self._heading_text.append(data)

    def _finish_anchor(self):
        anchor, self._anchor = self._anchor, None
        if anchor is None or anchor["ignored"] or not anchor["href"]:
            return
        text = clean_text(" ".join(anchor["text"])) or clean_text(anchor["label"])
        heading = clean_text(" ".join(anchor["heading"]))
        context = self.last_heading if self._anchors_since_heading <= 2 else ""
        self._anchors_since_heading += 1
        self.anchors.append((anchor["href"], text, context, heading, anchor["in_main"]))


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


_MONTH = r"(?:jan|feb|mar|apr|may|jun|jul|aug|sep|sept|oct|nov|dec)[a-z]*\.?"
_DATE = rf"(?:{_MONTH}\s+\d{{1,2}}(?:st|nd|rd|th)?,?\s+\d{{4}}|\d{{1,2}}\s+{_MONTH}\s+\d{{4}}|\d{{4}}-\d{{2}}-\d{{2}}|\d{{1,2}}[./]\d{{1,2}}[./]\d{{2,4}})"
_LABEL = r"(?:blog|news|press|business|events?|article|insights?|update|announcements?|featured|story|stories|case study|podcast|video|webinar|whitepaper|research|technical)"
_SEP = r"\s*(?:[|:·•–—-]\s*)?"
_HARD_SEP = r"\s*[|:·•–—-]\s*"
_LEADING_JUNK = re.compile(
    r"^(?:"
    r"press release\s+"                                   # unambiguous label
    rf"|{_LABEL}{_HARD_SEP}"                               # "News | ..." / "Blog: ..."
    rf"|(?:\S+\s+){{0,3}}?{_DATE}{_SEP}"                 # "Blog DECEMBER 2, 2025 ..." / "Quantum Simulation Jul 24, 2026 ..."
    r")+",
    re.I,
)
_TRAILING_JUNK = re.compile(
    rf"(?:{_SEP}(?:{_DATE}|read (?:more|on|now|the (?:article|post|story))|learn more|continue reading|\d+\s*min(?:ute)?s?\s*read)\s*[›»→]?)+\s*$",
    re.I,
)


def tidy_title(text: str) -> str:
    """Remove card furniture that sites put inside the same link as the title."""
    text = text.strip(" \t|·•–—-")
    for _ in range(3):
        stripped = _TRAILING_JUNK.sub("", _LEADING_JUNK.sub("", text)).strip(" \t|·•–—-")
        if stripped == text or not stripped:
            break
        text = stripped
    return text


def _is_generic(text: str) -> bool:
    if not text or text.lower().strip(" .:›»→") in _GENERIC_TEXT or len(text) < 6:
        return True
    words = text.lower().split()
    return len(words) > 1 and len(set(words)) == 1  # "News News News News"


def _pick_title(headings: list[str], texts: list[str], url: str) -> str:
    # 1. A heading inside the link is the article title in nearly every card layout.
    for heading in headings:
        title = tidy_title(heading)
        if not _is_generic(title):
            return truncate(title, 200)
    # 2. Otherwise the most descriptive anchor/heading text, after removing dates and labels.
    candidates = [tidy_title(t) for t in texts]
    candidates = [t for t in candidates if not _is_generic(t)]
    if candidates:
        # Prefer title-length text; very long text is usually title + teaser run together.
        reasonable = [t for t in candidates if len(t) <= 160]
        if reasonable:
            return max(reasonable, key=len)
        return truncate(min(candidates, key=len), 160)
    return _slug_from_url(url)


def _same_host_candidates(parser: _PageParser, page_url: str, main_only: bool) -> tuple[dict, list]:
    page_norm = normalize_url(page_url)
    grouped: dict[str, dict] = {}
    order: list[str] = []
    for href, text, context, heading, in_main in parser.anchors:
        if main_only and not in_main:
            continue
        href = href.strip()
        if not href or href.startswith(("#", "mailto:", "tel:", "javascript:", "data:")):
            continue
        url = urljoin(page_url, href).split("#", 1)[0]
        if not url.startswith(("http://", "https://")):
            continue
        key = normalize_url(url)
        if key == page_norm:
            continue
        if key not in grouped:
            grouped[key] = {"url": url, "texts": [], "headings": []}
            order.append(key)
        grouped[key]["texts"].append(text)
        if heading:
            grouped[key]["headings"].append(heading)
        if context:
            grouped[key]["texts"].append(context)
    return grouped, order


def extract_links(page_html: str, page_url: str, link_pattern: str | None = None) -> list[Item]:
    parser = _parse(page_html)
    page_host = _host(page_url)
    base_path = urlsplit(page_url).path.rstrip("/")
    custom = re.compile(link_pattern) if link_pattern else None

    def accepted(url: str, rule: str) -> bool:
        parts = urlsplit(url)
        path = parts.path
        if _FILE_EXT.search(path) or "page=" in parts.query.lower():
            return False
        if rule == "custom":
            return bool(custom.search(url))
        if _host(url) != page_host:
            return False
        if rule == "under-listing":
            if not base_path or not path.startswith(base_path + "/"):
                return False
            rest = path[len(base_path):].strip("/")
            return bool(rest) and not _SKIP_SEGMENTS.search("/" + rest)
        if rule == "slug":
            if _SKIP_SEGMENTS.search(path):
                return False
            segments = [seg for seg in path.split("/") if seg]
            return bool(segments) and bool(_SLUG_RE.match(segments[-1]))
        return False

    rules = ["custom"] if custom else ["under-listing", "slug"]
    # Links inside <main>/<article> first: that is where listings live and menus do not.
    scopes = [True, False] if parser.has_main else [False]
    for main_only in scopes:
        grouped, order = _same_host_candidates(parser, page_url, main_only)
        for rule in rules:
            items = []
            for key in order:
                entry = grouped[key]
                if accepted(entry["url"], rule):
                    title = _pick_title(entry["headings"], entry["texts"], entry["url"])
                    items.append(Item(title=title, link=entry["url"]))
            if items:
                return items[:MAX_HTML_ITEMS]
    return []


def link_samples(page_html: str, page_url: str, limit: int = 8) -> list[str]:
    """Same-site link paths on a page: shown by `check` to help write a link_pattern."""
    parser = _parse(page_html)
    grouped, order = _same_host_candidates(parser, page_url, main_only=False)
    host = _host(page_url)
    paths = []
    for key in order:
        url = grouped[key]["url"]
        if _host(url) == host:
            path = urlsplit(url).path
            if path and path != "/" and path not in paths:
                paths.append(path)
    return paths[:limit]
