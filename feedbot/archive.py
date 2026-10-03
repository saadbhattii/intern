"""A small rolling archive of recent articles (title, link, dates) per source.

The Discord state only stores fingerprints, which is enough to avoid duplicate
posts but not enough to show anything. The website needs the titles and links
themselves, so every run merges what it fetched into `state/archive.json`.

Design notes
- Independent of Discord: every enabled source is archived, with or without a
  webhook, so the website covers everything.
- Each item records `seen` (when the bot first saw it). The website uses that
  to flag what is new since a reader's last visit, even for undated pages.
- Links found the very first time an undated listing page is read are marked
  `bootstrap`: they may include menu links and have no real date, so the site
  tucks them away instead of presenting them as fresh headlines.
- Bounded: at most MAX_PER_SOURCE items per source; atomic writes; a corrupt
  file is set aside and rebuilt, never fatal.
"""

from __future__ import annotations

import json
import os
import tempfile
from datetime import datetime
from pathlib import Path

from .util import Item

MAX_PER_SOURCE = 40
ARCHIVE_VERSION = 1


def _sort_key(entry: dict) -> tuple:
    # Real dates first (newest first); bootstrap links last, in page order.
    if entry.get("bootstrap"):
        return (0, "")
    return (1, entry.get("published") or entry.get("seen") or "")


class Archive:
    def __init__(self, path: Path, sources: dict[str, list[dict]], warnings: list[str] | None = None):
        self.path = path
        self.sources = sources
        self.warnings = warnings or []
        self.changed = False

    @classmethod
    def load(cls, path: str | Path) -> "Archive":
        path = Path(path)
        warnings: list[str] = []
        sources: dict[str, list[dict]] = {}
        if path.exists():
            try:
                raw = json.loads(path.read_text(encoding="utf-8"))
                data = raw.get("sources", {}) if isinstance(raw, dict) else {}
                for sid, entries in data.items():
                    if isinstance(entries, list):
                        sources[sid] = [e for e in entries if isinstance(e, dict) and e.get("url") and e.get("id")]
            except (OSError, ValueError) as exc:
                backup = path.with_suffix(path.suffix + ".corrupt")
                try:
                    path.replace(backup)
                except OSError:
                    pass
                warnings.append(f"archive was unreadable ({exc}); moved to {backup.name} and rebuilding")
        return cls(path, sources, warnings)

    def merge(self, source_id: str, items: list[Item], now: datetime) -> int:
        """Add newly seen items; refresh titles/dates of known ones. Returns count added."""
        first_fill = source_id not in self.sources
        existing = self.sources.get(source_id, [])
        by_id = {e["id"]: e for e in existing}
        stamp = now.isoformat(timespec="seconds")
        fresh: list[dict] = []
        for item in items:
            key = item.keys()[0]
            published = item.published.isoformat(timespec="seconds") if item.published else None
            entry = by_id.get(key)
            if entry is not None:
                if entry.get("title") != item.title and item.title:
                    entry["title"] = item.title
                    self.changed = True
                if published and entry.get("published") != published:
                    entry["published"] = published
                    entry.pop("bootstrap", None)
                    self.changed = True
                continue
            entry = {"id": key, "title": item.title, "url": item.link, "seen": stamp}
            if published:
                entry["published"] = published
            elif first_fill:
                entry["bootstrap"] = True
            by_id[key] = entry
            fresh.append(entry)
        if not fresh and not first_fill:
            return 0
        combined = fresh + existing  # page order preserved for equal sort keys (stable sort)
        combined.sort(key=_sort_key, reverse=True)
        self.sources[source_id] = combined[:MAX_PER_SOURCE]
        self.changed = True
        return len(fresh)

    def prune(self, keep_ids: set[str]) -> None:
        for sid in list(self.sources):
            if sid not in keep_ids:
                del self.sources[sid]
                self.changed = True

    def save(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        payload = {"version": ARCHIVE_VERSION,
                   "sources": {sid: entries for sid, entries in sorted(self.sources.items())}}
        text = json.dumps(payload, indent=1, ensure_ascii=False) + "\n"
        fd, tmp = tempfile.mkstemp(dir=str(self.path.parent), prefix=".archive-", suffix=".tmp")
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as handle:
                handle.write(text)
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(tmp, self.path)
        except BaseException:
            try:
                os.unlink(tmp)
            except OSError:
                pass
            raise
