"""Load and validate `sources.toml` and the DISCORD_WEBHOOKS mapping.

Validation is strict on purpose: a typo in a key name or a broken regex is
caught by CI on the pull request, long before a scheduled run could trip on it.
"""

from __future__ import annotations

import json
import re
import tomllib
from dataclasses import dataclass, field
from pathlib import Path

ID_RE = re.compile(r"^[a-z0-9][a-z0-9-]{1,62}$")
WEBHOOK_RE = re.compile(
    r"^https://(?:(?:canary|ptb)\.)?discord(?:app)?\.com/api(?:/v\d+)?/webhooks/\d+/[A-Za-z0-9_\-]+$"
)
KINDS = {"auto", "feed", "html"}
MODES = {"auto", "each", "digest"}

DEFAULTS = {
    "max_per_run": 5,          # most items one source may post in a single run
    "max_age_days": 14,        # dated items older than this are never posted
    "mode": "auto",            # each | digest | auto (digest when > digest_over new items)
    "digest_over": 3,
    "bootstrap_posts": 0,      # items to post the very first time a source is seen
    "flood_threshold": 15,     # sudden "everything is new" guard (see runner)
    "alert_after_failures": 3, # consecutive failed runs before the status channel is pinged
    "stale_after_days": 365,   # `check` flags sources with no post for this long
}

_SOURCE_KEYS = {
    "id", "name", "category", "feed", "site", "kind", "link_pattern", "include_keywords",
    "exclude_keywords", "max_per_run", "max_age_days", "mode", "enabled", "username", "note",
    "author", "high_volume",
}


@dataclass
class Source:
    id: str
    name: str
    category: str
    feeds: list[str] = field(default_factory=list)
    site: str = ""
    kind: str = "auto"
    link_pattern: str = ""
    include_keywords: list[str] = field(default_factory=list)
    exclude_keywords: list[str] = field(default_factory=list)
    max_per_run: int = DEFAULTS["max_per_run"]
    max_age_days: int = DEFAULTS["max_age_days"]
    mode: str = DEFAULTS["mode"]
    enabled: bool = True
    username: str = ""
    note: str = ""
    author: str = ""
    high_volume: bool = False

    @property
    def display_name(self) -> str:
        return self.username or self.name

    def keyword_ok(self, title: str) -> bool:
        lowered = title.lower()
        if self.include_keywords and not any(k.lower() in lowered for k in self.include_keywords):
            return False
        if self.exclude_keywords and any(k.lower() in lowered for k in self.exclude_keywords):
            return False
        return True


SITE_DEFAULTS = {
    "title": "The Ket",
    "tagline": "Quantum computing, mathematics, theory and AI, from the people doing the work.",
    "repo_url": "",
    "items_per_source": 30,
}


@dataclass
class Config:
    defaults: dict
    sources: list[Source]
    categories: dict[str, str]
    site: dict = field(default_factory=lambda: dict(SITE_DEFAULTS))

    def by_id(self) -> dict[str, Source]:
        return {s.id: s for s in self.sources}


class ConfigError(Exception):
    def __init__(self, errors: list[str]):
        super().__init__("\n".join(errors))
        self.errors = errors


def _is_url(value: object) -> bool:
    return isinstance(value, str) and value.startswith(("http://", "https://")) and " " not in value


def load_config(path: str | Path = "sources.toml") -> Config:
    path = Path(path)
    try:
        raw = tomllib.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        raise ConfigError([f"{path} not found"]) from None
    except tomllib.TOMLDecodeError as exc:
        raise ConfigError([f"{path}: TOML syntax error: {exc}"]) from None

    errors: list[str] = []
    defaults = dict(DEFAULTS)
    for key, value in (raw.get("defaults") or {}).items():
        if key not in DEFAULTS:
            errors.append(f"[defaults] unknown key '{key}'")
            continue
        if key == "mode":
            if value not in MODES:
                errors.append(f"[defaults] mode must be one of {sorted(MODES)}")
                continue
        elif not isinstance(value, int) or isinstance(value, bool) or value < 0:
            errors.append(f"[defaults] {key} must be a non-negative integer")
            continue
        defaults[key] = value

    categories = raw.get("categories") or {}
    if not isinstance(categories, dict) or not all(isinstance(v, str) for v in categories.values()):
        errors.append("[categories] must map category ids to display names")
        categories = {}

    site_settings = dict(SITE_DEFAULTS)
    for key, value in (raw.get("site") or {}).items():
        if key not in SITE_DEFAULTS:
            errors.append(f"[site] unknown key '{key}'")
        elif key == "items_per_source":
            if not isinstance(value, int) or isinstance(value, bool) or not 1 <= value <= 40:
                errors.append("[site] items_per_source must be an integer from 1 to 40")
            else:
                site_settings[key] = value
        elif not isinstance(value, str):
            errors.append(f"[site] {key} must be a string")
        elif key == "repo_url" and value and not _is_url(value):
            errors.append("[site] repo_url must be an http(s) URL")
        else:
            site_settings[key] = value.strip()

    unknown_top = set(raw) - {"defaults", "categories", "source", "site"}
    for key in sorted(unknown_top):
        errors.append(f"unknown top-level key '{key}' (did you mean [[source]]?)")

    sources: list[Source] = []
    seen_ids: set[str] = set()
    for index, entry in enumerate(raw.get("source") or [], start=1):
        where = f"source #{index}"
        if not isinstance(entry, dict):
            errors.append(f"{where}: must be a table")
            continue
        sid = entry.get("id")
        if isinstance(sid, str):
            where = f"source '{sid}'"
        for key in sorted(set(entry) - _SOURCE_KEYS):
            errors.append(f"{where}: unknown key '{key}'")
        if not isinstance(sid, str) or not ID_RE.match(sid):
            errors.append(f"{where}: id must match {ID_RE.pattern}")
            continue
        if sid in seen_ids:
            errors.append(f"{where}: duplicate id")
            continue
        seen_ids.add(sid)

        name = entry.get("name")
        if not isinstance(name, str) or not name.strip():
            errors.append(f"{where}: name is required")
            name = sid
        category = entry.get("category")
        if not isinstance(category, str) or not ID_RE.match(category):
            errors.append(f"{where}: category must be a lowercase id like 'hardware'")
            category = "uncategorized"
        elif categories and category not in categories:
            errors.append(f"{where}: category '{category}' is not listed under [categories]")

        feed_value = entry.get("feed", [])
        feeds = [feed_value] if isinstance(feed_value, str) else feed_value
        if not isinstance(feeds, list) or not all(_is_url(f) for f in feeds):
            errors.append(f"{where}: feed must be a URL or a list of URLs")
            feeds = []
        site = entry.get("site", "")
        if site and not _is_url(site):
            errors.append(f"{where}: site must be an http(s) URL")
            site = ""
        kind = entry.get("kind", "auto")
        if kind not in KINDS:
            errors.append(f"{where}: kind must be one of {sorted(KINDS)}")
            kind = "auto"
        if kind == "feed" and not feeds:
            errors.append(f"{where}: kind 'feed' needs at least one feed URL")
        if kind == "html" and not site:
            errors.append(f"{where}: kind 'html' needs a site URL")
        if not feeds and not site:
            errors.append(f"{where}: needs a feed or a site")

        pattern = entry.get("link_pattern", "")
        if pattern:
            if not isinstance(pattern, str):
                errors.append(f"{where}: link_pattern must be a string")
                pattern = ""
            else:
                try:
                    re.compile(pattern)
                except re.error as exc:
                    errors.append(f"{where}: link_pattern is not a valid regex: {exc}")
                    pattern = ""

        def _str_list(key: str) -> list[str]:
            value = entry.get(key, [])
            if not isinstance(value, list) or not all(isinstance(v, str) and v.strip() for v in value):
                errors.append(f"{where}: {key} must be a list of non-empty strings")
                return []
            return [v.strip() for v in value]

        def _int(key: str) -> int:
            value = entry.get(key, defaults[key])
            if not isinstance(value, int) or isinstance(value, bool) or value < 0:
                errors.append(f"{where}: {key} must be a non-negative integer")
                return defaults[key]
            return value

        mode = entry.get("mode", defaults["mode"])
        if mode not in MODES:
            errors.append(f"{where}: mode must be one of {sorted(MODES)}")
            mode = defaults["mode"]
        enabled = entry.get("enabled", True)
        if not isinstance(enabled, bool):
            errors.append(f"{where}: enabled must be true or false")
            enabled = True
        high_volume = entry.get("high_volume", False)
        if not isinstance(high_volume, bool):
            errors.append(f"{where}: high_volume must be true or false")
            high_volume = False
        username = entry.get("username", "")
        if not isinstance(username, str) or len(username) > 80:
            errors.append(f"{where}: username must be a string of at most 80 characters")
            username = ""
        for text_key in ("note", "author"):
            if not isinstance(entry.get(text_key, ""), str):
                errors.append(f"{where}: {text_key} must be a string")

        sources.append(
            Source(
                id=sid,
                name=name.strip(),
                category=category,
                feeds=list(feeds),
                site=site,
                kind=kind,
                link_pattern=pattern,
                include_keywords=_str_list("include_keywords"),
                exclude_keywords=_str_list("exclude_keywords"),
                max_per_run=max(1, _int("max_per_run")),
                max_age_days=_int("max_age_days"),
                mode=mode,
                enabled=enabled,
                username=username.strip(),
                note=str(entry.get("note", "")),
                author=str(entry.get("author", "")),
                high_volume=high_volume,
            )
        )

    if not sources and not errors:
        errors.append("no [[source]] entries found")
    if errors:
        raise ConfigError(errors)
    return Config(defaults=defaults, sources=sources, categories=categories, site=site_settings)


@dataclass
class Webhooks:
    mapping: dict[str, str]
    problems: list[str]

    def for_source(self, source: Source) -> str | None:
        """Lookup order: source id -> 'category:<category>' -> 'default'."""
        for key in (source.id, f"category:{source.category}", "default"):
            url = self.mapping.get(key)
            if url:
                return url
        return None


def parse_webhooks(raw: str | None, config: Config | None = None) -> Webhooks:
    """Parse the DISCORD_WEBHOOKS secret. Never echoes webhook URLs in messages."""
    problems: list[str] = []
    if not raw or not raw.strip():
        return Webhooks({}, ["DISCORD_WEBHOOKS is empty; nothing will be posted"])
    try:
        data = json.loads(raw)
    except json.JSONDecodeError as exc:
        return Webhooks({}, [f"DISCORD_WEBHOOKS is not valid JSON (line {exc.lineno}, column {exc.colno})"])
    if not isinstance(data, dict):
        return Webhooks({}, ["DISCORD_WEBHOOKS must be a JSON object of {\"source-id\": \"webhook-url\"}"])

    valid_keys: set[str] | None = None
    if config is not None:
        valid_keys = {s.id for s in config.sources}
        valid_keys |= {f"category:{s.category}" for s in config.sources}
        valid_keys.add("default")

    mapping: dict[str, str] = {}
    for key, value in data.items():
        if not isinstance(key, str):
            continue
        if value in ("", None):
            continue  # placeholder: not configured yet
        if not isinstance(value, str) or not WEBHOOK_RE.match(value.strip()):
            problems.append(f"webhook for '{key}' is not a valid Discord webhook URL; ignored")
            continue
        if valid_keys is not None and key not in valid_keys:
            problems.append(f"webhook key '{key}' does not match any source id or category; ignored")
            continue
        mapping[key] = value.strip()
    return Webhooks(mapping, problems)
