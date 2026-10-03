"""Regression tests for the forced re-send, preview, archive and website changes."""

from __future__ import annotations

import json
import tempfile
import textwrap
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

from feedbot.archive import MAX_PER_SOURCE, Archive
from feedbot.config import ConfigError, load_config, parse_webhooks
from feedbot.discord import DiscordClient, format_digest, format_item
from feedbot.http import Response
from feedbot.runner import run
from feedbot.site import build_site, site_data
from feedbot.state import State
from feedbot.util import Item

HOOK = "https://discord.com/api/webhooks/123/abc"
NOW = datetime.now(timezone.utc)


def rss(*items: Item) -> bytes:
    body = "".join(
        f"<item><title>{i.title}</title><link>{i.link}</link>"
        f"<pubDate>{i.published.strftime('%a, %d %b %Y %H:%M:%S +0000')}</pubDate></item>" for i in items)
    return f"<rss><channel>{body}</channel></rss>".encode()


def item(n, hours=1.0, title=None):
    return Item(title=title or f"Post {n}", link=f"https://blog.test/p/{n}", published=NOW - timedelta(hours=hours))


class RecordingFetch:
    """Serves one feed; answers 304 whenever a conditional header is sent (like a real server)."""

    def __init__(self, body: bytes):
        self.body = body
        self.calls: list[dict] = []

    def __call__(self, url, etag=None, last_modified=None, **kw):
        self.calls.append({"url": url, "etag": etag, "last_modified": last_modified})
        if etag or last_modified:
            return Response(url, 304, b"", {})
        return Response(url, 200, self.body, {"etag": '"v1"', "last-modified": "Tue, 29 Sep 2026 16:22:33 GMT"})


class Post:
    def __init__(self):
        self.payloads = []

    def __call__(self, url, payload, timeout=20):
        self.payloads.append(payload)
        return 200, b"{}", {}


def client(post):
    return DiscordClient(min_interval=0, sleep=lambda s: None, post=post)


class Base(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.dir = Path(self.tmp.name)
        (self.dir / "sources.toml").write_text(textwrap.dedent("""
            [categories]
            research = "Research"
            [site]
            title = "Test Times"
            [[source]]
            id = "blog"
            name = "Blog"
            category = "research"
            feed = "https://blog.test/feed"
            [[source]]
            id = "nohook"
            name = "No Hook"
            category = "research"
            feed = "https://blog.test/feed"
        """))
        self.config = load_config(self.dir / "sources.toml")
        self.hooks = parse_webhooks(json.dumps({"blog": HOOK}), self.config)

    def tearDown(self):
        self.tmp.cleanup()


class ForcedResendTests(Base):
    def test_forced_resend_ignores_conditional_get_and_posts(self):
        """The reported bug: forcing a re-send of an unchanged feed sent nothing."""
        fetch = RecordingFetch(rss(item(3, 1), item(2, 2), item(1, 3)))
        state = State.load(self.dir / "s.json")
        run(self.config, self.hooks, state, only={"blog"}, client=client(Post()), fetch=fetch)   # seed, stores ETag
        self.assertTrue(state.source("blog").get("etag"))

        routine = Post()
        run(self.config, self.hooks, state, only={"blog"}, client=client(routine), fetch=fetch)
        self.assertEqual(routine.payloads, [])                                    # routine run: 304, nothing new
        self.assertTrue(fetch.calls[-1]["etag"])

        forced = Post()
        report = run(self.config, self.hooks, state, only={"blog"}, force_latest=2, client=client(forced), fetch=fetch)
        self.assertIsNone(fetch.calls[-1]["etag"])                                # full fetch
        self.assertEqual(len(forced.payloads), 2)
        self.assertIn("Post 3", forced.payloads[-1]["content"])                  # newest last
        self.assertIn("forced", report.outcomes[0].detail)

    def test_forced_resend_on_unseeded_source_records_whole_feed(self):
        """Hidden bug: only the re-sent item was remembered, so older posts later looked new."""
        feed = [item(n, n) for n in range(1, 9)]
        fetch = RecordingFetch(rss(*feed))
        state = State.load(self.dir / "s.json")
        run(self.config, self.hooks, state, only={"blog"}, force_latest=1, client=client(Post()), fetch=fetch)
        st = state.source("blog")
        self.assertTrue(all(st.has_seen(i.keys()) for i in feed))

        later = Post()
        fetch.body = rss(item(9, 0.5), *feed)
        fetch_no_cache = lambda url, **kw: Response(url, 200, fetch.body, {})
        run(self.config, self.hooks, state, only={"blog"}, client=client(later), fetch=fetch_no_cache)
        self.assertEqual(len(later.payloads), 1)                                  # only the genuinely new post
        self.assertIn("Post 9", later.payloads[0]["content"])


class PreviewTests(unittest.TestCase):
    def test_single_post_is_bold_title_plus_bare_url_and_allows_previews(self):
        post = Post()
        text = format_item(item(1))
        client(post).send(HOOK, text, "Blog")
        self.assertEqual(text, "**Post 1**\nhttps://blog.test/p/1")
        self.assertNotIn("flags", post.payloads[0])

    def test_digest_is_compact_without_previews(self):
        post = Post()
        for message in format_digest("Blog", [item(n) for n in range(6)]):
            client(post).send(HOOK, message, "Blog", previews=False)
        self.assertEqual(post.payloads[0]["flags"], 4)


class WebsiteOnlySourceTests(Base):
    def test_source_without_webhook_is_archived_but_discord_state_untouched(self):
        fetch = RecordingFetch(rss(item(1)))
        state = State.load(self.dir / "s.json")
        archive = Archive.load(self.dir / "a.json")
        report = run(self.config, self.hooks, state, only={"nohook"}, client=client(Post()), fetch=fetch, archive=archive)
        self.assertEqual(report.outcomes[0].status, "archived")
        self.assertEqual(len(archive.sources["nohook"]), 1)
        st = state.source("nohook")
        self.assertFalse(st.initialized)
        self.assertEqual(st.data["seen"], [])
        self.assertIsNone(st.get("etag"))

        run(self.config, self.hooks, state, only={"nohook"}, client=client(Post()), fetch=fetch, archive=archive)
        self.assertIsNone(fetch.calls[-1]["etag"])                                # never 304-starved


class ArchiveBackfillTests(Base):
    def test_existing_source_missing_from_archive_gets_a_full_fetch(self):
        """Upgrading: sources with saved ETags must still be archived on the first run."""
        fetch = RecordingFetch(rss(item(1), item(2, 2)))
        state = State.load(self.dir / "s.json")
        run(self.config, self.hooks, state, only={"blog"}, client=client(Post()), fetch=fetch)  # pre-upgrade: no archive
        self.assertTrue(state.source("blog").get("etag"))
        archive = Archive.load(self.dir / "a.json")
        run(self.config, self.hooks, state, only={"blog"}, client=client(Post()), fetch=fetch, archive=archive)
        self.assertIsNone(fetch.calls[-1]["etag"])
        self.assertEqual(len(archive.sources["blog"]), 2)
        run(self.config, self.hooks, state, only={"blog"}, client=client(Post()), fetch=fetch, archive=archive)
        self.assertTrue(fetch.calls[-1]["etag"])                                  # afterwards: cheap 304s again


class ArchiveTests(unittest.TestCase):
    def test_merge_order_bootstrap_cap_and_updates(self):
        with tempfile.TemporaryDirectory() as tmp:
            arch = Archive.load(Path(tmp) / "a.json")
            undated = [Item(title=f"Link {n}", link=f"https://co.test/news/{n}") for n in range(3)]
            arch.merge("co", undated, NOW)
            self.assertTrue(all(e.get("bootstrap") for e in arch.sources["co"]))  # first read of an undated page

            arch.merge("co", [Item(title="Fresh story", link="https://co.test/news/new")] + undated, NOW)
            first = arch.sources["co"][0]
            self.assertEqual(first["title"], "Fresh story")
            self.assertNotIn("bootstrap", first)                                  # appeared later: a real new link

            arch.merge("co", [Item(title="Fresh story (updated)", link="https://co.test/news/new")], NOW)
            self.assertEqual(arch.sources["co"][0]["title"], "Fresh story (updated)")

            arch.merge("big", [item(n, n) for n in range(MAX_PER_SOURCE + 10)], NOW)
            self.assertEqual(len(arch.sources["big"]), MAX_PER_SOURCE)
            self.assertEqual(arch.sources["big"][0]["title"], "Post 0")           # newest first

            arch.save()
            again = Archive.load(Path(tmp) / "a.json")
            self.assertEqual(again.sources["co"][0]["title"], "Fresh story (updated)")
            again.prune({"co"})
            self.assertNotIn("big", again.sources)

    def test_corrupt_archive_is_set_aside(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "a.json"
            path.write_text("{oops")
            arch = Archive.load(path)
            self.assertEqual(arch.sources, {})
            self.assertTrue(arch.warnings)


class SiteTests(Base):
    def test_build_embeds_data_safely(self):
        archive = Archive.load(self.dir / "a.json")
        archive.merge("blog", [Item(title="Danger </script><script>alert(1)</script>",
                                    link="https://blog.test/x", published=NOW)], NOW)
        out = build_site(self.config, archive, self.dir / "_site")
        page = out.read_text()
        self.assertIn("Test Times", page)
        self.assertNotIn("</script><script>alert", page)                        # cannot break out of the data block
        data = json.loads((self.dir / "_site" / "data.json").read_text())
        self.assertEqual(data["items"][0]["t"], "Danger </script><script>alert(1)</script>")
        self.assertEqual(data["categories"][0]["short"], "Research")

    def test_disabled_sources_are_not_published(self):
        archive = Archive.load(self.dir / "a.json")
        archive.merge("blog", [item(1)], NOW)
        self.config.sources[0].enabled = False
        data = site_data(self.config, archive)
        self.assertEqual([s["id"] for s in data["sources"]], ["nohook"])
        self.assertEqual(data["items"], [])

    def test_site_config_validation(self):
        path = self.dir / "bad.toml"
        path.write_text('[site]\ncolour = "x"\nitems_per_source = 99\n[[source]]\nid = "aa"\nname = "A"\ncategory = "c"\nfeed = "https://a.test/f"\n')
        with self.assertRaises(ConfigError) as ctx:
            load_config(path)
        text = " ".join(ctx.exception.errors)
        self.assertIn("unknown key 'colour'", text)
        self.assertIn("items_per_source", text)


if __name__ == "__main__":
    unittest.main()
