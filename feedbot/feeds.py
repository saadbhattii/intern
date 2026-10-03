"""Parse RSS 2.0, RSS 1.0 (RDF) and Atom into Items.

Robustness choices
- DOCTYPE declarations are stripped before parsing, which rules out entity
  expansion attacks (billion laughs / external entities) entirely.
- Real-world feeds are often slightly broken (HTML entities like &nbsp;, bare
  '&', control characters, junk before the XML declaration). If strict parsing
  fails, a repaired copy is parsed instead of giving up.
- Namespaces are ignored by local name, so dc:date, atom:link, content:encoded
  etc. are found no matter which prefix a site uses.
"""

from __future__ import annotations

import re
import xml.etree.ElementTree as ET
from html.entities import name2codepoint
from urllib.parse import urljoin

from .util import Item, clean_text, parse_date

MAX_ITEMS = 100

_DOCTYPE_RE = re.compile(rb"<!DOCTYPE[^>\[]*(\[.*?\])?\s*>", re.I | re.S)
_ENTITY_RE = re.compile(rb"&([A-Za-z][A-Za-z0-9]*);")
_BARE_AMP_RE = re.compile(rb"&(?!#\d+;|#x[0-9A-Fa-f]+;|[A-Za-z][A-Za-z0-9]*;)")
_CTRL_RE = re.compile(rb"[\x00-\x08\x0b\x0c\x0e-\x1f]")
_XML_ENTITIES = {b"amp", b"lt", b"gt", b"quot", b"apos"}


class FeedParseError(Exception):
    pass


def looks_like_feed(body: bytes) -> bool:
    head = body[:2048].lstrip().lower()
    if head.startswith(b"<!doctype html") or head.startswith(b"<html"):
        return False
    return b"<rss" in head or b"<feed" in head or b"<rdf:rdf" in head or b"<rdf " in head


def _local(tag: str) -> str:
    if not isinstance(tag, str):
        return ""
    return tag.rsplit("}", 1)[-1].lower()


def _repair(data: bytes) -> bytes:
    def fix_entity(m: re.Match) -> bytes:
        name = m.group(1)
        if name in _XML_ENTITIES:
            return m.group(0)
        cp = name2codepoint.get(name.decode("ascii", "ignore"))
        return f"&#{cp};".encode() if cp else b"&amp;" + name + b";"

    data = _CTRL_RE.sub(b"", data)
    data = _ENTITY_RE.sub(fix_entity, data)
    data = _BARE_AMP_RE.sub(b"&amp;", data)
    return data


def _parse_xml(data: bytes) -> ET.Element:
    data = _DOCTYPE_RE.sub(b"", data)
    start = data.find(b"<")
    if start > 0:
        data = data[start:]
    try:
        return ET.fromstring(data)
    except ET.ParseError:
        pass
    try:
        return ET.fromstring(_repair(data))
    except ET.ParseError as exc:
        raise FeedParseError(f"invalid XML: {exc}") from None


def _child(el: ET.Element, *names: str) -> ET.Element | None:
    for child in el:
        if _local(child.tag) in names:
            return child
    return None


def _child_text(el: ET.Element, *names: str) -> str:
    for name in names:
        c = _child(el, name)
        if c is not None:
            text = "".join(c.itertext()).strip()
            if text:
                return text
    return ""


def _atom_link(el: ET.Element) -> str:
    best = ""
    for child in el:
        if _local(child.tag) != "link":
            continue
        href = (child.get("href") or "").strip()
        rel = (child.get("rel") or "alternate").lower()
        ctype = (child.get("type") or "").lower()
        if not href:
            # RSS-style <link>text</link> inside an Atom-namespaced document
            href = (child.text or "").strip()
            if href:
                return href
            continue
        if rel == "alternate" and (not ctype or "html" in ctype):
            return href
        if rel == "alternate" and not best:
            best = href
    return best


def parse_feed(data: bytes, base_url: str = "") -> list[Item]:
    root = _parse_xml(data)
    kind = _local(root.tag)
    items: list[Item] = []

    if kind == "feed":
        for entry in root:
            if _local(entry.tag) != "entry":
                continue
            link = _atom_link(entry)
            guid = _child_text(entry, "id")
            date = parse_date(_child_text(entry, "published", "issued", "created", "updated", "modified"))
            items.append(_make_item(_child_text(entry, "title"), link, guid, date, base_url))
    elif kind in ("rss", "rdf"):
        for item in root.iter():
            if _local(item.tag) != "item":
                continue
            link = _child_text(item, "link") or _atom_link(item)
            guid = _child_text(item, "guid", "id")
            if not link and guid.startswith(("http://", "https://")):
                link = guid
            date = parse_date(_child_text(item, "pubdate", "date", "published", "issued", "updated"))
            items.append(_make_item(_child_text(item, "title"), link, guid, date, base_url))
    else:
        raise FeedParseError(f"not an RSS/Atom document (root element <{kind}>)")

    result = [i for i in items if i is not None]
    return result[:MAX_ITEMS]


def _make_item(title: str, link: str, guid: str, date, base_url: str) -> Item | None:
    link = (link or "").strip()
    if not link:
        return None
    if base_url:
        link = urljoin(base_url, link)
    if not link.startswith(("http://", "https://")):
        return None
    title = clean_text(title)
    if not title:
        title = link
    return Item(title=title, link=link, guid=(guid or "").strip(), published=date)
