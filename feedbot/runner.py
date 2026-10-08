"""The run loop: fetch every source, work out what is genuinely new, post it.

Failure isolation: every source is processed inside its own try/except. A
broken feed, a dead webhook or a parser surprise affects that one source only;
it is recorded in state and in the run summary, and the run continues.

Deciding what is "new" (in order):
1. First time a source is seen: record everything currently listed as seen and
   post nothing (or `bootstrap_posts` items). Adding 100 sources never floods.
2. Items already seen (by normalized URL or by GUID) are skipped.
3. Items failing the source's keyword filters are skipped.
4. Dated items older than `max_age_days` are marked seen and skipped, so an old
   post resurfacing in a feed is never announced as new.
5. Flood guard: if most of a sizeable feed suddenly looks new (a site migrated
   its URLs, or swapped feed formats), it is re-seeded silently and reported.
6. At most `max_per_run` items are posted per source per run (newest kept).
Only items Discord confirms are marked seen; the rest are retried next run.
"""

from __future__ import annotations

import os
import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from datetime import timedelta
from typing import Callable

from .config import Config, Source, Webhooks
from .discord import (DEFAULT_COLOR, DiscordClient, WebhookError, card_embed, chunk_lines, digest_embeds,
                      favicon_url, format_digest, format_item, group_embeds, preview_embed)
from .journal import Journal
from .preview import find_image
from .feeds import FeedParseError, looks_like_feed, parse_feed
from .http import FetchError, fetch as http_fetch
from .pages import discover_feeds, extract_links
from .state import SourceState, State
from urllib.parse import urlsplit

from .util import Item, normalize_url, utcnow


class SourceError(Exception):
    pass


@dataclass
class Collected:
    items: list[Item]
    method: str  # "feed" or "html"
    url: str
    not_modified: bool = False
    etag: str | None = None
    last_modified: str | None = None
    note: str = ""


@dataclass
class Outcome:
    source: Source
    status: str  # posted | ok | not-modified | seeded | failed | no-webhook | skipped
    detail: str = ""
    posted: int = 0
    method: str = ""


@dataclass
class DeliveryContext:
    """Everything delivery needs beyond the source itself (shared across one run)."""

    discord: dict
    categories: dict
    now: object
    journal: Journal | None = None
    firehose: str | None = None
    fetch: Callable | None = None
    report: "RunReport | None" = None
    pinged: set = field(default_factory=set)
    firehose_failed: bool = False
    # (webhook, message id, item, source, posted at) for the end-of-run preview check
    preview_checks: list = field(default_factory=list)

    def avatar_for(self, source: Source) -> str:
        if source.avatar:
            if source.avatar.startswith(("http://", "https://")):
                return source.avatar
            base = self.discord.get("asset_base_url", "")
            return f"{base.rstrip('/')}/{source.avatar.lstrip('/')}" if base else ""
        if self.discord.get("avatars", "auto") == "auto":
            return favicon_url(source.site or (source.feeds[0] if source.feeds else ""))
        return ""

    def color_for(self, category: str) -> int:
        return self.discord.get("colors", {}).get(category, DEFAULT_COLOR)

    def category_name(self, category: str) -> str:
        return self.categories.get(category, category)

    def role_once(self, category: str) -> str:
        """The category's role, but only for the first message in this run (no ping storms)."""
        role = self.discord.get("roles", {}).get(category, "")
        if role and category not in self.pinged:
            self.pinged.add(category)
            return role
        return ""

    def image_for(self, item: Item) -> str:
        if self.discord.get("preview_image") == "none":
            return ""
        if item.image:
            return item.image
        return find_image(item.link, self.fetch) if self.fetch else ""


@dataclass
class RunReport:
    outcomes: list[Outcome] = field(default_factory=list)
    alerts: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)


# --------------------------------------------------------------------------
# Collection (runs in worker threads; must not touch shared state)
# --------------------------------------------------------------------------

def collect(
    source: Source,
    resolved_feed: str | None = None,
    etag: str | None = None,
    last_modified: str | None = None,
    fetch: Callable = http_fetch,
) -> Collected:
    """Resolve a source to items. Order: feeds -> advertised feeds -> listing page -> homepage.

    A feed that parses but contains no items is kept only as a last resort, so a
    stub or comments feed never hides a working listing page.
    """
    problems: list[str] = []
    empty: list[Collected] = []
    candidates: list[str] = []
    if source.kind in ("auto", "feed"):
        if resolved_feed and source.kind == "auto":
            candidates.append(resolved_feed)
        for url in source.feeds:
            if url not in candidates:
                candidates.append(url)
        if resolved_feed and resolved_feed not in candidates:
            candidates.append(resolved_feed)

    for url in candidates:
        use_cache = url == resolved_feed
        result = _try_feed(url, fetch, problems, empty,
                           etag=etag if use_cache else None,
                           last_modified=last_modified if use_cache else None)
        if result:
            return result

    if source.kind in ("auto", "html") and source.site:
        pages = [source.site]
        root = _origin(source.site)
        if normalize_url(root) != normalize_url(source.site):
            pages.append(root)
        for page_url in pages:
            is_fallback = page_url != source.site
            try:
                resp = fetch(page_url)
            except FetchError as exc:
                problems.append(f"{page_url}: {exc}")
                if exc.status in (404, 410) and not is_fallback:
                    continue  # listing page moved: try the homepage
                break
            if looks_like_feed(resp.body):
                result = _parsed(resp, page_url, problems, empty)
                if result:
                    return result
                continue
            page = resp.text()
            if source.kind == "auto":
                for found in discover_feeds(page, resp.url)[:3]:
                    if found in candidates:
                        continue
                    candidates.append(found)
                    result = _try_feed(found, fetch, problems, empty)
                    if result:
                        if is_fallback:
                            result.note = f"listing page {source.site} is gone; using feed found on the homepage"
                        return result
            try:
                items = extract_links(page, resp.url, source.link_pattern or None)
            except Exception as exc:  # noqa: BLE001 - never let one page break the run
                items = []
                problems.append(f"{page_url}: link extraction failed: {exc}")
            if items:
                note = (f"listing page {source.site} is gone; scraping article links from the homepage"
                        if is_fallback else "")
                return Collected(items, "html", resp.url, note=note)
            problems.append(f"{page_url}: no article links found")

    if empty:
        return empty[0]
    if not problems:
        problems.append("no feed or site configured")
    raise SourceError(" | ".join(problems))


def _origin(url: str) -> str:
    parts = urlsplit(url)
    return f"{parts.scheme}://{parts.netloc}/"


def _try_feed(url, fetch, problems, empty, etag=None, last_modified=None) -> Collected | None:
    try:
        resp = fetch(url, etag=etag, last_modified=last_modified)
    except FetchError as exc:
        problems.append(f"{url}: {exc}")
        return None
    if resp.status == 304:
        return Collected([], "feed", url, not_modified=True, etag=etag, last_modified=last_modified)
    if not looks_like_feed(resp.body):
        problems.append(f"{url}: not an RSS/Atom feed")
        return None
    return _parsed(resp, url, problems, empty)


def _parsed(resp, url, problems, empty) -> Collected | None:
    try:
        items = parse_feed(resp.body, resp.url)
    except FeedParseError as exc:
        problems.append(f"{url}: {exc}")
        return None
    result = Collected(items, "feed", url, etag=resp.headers.get("etag"),
                       last_modified=resp.headers.get("last-modified"))
    if not items:
        problems.append(f"{url}: feed has no items")
        empty.append(result)
        return None
    return result


# --------------------------------------------------------------------------
# Selection (pure logic; easy to unit test)
# --------------------------------------------------------------------------

@dataclass
class Selection:
    to_post: list[Item]
    mark_without_posting: list[Item]
    skipped_over_limit: int = 0
    note: str = ""
    reseeded: bool = False


def select_new(source: Source, items: list[Item], st: SourceState, defaults: dict, now) -> Selection:
    # De-duplicate within the fetch (feeds sometimes repeat items).
    unique: list[Item] = []
    seen_links: set[str] = set()
    for item in items:
        key = normalize_url(item.link)
        if key not in seen_links:
            seen_links.add(key)
            unique.append(item)
    relevant = [i for i in unique if source.keyword_ok(i.title)]

    if not st.initialized:
        count = min(defaults["bootstrap_posts"], len(relevant))
        newest = _newest_first(relevant)[:count]
        return Selection(
            to_post=list(reversed(newest)),
            mark_without_posting=unique,
            note=f"first run: recorded {len(unique)} existing items",
        )

    fresh = [i for i in relevant if not st.has_seen(i.keys())]
    too_old: list[Item] = []
    if source.max_age_days:
        cutoff = now - timedelta(days=source.max_age_days)
        too_old = [i for i in fresh if i.published is not None and i.published < cutoff]
        fresh = [i for i in fresh if not (i.published is not None and i.published < cutoff)]

    threshold = defaults["flood_threshold"]
    if threshold and len(fresh) > threshold and len(fresh) >= 0.8 * len(relevant) and len(relevant) >= 10:
        return Selection(
            to_post=[],
            mark_without_posting=unique,
            note=(f"flood guard: {len(fresh)} of {len(relevant)} items looked new at once; "
                  "re-seeded without posting (site migration?)"),
            reseeded=True,
        )

    ordered = _newest_first(fresh)
    limited = ordered[: source.max_per_run]
    dropped = ordered[source.max_per_run:]
    return Selection(
        to_post=list(reversed(limited)),  # post oldest first so channels read chronologically
        mark_without_posting=too_old + dropped,
        skipped_over_limit=len(dropped),
    )


def _newest_first(items: list[Item]) -> list[Item]:
    """Dated items by date; undated items keep listing order (listings are newest-first)."""
    indexed = list(enumerate(items))
    if any(i.published for _, i in indexed):
        floor = min((i.published for _, i in indexed if i.published), default=None)
        indexed.sort(key=lambda pair: (pair[1].published or floor, -pair[0]), reverse=True)
    return [item for _, item in indexed]


# --------------------------------------------------------------------------
# Orchestration
# --------------------------------------------------------------------------

def run(
    config: Config,
    webhooks: Webhooks,
    state: State,
    *,
    only: set[str] | None = None,
    dry_run: bool = False,
    force_latest: int = 0,
    client: DiscordClient | None = None,
    fetch: Callable = http_fetch,
    workers: int = 12,
    journal: Journal | None = None,
) -> RunReport:
    """Fetch every enabled source that has a webhook and post what is new to Discord.

    Sources without a webhook are not fetched at all; adding a webhook later
    starts with a silent seed, exactly like a brand-new source.
    """
    now = utcnow()
    report = RunReport(warnings=list(state.warnings) + list(webhooks.problems)
                       + (list(journal.warnings) if journal else []))
    client = client or DiscordClient(dry_run=dry_run)
    ctx = DeliveryContext(discord=config.discord, categories=config.categories, now=now, journal=journal,
                          firehose=webhooks.special("firehose"), fetch=fetch, report=report)

    selected: list[tuple[Source, str | None]] = []
    for source in config.sources:
        if only and source.id not in only:
            continue
        if not source.enabled:
            report.outcomes.append(Outcome(source, "skipped", "disabled in sources.toml"))
            continue
        hook = webhooks.for_source(source)
        if not hook and not dry_run:
            report.outcomes.append(Outcome(source, "no-webhook", "no webhook configured"))
            continue
        selected.append((source, hook))

    def task(pair):
        source, _ = pair
        st = state.sources.get(source.id) or SourceState()
        # Conditional GET ("has it changed since?") only for routine runs of a source
        # that already has a Discord baseline. A forced re-send needs the items even
        # if nothing changed, and a source being seeded needs a full listing.
        conditional = st.initialized and not force_latest
        try:
            return collect(
                source,
                st.get("resolved_feed"),
                st.get("etag") if conditional else None,
                st.get("last_modified") if conditional else None,
                fetch=fetch,
            )
        except SourceError as exc:
            return exc
        except Exception as exc:  # noqa: BLE001 - isolate unexpected bugs per source
            return SourceError(f"unexpected {type(exc).__name__}: {exc}")

    with ThreadPoolExecutor(max_workers=max(1, workers)) as pool:
        results = list(pool.map(task, selected))

    for (source, hook), result in zip(selected, results):
        st = state.source(source.id)
        try:
            outcome = _process(source, hook, result, st, config.defaults, client, now, force_latest,
                               report, dry_run, ctx)
        except Exception as exc:  # noqa: BLE001 - last line of defence
            outcome = Outcome(source, "failed", f"internal error: {type(exc).__name__}: {exc}")
            if st.record_failure(outcome.detail, config.defaults["alert_after_failures"]):
                report.alerts.append(f"⚠️ **{source.name}** (`{source.id}`) failing: {outcome.detail}")
        report.outcomes.append(outcome)

    if ctx.preview_checks and not dry_run:
        try:
            ensure_previews(ctx, client)
        except Exception as exc:  # noqa: BLE001 - previews are cosmetic: never fail a run for them
            report.warnings.append(f"preview check stopped early: {exc}")

    if not only:
        state.prune({s.id for s in config.sources})
    state.heartbeat(now)
    return report


def _process(source, hook, result, st, defaults, client, now, force_latest, report, dry_run=False,
             ctx: DeliveryContext | None = None) -> Outcome:
    alert_after = defaults["alert_after_failures"]
    if isinstance(result, SourceError):
        detail = str(result)
        if st.record_failure(detail, alert_after):
            report.alerts.append(f"⚠️ **{source.name}** (`{source.id}`) has failed {st.failures} runs in a row: {detail[:300]}")
        return Outcome(source, "failed", detail)

    collected: Collected = result
    if collected.note:
        report.warnings.append(f"{source.id}: {collected.note} (update `site` in sources.toml)")

    if collected.method == "feed":
        st.set("resolved_feed", collected.url)
        st.set("etag", collected.etag)
        st.set("last_modified", collected.last_modified)
    else:
        st.set("resolved_feed", None)
        st.set("etag", None)
        st.set("last_modified", None)

    if collected.not_modified:
        _success(source, st, now, report)
        return Outcome(source, "not-modified", "feed unchanged (HTTP 304)", method=collected.method)

    if force_latest:
        relevant = [i for i in collected.items if source.keyword_ok(i.title)]
        newest = _newest_first(relevant)[:force_latest]
        # On a source that was never seeded, record everything it lists right now;
        # otherwise its other recent posts would be announced as new later.
        selection = Selection(
            to_post=list(reversed(newest)),
            mark_without_posting=[] if st.initialized else list(collected.items),
            note=f"forced re-send of newest {len(newest)}",
        )
    else:
        selection = select_new(source, collected.items, st, defaults, now)

    for item in selection.mark_without_posting:
        st.mark_seen(item.keys())
    if not st.initialized:
        st.mark_initialized()
        if not selection.to_post:
            _success(source, st, now, report)
            return Outcome(source, "seeded", selection.note, method=collected.method)
    if selection.reseeded:
        report.warnings.append(f"{source.id}: {selection.note}")

    if not selection.to_post:
        _success(source, st, now, report)
        return Outcome(source, "ok", selection.note or f"{len(collected.items)} items, nothing new",
                       method=collected.method)

    posted = dupes = 0
    try:
        posted, dupes = _deliver(source, hook, selection, st, client, defaults["digest_over"], ctx)
    except WebhookError as exc:
        detail = f"Discord: {exc}"
        if st.record_failure(detail, alert_after):
            report.alerts.append(f"⚠️ **{source.name}** (`{source.id}`) cannot post: {exc}")
        return Outcome(source, "failed", detail, posted=posted, method=collected.method)

    _success(source, st, now, report)
    if not posted:
        return Outcome(source, "ok", f"skipped {dupes} duplicate(s) of stories already posted",
                       method=collected.method)
    note = f"posted {posted}"
    if dupes:
        note += f", skipped {dupes} duplicate(s)"
    if force_latest:
        note += " (forced re-send)"
    if selection.skipped_over_limit:
        note += f", {selection.skipped_over_limit} older items skipped (max_per_run={source.max_per_run})"
    return Outcome(source, "posted", note, posted=posted, method=collected.method)


def _deliver(source: Source, hook: str | None, selection: Selection, st: SourceState,
             client: DiscordClient, digest_over: int = 3, ctx: DeliveryContext | None = None) -> tuple[int, int]:
    """Post the selected items. Returns (posted, skipped_duplicates).

    Order per item: skip if the same story already went to this category's channel;
    post it; mark it seen only after Discord confirms; record it in the journal;
    copy it to the firehose channel unless the story already appeared there.
    """
    from .config import DISCORD_DEFAULTS  # local import keeps module load order simple

    if ctx is None:
        ctx = DeliveryContext(discord=dict(DISCORD_DEFAULTS, colors={}, roles={}), categories={}, now=utcnow())
    hours = ctx.discord.get("dedupe_hours", 72)
    category = source.category

    kept, dupes = [], 0
    for item in selection.to_post:
        if ctx.journal and ctx.journal.find_duplicate(item.title, item.link, ctx.now, hours, category=category):
            st.mark_seen(item.keys())
            dupes += 1
        else:
            kept.append(item)
    if not kept:
        return 0, dupes

    card = ctx.discord.get("style", "link") == "card"
    mode = source.mode
    if mode == "auto":
        mode = "digest" if len(kept) > digest_over else "each"
    if not card:
        mode = "each"  # link style: every article is its own message, so every one gets a preview
    avatar = ctx.avatar_for(source)
    username = source.display_name
    common = dict(source_name=source.name, source_url=source.site, avatar=avatar,
                  category_name=ctx.category_name(category), color=ctx.color_for(category),
                  brand=ctx.discord.get("brand", "Within Quantum"))

    def firehose_copy(payloads: list[dict], item: Item | None = None) -> None:
        if not ctx.firehose or ctx.firehose_failed:
            return
        try:
            for payload in payloads:
                mid = client.deliver(ctx.firehose, payload, username=username, avatar_url=avatar)
                if item is not None and mid and not card:
                    ctx.preview_checks.append((ctx.firehose, mid, item, source, time.monotonic()))
        except WebhookError as exc:  # best effort: never blocks the category channel
            ctx.firehose_failed = True
            if ctx.report is not None:
                ctx.report.warnings.append(f"firehose channel: {exc}")

    def is_firehose_duplicate(item: Item) -> bool:
        return bool(ctx.journal and ctx.journal.find_duplicate(item.title, item.link, ctx.now, hours))

    if mode == "digest":
        fresh_for_firehose = [i for i in kept if not is_firehose_duplicate(i)]
        if card:
            payloads = [{"embeds": group} for group in group_embeds(
                digest_embeds(kept, skipped=selection.skipped_over_limit, **common))]
        else:
            payloads = [{"content": m, "flags": 4} for m in format_digest(source.name, kept, selection.skipped_over_limit)]
        for index, payload in enumerate(payloads):
            client.deliver(hook or "", payload, username=username, avatar_url=avatar,
                           ping_role=ctx.role_once(category) if index == 0 else "")
        for item in kept:
            st.mark_seen(item.keys())
            if ctx.journal:
                ctx.journal.add(title=item.title, url=item.link, source_id=source.id, source_name=source.name,
                                category=category, when=ctx.now, published=item.published,
                                firehose=item in fresh_for_firehose and bool(ctx.firehose))
        if fresh_for_firehose:
            if card:
                firehose_copy([{"embeds": g} for g in group_embeds(digest_embeds(fresh_for_firehose, **common))])
            else:
                firehose_copy([{"content": m, "flags": 4} for m in format_digest(source.name, fresh_for_firehose)])
        return len(kept), dupes

    posted = 0
    for item in kept:
        to_firehose = not is_firehose_duplicate(item)
        if card:
            payload = {"embeds": [card_embed(item, image=ctx.image_for(item),
                                             image_mode=ctx.discord.get("preview_image", "thumbnail"), **common)]}
        else:
            payload = {"content": "\n".join(chunk_lines([format_item(item)]))}
        mid = client.deliver(hook or "", payload, username=username, avatar_url=avatar,
                             ping_role=ctx.role_once(category))
        st.mark_seen(item.keys())  # only after Discord confirmed delivery
        if mid and not card:
            ctx.preview_checks.append((hook or "", mid, item, source, time.monotonic()))
        if ctx.journal:
            ctx.journal.add(title=item.title, url=item.link, source_id=source.id, source_name=source.name,
                            category=category, when=ctx.now, published=item.published,
                            firehose=to_firehose and bool(ctx.firehose))
        if to_firehose:
            firehose_copy([payload], item)
        posted += 1
    return posted, dupes


PREVIEW_WAIT_SECONDS = 10


def ensure_previews(ctx: DeliveryContext, client: DiscordClient, wait: float = PREVIEW_WAIT_SECONDS,
                    sleep=None) -> int:
    """Make sure every link-style post shows a preview.

    Discord draws link previews a few seconds after a message is posted, and
    only if the linked page offers preview information and lets Discord read
    it. After giving it time, each post is read back; any post still without a
    preview gets a box shaped like one (site name, linked title, the page's
    image if it has one). Posts where Discord made its own preview are left alone.
    Returns how many posts received a box.
    """
    sleep = sleep or getattr(client, "_sleep", time.sleep)
    oldest = min(entry[4] for entry in ctx.preview_checks)
    remaining = wait - (time.monotonic() - oldest)
    if remaining > 0:
        sleep(remaining)
    added = 0
    large = ctx.discord.get("preview_image", "large") != "thumbnail"
    for hook, mid, item, source, _ in ctx.preview_checks:
        message = client.get(hook, mid)
        if message is None or message.get("embeds"):
            continue  # unreadable, or Discord made its own preview
        box = preview_embed(item, site_name=source.name, site_url=source.site, image=ctx.image_for(item), large=large)
        if client.edit(hook, mid, {"embeds": [box]}):
            added += 1
    if added and ctx.report is not None:
        ctx.report.warnings.append(f"added a preview box to {added} post(s) where Discord made none")
    ctx.preview_checks.clear()
    return added


def _success(source: Source, st: SourceState, now, report: RunReport) -> None:
    if st.record_success(now):
        report.alerts.append(f"✅ **{source.name}** (`{source.id}`) recovered.")


# --------------------------------------------------------------------------
# Reporting
# --------------------------------------------------------------------------

STATUS_ICON = {
    "posted": "📬", "ok": "✅", "not-modified": "✅", "seeded": "🌱",
    "failed": "❌", "no-webhook": "⚪", "skipped": "⏸️",
}


def summary_markdown(report: RunReport, title: str = "Feed run") -> str:
    counts: dict[str, int] = {}
    for outcome in report.outcomes:
        counts[outcome.status] = counts.get(outcome.status, 0) + 1
    total_posted = sum(o.posted for o in report.outcomes)
    lines = [f"## {title}", "",
             f"**{total_posted}** items posted · " + " · ".join(f"{STATUS_ICON.get(k, '')} {k}: {v}" for k, v in sorted(counts.items())),
             ""]
    if report.warnings:
        lines += ["### Warnings", ""] + [f"- {w}" for w in report.warnings] + [""]
    interesting = [o for o in report.outcomes if o.status in ("posted", "failed", "seeded")]
    if interesting:
        lines += ["| | Source | Method | Detail |", "|---|---|---|---|"]
        for o in sorted(interesting, key=lambda o: (o.status != "failed", o.source.id)):
            detail = o.detail.replace("|", "\\|").replace("\n", " ")[:300]
            lines.append(f"| {STATUS_ICON.get(o.status, '')} | `{o.source.id}` | {o.method or '-'} | {detail} |")
    quiet = [o.source.id for o in report.outcomes if o.status == "no-webhook"]
    if quiet:
        lines += ["", f"<details><summary>{len(quiet)} sources without a webhook (not polled)</summary>", "",
                  ", ".join(f"`{q}`" for q in quiet), "", "</details>"]
    return "\n".join(lines) + "\n"


def write_step_summary(markdown: str) -> None:
    path = os.environ.get("GITHUB_STEP_SUMMARY")
    if not path:
        return
    try:
        with open(path, "a", encoding="utf-8") as handle:
            handle.write(markdown + "\n")
    except OSError:
        pass


def send_alerts(alerts: list[str], status_webhook: str | None, client: DiscordClient) -> None:
    if not alerts or not status_webhook:
        return
    for message in chunk_lines(alerts):
        try:
            client.send(status_webhook, message, "Feed Bot Status")
        except WebhookError as exc:
            print(f"::warning::could not send status alert: {exc}")
            return
