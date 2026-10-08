"""Maintenance commands: live source health check, source docs, webhook template."""

from __future__ import annotations

import json
from concurrent.futures import ThreadPoolExecutor
from datetime import timedelta
from typing import Callable

from .config import Config
from .http import fetch as http_fetch
from .pages import link_samples
from .runner import SourceError, collect
from .util import format_date, utcnow


def check_sources(config: Config, only: set[str] | None = None, fetch: Callable = http_fetch) -> tuple[str, int]:
    """Fetch every source (ignoring state) and report how it resolved.

    Returns (markdown, number_of_sources_needing_attention). Never posts anything.
    Icons: ✅ healthy · ℹ️ healthy but nothing currently matches its keyword filter
    · 💤 no post within stale_after_days · ⚠️ works via a fallback or returned
    nothing · ❌ failed.
    """
    now = utcnow()
    stale_cutoff = now - timedelta(days=config.defaults["stale_after_days"])
    sources = [s for s in config.sources if not only or s.id in only]

    def task(source):
        try:
            return collect(source, fetch=fetch)
        except SourceError as exc:
            message = str(exc)
            if "no article links found" in message and source.site:
                message += _samples_hint(source.site, fetch)
            return SourceError(message)
        except Exception as exc:  # noqa: BLE001
            return SourceError(f"unexpected {type(exc).__name__}: {exc}")

    with ThreadPoolExecutor(max_workers=12) as pool:
        results = list(pool.map(task, sources))

    order = {"❌": 0, "⚠️": 1, "💤": 2, "ℹ️": 3, "✅": 4}
    rows = []
    for source, result in zip(sources, results):
        if isinstance(result, SourceError):
            rows.append(("❌", source, "-", "-", "-", str(result)[:600]))
            continue
        items = result.items
        relevant = [i for i in items if source.keyword_ok(i.title)]
        dated = [i.published for i in relevant if i.published]
        newest = max(dated) if dated else None
        sample = relevant[0].title[:80] if relevant else ""
        if result.note:
            icon, note = "⚠️", result.note
        elif not items:
            icon, note = "⚠️", "resolved but returned 0 items"
        elif source.include_keywords and not relevant:
            icon, note = "ℹ️", "working; no current titles match include_keywords (normal for filtered feeds)"
        elif newest and newest < stale_cutoff:
            icon, note = "💤", f"no post since {format_date(newest)}"
        elif result.method == "html":
            icon, note = "✅", "listing page: " + sample
        else:
            icon, note = "✅", sample
        if not source.enabled:
            note = "(disabled) " + note
        rows.append((icon, source, result.method, str(len(relevant)), format_date(newest) or "undated", note))

    rows.sort(key=lambda r: (order[r[0]], r[1].category, r[1].id))
    attention = sum(1 for r in rows if r[0] in ("❌", "⚠️") and r[1].enabled)
    counts = {icon: sum(1 for r in rows if r[0] == icon) for icon in order}
    lines = ["## Source health check", "",
             " · ".join(f"{icon} {n}" for icon, n in counts.items() if n)
             + f" · **{attention} need attention**. This check never posts and never changes state.", "",
             "| | Source | Method | Items | Newest | Note |", "|---|---|---|---|---|---|"]
    for icon, source, method, count, newest, note in rows:
        note = note.replace("|", "\\|").replace("\n", " ")
        lines.append(f"| {icon} | `{source.id}` | {method} | {count} | {newest} | {note} |")
    return "\n".join(lines) + "\n", attention


def _samples_hint(site: str, fetch: Callable) -> str:
    """List the links a page actually contains, to make writing link_pattern easy."""
    try:
        resp = fetch(site)
        samples = link_samples(resp.text(), resp.url)
    except Exception:  # noqa: BLE001 - diagnostics only
        return ""
    if not samples:
        return ". The page has no same-site links (probably rendered by JavaScript); find a feed or another page."
    return ". Links on page: " + ", ".join(samples)


def sources_markdown(config: Config) -> str:
    lines = [
        "# Sources",
        "",
        "<!-- Generated from sources.toml by `python -m feedbot docs`. Do not edit by hand. -->",
        "",
        f"{sum(1 for s in config.sources if s.enabled)} active sources. "
        "Each posts titles and links only, to its own Discord channel.",
        "",
    ]
    order = list(config.categories) + sorted({s.category for s in config.sources} - set(config.categories))
    for category in order:
        members = [s for s in config.sources if s.category == category]
        if not members:
            continue
        lines += [f"## {config.categories.get(category, category)}", "",
                  "| Source | ID | How it is read | Notes |", "|---|---|---|---|"]
        for s in members:
            link = s.site or (s.feeds[0] if s.feeds else "")
            if s.kind == "html":
                how = "listing page"
            elif s.feeds:
                how = "feed"
            else:
                how = "feed discovery, else listing page"
            if s.include_keywords:
                how += f" · filtered: {', '.join(s.include_keywords)}"
            notes = s.note
            if s.high_volume:
                notes = ("High volume. " + notes).strip()
            if not s.enabled:
                notes = ("Disabled. " + notes).strip()
            name = f"[{s.name}]({link})" if link else s.name
            lines.append(f"| {name} | `{s.id}` | {how} | {notes.replace('|', '/')} |")
        lines.append("")
    return "\n".join(lines)


def webhook_template(config: Config) -> str:
    """JSON skeleton for the DISCORD_WEBHOOKS secret: fill in the ones you want."""
    from .config import SPECIAL_WEBHOOKS

    data = {"default": ""}
    for name in SPECIAL_WEBHOOKS:
        data[name] = ""
    for category in config.categories:
        data[f"category:{category}"] = ""
    for source in config.sources:
        data[source.id] = ""
    return json.dumps(data, indent=2) + "\n"
