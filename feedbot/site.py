"""Build the static newspaper website from the archive.

Output: `<out>/index.html` (one self-contained page with the data embedded),
`<out>/data.json` (the same data, for anyone who wants to reuse it) and
`<out>/.nojekyll`. Published by .github/workflows/site.yml to GitHub Pages.

Reading state (what a visitor has opened, when they last visited) lives only in
that visitor's browser (localStorage). Nothing about readers is ever sent anywhere.
"""

from __future__ import annotations

import html
import json
from pathlib import Path

from .archive import Archive
from .config import Config
from .util import utcnow


# Short labels for the section bar (full names are used on headings). Unknown
# categories fall back to their full name, so new categories need no code change.
NAV_LABELS = {
    "quantum-researchers": "Quantum", "math": "Mathematics", "tcs": "Theory", "ai": "AI",
    "big-tech": "Big tech", "hardware": "Hardware", "qec": "Error correction", "software": "Software",
    "control": "Control", "networking": "Networks & crypto", "publications": "Publications",
    "government": "Labs & government", "institutes": "Institutes", "analysts": "Analysts",
}


def site_data(config: Config, archive: Archive) -> dict:
    per_source = config.site["items_per_source"]
    enabled = [s for s in config.sources if s.enabled]
    used_categories = {s.category for s in enabled}
    categories = [{"id": cid, "name": name, "short": NAV_LABELS.get(cid, name)}
                  for cid, name in config.categories.items() if cid in used_categories]
    for cid in sorted(used_categories - set(config.categories)):
        name = cid.replace("-", " ").capitalize()
        categories.append({"id": cid, "name": name, "short": NAV_LABELS.get(cid, name)})

    sources = []
    items = []
    for s in enabled:
        sources.append({"id": s.id, "n": s.name, "c": s.category, "a": s.author, "h": s.site or (s.feeds[0] if s.feeds else "")})
        for entry in archive.sources.get(s.id, [])[:per_source]:
            item = {
                "id": entry["id"],
                "s": s.id,
                "t": entry.get("title") or entry["url"],
                "u": entry["url"],
                "d": entry.get("published") or entry.get("seen"),
                "f": entry.get("seen") or entry.get("published"),
            }
            if not entry.get("published"):
                item["x"] = 1  # date is when the bot first saw it, not a publication date
            if entry.get("bootstrap"):
                item["b"] = 1  # found on the first read of an undated page
            items.append(item)
    return {
        "generated": utcnow().isoformat(timespec="seconds"),
        "site": {"title": config.site["title"], "tagline": config.site["tagline"], "repo": config.site["repo_url"]},
        "categories": categories,
        "sources": sources,
        "items": items,
    }


def build_site(config: Config, archive: Archive, out: Path) -> Path:
    out.mkdir(parents=True, exist_ok=True)
    data = site_data(config, archive)
    payload = json.dumps(data, ensure_ascii=False, separators=(",", ":"))
    embedded = payload.replace("</", "<\\/")  # cannot terminate the <script> element
    page = (
        TEMPLATE.replace("__TITLE__", html.escape(config.site["title"]))
        .replace("__TAGLINE__", html.escape(config.site["tagline"]))
        .replace("__DATA__", embedded)
    )
    (out / "index.html").write_text(page, encoding="utf-8")
    (out / "data.json").write_text(payload, encoding="utf-8")
    (out / ".nojekyll").write_text("", encoding="utf-8")
    return out / "index.html"


TEMPLATE = r"""<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1, viewport-fit=cover">
<title>__TITLE__</title>
<meta name="description" content="__TAGLINE__">
<meta name="color-scheme" content="light dark">
<link rel="icon" href="data:image/svg+xml,%3Csvg xmlns='http://www.w3.org/2000/svg' viewBox='0 0 64 64'%3E%3Crect width='64' height='64' rx='12' fill='%232E5E66'/%3E%3Ctext x='32' y='44' font-family='Georgia,serif' font-size='34' text-anchor='middle' fill='%23ECEEE7'%3E%7Ck%E2%9F%A9%3C/text%3E%3C/svg%3E">
<link rel="preconnect" href="https://fonts.googleapis.com">
<link rel="preconnect" href="https://fonts.gstatic.com" crossorigin>
<link href="https://fonts.googleapis.com/css2?family=Atkinson+Hyperlegible:wght@400;700&family=Newsreader:ital,opsz,wght@0,6..72,400..700;1,6..72,400&display=swap" rel="stylesheet">
<style>
:root {
  --paper: #ECEEE7; --sheet: #F4F5F0; --ink: #283035; --ink-soft: #5C666B; --ink-faint: #8C969A;
  --rule: #CED4CA; --accent: #2E5E66; --accent-wash: #DDE6E2; --fresh: #B38A35;
  --serif: "Newsreader", "Iowan Old Style", Georgia, serif;
  --sans: "Atkinson Hyperlegible", "Segoe UI", system-ui, -apple-system, sans-serif;
  --measure: 22rem;
}
@media (prefers-color-scheme: dark) {
  :root:not([data-theme="light"]) {
    --paper: #1C2225; --sheet: #232A2D; --ink: #DCE0D8; --ink-soft: #9CA6AA; --ink-faint: #6E787C;
    --rule: #364247; --accent: #86B6BA; --accent-wash: #2A3A3C; --fresh: #D2B062;
  }
}
:root[data-theme="dark"] {
  --paper: #1C2225; --sheet: #232A2D; --ink: #DCE0D8; --ink-soft: #9CA6AA; --ink-faint: #6E787C;
  --rule: #364247; --accent: #86B6BA; --accent-wash: #2A3A3C; --fresh: #D2B062;
}
* { box-sizing: border-box; }
html { -webkit-text-size-adjust: 100%; }
body {
  margin: 0; background: var(--paper); color: var(--ink);
  font: 400 1rem/1.5 var(--sans);
  padding: env(safe-area-inset-top) env(safe-area-inset-right) env(safe-area-inset-bottom) env(safe-area-inset-left);
}
a { color: inherit; }
button { font: inherit; color: inherit; }
:focus-visible { outline: 2px solid var(--accent); outline-offset: 2px; border-radius: 3px; }
.sr { position: absolute; width: 1px; height: 1px; overflow: hidden; clip: rect(0 0 0 0); white-space: nowrap; }

.wrap { max-width: 92rem; margin: 0 auto; padding: 0 clamp(1rem, 3vw, 2.5rem); }

/* Masthead: the one bold element */
.masthead { text-align: center; padding: 1.1rem 0 0.3rem; }
.masthead h1 {
  margin: 0; font-family: var(--serif); font-weight: 600;
  font-size: clamp(2.4rem, 5vw, 3.6rem); line-height: 1; letter-spacing: -0.01em;
  font-variation-settings: "opsz" 72;
}
.masthead h1 .bra { font-weight: 300; color: var(--ink-faint); margin: 0 0.06em; }
.masthead p { margin: 0.45rem auto 0; max-width: 40rem; font-family: var(--serif); font-style: italic; color: var(--ink-soft); font-size: 1.02rem; }

.dateline {
  display: grid; grid-template-columns: 1fr auto 1fr; align-items: center; gap: 1rem;
  border-top: 1px solid var(--rule); border-bottom: 3px double var(--rule);
  margin-top: 0.9rem; padding: 0.45rem 0; font-size: 0.86rem; color: var(--ink-soft);
}
.dateline .newcount { color: var(--ink); }
.dateline .newcount b { color: var(--fresh); font-weight: 700; }
.dateline .right { text-align: right; }

/* Section bar */
.sections { display: flex; flex-wrap: wrap; gap: 0.15rem 0.2rem; padding: 0.5rem 0 0.25rem; }
.sections a {
  text-decoration: none; padding: 0.28rem 0.6rem; border-radius: 999px; font-size: 0.92rem; color: var(--ink-soft);
  white-space: nowrap;
}
.sections a:hover { color: var(--ink); background: var(--accent-wash); }
.sections a[aria-current="page"] { color: var(--paper); background: var(--accent); }
.sections .n { margin-left: 0.35rem; font-size: 0.78rem; font-weight: 700; color: var(--fresh); }
.sections a[aria-current="page"] .n { color: var(--paper); opacity: 0.85; }

.tools { display: flex; flex-wrap: wrap; align-items: center; gap: 0.5rem 0.75rem; padding: 0.3rem 0 0.7rem; border-bottom: 1px solid var(--rule); }
.search { flex: 1 1 16rem; max-width: 26rem; position: relative; }
.search input {
  width: 100%; padding: 0.45rem 0.75rem; border: 1px solid var(--rule); border-radius: 8px;
  background: var(--sheet); color: var(--ink); font: inherit; font-size: 0.95rem;
}
.search input::placeholder { color: var(--ink-faint); }
.spacer { flex: 1; }
.btn {
  border: 1px solid var(--rule); background: var(--sheet); padding: 0.38rem 0.75rem; border-radius: 8px;
  font-size: 0.9rem; cursor: pointer; color: var(--ink);
}
.btn:hover { border-color: var(--accent); }
.btn[aria-pressed="true"] { background: var(--accent-wash); border-color: var(--accent); }

/* Headlines */
.list { list-style: none; margin: 0; padding: 0; }
.h { display: grid; grid-template-columns: 0.9rem minmax(0, 1fr) auto; column-gap: 0.25rem; padding: 0.38rem 0; border-top: 1px solid var(--rule); }
.h > span:nth-child(2) { min-width: 0; overflow-wrap: anywhere; }
.h:first-child { border-top: 0; }
.h .dot { width: 0.45rem; height: 0.45rem; border-radius: 50%; margin-top: 0.55rem; background: transparent; }
.h.is-new .dot { background: var(--fresh); }
.h a.t {
  font-family: var(--serif); font-size: 1.05rem; line-height: 1.4; font-weight: 500; text-decoration: none;
  font-variation-settings: "opsz" 16; color: var(--ink);
}
.h a.t:hover { text-decoration: underline; text-decoration-color: var(--accent); text-underline-offset: 0.18em; }
.h.is-read a.t { color: var(--ink-faint); font-weight: 400; }
.h time, .h .when { font-size: 0.8rem; color: var(--ink-soft); white-space: nowrap; padding-left: 0.6rem; padding-top: 0.15rem; font-variant-numeric: tabular-nums; }
.h .src { font-family: var(--sans); font-size: 0.8rem; color: var(--ink-soft); margin-left: 0.45rem; }

/* Front page */
.front { display: grid; gap: 0 1.75rem; padding-top: 0.9rem; align-items: start; grid-template-columns: minmax(18rem, 1.3fr) repeat(4, minmax(13rem, 1fr)); }
.front .latest { grid-row: span 8; border-right: 1px solid var(--rule); padding-right: 1.75rem; }
.front .latest h2, .dept h2, .col h2 { font-family: var(--serif); font-weight: 600; margin: 0 0 0.25rem; font-size: 1.22rem; line-height: 1.25; }
.dept { padding: 0.2rem 0 0.8rem; margin-bottom: 0.6rem; border-bottom: 1px solid var(--rule); }
.dept h2 a { text-decoration: none; }
.dept .list .h:last-child { padding-bottom: 0.1rem; }
.dept h2 a:hover { color: var(--accent); }
.dept h2 .n { font-family: var(--sans); font-size: 0.78rem; font-weight: 700; color: var(--fresh); margin-left: 0.4rem; white-space: nowrap; }
.more { display: inline-block; margin-top: 0.35rem; font-size: 0.88rem; color: var(--accent); text-decoration: none; background: none; border: 0; padding: 0; cursor: pointer; }
.more:hover { text-decoration: underline; }
@media (min-width: 120rem) { .front { grid-template-columns: minmax(20rem, 1.3fr) repeat(5, minmax(13rem, 1fr)); } }
@media (max-width: 82rem) { .front { grid-template-columns: minmax(17rem, 1.25fr) repeat(3, minmax(13rem, 1fr)); } }
@media (max-width: 64rem) { .front { grid-template-columns: minmax(16rem, 1.2fr) repeat(2, minmax(13rem, 1fr)); } }
@media (max-width: 52rem) {
  .front { grid-template-columns: 1fr 1fr; }
  .front .latest { grid-column: 1 / -1; grid-row: auto; border-right: 0; padding-right: 0; border-bottom: 3px double var(--rule); margin-bottom: 0.8rem; padding-bottom: 0.6rem; }
  .front .latest .h:nth-child(n+9) { display: none; }     /* phones and small tablets: less scrolling */
  .dept .h:nth-child(n+3) { display: none; }
}
@media (max-width: 34rem) { .front { grid-template-columns: 1fr; } }

/* Section page: newspaper columns */
.cols { column-width: var(--measure); column-gap: 2rem; column-rule: 1px solid var(--rule); padding-top: 1rem; }
.col { break-inside: avoid; margin-bottom: 1.3rem; }
.col h2 { font-size: 1.18rem; }
.col h2 a { text-decoration: none; }
.col h2 a:hover { color: var(--accent); }
.col .by { font-size: 0.82rem; color: var(--ink-soft); margin: -0.25rem 0 0.25rem; }
.col .quietnote { font-size: 0.88rem; color: var(--ink-soft); margin: 0.2rem 0; }
.col details { margin-top: 0.3rem; font-size: 0.88rem; color: var(--ink-soft); }
.col details summary { cursor: pointer; color: var(--accent); }
.quiet { margin-top: 0.5rem; padding: 0.8rem 0 1.2rem; border-top: 3px double var(--rule); color: var(--ink-soft); font-size: 0.92rem; }
.quiet a { color: var(--ink-soft); }

.empty { padding: 3rem 0; text-align: center; color: var(--ink-soft); font-family: var(--serif); font-size: 1.15rem; }
footer { margin: 2rem 0 2.5rem; padding-top: 0.8rem; border-top: 1px solid var(--rule); font-size: 0.85rem; color: var(--ink-soft); display: flex; flex-wrap: wrap; gap: 0.5rem 1.5rem; }
footer a { color: var(--ink-soft); }

@media (max-width: 40rem) {
  .dateline { grid-template-columns: 1fr; text-align: center; gap: 0.15rem; }
  .dateline .right { text-align: center; }
  .sections { flex-wrap: nowrap; overflow-x: auto; scrollbar-width: none; margin: 0 -1rem; padding-left: 1rem; padding-right: 1rem; }
  .sections::-webkit-scrollbar { display: none; }
  .masthead { padding-top: 1rem; }
}
@media (prefers-reduced-motion: no-preference) {
  .h a.t, .sections a, .btn { transition: color .15s, background-color .15s, border-color .15s; }
}
</style>
</head>
<body>
<div class="wrap">
  <header class="masthead">
    <h1><span class="bra" aria-hidden="true">|</span>__TITLE__<span class="bra" aria-hidden="true">⟩</span></h1>
    <p>__TAGLINE__</p>
  </header>
  <div class="dateline" role="status" aria-live="polite">
    <span id="today"></span>
    <span class="newcount" id="newcount"></span>
    <span class="right" id="updated"></span>
  </div>
  <nav class="sections" id="sections" aria-label="Sections"></nav>
  <div class="tools">
    <div class="search">
      <label class="sr" for="q">Search headlines</label>
      <input id="q" type="search" placeholder="Search headlines  ( / )" autocomplete="off">
    </div>
    <span class="spacer"></span>
    <button class="btn" id="unread" type="button" aria-pressed="false">Unread only</button>
    <button class="btn" id="markread" type="button">Mark these read</button>
    <button class="btn" id="theme" type="button" aria-label="Switch colour theme">Dark</button>
  </div>
  <main id="main"></main>
  <noscript><p class="empty">This page needs JavaScript to lay out the headlines. The raw list is in data.json.</p></noscript>
  <footer id="footer"></footer>
</div>
<script id="data" type="application/json">__DATA__</script>
<script>
(() => {
  "use strict";
  const DATA = JSON.parse(document.getElementById("data").textContent);
  const SOURCES = Object.fromEntries(DATA.sources.map(s => [s.id, s]));
  const CATS = DATA.categories;
  const CAT_NAME = Object.fromEntries(CATS.map(c => [c.id, c.name]));
  const ITEMS = DATA.items.filter(i => SOURCES[i.s]).map(i => ({ ...i, date: new Date(i.d), first: new Date(i.f) }));
  const QUIET_DAYS = 60;

  // ---------- browser-local reading state ----------
  const store = {
    get(key, fallback) { try { const v = localStorage.getItem(key); return v === null ? fallback : JSON.parse(v); } catch { return fallback; } },
    set(key, value) { try { localStorage.setItem(key, JSON.stringify(value)); } catch { /* private mode: keep in memory */ } },
  };
  let read = new Set(store.get("ket:read", []));
  const prefs = store.get("ket:prefs", { unreadOnly: false, theme: "" });
  const now = Date.now();
  const visit = store.get("ket:visit", null);
  let since;
  if (!visit || !visit.last) since = now - 24 * 3600e3;                 // first visit: last 24 hours are "new"
  else if (now - visit.last > 30 * 60e3) since = visit.last;            // new session: since you last looked
  else since = visit.since || now - 24 * 3600e3;                        // same session (reload): keep the same marker
  store.set("ket:visit", { last: now, since });

  const isNew = i => !i.b && i.first.getTime() > since && !read.has(i.id);
  function saveRead() {
    let ids = [...read];
    if (ids.length > 5000) ids = ids.slice(ids.length - 5000);
    store.set("ket:read", ids);
  }
  window.addEventListener("storage", e => {            // keep several open tabs in sync
    if (e.key === "ket:read") { read = new Set(store.get("ket:read", [])); render(); }
  });

  // ---------- helpers ----------
  const esc = s => String(s).replace(/[&<>"']/g, c => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
  const fmtDay = new Intl.DateTimeFormat(undefined, { month: "short", day: "numeric" });
  const fmtDayYear = new Intl.DateTimeFormat(undefined, { month: "short", day: "numeric", year: "numeric" });
  const fmtFull = new Intl.DateTimeFormat(undefined, { dateStyle: "full", timeStyle: "short" });
  function when(d) {
    const mins = (now - d.getTime()) / 60e3;
    if (mins < 1) return "just now";
    if (mins < 60) return Math.round(mins) + " min";
    if (mins < 24 * 60) return Math.round(mins / 60) + " h";
    if (mins < 7 * 24 * 60) return Math.max(1, Math.round(mins / 1440)) + " d";
    return d.getFullYear() === new Date(now).getFullYear() ? fmtDay.format(d) : fmtDayYear.format(d);
  }
  const byDate = (a, b) => b.date - a.date;
  const dated = list => list.filter(i => !i.b);

  // ---------- view state ----------
  let query = "";
  let unreadOnly = !!prefs.unreadOnly;
  const expanded = new Set();
  const route = () => { const h = decodeURIComponent(location.hash.replace(/^#\/?/, "")); return CAT_NAME[h] ? h : ""; };

  function visible(list) {
    let out = list;
    if (unreadOnly) out = out.filter(i => !read.has(i.id));
    if (query) {
      const q = query.toLowerCase();
      out = out.filter(i => i.t.toLowerCase().includes(q) || SOURCES[i.s].n.toLowerCase().includes(q));
    }
    return out;
  }

  function headline(i, withSource) {
    const cls = ["h", read.has(i.id) ? "is-read" : "", isNew(i) ? "is-new" : ""].join(" ");
    const status = isNew(i) ? '<span class="sr">New: </span>' : read.has(i.id) ? '<span class="sr">Opened: </span>' : "";
    const timeTitle = (i.x ? "First seen " : "Published ") + fmtFull.format(i.date);
    const t = i.b ? "" : `<time datetime="${esc(i.d)}" title="${esc(timeTitle)}">${esc(when(i.date))}</time>`;
    const src = withSource ? `<span class="src">${esc(SOURCES[i.s].n)}</span>` : "";
    return `<li class="${cls}"><span class="dot" aria-hidden="true"></span>` +
      `<span><a class="t" href="${esc(i.u)}" target="_blank" rel="noopener" data-id="${esc(i.id)}">${status}${esc(i.t)}</a>${src}</span>${t}</li>`;
  }

  // ---------- views ----------
  function renderFront() {
    const all = visible(dated(ITEMS)).sort(byDate);
    if (query) return renderResults(all);
    const latest = all.slice(0, 14);
    let html = '<div class="front"><section class="latest" aria-labelledby="latest-h"><h2 id="latest-h">Latest</h2>';
    html += latest.length ? `<ul class="list">${latest.map(i => headline(i, true)).join("")}</ul>` : '<p class="empty">Nothing unread. Everything is caught up.</p>';
    html += "</section>";
    for (const c of CATS) {
      const inCat = all.filter(i => SOURCES[i.s].c === c.id);
      if (!inCat.length && unreadOnly) continue;
      const fresh = inCat.filter(isNew).length;
      html += `<section class="dept" aria-labelledby="d-${esc(c.id)}"><h2 id="d-${esc(c.id)}"><a href="#/${esc(c.id)}">${esc(c.name)}</a>` +
        (fresh ? `<span class="n">${fresh} new</span>` : "") + "</h2>";
      html += inCat.length ? `<ul class="list">${inCat.slice(0, 3).map(i => headline(i, true)).join("")}</ul>` : '<p class="quietnote">No recent posts.</p>';
      html += `<a class="more" href="#/${esc(c.id)}">More in ${esc(c.short)}</a></section>`;
    }
    return html + "</div>";
  }

  function renderResults(list) {
    if (!list.length) return `<p class="empty">No headlines match “${esc(query)}”.</p>`;
    return `<div class="cols"><section class="col"><h2>${list.length} matching headline${list.length === 1 ? "" : "s"}</h2>` +
      `<ul class="list">${list.slice(0, 200).map(i => headline(i, true)).join("")}</ul></section></div>`;
  }

  function renderSection(cat) {
    const cutoff = now - QUIET_DAYS * 864e5;
    const latestOf = s => ITEMS.reduce((m, i) => (i.s === s.id && !i.b ? Math.max(m, i.date.getTime()) : m), 0);
    const sources = DATA.sources.filter(s => s.c === cat).sort((a, b) => latestOf(b) - latestOf(a));  // freshest first
    const blocks = [], quiet = [];
    for (const s of sources) {
      const own = ITEMS.filter(i => i.s === s.id);
      const recent = visible(dated(own)).sort(byDate);
      const earlier = own.filter(i => i.b);
      const latestDate = dated(own).reduce((m, i) => Math.max(m, i.date.getTime()), 0);
      if (!recent.length && (latestDate < cutoff || unreadOnly || query) ) {
        if (!unreadOnly && !query) quiet.push(s);
        continue;
      }
      const open = expanded.has(s.id);
      const shown = open ? recent : recent.slice(0, 5);
      let b = `<section class="col" aria-labelledby="s-${esc(s.id)}"><h2 id="s-${esc(s.id)}"><a href="${esc(s.h)}" target="_blank" rel="noopener">${esc(s.n)}</a></h2>`;
      if (s.a && s.a !== s.n) b += `<p class="by">${esc(s.a)}</p>`;
      b += shown.length ? `<ul class="list">${shown.map(i => headline(i, false)).join("")}</ul>` : '<p class="quietnote">Nothing new on this page since it was first read.</p>';
      if (recent.length > 5) b += `<button class="more" type="button" data-expand="${esc(s.id)}" aria-expanded="${open}">${open ? "Show fewer" : `Show ${recent.length - 5} more`}</button>`;
      if (earlier.length && !query && !unreadOnly) {
        b += `<details><summary>Links on the page when it was first read (${earlier.length})</summary><ul class="list">${earlier.map(i => headline(i, false)).join("")}</ul></details>`;
      }
      blocks.push(b + "</section>");
    }
    let html = blocks.length ? `<div class="cols">${blocks.join("")}</div>`
      : `<p class="empty">${query ? `No headlines match “${esc(query)}” in ${esc(CAT_NAME[cat])}.` : "Nothing unread in this section."}</p>`;
    if (quiet.length) {
      html += `<p class="quiet">Quiet lately: ${quiet.map(s => `<a href="${esc(s.h)}" target="_blank" rel="noopener">${esc(s.n)}</a>`).join(", ")}.</p>`;
    }
    return html;
  }

  function currentItems() {
    const cat = route();
    const base = dated(ITEMS).filter(i => !cat || SOURCES[i.s].c === cat);
    return visible(base);
  }

  function renderChrome() {
    const cat = route();
    const counts = {};
    let totalNew = 0;
    for (const i of ITEMS) if (isNew(i)) { counts[SOURCES[i.s].c] = (counts[SOURCES[i.s].c] || 0) + 1; totalNew++; }
    const link = (id, name, n) => `<a href="#/${esc(id)}"${cat === id ? ' aria-current="page"' : ""}>${esc(name)}${n ? `<span class="n" aria-label="${n} new">${n}</span>` : ""}</a>`;
    document.getElementById("sections").innerHTML = link("", "Front page", totalNew) + CATS.map(c => link(c.id, c.short, counts[c.id])).join("");
    document.getElementById("newcount").innerHTML = totalNew
      ? `<b>${totalNew}</b> new since your last visit`
      : "You are all caught up";
    document.title = (totalNew ? `(${totalNew}) ` : "") + DATA.site.title + (cat ? ` — ${CAT_NAME[cat]}` : "");
  }

  function render() {
    const cat = route();
    renderChrome();
    document.getElementById("main").innerHTML = cat ? renderSection(cat) : renderFront();
  }

  // ---------- events ----------
  function markOpened(a) {
    const id = a.getAttribute("data-id");
    if (!id || read.has(id)) return;
    read.add(id); saveRead();
    const li = a.closest(".h");
    if (li) { li.classList.add("is-read"); li.classList.remove("is-new"); }   // update in place: no layout jump
    renderChrome();
  }
  document.addEventListener("click", e => {
    const a = e.target.closest("a[data-id]"); if (a) markOpened(a);
    const more = e.target.closest("[data-expand]");
    if (more) { const id = more.getAttribute("data-expand"); expanded.has(id) ? expanded.delete(id) : expanded.add(id); render(); }
  });
  document.addEventListener("auxclick", e => { const a = e.target.closest("a[data-id]"); if (a && e.button === 1) markOpened(a); });

  const q = document.getElementById("q");
  let timer;
  q.addEventListener("input", () => { clearTimeout(timer); timer = setTimeout(() => { query = q.value.trim(); render(); }, 120); });
  document.addEventListener("keydown", e => {
    if (e.key === "/" && document.activeElement !== q) { e.preventDefault(); q.focus(); }
    else if (e.key === "Escape" && document.activeElement === q) { q.value = ""; query = ""; render(); }
  });

  const unreadBtn = document.getElementById("unread");
  const syncUnread = () => unreadBtn.setAttribute("aria-pressed", String(unreadOnly));
  unreadBtn.addEventListener("click", () => { unreadOnly = !unreadOnly; prefs.unreadOnly = unreadOnly; store.set("ket:prefs", prefs); syncUnread(); render(); });

  document.getElementById("markread").addEventListener("click", () => {
    for (const i of currentItems()) read.add(i.id);
    saveRead(); render();
  });

  const themeBtn = document.getElementById("theme");
  const sysDark = () => window.matchMedia && matchMedia("(prefers-color-scheme: dark)").matches;
  function applyTheme() {
    const dark = prefs.theme ? prefs.theme === "dark" : sysDark();
    if (prefs.theme) document.documentElement.setAttribute("data-theme", prefs.theme); else document.documentElement.removeAttribute("data-theme");
    themeBtn.textContent = dark ? "Light" : "Dark";
  }
  themeBtn.addEventListener("click", () => { const dark = prefs.theme ? prefs.theme === "dark" : sysDark(); prefs.theme = dark ? "light" : "dark"; store.set("ket:prefs", prefs); applyTheme(); });

  window.addEventListener("hashchange", () => { window.scrollTo(0, 0); render(); });

  // ---------- static chrome ----------
  document.getElementById("today").textContent = new Intl.DateTimeFormat(undefined, { weekday: "long", month: "long", day: "numeric", year: "numeric" }).format(new Date(now));
  const gen = new Date(DATA.generated);
  const upd = document.getElementById("updated");
  const ago = when(gen);
  upd.textContent = "Updated " + (ago === "just now" ? ago : /\d (min|h|d)$/.test(ago) ? ago + " ago" : "on " + ago);
  upd.title = fmtFull.format(gen);
  document.getElementById("footer").innerHTML =
    `<span>${DATA.sources.length} sources in ${CATS.length} sections. Updated hourly.</span>` +
    `<span>Your reading history stays in this browser.</span>` +
    (DATA.site.repo ? `<a href="${esc(DATA.site.repo)}">Source list and code</a>` : "");

  syncUnread(); applyTheme(); render();
})();
</script>
</body>
</html>
"""
