"""Persistent per-source state, committed back to the repository by the workflow.

Why commit state instead of using the Actions cache: the cache is evicted after
7 days without access and can be lost at any time, which would make the bot
forget what it has posted. A small JSON file in git is durable, auditable and
diff-friendly (one fingerprint per line).

Invariants
- Writes are atomic (temp file + rename): a crash can never leave half a file.
- A corrupt or missing file is not fatal: it is backed up and the bot starts
  fresh. Starting fresh re-seeds silently (see runner), so it cannot flood.
- MAX_SEEN is far larger than the most items any source can return, so a post
  still present in a feed can never fall out of memory and be posted again.
"""

from __future__ import annotations

import json
import os
import tempfile
from datetime import datetime
from pathlib import Path

from .feeds import MAX_ITEMS
from .pages import MAX_HTML_ITEMS

MAX_SEEN = 400
assert MAX_SEEN >= 2 * 2 * max(MAX_ITEMS, MAX_HTML_ITEMS), "seen buffer must comfortably exceed feed sizes"

STATE_VERSION = 1


class SourceState:
    __slots__ = ("data",)

    def __init__(self, data: dict | None = None):
        self.data = data if isinstance(data, dict) else {}
        seen = self.data.get("seen")
        if not isinstance(seen, list) or not all(isinstance(x, str) for x in seen):
            self.data["seen"] = []

    # ---- seen items -------------------------------------------------------
    @property
    def initialized(self) -> bool:
        return bool(self.data.get("initialized"))

    def mark_initialized(self) -> None:
        self.data["initialized"] = True

    def has_seen(self, keys: list[str]) -> bool:
        seen = self._seen_set()
        return any(k in seen for k in keys)

    def _seen_set(self) -> set[str]:
        cache = self.data.get("_cache")
        if cache is None or len(cache) != len(self.data["seen"]):
            cache = set(self.data["seen"])
            self.data["_cache"] = cache
        return cache

    def mark_seen(self, keys: list[str]) -> None:
        seen = self.data["seen"]
        current = self._seen_set()
        for key in keys:
            if key not in current:
                seen.append(key)
                current.add(key)
        if len(seen) > MAX_SEEN:
            del seen[: len(seen) - MAX_SEEN]
            self.data["_cache"] = set(seen)

    # ---- HTTP caching / discovery ----------------------------------------
    def get(self, key: str, default=None):
        return self.data.get(key, default)

    def set(self, key: str, value) -> None:
        if value in (None, ""):
            self.data.pop(key, None)
        else:
            self.data[key] = value

    # ---- health ------------------------------------------------------------
    @property
    def failures(self) -> int:
        value = self.data.get("failures", 0)
        return value if isinstance(value, int) else 0

    def record_success(self, when: datetime) -> bool:
        """Returns True if this is a recovery after an alert was sent."""
        recovered = bool(self.data.get("alerted"))
        self.data["failures"] = 0
        self.data.pop("alerted", None)
        self.data.pop("last_error", None)
        self.data["last_ok"] = when.isoformat(timespec="seconds")
        return recovered

    def record_failure(self, message: str, alert_after: int) -> bool:
        """Returns True exactly once, when the failure streak reaches the alert threshold."""
        count = self.failures + 1
        self.data["failures"] = count
        self.data["last_error"] = message[:300]
        if count >= alert_after and not self.data.get("alerted"):
            self.data["alerted"] = True
            return True
        return False

    def to_json(self) -> dict:
        return {k: v for k, v in self.data.items() if not k.startswith("_")}


class State:
    def __init__(self, path: Path, sources: dict[str, SourceState], meta: dict):
        self.path = path
        self.sources = sources
        self.meta = meta
        self.warnings: list[str] = []

    @classmethod
    def load(cls, path: str | Path) -> "State":
        path = Path(path)
        warnings: list[str] = []
        raw: dict = {}
        if path.exists():
            try:
                raw = json.loads(path.read_text(encoding="utf-8"))
                if not isinstance(raw, dict):
                    raise ValueError("top level is not an object")
            except (OSError, ValueError) as exc:
                backup = path.with_suffix(path.suffix + ".corrupt")
                try:
                    path.replace(backup)
                except OSError:
                    pass
                warnings.append(f"state file was unreadable ({exc}); moved to {backup.name} and starting fresh")
                raw = {}
        sources_raw = raw.get("sources", {}) if isinstance(raw.get("sources"), dict) else {}
        sources = {sid: SourceState(data) for sid, data in sources_raw.items() if isinstance(data, dict)}
        meta = raw.get("meta", {}) if isinstance(raw.get("meta"), dict) else {}
        state = cls(path, sources, meta)
        state.warnings = warnings
        return state

    def source(self, source_id: str) -> SourceState:
        if source_id not in self.sources:
            self.sources[source_id] = SourceState()
        return self.sources[source_id]

    def prune(self, keep_ids: set[str]) -> None:
        for sid in list(self.sources):
            if sid not in keep_ids:
                del self.sources[sid]

    def heartbeat(self, now: datetime) -> None:
        """Changes once per month so the repo always has recent activity.

        GitHub disables scheduled workflows in repositories with no activity for
        60 days. A monthly state commit guarantees that can never happen, even if
        every source goes quiet.
        """
        self.meta["heartbeat"] = now.strftime("%Y-%m")
        self.meta["version"] = STATE_VERSION

    def save(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        payload = {
            "meta": self.meta,
            "sources": {sid: st.to_json() for sid, st in sorted(self.sources.items())},
        }
        text = json.dumps(payload, indent=1, sort_keys=True, ensure_ascii=False) + "\n"
        fd, tmp = tempfile.mkstemp(dir=str(self.path.parent), prefix=".state-", suffix=".tmp")
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
