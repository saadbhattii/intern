"""Tests for the public-server features: avatars, cards, duplicates, firehose,
role pings, the journal, briefings, the source directory and preview images."""

from __future__ import annotations

import json
import tempfile
import textwrap
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

from feedbot.config import ConfigError, load_config, parse_webhooks
from feedbot.discord import DiscordClient, favicon_url, group_embeds, list_embeds
from feedbot.feeds import parse_feed
from feedbot.http import Response
from feedbot.journal import Journal, similar_titles, title_tokens
from feedbot.preview import find_image
from feedbot.runner import run
from feedbot.server import build_briefing, build_directory, send_briefing, sync_directory
from feedbot.state import State

NOW = datetime.now(timezone.utc)
HOOK_A = "https://discord.com/api/webhooks/1/hardware"
HOOK_B = "https://discord.com/api/webhooks/2/news"
FIRE = "https://discord.com/api/webhooks/3/firehose"


def rss(items, image=False):
    out = []
    for title, link, hours in items:
        date = (NOW - timedelta(hours=hours)).strftime("%a, %d %b %Y %H:%M:%S +0000")
        media = f'<media:thumbnail url="https://img.test/{abs(hash(link))}.png"/>' if image else ""
        out.append(f"<item><title>{title}</title><link>{link}</link><pubDate>{date}</pubDate>{media}</item>")
    return ('<rss xmlns:media="http://search.yahoo.com/mrss/"><channel>' + "".join(out) + "</channel></rss>").encode()


class Recorder:
    """Fake Discord: records every request and returns message ids."""

    def __init__(self, fail_on: str = ""):
        self.calls = []
        self.fail_on = fail_on
        self.counter = 100

    def __call__(self, url, payload, timeout=20, method="POST"):
        self.calls.append({"url": url, "payload": payload, "method": method})
        if self.fail_on and url.startswith(self.fail_on):
            return 404, b'{"message": "Unknown Webhook"}', {}
        self.counter += 1
        return 200, json.dumps({"id": str(self.counter)}).encode(), {}

    def posts_to(self, hook):
        return [c for c in self.calls if c["url"].startswith(hook) and c["method"] == "POST"]


def client(rec):
    return DiscordClient(min_interval=0, sleep=lambda s: None, post=rec, request=rec)


class Base(unittest.TestCase):
    TOML = """
        [discord]
        brand = "Within Quantum"
        dedupe_hours = 72
        preview_image = "thumbnail"
        [discord.colors]
        hardware = "#2E8B8B"
        [discord.roles]
        hardware = "111222333444555666"
        [categories]
        hardware = "Quantum hardware companies"
        publications = "Publications"
        [[source]]
        id = "acme"
        name = "Acme Quantum"
        category = "hardware"
        feed = "https://acme.test/feed"
        site = "https://www.acme.test/news"
        [[source]]
        id = "beta"
        name = "Beta Qubits"
        category = "hardware"
        feed = "https://beta.test/feed"
        site = "https://beta.test/"
        avatar = "https://cdn.test/beta.png"
        [[source]]
        id = "wire"
        name = "Quantum Wire"
        category = "publications"
        feed = "https://wire.test/feed"
        site = "https://wire.test/"
    """

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.dir = Path(self.tmp.name)
        (self.dir / "sources.toml").write_text(textwrap.dedent(self.TOML))
        self.config = load_config(self.dir / "sources.toml")
        self.hooks = parse_webhooks(json.dumps({
            "category:hardware": HOOK_A, "category:publications": HOOK_B, "firehose": FIRE}), self.config)
        self.feeds = {"https://acme.test/feed": rss([("Old acme", "https://acme.test/p/0", 5)], image=True),
                      "https://beta.test/feed": rss([("Old beta", "https://beta.test/p/0", 5)]),
                      "https://wire.test/feed": rss([("Old wire", "https://wire.test/p/0", 5)])}

    def tearDown(self):
        self.tmp.cleanup()

    def fetch(self, url, **kw):
        if url in self.feeds:
            return Response(url, 200, self.feeds[url], {})
        return Response(url, 200, b"<html><head></head></html>", {})  # article pages: no og:image

    def cycle(self, rec):
        """Seed (silent first run), then a second run that sees whatever is in self.feeds."""
        state, journal = State.load(self.dir / "s.json"), Journal.load(self.dir / "j.json")
        run(self.config, self.hooks, state, client=client(Recorder()), fetch=self.fetch, journal=journal)
        return state, journal

    def go(self, state, journal, rec):
        return run(self.config, self.hooks, state, client=client(rec), fetch=self.fetch, journal=journal)


class CardTests(Base):
    def test_card_has_identity_colour_brand_time_and_image(self):
        state, journal = self.cycle(None)
        self.feeds["https://acme.test/feed"] = rss([("Acme reaches 1000 qubits", "https://acme.test/p/1", 1),
                                                    ("Old acme", "https://acme.test/p/0", 5)], image=True)
        rec = Recorder()
        self.go(state, journal, rec)
        post = rec.posts_to(HOOK_A)[0]["payload"]
        card = post["embeds"][0]
        self.assertEqual(post["username"], "Acme Quantum")
        self.assertEqual(post["avatar_url"], "https://www.google.com/s2/favicons?domain=acme.test&sz=128")
        self.assertEqual(card["title"], "Acme reaches 1000 qubits")
        self.assertEqual(card["url"], "https://acme.test/p/1")
        self.assertEqual(card["color"], 0x2E8B8B)
        self.assertEqual(card["footer"]["text"], "Within Quantum | Quantum hardware companies")
        self.assertEqual(card["author"]["icon_url"], post["avatar_url"])
        self.assertIn("timestamp", card)
        self.assertTrue(card["thumbnail"]["url"].startswith("https://img.test/"))
        self.assertNotIn("description", card)  # titles and links only, never summaries
        self.assertNotIn("http", post.get("content", ""))  # only the role mention; no bare URL, so no second preview

    def test_avatar_override_and_off(self):
        state, journal = self.cycle(None)
        self.feeds["https://beta.test/feed"] = rss([("Beta news", "https://beta.test/p/1", 1)])
        rec = Recorder()
        self.go(state, journal, rec)
        self.assertEqual(rec.posts_to(HOOK_A)[0]["payload"]["avatar_url"], "https://cdn.test/beta.png")
        self.assertEqual(favicon_url("https://www.example.org/x"), "https://www.google.com/s2/favicons?domain=example.org&sz=128")


class DuplicateAndFirehoseTests(Base):
    def test_same_story_twice_in_one_channel_is_skipped(self):
        state, journal = self.cycle(None)
        self.feeds["https://acme.test/feed"] = rss([("Acme and Beta sign quantum chip partnership deal", "https://acme.test/p/9", 1)])
        self.feeds["https://beta.test/feed"] = rss([("Acme and Beta sign a quantum chip partnership deal", "https://beta.test/p/9", 1)])
        rec = Recorder()
        report = self.go(state, journal, rec)
        self.assertEqual(len(rec.posts_to(HOOK_A)), 1)
        beta = next(o for o in report.outcomes if o.source.id == "beta")
        self.assertIn("duplicate", beta.detail)
        self.assertTrue(state.source("beta").has_seen(parse_feed(self.feeds["https://beta.test/feed"])[0].keys()))

    def test_other_section_still_gets_it_but_firehose_only_once(self):
        state, journal = self.cycle(None)
        self.feeds["https://acme.test/feed"] = rss([("IonQ completes acquisition of chip maker", "https://acme.test/p/5", 2)])
        self.feeds["https://wire.test/feed"] = rss([("IonQ completes acquisition of chip maker", "https://wire.test/p/5", 1)])
        rec = Recorder()
        self.go(state, journal, rec)
        self.assertEqual(len(rec.posts_to(HOOK_A)), 1)
        self.assertEqual(len(rec.posts_to(HOOK_B)), 1)
        self.assertEqual(len(rec.posts_to(FIRE)), 1)

    def test_firehose_failure_never_blocks_section_posts(self):
        state, journal = self.cycle(None)
        self.feeds["https://acme.test/feed"] = rss([("First", "https://acme.test/p/1", 2), ("Second story here", "https://acme.test/p/2", 1)])
        rec = Recorder(fail_on=FIRE)
        report = self.go(state, journal, rec)
        self.assertEqual(len(rec.posts_to(HOOK_A)), 2)
        self.assertTrue(any("firehose" in w for w in report.warnings))
        self.assertEqual(next(o for o in report.outcomes if o.source.id == "acme").status, "posted")


class RolePingTests(Base):
    def test_role_pinged_once_per_section_per_run_and_nothing_else(self):
        state, journal = self.cycle(None)
        self.feeds["https://acme.test/feed"] = rss([("A1 qubit result", "https://acme.test/p/1", 2), ("A2 fabrication update", "https://acme.test/p/2", 1)])
        self.feeds["https://beta.test/feed"] = rss([("B1 cryostat launch", "https://beta.test/p/1", 1)])
        self.feeds["https://wire.test/feed"] = rss([("W1 industry analysis", "https://wire.test/p/1", 1)])
        rec = Recorder()
        self.go(state, journal, rec)
        hardware = [c["payload"] for c in rec.posts_to(HOOK_A)]
        pinged = [p for p in hardware if "<@&111222333444555666>" in p.get("content", "")]
        self.assertEqual(len(pinged), 1)
        self.assertEqual(pinged[0]["allowed_mentions"], {"parse": [], "roles": ["111222333444555666"]})
        self.assertTrue(all(p["allowed_mentions"] == {"parse": []} for p in hardware if p not in pinged))
        self.assertTrue(all("content" not in c["payload"] for c in rec.posts_to(HOOK_B)))  # no role for this section


class JournalTests(unittest.TestCase):
    def test_similarity(self):
        a = title_tokens("Google unveils new error-corrected logical qubit milestone")
        b = title_tokens("Google unveils error-corrected logical qubit milestone")
        c = title_tokens("Microsoft announces Majorana 2 processor")
        self.assertTrue(similar_titles(a, b))
        self.assertFalse(similar_titles(a, c))

    def test_prune_and_persist(self):
        with tempfile.TemporaryDirectory() as tmp:
            j = Journal.load(Path(tmp) / "j.json")
            j.add(title="old", url="https://x.test/1", source_id="x", source_name="X", category="c", when=NOW - timedelta(days=10))
            j.add(title="new", url="https://x.test/2", source_id="x", source_name="X", category="c", when=NOW)
            j.save(NOW)
            again = Journal.load(Path(tmp) / "j.json")
            self.assertEqual([e["t"] for e in again.entries], ["new"])

    def test_corrupt_file_is_set_aside(self):
        with tempfile.TemporaryDirectory() as tmp:
            (Path(tmp) / "j.json").write_text("{broken")
            self.assertTrue(Journal.load(Path(tmp) / "j.json").warnings)


class BriefingTests(Base):
    def journal(self):
        j = Journal.load(self.dir / "j.json")
        j.add(title="Acme reaches 1000 qubits", url="https://acme.test/p/1", source_id="acme", source_name="Acme Quantum", category="hardware", when=NOW - timedelta(hours=2))
        j.add(title="Acme reaches 1,000 qubits", url="https://wire.test/p/1", source_id="wire", source_name="Quantum Wire", category="publications", when=NOW - timedelta(hours=1))
        j.add(title="Industry roundup for the week", url="https://wire.test/p/2", source_id="wire", source_name="Quantum Wire", category="publications", when=NOW - timedelta(days=3))
        return j

    def test_daily_groups_by_section_and_merges_duplicates(self):
        payloads = build_briefing(self.config, self.journal(), "daily", NOW)
        embeds = [e for p in payloads for e in p["embeds"]]
        self.assertIn("daily briefing", embeds[0]["title"])
        self.assertIn("1 new posts from 1 sources", embeds[0]["description"])
        self.assertEqual([e["title"] for e in embeds[1:]], ["Quantum hardware companies (1)"])

    def test_weekly_covers_seven_days(self):
        embeds = [e for p in build_briefing(self.config, self.journal(), "weekly", NOW) for e in p["embeds"]]
        self.assertIn("weekly roundup", embeds[0]["title"])
        self.assertEqual(len(embeds), 3)

    def test_sent_once_per_period(self):
        j, rec = self.journal(), Recorder()
        first = send_briefing(self.config, j, client(rec), HOOK_B, "daily", NOW)
        second = send_briefing(self.config, j, client(rec), HOOK_B, "daily", NOW)
        self.assertIn("sent", first)
        self.assertIn("already sent", second)
        self.assertEqual(len(rec.calls), 1)
        self.assertEqual(rec.calls[0]["payload"]["allowed_mentions"], {"parse": []})

    def test_nothing_posted_means_no_message(self):
        j, rec = Journal.load(self.dir / "empty.json"), Recorder()
        self.assertIn("nothing was posted", send_briefing(self.config, j, client(rec), HOOK_B, "daily", NOW))
        self.assertEqual(rec.calls, [])


class DirectoryTests(Base):
    def test_post_then_unchanged_then_edit_then_repost(self):
        j, rec = Journal.load(self.dir / "j.json"), Recorder()
        self.assertIn("posted", sync_directory(self.config, j, client(rec), HOOK_B))
        ids = j.meta["directory"]["ids"]
        self.assertTrue(ids)
        self.assertEqual(sync_directory(self.config, j, client(rec), HOOK_B), "directory unchanged")
        posts_before = len(rec.calls)

        self.config.sources[2].name = "Quantum Wire Daily"           # a change, same layout
        self.assertIn("updated in place", sync_directory(self.config, j, client(rec), HOOK_B))
        self.assertTrue(all(c["method"] == "PATCH" for c in rec.calls[posts_before:]))
        self.assertTrue(rec.calls[-1]["url"].endswith(f"/messages/{ids[-1]}"))

        class Gone(Recorder):  # someone deleted the message by hand
            def __call__(self, url, payload, timeout=20, method="POST"):
                if method == "PATCH":
                    self.calls.append({"url": url, "payload": payload, "method": method})
                    return 404, b'{"code": 10008, "message": "Unknown Message"}', {}
                return super().__call__(url, payload, timeout, method)

        gone = Gone()
        self.config.sources[0].name = "Acme Quantum Computing"
        self.assertIn("posted", sync_directory(self.config, j, client(gone), HOOK_B))
        self.assertTrue(gone.posts_to(HOOK_B))

    def test_directory_lists_every_enabled_source_by_section(self):
        embeds = [e for p in build_directory(self.config) for e in p["embeds"]]
        text = "\n".join(e.get("description", "") for e in embeds)
        for name in ("Acme Quantum", "Beta Qubits", "Quantum Wire"):
            self.assertIn(name, text)
        self.assertIn("3 sources", embeds[0]["description"])


class LimitsTests(unittest.TestCase):
    def test_long_lists_split_within_discord_limits(self):
        lines = [f"- [Headline number {i} with some length to it](https://x.test/{i})" for i in range(400)]
        embeds = list_embeds("Big section (400)", lines, color=1, footer="f")
        self.assertTrue(all(len(e["description"]) <= 4096 for e in embeds))
        messages = group_embeds(embeds)
        self.assertTrue(all(len(m) <= 10 for m in messages))
        self.assertTrue(all(sum(len(e.get("description", "")) for e in m) <= 6000 for m in messages))


class PreviewImageTests(unittest.TestCase):
    def test_og_image(self):
        page = b'<html><head><meta content="/img/card.png" property="og:image"></head></html>'
        fetch = lambda url, **kw: Response("https://site.test/a/b", 200, page, {})
        self.assertEqual(find_image("https://site.test/a/b", fetch), "https://site.test/img/card.png")

    def test_failures_mean_no_image(self):
        def broken(url, **kw):
            raise RuntimeError("boom")
        self.assertEqual(find_image("https://site.test/x", broken), "")

    def test_feed_media_thumbnail(self):
        item = parse_feed(rss([("T", "https://a.test/1", 1)], image=True))[0]
        self.assertTrue(item.image.startswith("https://img.test/"))


class ConfigTests(unittest.TestCase):
    def test_bad_discord_settings_are_reported(self):
        with tempfile.TemporaryDirectory() as tmp:
            p = Path(tmp) / "s.toml"
            p.write_text(textwrap.dedent("""
                [discord]
                style = "fancy"
                colour = "x"
                [discord.colors]
                hardware = "teal"
                [discord.roles]
                hardware = "not-a-number"
                [[source]]
                id = "aa"
                name = "A"
                category = "hardware"
                feed = "https://a.test/f"
            """))
            with self.assertRaises(ConfigError) as ctx:
                load_config(p)
            text = " ".join(ctx.exception.errors)
            for expected in ("style", "unknown key 'colour'", "colour must look like", "role id must be digits"):
                self.assertIn(expected, text)

    def test_special_webhooks_accepted(self):
        with tempfile.TemporaryDirectory() as tmp:
            p = Path(tmp) / "s.toml"
            p.write_text('[[source]]\nid = "aa"\nname = "A"\ncategory = "cc"\nfeed = "https://a.test/f"\n')
            cfg = load_config(p)
            hooks = parse_webhooks(json.dumps({"firehose": FIRE, "briefing": HOOK_B, "directory": HOOK_A}), cfg)
            self.assertEqual(hooks.problems, [])
            self.assertEqual(hooks.special("weekly"), HOOK_B)  # falls back to the briefing channel


if __name__ == "__main__":
    unittest.main()
