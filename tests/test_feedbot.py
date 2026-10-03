"""Offline tests: no network access is used anywhere in this file.

Run with:  python -m unittest discover -s tests -v
"""

from __future__ import annotations

import json
import tempfile
import textwrap
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

from feedbot.config import ConfigError, Source, load_config, parse_webhooks
from feedbot.discord import DiscordClient, WebhookError, chunk_lines, escape_markdown, format_digest, format_item
from feedbot.feeds import FeedParseError, looks_like_feed, parse_feed
from feedbot.http import FetchError, Response
from feedbot.pages import discover_feeds, extract_links
from feedbot.runner import SourceError, collect, run, select_new, summary_markdown
from feedbot.state import MAX_SEEN, SourceState, State
from feedbot.util import Item, normalize_url, parse_date

NOW = datetime(2026, 10, 3, 12, 0, tzinfo=timezone.utc)
DEFAULTS = {
    "max_per_run": 5, "max_age_days": 14, "mode": "auto", "digest_over": 3, "bootstrap_posts": 0,
    "flood_threshold": 15, "alert_after_failures": 3, "stale_after_days": 365,
}
HOOK = "https://discord.com/api/webhooks/123456/abcDEF_-xyz"

RSS = b"""<?xml version="1.0" encoding="UTF-8"?>
<rss version="2.0" xmlns:dc="http://purl.org/dc/elements/1.1/">
<channel><title>Blog</title>
<item><title>First &amp; foremost</title><link>https://ex.com/a?utm_source=x</link>
<guid isPermaLink="false">post-1</guid><pubDate>Thu, 01 Oct 2026 10:00:00 +0000</pubDate></item>
<item><title><![CDATA[Second <em>post</em>]]></title><link>https://ex.com/b</link>
<dc:date>2026-10-02T09:00:00Z</dc:date></item>
</channel></rss>"""

ATOM = b"""<?xml version="1.0" encoding="utf-8"?>
<feed xmlns="http://www.w3.org/2005/Atom"><title>Atom</title>
<entry><title type="html">Atom &lt;b&gt;one&lt;/b&gt;</title>
<link rel="replies" href="https://ex.com/one#comments"/>
<link rel="alternate" type="text/html" href="/one"/>
<id>tag:ex.com,2026:1</id><updated>2026-10-01T00:00:00Z</updated></entry>
</feed>"""

RDF = b"""<?xml version="1.0"?>
<rdf:RDF xmlns:rdf="http://www.w3.org/1999/02/22-rdf-syntax-ns#" xmlns="http://purl.org/rss/1.0/"
 xmlns:dc="http://purl.org/dc/elements/1.1/">
<channel rdf:about="https://ex.com/"><title>RDF</title></channel>
<item rdf:about="https://ex.com/r1"><title>RDF item</title><link>https://ex.com/r1</link>
<dc:date>2026-09-30T08:00:00+02:00</dc:date></item>
</rdf:RDF>"""

BROKEN = b"""\n\n<?xml version="1.0"?><rss><channel>
<item><title>Caf&eacute; &nbsp; Q&A \x01</title><link>https://ex.com/c?a=1&b=2</link></item>
</channel></rss>"""

EVIL = b"""<?xml version="1.0"?><!DOCTYPE lolz [<!ENTITY lol "lol"><!ENTITY lol2 "&lol;&lol;&lol;">]>
<rss><channel><item><title>&lol2;</title><link>https://ex.com/x</link></item></channel></rss>"""

LISTING = """<html><head>
<link rel="alternate" type="application/rss+xml" title="Comments" href="/comments/feed/">
</head><body>
<nav><a href="/news/about-us-page-here">About us page here</a></nav>
<main>
 <article><a href="/news/why-decoders-matter"><img src="x.png"></a>
   <h3><a href="/news/why-decoders-matter">Why the decoder is the heart of QEC</a></h3>
   <a href="/news/why-decoders-matter">Read more</a></article>
 <article><h3>Scaling error correction on real systems</h3>
   <a href="/news/scaling-qec-real-systems">Read more</a></article>
 <a href="/news/page/2">Next page</a>
 <a href="/news/category/press">Press</a>
 <a href="https://other.com/news/external-story-here">External</a>
</main>
<footer><a href="/news/privacy-policy-update">Privacy</a></footer>
</body></html>"""


def item(n: int, days_ago: float | None = 1, title: str | None = None) -> Item:
    published = NOW - timedelta(days=days_ago) if days_ago is not None else None
    return Item(title=title or f"Post {n}", link=f"https://ex.com/p/{n}", guid=f"g{n}", published=published)


def source(**kw) -> Source:
    base = dict(id="src", name="Src", category="test", feeds=["https://ex.com/feed"], site="https://ex.com/")
    base.update(kw)
    return Source(**base)


class FakeFetch:
    def __init__(self, routes: dict):
        self.routes = routes
        self.calls: list[str] = []

    def __call__(self, url, **kwargs):
        self.calls.append(url)
        value = self.routes.get(url)
        if isinstance(value, Exception):
            raise value
        if value is None:
            raise FetchError("HTTP 404", status=404)
        if isinstance(value, Response):
            return value
        body = value.encode() if isinstance(value, str) else value
        return Response(url, 200, body, {"content-type": "text/html; charset=utf-8"})


class FakePost:
    def __init__(self, statuses: list[int] | None = None):
        self.statuses = list(statuses or [])
        self.payloads: list[dict] = []

    def __call__(self, url, payload, timeout=20):
        self.payloads.append(payload)
        status = self.statuses.pop(0) if self.statuses else 200
        body = json.dumps({"retry_after": 0.01}).encode() if status == 429 else b"{}"
        return status, body, {}


def client(post=None) -> DiscordClient:
    return DiscordClient(dry_run=False, min_interval=0, sleep=lambda s: None, post=post or FakePost())


# ---------------------------------------------------------------------------

class UtilTests(unittest.TestCase):
    def test_normalize_url_strips_tracking_and_trivia(self):
        a = normalize_url("http://WWW.Ex.com/a/?utm_source=x&id=3#frag")
        b = normalize_url("https://ex.com/a?id=3")
        self.assertEqual(a, b)

    def test_normalize_keeps_meaningful_query(self):
        self.assertNotEqual(normalize_url("https://ex.com/?p=1"), normalize_url("https://ex.com/?p=2"))

    def test_parse_date_formats(self):
        self.assertEqual(parse_date("Thu, 01 Oct 2026 10:00:00 +0000").day, 1)
        self.assertEqual(parse_date("2026-10-02T09:00:00Z").hour, 9)
        self.assertEqual(parse_date("2026-09-30T08:00:00+02:00").hour, 6)
        self.assertIsNone(parse_date("not a date"))
        self.assertIsNone(parse_date("1970-01-01T00:00:00Z"))
        self.assertIsNotNone(parse_date("2026-10-02").tzinfo)


class FeedTests(unittest.TestCase):
    def test_rss(self):
        items = parse_feed(RSS, "https://ex.com/feed")
        self.assertEqual([i.title for i in items], ["First & foremost", "Second post"])
        self.assertEqual(items[0].guid, "post-1")
        self.assertEqual(items[1].published.day, 2)

    def test_atom_prefers_alternate_and_resolves_relative(self):
        items = parse_feed(ATOM, "https://ex.com/atom.xml")
        self.assertEqual(items[0].link, "https://ex.com/one")
        self.assertEqual(items[0].title, "Atom one")

    def test_rdf(self):
        items = parse_feed(RDF)
        self.assertEqual(items[0].title, "RDF item")
        self.assertEqual(items[0].published.hour, 6)

    def test_broken_feed_is_repaired(self):
        items = parse_feed(BROKEN)
        self.assertEqual(items[0].title, "Café Q&A")
        self.assertEqual(items[0].link, "https://ex.com/c?a=1&b=2")

    def test_entity_expansion_is_neutralised(self):
        items = parse_feed(EVIL)
        self.assertEqual(len(items), 1)
        self.assertNotIn("lollollol", items[0].title)

    def test_not_a_feed(self):
        self.assertFalse(looks_like_feed(b"<!DOCTYPE html><html><body>x</body></html>"))
        self.assertTrue(looks_like_feed(RSS))
        with self.assertRaises(FeedParseError):
            parse_feed(b"<html><body>nope</body></html>")


class PageTests(unittest.TestCase):
    def test_discovery_skips_comment_feeds(self):
        html = LISTING.replace(
            "</head>", '<link rel="alternate" type="application/atom+xml" href="/feed.atom"></head>')
        self.assertEqual(discover_feeds(html, "https://ex.com/news"), ["https://ex.com/feed.atom"])

    def test_extract_listing(self):
        items = extract_links(LISTING, "https://ex.com/news")
        links = [i.link for i in items]
        self.assertEqual(links, ["https://ex.com/news/why-decoders-matter", "https://ex.com/news/scaling-qec-real-systems"])
        self.assertEqual(items[0].title, "Why the decoder is the heart of QEC")
        # "Read more" anchor falls back to the nearby heading
        self.assertEqual(items[1].title, "Scaling error correction on real systems")

    def test_custom_pattern(self):
        items = extract_links(LISTING, "https://ex.com/news", r"scaling-qec")
        self.assertEqual([i.link for i in items], ["https://ex.com/news/scaling-qec-real-systems"])

    def test_slug_fallback_when_articles_live_elsewhere(self):
        html = '<main><a href="/stories/2026/new-quantum-chip-announced">New quantum chip announced</a></main>'
        items = extract_links(html, "https://ex.com/blog")
        self.assertEqual(len(items), 1)

    def test_garbage_html_does_not_raise(self):
        self.assertEqual(extract_links("<a href=<<<>>", "https://ex.com/news"), [])


class CollectTests(unittest.TestCase):
    def test_feed_first(self):
        fetch = FakeFetch({"https://ex.com/feed": RSS})
        result = collect(source(), fetch=fetch)
        self.assertEqual(result.method, "feed")
        self.assertEqual(len(result.items), 2)

    def test_falls_back_to_second_feed(self):
        fetch = FakeFetch({"https://ex.com/feed2": ATOM})
        result = collect(source(feeds=["https://ex.com/feed", "https://ex.com/feed2"]), fetch=fetch)
        self.assertEqual(result.url, "https://ex.com/feed2")

    def test_discovers_feed_from_site(self):
        html = '<html><head><link rel="alternate" type="application/rss+xml" href="/real-feed"></head></html>'
        fetch = FakeFetch({"https://ex.com/": html, "https://ex.com/real-feed": RSS})
        result = collect(source(), fetch=fetch)
        self.assertEqual(result.url, "https://ex.com/real-feed")

    def test_scrapes_when_no_feed(self):
        fetch = FakeFetch({"https://ex.com/news": LISTING})
        result = collect(source(feeds=[], site="https://ex.com/news"), fetch=fetch)
        self.assertEqual(result.method, "html")
        self.assertEqual(len(result.items), 2)

    def test_html_page_served_at_feed_url_is_rejected(self):
        fetch = FakeFetch({"https://ex.com/feed": "<html>moved</html>", "https://ex.com/news": LISTING})
        result = collect(source(site="https://ex.com/news"), fetch=fetch)
        self.assertEqual(result.method, "html")

    def test_not_modified(self):
        fetch = FakeFetch({"https://ex.com/feed": Response("https://ex.com/feed", 304, b"", {})})
        result = collect(source(), resolved_feed="https://ex.com/feed", etag="abc", fetch=fetch)
        self.assertTrue(result.not_modified)

    def test_total_failure_raises_source_error(self):
        with self.assertRaises(SourceError) as ctx:
            collect(source(), fetch=FakeFetch({}))
        self.assertIn("HTTP 404", str(ctx.exception))


class SelectionTests(unittest.TestCase):
    def test_first_run_seeds_without_posting(self):
        st = SourceState()
        sel = select_new(source(), [item(1), item(2)], st, DEFAULTS, NOW)
        self.assertEqual(sel.to_post, [])
        self.assertEqual(len(sel.mark_without_posting), 2)

    def test_bootstrap_posts(self):
        sel = select_new(source(), [item(1, 3), item(2, 1)], SourceState(), dict(DEFAULTS, bootstrap_posts=1), NOW)
        self.assertEqual([i.title for i in sel.to_post], ["Post 2"])

    def initialized(self, *items) -> SourceState:
        st = SourceState()
        st.mark_initialized()
        for i in items:
            st.mark_seen(i.keys())
        return st

    def test_only_new_items_oldest_first(self):
        st = self.initialized(item(1))
        sel = select_new(source(), [item(3, 0.1), item(2, 0.5), item(1)], st, DEFAULTS, NOW)
        self.assertEqual([i.title for i in sel.to_post], ["Post 2", "Post 3"])

    def test_seen_by_guid_even_if_url_changed(self):
        st = self.initialized(item(1))
        moved = Item(title="Post 1", link="https://new-domain.com/p/1", guid="g1", published=NOW)
        self.assertEqual(select_new(source(), [moved], st, DEFAULTS, NOW).to_post, [])

    def test_old_items_are_not_posted(self):
        st = self.initialized()
        sel = select_new(source(), [item(1, days_ago=60)], st, DEFAULTS, NOW)
        self.assertEqual(sel.to_post, [])
        self.assertEqual(len(sel.mark_without_posting), 1)

    def test_max_per_run_keeps_newest(self):
        st = self.initialized()
        items = [item(n, days_ago=n / 10) for n in range(1, 9)]
        sel = select_new(source(max_per_run=3), items, st, DEFAULTS, NOW)
        self.assertEqual([i.title for i in sel.to_post], ["Post 3", "Post 2", "Post 1"])
        self.assertEqual(sel.skipped_over_limit, 5)

    def test_flood_guard(self):
        st = self.initialized(Item(title="x", link="https://ex.com/unrelated"))
        items = [item(n, days_ago=0.5) for n in range(1, 21)]
        sel = select_new(source(), items, st, DEFAULTS, NOW)
        self.assertTrue(sel.reseeded)
        self.assertEqual(sel.to_post, [])

    def test_keyword_filter(self):
        st = self.initialized()
        items = [item(1, title="New quantum chip"), item(2, title="Gardening tips")]
        sel = select_new(source(include_keywords=["Quantum"]), items, st, DEFAULTS, NOW)
        self.assertEqual([i.title for i in sel.to_post], ["New quantum chip"])

    def test_undated_listing_order_is_preserved(self):
        st = self.initialized()
        items = [item(1, None), item(2, None), item(3, None)]  # listing order: newest first
        sel = select_new(source(), items, st, DEFAULTS, NOW)
        self.assertEqual([i.title for i in sel.to_post], ["Post 3", "Post 2", "Post 1"])


class DiscordTests(unittest.TestCase):
    def test_format_item_escapes_markdown(self):
        text = format_item(Item(title="A [weird] *title* @everyone", link="https://ex.com/a_(b)", published=NOW))
        self.assertIn("\\[weird\\]", text)
        self.assertNotIn("@everyone", text)
        title_line, url_line = text.split("\n")
        self.assertEqual(url_line, "https://ex.com/a_(b)")  # bare URL on its own line -> Discord preview
        self.assertTrue(title_line.startswith("**") and "](" not in title_line)

    def test_chunking_respects_limit(self):
        chunks = chunk_lines(["x" * 500] * 10)
        self.assertTrue(all(len(c) <= 1900 for c in chunks))
        self.assertEqual(sum(c.count("x") for c in chunks), 5000)

    def test_digest(self):
        msgs = format_digest("Src", [item(n) for n in range(60)], skipped=3)
        self.assertGreater(len(msgs), 1)
        self.assertIn("+3 older", msgs[0])

    def test_payload_never_pings_and_allows_previews(self):
        post = FakePost()
        client(post).send(HOOK, "hello", "Discord Bot")
        payload = post.payloads[0]
        self.assertEqual(payload["allowed_mentions"], {"parse": []})
        self.assertNotIn("flags", payload)  # no SUPPRESS_EMBEDS: Discord shows link previews
        self.assertNotIn("discord", payload["username"].lower())

    def test_rate_limit_then_success(self):
        post = FakePost([429, 200])
        client(post).send(HOOK, "hi", "x")
        self.assertEqual(len(post.payloads), 2)

    def test_deleted_webhook_is_permanent(self):
        with self.assertRaises(WebhookError) as ctx:
            client(FakePost([404])).send(HOOK, "hi", "x")
        self.assertTrue(ctx.exception.permanent)

    def test_escape(self):
        self.assertEqual(escape_markdown("a_b"), "a\\_b")


class StateTests(unittest.TestCase):
    def test_round_trip_and_cap(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "state.json"
            state = State.load(path)
            st = state.source("a")
            st.mark_initialized()
            st.mark_seen([f"k{i}" for i in range(MAX_SEEN + 50)])
            state.save()
            again = State.load(path)
            self.assertEqual(len(again.source("a").data["seen"]), MAX_SEEN)
            self.assertTrue(again.source("a").has_seen([f"k{MAX_SEEN + 49}"]))
            self.assertFalse(again.source("a").has_seen(["k0"]))

    def test_corrupt_state_is_backed_up(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "state.json"
            path.write_text("{not json")
            state = State.load(path)
            self.assertTrue(state.warnings)
            self.assertTrue((Path(tmp) / "state.json.corrupt").exists())

    def test_alert_once_then_recover(self):
        st = SourceState()
        self.assertFalse(st.record_failure("x", 3))
        self.assertFalse(st.record_failure("x", 3))
        self.assertTrue(st.record_failure("x", 3))
        self.assertFalse(st.record_failure("x", 3))
        self.assertTrue(st.record_success(NOW))
        self.assertFalse(st.record_success(NOW))


class ConfigTests(unittest.TestCase):
    def write(self, text: str) -> Path:
        tmp = tempfile.NamedTemporaryFile("w", suffix=".toml", delete=False, encoding="utf-8")
        tmp.write(textwrap.dedent(text))
        tmp.close()
        return Path(tmp.name)

    def test_repository_config_is_valid(self):
        config = load_config(Path(__file__).resolve().parent.parent / "sources.toml")
        self.assertGreater(len(config.sources), 50)

    def test_errors_are_collected(self):
        path = self.write("""
            [[source]]
            id = "Bad ID"
            name = "x"
            [[source]]
            id = "ok-id"
            name = "y"
            category = "test"
            feed = "not a url"
            link_pattern = "("
            colour = "blue"
        """)
        with self.assertRaises(ConfigError) as ctx:
            load_config(path)
        text = "\n".join(ctx.exception.errors)
        for expected in ("id must match", "feed must be", "link_pattern", "unknown key 'colour'"):
            self.assertIn(expected, text)

    def test_webhook_parsing_and_lookup(self):
        path = self.write(f"""
            [[source]]
            id = "one"
            name = "One"
            category = "alpha"
            feed = "https://ex.com/feed"
            [[source]]
            id = "two"
            name = "Two"
            category = "alpha"
            feed = "https://ex.com/feed2"
        """)
        config = load_config(path)
        hooks = parse_webhooks(json.dumps({
            "one": HOOK, "category:alpha": HOOK + "2", "typo": HOOK, "two": "", "bad": "https://evil.com/x",
        }), config)
        self.assertEqual(hooks.for_source(config.sources[0]), HOOK)
        self.assertEqual(hooks.for_source(config.sources[1]), HOOK + "2")
        joined = " ".join(hooks.problems)
        self.assertIn("typo", joined)
        self.assertIn("bad", joined)
        self.assertNotIn("evil.com", joined)  # never echo URLs

    def test_invalid_webhook_json(self):
        hooks = parse_webhooks("{nope")
        self.assertEqual(hooks.mapping, {})
        self.assertTrue(hooks.problems)


class EndToEndTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.dir = Path(self.tmp.name)
        (self.dir / "sources.toml").write_text(textwrap.dedent("""
            [categories]
            test = "Test"
            [[source]]
            id = "good"
            name = "Good"
            category = "test"
            feed = "https://good.com/feed"
            [[source]]
            id = "broken"
            name = "Broken"
            category = "test"
            feed = "https://broken.com/feed"
            [[source]]
            id = "unhooked"
            name = "Unhooked"
            category = "test"
            feed = "https://good.com/feed"
        """))
        self.config = load_config(self.dir / "sources.toml")
        self.hooks = parse_webhooks(json.dumps({"good": HOOK, "broken": HOOK}), self.config)

    def tearDown(self):
        self.tmp.cleanup()

    def feed(self, *items: Item) -> bytes:
        entries = "".join(
            f"<item><title>{i.title}</title><link>{i.link}</link>"
            f"<pubDate>{i.published.strftime('%a, %d %b %Y %H:%M:%S +0000')}</pubDate></item>"
            for i in items)
        return f"<rss><channel>{entries}</channel></rss>".encode()

    def test_full_lifecycle(self):
        now_items = [item(1, 0.2), item(2, 0.1)]
        fetch = FakeFetch({"https://good.com/feed": self.feed(*now_items),
                           "https://broken.com/feed": FetchError("HTTP 500", 500)})
        state = State.load(self.dir / "state.json")
        post = FakePost()

        # Run 1: seeds silently, broken source isolated, unhooked source not polled.
        report = run(self.config, self.hooks, state, client=client(post), fetch=fetch, workers=2)
        statuses = {o.source.id: o.status for o in report.outcomes}
        self.assertEqual(statuses, {"good": "seeded", "broken": "failed", "unhooked": "archived"})
        self.assertEqual(post.payloads, [])
        state.save()

        # Run 2: a new item appears and is posted exactly once.
        fetch.routes["https://good.com/feed"] = self.feed(item(3, 0.05), *now_items)
        state = State.load(self.dir / "state.json")
        report = run(self.config, self.hooks, state, client=client(post), fetch=fetch, workers=2)
        self.assertEqual(len(post.payloads), 1)
        self.assertIn("Post 3", post.payloads[0]["content"])
        state.save()

        # Run 3: nothing new; broken source reaches the alert threshold exactly once.
        state = State.load(self.dir / "state.json")
        report = run(self.config, self.hooks, state, client=client(post), fetch=fetch, workers=2)
        self.assertEqual(len(post.payloads), 1)
        self.assertEqual(len(report.alerts), 1)
        self.assertIn("broken", report.alerts[0])
        self.assertIn("Feed run", summary_markdown(report))

    def test_failed_delivery_is_retried_next_run(self):
        fetch = FakeFetch({"https://good.com/feed": self.feed(item(1, 0.5))})
        state = State.load(self.dir / "state.json")
        run(self.config, self.hooks, state, only={"good"}, client=client(), fetch=fetch)  # seed
        fetch.routes["https://good.com/feed"] = self.feed(item(2, 0.1), item(1, 0.5))

        failing = FakePost([500] * 6)
        report = run(self.config, self.hooks, state, only={"good"}, client=client(failing), fetch=fetch)
        self.assertEqual(report.outcomes[0].status, "failed")

        working = FakePost()
        run(self.config, self.hooks, state, only={"good"}, client=client(working), fetch=fetch)
        self.assertEqual(len(working.payloads), 1)
        self.assertIn("Post 2", working.payloads[0]["content"])

    def test_dead_webhook_does_not_mark_seen(self):
        fetch = FakeFetch({"https://good.com/feed": self.feed(item(1, 0.5))})
        state = State.load(self.dir / "state.json")
        run(self.config, self.hooks, state, only={"good"}, client=client(), fetch=fetch)
        fetch.routes["https://good.com/feed"] = self.feed(item(2, 0.1), item(1, 0.5))
        report = run(self.config, self.hooks, state, only={"good"}, client=client(FakePost([404])), fetch=fetch)
        self.assertIn("deleted or revoked", report.outcomes[0].detail)
        self.assertFalse(state.source("good").has_seen(item(2).keys()))


if __name__ == "__main__":
    unittest.main()