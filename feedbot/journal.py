"""A short record of what was posted to Discord recently.

Used for three things:
- Daily and weekly briefings (what went out in the last day or week).
- Duplicate detection (the same story from several outlets within a few days).
- Remembering Discord message ids for the self-updating source directory and
  which briefings were already sent, so a re-run never posts twice.

Kept small on purpose: entries older than KEEP_DAYS are dropped on every save.
Writes are atomic; an unreadable file is set aside and the bot starts fresh.
"""

from __future__ import annotations

import json
import os
import re
import tempfile
from datetime import datetime, timedelta
from pathlib import Path

from .util import hash_key, normalize_url, parse_date

KEEP_DAYS = 8
_STOP = {
    "a", "an", "the", "of", "to", "in", "on", "for", "and", "or", "with", "by", "at", "as", "is",
    "are", "its", "from", "new", "how", "why", "what", "into", "via", "be", "this", "that",
}
_WORD = re.compile(r"[a-z0-9]+")


_DIGIT_SEP = re.compile(r"(?<=\d)[,.\u2009\u202f ](?=\d{3}\b)")


def title_tokens(title: str) -> frozenset[str]:
    text = _DIGIT_SEP.sub("", title.lower())  # "1,000" and "1000" are the same number
    return frozenset(w for w in _WORD.findall(text) if w not in _STOP)


def similar_titles(a: frozenset[str], b: frozenset[str]) -> bool:
    """Near-identical headlines, e.g. one outlet adding a word to another's title."""
    if len(a) < 5 or len(b) < 5:
        return a == b and len(a) > 0
    return len(a & b) / len(a | b) >= 0.8


class Journal:
    def __init__(self, path: Path, entries: list[dict], meta: dict, warnings: list[str] | None = None):
        self.path = path
        self.entries = entries
        self.meta = meta
        self.warnings = warnings or []

    @classmethod
    def load(cls, path: str | Path) -> "Journal":
        path = Path(path)
        entries: list[dict] = []
        meta: dict = {}
        warnings: list[str] = []
        if path.exists():
            try:
                raw = json.loads(path.read_text(encoding="utf-8"))
                if not isinstance(raw, dict):
                    raise ValueError("top level is not an object")
                entries = [e for e in raw.get("items", []) if isinstance(e, dict) and e.get("u") and e.get("p")]
                meta = raw.get("meta", {}) if isinstance(raw.get("meta"), dict) else {}
            except (OSError, ValueError) as exc:
                backup = path.with_suffix(path.suffix + ".corrupt")
                try:
                    path.replace(backup)
                except OSError:
                    pass
                warnings.append(f"journal was unreadable ({exc}); moved to {backup.name} and starting fresh")
        return cls(path, entries, meta, warnings)

    # ---- recording -------------------------------------------------------
    def add(self, *, title: str, url: str, source_id: str, source_name: str, category: str,
            when: datetime, published: datetime | None = None, firehose: bool = False) -> None:
        entry = {
            "id": hash_key(normalize_url(url)),
            "t": title,
            "u": url,
            "s": source_id,
            "n": source_name,
            "c": category,
            "p": when.isoformat(timespec="seconds"),
        }
        if published:
            entry["d"] = published.isoformat(timespec="seconds")
        if firehose:
            entry["f"] = 1
        self.entries.append(entry)

    # ---- duplicate detection ----------------------------------------------
    def find_duplicate(self, title: str, url: str, now: datetime, hours: int,
                       category: str | None = None) -> dict | None:
        """An earlier post of the same story (same link or near-identical headline).

        With `category`, only that category's channel is considered; without it,
        everything posted anywhere counts (used for the firehose channel).
        """
        if hours <= 0:
            return None
        cutoff = now - timedelta(hours=hours)
        key = hash_key(normalize_url(url))
        tokens = title_tokens(title)
        for entry in reversed(self.entries):
            posted = parse_date(entry["p"])
            if posted is None or posted < cutoff:
                continue
            if category is not None and entry.get("c") != category:
                continue
            if entry.get("id") == key or similar_titles(tokens, title_tokens(entry.get("t", ""))):
                return entry
        return None

    def since(self, start: datetime) -> list[dict]:
        out = []
        for entry in self.entries:
            posted = parse_date(entry["p"])
            if posted is not None and posted >= start:
                out.append(entry)
        return out

    # ---- persistence -------------------------------------------------------
    def prune(self, now: datetime) -> None:
        cutoff = now - timedelta(days=KEEP_DAYS)
        self.entries = [e for e in self.entries if (parse_date(e["p"]) or now) >= cutoff]

    def save(self, now: datetime) -> None:
        self.prune(now)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        payload = {"meta": self.meta, "items": self.entries}
        text = json.dumps(payload, indent=1, ensure_ascii=False, sort_keys=True) + "\n"
        fd, tmp = tempfile.mkstemp(dir=str(self.path.parent), prefix=".journal-", suffix=".tmp")
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
