"""Tests for the self-healing behaviours added after the first live health check."""

from __future__ import annotations

import io
import unittest
import urllib.error
from unittest import mock

from feedbot import http
from feedbot.config import Source
from feedbot.http import FetchError, Response
from feedbot.pages import extract_links, link_samples, tidy_title
from feedbot.runner import collect
from feedbot.tools import check_sources
from feedbot.config import Config, DEFAULTS

RSS = b"<rss><channel><item><title>Hello</title><link>https://ex.com/hello</link></item></channel></rss>"
EMPTY_RSS = b"<rss><channel><title>nothing here</title></channel></rss>"


def source(**kw) -> Source:
    base = dict(id="src", name="Src", category="test", feeds=[], site="https://ex.com/news")
    base.update(kw)
    return Source(**base)


class Fake:
    def __init__(self, routes):
        self.routes = routes

    def __call__(self, url, **kw):
        value = self.routes.get(url)
        if isinstance(value, Exception):
            raise value
        if value is None:
            raise FetchError("HTTP 404", status=404)
        return Response(url, 200, value if isinstance(value, bytes) else value.encode(), {})


class UserAgentRetryTests(unittest.TestCase):
    def test_403_is_retried_once_with_feed_reader_agent(self):
        agents = []

        class FakeResp(io.BytesIO):
            status = 200
            headers = {}

            def geturl(self):
                return "https://ex.com/feed"

            def __enter__(self):
                return self

            def __exit__(self, *a):
                return False

        def fake_urlopen(request, timeout=None):
            agents.append(request.get_header("User-agent"))
            if len(agents) == 1:
                raise urllib.error.HTTPError(request.full_url, 403, "Forbidden", {}, None)
            return FakeResp(RSS)

        with mock.patch.object(http.urllib.request, "urlopen", fake_urlopen):
            resp = http.fetch("https://ex.com/feed")
        self.assertEqual(resp.body, RSS)
        self.assertEqual(len(agents), 2)
        self.assertNotEqual(agents[0], agents[1])
        self.assertIn("RSS reader", agents[1])

    def test_403_twice_fails_without_sleeping(self):
        def always_403(request, timeout=None):
            raise urllib.error.HTTPError(request.full_url, 403, "Forbidden", {}, None)

        with mock.patch.object(http.urllib.request, "urlopen", always_403), \
             mock.patch.object(http.time, "sleep") as sleep:
            with self.assertRaises(FetchError) as ctx:
                http.fetch("https://ex.com/feed")
        self.assertEqual(ctx.exception.status, 403)
        sleep.assert_not_called()


class CollectFallbackTests(unittest.TestCase):
    LISTING = '<main><a href="/news/a-real-article-title">A real article title</a></main>'

    def test_empty_feed_does_not_hide_listing_page(self):
        fetch = Fake({"https://ex.com/feed": EMPTY_RSS, "https://ex.com/news": self.LISTING})
        result = collect(source(feeds=["https://ex.com/feed"]), fetch=fetch)
        self.assertEqual(result.method, "html")
        self.assertEqual(len(result.items), 1)

    def test_empty_feed_is_last_resort(self):
        fetch = Fake({"https://ex.com/feed": EMPTY_RSS})
        result = collect(source(feeds=["https://ex.com/feed"]), fetch=fetch)
        self.assertEqual(result.items, [])
        self.assertEqual(result.method, "feed")

    def test_404_listing_falls_back_to_homepage_feed(self):
        home = '<head><link rel="alternate" type="application/rss+xml" href="/feed/"></head>'
        fetch = Fake({"https://ex.com/": home, "https://ex.com/feed/": RSS})
        result = collect(source(), fetch=fetch)
        self.assertEqual(result.url, "https://ex.com/feed/")
        self.assertIn("is gone", result.note)

    def test_404_listing_falls_back_to_homepage_links(self):
        home = '<main><a href="/updates/new-quantum-processor-unveiled">New quantum processor unveiled</a></main>'
        fetch = Fake({"https://ex.com/": home})
        result = collect(source(), fetch=fetch)
        self.assertEqual(result.method, "html")
        self.assertIn("homepage", result.note)

    def test_non_404_errors_do_not_fall_back(self):
        fetch = Fake({"https://ex.com/news": FetchError("HTTP 403", status=403), "https://ex.com/": RSS})
        with self.assertRaises(Exception):
            collect(source(), fetch=fetch)


class ExtractionQualityTests(unittest.TestCase):
    def test_prefers_main_content_over_menus(self):
        html = """<header><a href="/cloud-access-platform-overview">Cloud Access</a></header>
        <main><a href="/blog/real-post-one-here">Real post one here</a></main>"""
        links = [i.link for i in extract_links(html, "https://ex.com/blog")]
        self.assertEqual(links, ["https://ex.com/blog/real-post-one-here"])

    def test_heading_inside_card_link_wins(self):
        html = """<main><a href="/blog/x-y-z"><span>Blog</span><span>Dec 2, 2025</span>
        <h3>The actual title</h3><p>A long teaser that should never be posted as the title.</p></a></main>"""
        self.assertEqual(extract_links(html, "https://ex.com/blog")[0].title, "The actual title")

    def test_nested_divs_inside_role_main(self):
        html = """<div role="main"><div><div></div><a href="/news/first-real-story">First real story</a></div>
        <a href="/news/second-real-story">Second real story</a></div><a href="/news/outside-the-main-area">x</a>"""
        links = [i.link.rsplit("/", 1)[-1] for i in extract_links(html, "https://ex.com/news")]
        self.assertEqual(links, ["first-real-story", "second-real-story"])

    def test_tidy_title_never_mangles_real_titles(self):
        for title in ["Research shows press freedom matters", "News from the lab", "The year 2026 in review",
                      "Events that shaped quantum computing"]:
            self.assertEqual(tidy_title(title), title)

    def test_tidy_title_strips_card_furniture(self):
        self.assertEqual(tidy_title("News | September 19, 2026 Phase II consultation"), "Phase II consultation")
        self.assertEqual(tidy_title("Why decoders matter · 5 min read"), "Why decoders matter")

    def test_link_samples(self):
        html = '<a href="/a/b">x</a><a href="https://other.com/z">y</a><a href="/a/b">dup</a>'
        self.assertEqual(link_samples(html, "https://ex.com/"), ["/a/b"])


class CheckReportTests(unittest.TestCase):
    def test_keyword_filtered_source_is_informational_not_a_problem(self):
        cfg = Config(defaults=dict(DEFAULTS), categories={},
                     sources=[source(id="mag", feeds=["https://ex.com/feed"], include_keywords=["quantum"])])
        markdown, attention = check_sources(cfg, fetch=Fake({"https://ex.com/feed": RSS}))
        self.assertEqual(attention, 0)
        self.assertIn("ℹ️", markdown)

    def test_failed_scrape_shows_links_on_page(self):
        page = '<p><a href="/press/2026/item">x</a></p>'
        cfg = Config(defaults=dict(DEFAULTS), categories={},
                     sources=[source(id="co", site="https://ex.com/news", link_pattern="nomatch")])
        markdown, attention = check_sources(cfg, fetch=Fake({"https://ex.com/news": page, "https://ex.com/": page}))
        self.assertEqual(attention, 1)
        self.assertIn("/press/2026/item", markdown)


if __name__ == "__main__":
    unittest.main()
