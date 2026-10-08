"""Regression tests for the forced re-send and link-preview fixes."""

from __future__ import annotations

import json
import tempfile
import textwrap
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

from feedbot.config import load_config, parse_webhooks
from feedbot.discord import DiscordClient, format_digest, format_item
from feedbot.http import Response
from feedbot.runner import run
from feedbot.state import State
from feedbot.util import Item

HOOK = "https://discord.com/api/webhooks/123/abc"
NOW = datetime.now(timezone.utc)



def text_of(payload: dict) -> str:
    """Visible text of a webhook message: plain content plus card titles and lists."""
    parts = [payload.get("content", "")]
    for embed in payload.get("embeds", []):
        parts += [embed.get("title", ""), embed.get("description", "")]
    return "\n".join(p for p in parts if p)

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
        self.assertIn("Post 3", text_of(forced.payloads[-1]))                  # newest last
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
        self.assertIn("Post 9", text_of(later.payloads[0]))


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


if __name__ == "__main__":
    unittest.main()
