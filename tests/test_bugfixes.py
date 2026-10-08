"""Regression tests for: forced re-sends being blocked, posts appearing twice after
an ambiguous Discord failure, double previews, and duplicates in shared channels."""

from __future__ import annotations

import json
import socket
import tempfile
import textwrap
import unittest
import urllib.error
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest import mock

from feedbot import http
from feedbot.config import load_config, parse_webhooks
from feedbot.discord import DiscordClient
from feedbot.http import AMBIGUOUS, Response
from feedbot.journal import Journal
from feedbot.runner import run
from feedbot.state import State

NOW = datetime.now(timezone.utc)
HOOK = "https://discord.com/api/webhooks/111/sectiontoken"


def rss(*items):
    body = "".join(
        f"<item><title>{t}</title><link>{u}</link>"
        f"<pubDate>{(NOW - timedelta(hours=h)).strftime('%a, %d %b %Y %H:%M:%S +0000')}</pubDate></item>"
        for t, u, h in items)
    return f"<rss><channel>{body}</channel></rss>".encode()


class Discord:
    """Fake Discord. `script` is a list of statuses to return for successive POSTs."""

    def __init__(self, script=None, previews=()):
        self.script = list(script or [])
        self.previews = set(previews)
        self.calls = []
        self.next_id = 500

    def __call__(self, url, payload, timeout=20, method="POST"):
        self.calls.append({"url": url, "payload": payload, "method": method})
        if method == "GET":
            mid = url.rsplit("/", 1)[-1]
            return 200, json.dumps({"id": mid, "content": "**T**\nhttps://x.test/p/1",
                                    "embeds": [{"type": "link"}] if mid in self.previews else []}).encode(), {}
        if method == "POST" and self.script:
            status = self.script.pop(0)
            if status != 200:
                return status, b"", {}
        self.next_id += 1
        return 200, json.dumps({"id": str(self.next_id)}).encode(), {}

    def posts(self):
        return [c for c in self.calls if c["method"] == "POST"]


def client(d):
    return DiscordClient(min_interval=0, sleep=lambda s: None, post=d, request=d)


class Base(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.dir = Path(self.tmp.name)
        (self.dir / "s.toml").write_text(textwrap.dedent("""
            [categories]
            hardware = "Hardware"
            qec = "Error correction"
            [[source]]
            id = "acme"
            name = "Acme"
            category = "hardware"
            feed = "https://acme.test/feed"
            site = "https://acme.test/"
            [[source]]
            id = "riverlane"
            name = "Riverlane"
            category = "qec"
            feed = "https://river.test/feed"
            site = "https://river.test/"
        """))
        self.config = load_config(self.dir / "s.toml")
        self.feeds = {"https://acme.test/feed": rss(("Acme old", "https://acme.test/p/0", 9)),
                      "https://river.test/feed": rss(("River old", "https://river.test/p/0", 9))}
        self.state = State.load(self.dir / "state.json")
        self.journal = Journal.load(self.dir / "journal.json")

    def tearDown(self):
        self.tmp.cleanup()

    def fetch(self, url, **kw):
        return Response(url, 200, self.feeds.get(url, b"<html></html>"), {})

    def go(self, d, hooks, **kw):
        return run(self.config, parse_webhooks(json.dumps(hooks), self.config), self.state,
                   client=client(d), fetch=self.fetch, journal=self.journal, **kw)


class ForcedResendTests(Base):
    def test_force_latest_resends_even_if_posted_minutes_ago(self):
        """Reported bug: force_latest = 1 sent nothing for anything posted in the last 72 hours."""
        hooks = {"category:hardware": HOOK}
        self.go(Discord(), hooks, only={"acme"})                       # seed silently
        self.feeds["https://acme.test/feed"] = rss(("Acme news", "https://acme.test/p/1", 1), ("Acme old", "https://acme.test/p/0", 9))
        first = Discord()
        self.go(first, hooks, only={"acme"})                           # normal post
        self.assertEqual(len(first.posts()), 1)

        for attempt in range(2):                                       # force it twice in a row
            forced = Discord()
            report = self.go(forced, hooks, only={"acme"}, force_latest=1)
            self.assertEqual(len(forced.posts()), 1, f"forced attempt {attempt + 1} sent nothing")
            self.assertIn("Acme news", forced.posts()[0]["payload"]["content"])
            self.assertEqual(report.outcomes[0].status, "posted")

    def test_normal_runs_still_skip_duplicates(self):
        hooks = {"category:hardware": HOOK}
        self.go(Discord(), hooks, only={"acme"})
        self.feeds["https://acme.test/feed"] = rss(("Acme news", "https://acme.test/p/1", 1))
        self.go(Discord(), hooks, only={"acme"})
        again = Discord()
        self.go(again, hooks, only={"acme"})
        self.assertEqual(again.posts(), [])


class AmbiguousFailureTests(Base):
    def test_timeout_after_sending_is_not_retried(self):
        """Discord stored the message but the reply was lost: retrying posted it twice."""
        hooks = {"category:hardware": HOOK}
        self.go(Discord(), hooks, only={"acme"})
        self.feeds["https://acme.test/feed"] = rss(("Acme news", "https://acme.test/p/1", 1))
        d = Discord(script=[AMBIGUOUS, 200, 200])
        report = self.go(d, hooks, only={"acme"})
        self.assertEqual(len(d.posts()), 1)                            # no second attempt
        self.assertTrue(any("not confirmed" in w for w in report.warnings))
        later = Discord()
        self.go(later, hooks, only={"acme"})
        self.assertEqual(later.posts(), [])                            # and not re-posted next run

    def test_server_error_500_is_not_retried_but_503_is(self):
        for status, expected_posts in ((500, 1), (504, 1), (503, 2)):
            d = Discord(script=[status, 200])
            client(d).deliver(HOOK, {"content": "x"}, username="A")
            self.assertEqual(len(d.posts()), expected_posts, f"HTTP {status}")

    def test_edits_and_reads_are_still_retried(self):
        d = Discord()
        flaky = iter([(AMBIGUOUS, b"", {}), (200, b"{}", {})])
        c = DiscordClient(min_interval=0, sleep=lambda s: None, post=d, request=lambda *a, **k: next(flaky))
        self.assertTrue(c.edit(HOOK, "1", {"content": "x"}))

    def test_post_json_classifies_failures(self):
        def timeout(request, timeout=None):
            raise TimeoutError("read timed out")
        def refused(request, timeout=None):
            raise urllib.error.URLError(ConnectionRefusedError("refused"))
        with mock.patch.object(http.urllib.request, "urlopen", timeout):
            self.assertEqual(http.post_json(HOOK, {"content": "x"})[0], AMBIGUOUS)
        with mock.patch.object(http.urllib.request, "urlopen", refused):
            self.assertEqual(http.post_json(HOOK, {"content": "x"})[0], 0)


class DoublePreviewTests(Base):
    def test_fallback_box_suppresses_discords_late_preview(self):
        hooks = {"category:hardware": HOOK}
        self.go(Discord(), hooks, only={"acme"})
        self.feeds["https://acme.test/feed"] = rss(("T", "https://x.test/p/1", 1))
        d = Discord()                                                  # Discord made no preview
        self.go(d, hooks, only={"acme"})
        patch = next(c for c in d.calls if c["method"] == "PATCH")
        self.assertIn("<https://x.test/p/1>", patch["payload"]["content"])  # Discord cannot add a second one
        self.assertEqual(len(patch["payload"]["embeds"]), 1)

    def test_posts_with_discords_preview_are_untouched(self):
        hooks = {"category:hardware": HOOK}
        self.go(Discord(), hooks, only={"acme"})
        self.feeds["https://acme.test/feed"] = rss(("T", "https://x.test/p/1", 1))
        d = Discord(previews={"501"})
        self.go(d, hooks, only={"acme"})
        self.assertFalse([c for c in d.calls if c["method"] == "PATCH"])


class SharedChannelTests(Base):
    def test_two_sections_sharing_one_channel_never_repeat_a_story(self):
        """Duplicate detection was per section, so a shared channel showed the story twice."""
        hooks = {"category:hardware": HOOK, "category:qec": HOOK}   # both sections -> one channel
        self.go(Discord(), hooks)
        self.feeds["https://acme.test/feed"] = rss(("Acme and Riverlane demonstrate real-time decoding on chip", "https://acme.test/p/7", 2))
        self.feeds["https://river.test/feed"] = rss(("Acme and Riverlane demonstrate real-time decoding on a chip", "https://river.test/p/7", 1))
        d = Discord()
        self.go(d, hooks)
        self.assertEqual(len(d.posts()), 1)

    def test_separate_channels_each_get_the_story(self):
        hooks = {"category:hardware": HOOK, "category:qec": "https://discord.com/api/webhooks/222/othertoken"}
        self.go(Discord(), hooks)
        self.feeds["https://acme.test/feed"] = rss(("Acme and Riverlane demonstrate real-time decoding on chip", "https://acme.test/p/7", 2))
        self.feeds["https://river.test/feed"] = rss(("Acme and Riverlane demonstrate real-time decoding on a chip", "https://river.test/p/7", 1))
        d = Discord()
        self.go(d, hooks)
        self.assertEqual(len(d.posts()), 2)

    def test_journal_never_stores_webhook_urls(self):
        hooks = {"category:hardware": HOOK}
        self.go(Discord(), hooks, only={"acme"})
        self.feeds["https://acme.test/feed"] = rss(("Acme news", "https://acme.test/p/1", 1))
        self.go(Discord(), hooks, only={"acme"})
        self.journal.save(NOW)
        text = (self.dir / "journal.json").read_text()
        self.assertNotIn("sectiontoken", text)
        self.assertNotIn("/webhooks/", text)


if __name__ == "__main__":
    unittest.main()
