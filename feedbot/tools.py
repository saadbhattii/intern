"""Maintenance commands: live source health check, source docs, webhook template."""

from __future__ import annotations

import json
from concurrent.futures import ThreadPoolExecutor
from datetime import timedelta
from typing import Callable

from .config import Config
from .http import fetch as http_fetch
from .runner import SourceError, collect
from .util import format_date, utcnow


def check_sources(config: Config, only: set[str] | None = None, fetch: Callable = http_fetch) -> tuple[str, int]:
    """Fetch every source (ignoring state) and report how it resolved.

    Returns (markdown, number_of_problem_sources). Never posts anything.
    """
    now = utcnow()
    stale_cutoff = now - timedelta(days=config.defaults["stale_after_days"])
    sources = [s for s in config.sources if not only or s.id in only]

    def task(source):
        try:
            return collect(source, fetch=fetch)
        except SourceError as exc:
            return exc
        except Exception as exc:  # noqa: BLE001
            return SourceError(f"unexpected {type(exc).__name__}: {exc}")

    with ThreadPoolExecutor(max_workers=12) as pool:
        results = list(pool.map(task, sources))

    rows = []
    problems = 0
    for source, result in zip(sources, results):
        if isinstance(result, SourceError):
            problems += 1
            rows.append((0, source, "❌", "-", "-", "-", str(result)[:300]))
            continue
        items = result.items
        relevant = [i for i in items if source.keyword_ok(i.title)]
        dated = [i.published for i in relevant if i.published]
        newest = max(dated) if dated else None
        note = ""
        icon = "✅"
        if not items:
            icon, note = "⚠️", "resolved but returned 0 items"
        elif source.include_keywords and not relevant:
            icon, note = "⚠️", "no recent titles match include_keywords"
        elif newest and newest < stale_cutoff:
            icon, note = "💤", f"no post since {format_date(newest)}"
        elif result.method == "html":
            note = "scraped listing page (no feed): " + (relevant[0].title[:80] if relevant else "")
        else:
            note = relevant[0].title[:80] if relevant else ""
        if icon != "✅":
            problems += 1
        if not source.enabled:
            note = "(disabled) " + note
        rows.append((1 if icon == "✅" else 0, source, icon, result.method, str(len(relevant)),
                     format_date(newest) or "undated", note))

    rows.sort(key=lambda r: (r[0], r[1].category, r[1].id))
    lines = ["## Source health check", "",
             f"{len(sources) - problems}/{len(sources)} sources healthy. "
             "This check never posts and never changes state.", "",
             "| | Source | Method | Items | Newest | Note |", "|---|---|---|---|---|---|"]
    for _, source, icon, method, count, newest, note in rows:
        note = note.replace("|", "\\|").replace("\n", " ")
        lines.append(f"| {icon} | `{source.id}` | {method} | {count} | {newest} | {note} |")
    return "\n".join(lines) + "\n", problems


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
    data = {"default": ""}
    for category in config.categories:
        data[f"category:{category}"] = ""
    for source in config.sources:
        data[source.id] = ""
    return json.dumps(data, indent=2) + "\n"
