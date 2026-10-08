"""Server-wide messages: daily and weekly briefings, and the source directory.

Briefing: one tidy message per day (and per week) listing everything that was
posted, grouped by category. Built from the journal, so it costs no extra
fetching. The same story from several outlets appears once. Each period is sent
at most once, even if the workflow runs twice.

Directory: a pinned-style list of every source, grouped by category. The bot
edits its own messages when sources.toml changes, so the server always shows
an up-to-date directory without anyone touching it.
"""

from __future__ import annotations

import hashlib
import json
from collections import OrderedDict
from datetime import datetime, timedelta

from .config import Config
from .discord import DiscordClient, escape_markdown, group_embeds, list_embeds, safe_url
from .journal import Journal, similar_titles, title_tokens
from .util import truncate

BRAND_COLOR = 0x4F7CAC


def _category_order(config: Config) -> list[str]:
    return list(config.categories) + sorted({s.category for s in config.sources} - set(config.categories))


# --------------------------------------------------------------------------
# Briefings
# --------------------------------------------------------------------------

def period_key(period: str, now: datetime) -> str:
    if period == "weekly":
        year, week, _ = now.isocalendar()
        return f"{year}-W{week:02d}"
    return now.date().isoformat()


def build_briefing(config: Config, journal: Journal, period: str, now: datetime) -> list[dict]:
    """Webhook payloads for a briefing; an empty list means there is nothing to send."""
    days = 7 if period == "weekly" else 1
    entries = journal.since(now - timedelta(days=days))
    unique: list[dict] = []
    for entry in entries:  # same story from several outlets: keep the first
        tokens = title_tokens(entry.get("t", ""))
        if any(e.get("id") == entry.get("id") or similar_titles(tokens, title_tokens(e.get("t", ""))) for e in unique):
            continue
        unique.append(entry)
    if not unique:
        return []

    brand = config.discord.get("brand", "Within Quantum")
    limit = max(1, config.discord.get("briefing_max_per_category", 15))
    grouped: "OrderedDict[str, list[dict]]" = OrderedDict((c, []) for c in _category_order(config))
    for entry in unique:
        grouped.setdefault(entry.get("c", ""), []).append(entry)

    if period == "weekly":
        start = now - timedelta(days=7)
        heading = f"{brand} weekly roundup"
        span = f"{start.day} {start:%B} to {now.day} {now:%B %Y}"
    else:
        heading = f"{brand} daily briefing"
        span = f"{now:%A}, {now.day} {now:%B %Y}"
    sources = len({e.get("s") for e in unique})
    header = {
        "title": heading,
        "description": f"{span}\n{len(unique)} new posts from {sources} sources, grouped by section.",
        "color": BRAND_COLOR,
    }
    embeds = [header]
    for category, items in grouped.items():
        if not items:
            continue
        newest_first = sorted(items, key=lambda e: e.get("p", ""), reverse=True)
        lines = [
            f"- [{escape_markdown(truncate(e.get('t', ''), 150))}]({safe_url(e['u'])}) ({escape_markdown(e.get('n', ''))})"
            for e in newest_first[:limit]
        ]
        if len(items) > limit:
            lines.append(f"+{len(items) - limit} more in this section's channel")
        name = config.categories.get(category, category)
        embeds += list_embeds(f"{name} ({len(items)})", lines,
                              color=config.discord.get("colors", {}).get(category, BRAND_COLOR),
                              footer=f"{brand} | {name}")
    return [{"embeds": group} for group in group_embeds(embeds)]


def send_briefing(config: Config, journal: Journal, client: DiscordClient, hook: str, period: str,
                  now: datetime, force: bool = False) -> str:
    sent = journal.meta.setdefault("briefings", {})
    key = period_key(period, now)
    if sent.get(period) == key and not force:
        return f"{period} briefing for {key} was already sent"
    payloads = build_briefing(config, journal, period, now)
    if not payloads:
        sent[period] = key
        return f"nothing was posted in this period; no {period} briefing sent"
    brand = config.discord.get("brand", "Within Quantum")
    for payload in payloads:
        client.deliver(hook, payload, username=brand)
    sent[period] = key
    return f"sent the {period} briefing ({len(payloads)} message(s))"


# --------------------------------------------------------------------------
# Source directory
# --------------------------------------------------------------------------

def build_directory(config: Config) -> list[dict]:
    brand = config.discord.get("brand", "Within Quantum")
    enabled = [s for s in config.sources if s.enabled]
    header = {
        "title": f"{brand}: sources",
        "description": (
            f"Every post in this server comes from one of these {len(enabled)} sources, "
            f"grouped into {len({s.category for s in enabled})} sections. Posts are titles and links "
            "straight to the original. This list updates itself whenever a source is added or removed."
        ),
        "color": BRAND_COLOR,
    }
    embeds = [header]
    for category in _category_order(config):
        members = [s for s in enabled if s.category == category]
        if not members:
            continue
        lines = []
        for s in sorted(members, key=lambda x: x.name.lower()):
            link = s.site or (s.feeds[0] if s.feeds else "")
            label = escape_markdown(s.name)
            if s.author and s.author not in s.name:
                label += f" ({escape_markdown(truncate(s.author, 60))})"
            lines.append(f"- [{label}]({safe_url(link)})" if link else f"- {label}")
        name = config.categories.get(category, category)
        embeds += list_embeds(f"{name} ({len(members)})", lines,
                              color=config.discord.get("colors", {}).get(category, BRAND_COLOR),
                              footer=f"{brand} | {name}")
    return [{"embeds": group} for group in group_embeds(embeds)]


def sync_directory(config: Config, journal: Journal, client: DiscordClient, hook: str) -> str:
    payloads = build_directory(config)
    digest = hashlib.sha256(json.dumps(payloads, sort_keys=True).encode()).hexdigest()[:16]
    state = journal.meta.setdefault("directory", {})
    ids: list[str] = list(state.get("ids", []))
    if state.get("hash") == digest and len(ids) == len(payloads):
        return "directory unchanged"
    brand = config.discord.get("brand", "Within Quantum")
    if ids and len(ids) == len(payloads):
        if all(client.edit(hook, mid, payload) for mid, payload in zip(ids, payloads)):
            state["hash"] = digest
            return f"directory updated in place ({len(ids)} message(s))"
    for mid in ids:  # layout changed or a message was removed by hand: post a fresh copy
        client.delete(hook, mid)
    new_ids = []
    for payload in payloads:
        mid = client.deliver(hook, payload, username=brand)
        if mid:
            new_ids.append(mid)
    state["ids"] = new_ids
    state["hash"] = digest
    return f"directory posted ({len(new_ids)} message(s))"
