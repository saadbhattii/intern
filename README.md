# Intern

Posts **new article titles and links** from 120+ research blogs, company blogs and research organizations in quantum computing, mathematics, theoretical computer science and AI to Discord, with **one channel per source**. No summaries, no AI, no link previews. Runs entirely on GitHub Actions.

The full list of sources lives in [`sources.toml`](sources.toml) and is rendered as a table in [`docs/SOURCES.md`](docs/SOURCES.md).

## How it works

Every hour a GitHub Actions workflow:

1. Reads `sources.toml`.
2. Fetches every source that has a Discord webhook configured (in parallel, with timeouts).
3. Works out which items are genuinely new by comparing against `state/state.json`.
4. Posts each new item's title and link to that source's channel.
5. Commits the updated state back to the repository.

For each source, the bot tries, in order: the listed feed URL(s); feeds the site advertises in its HTML; the article links on the site's blog or news listing page (for sites like IBM or Riverlane that publish no feed); and, if that page has moved, the site's homepage. A feed that turns out to be empty is used only as a last resort. A moved or mistyped URL therefore degrades gracefully instead of breaking.

## Design decisions

These are the choices that keep it running unattended.

| Problem | What the bot does |
|---|---|
| Adding many sources floods channels with old posts | The first time a source is seen, everything currently listed is recorded as seen and **nothing is posted**. Only posts that appear later are announced. |
| GitHub runs are delayed or skipped | No time windows. State remembers exactly what was posted, so a late or missed run just catches up. |
| A site changes its URLs and everything looks new | **Flood guard:** if most of a sizeable feed suddenly looks new, it is re-seeded silently and reported. Dated items older than 14 days are never posted. |
| A post fails to send | Items are only marked seen after Discord **confirms delivery** (`?wait=true`). Failed items are retried next run. |
| One broken site or deleted webhook | Every source is isolated. Failures are recorded and shown in the run summary. The status channel gets **one** alert after 3 consecutive failures and one when the source recovers. The workflow itself stays green, so you don't get an email every hour. |
| Discord rate limits | Per-webhook pacing, honours `retry_after` and rate-limit bucket headers. |
| Link previews show the site's description | Messages are sent with Discord's `SUPPRESS_EMBEDS` flag. Titles and links only. |
| `@everyone` in a title | `allowed_mentions` is empty and `@` is neutralised; markdown in titles is escaped. |
| Busy sources | `max_per_run` caps each source per run; more than three new items are combined into one list message. |
| Two runs overlap | `concurrency` in the workflow guarantees one posting job at a time. |
| State lost or corrupted | State is a JSON file committed to git (the Actions cache expires after 7 days). Writes are atomic. A corrupt file is backed up and the bot re-seeds silently instead of crashing. |
| GitHub disables scheduled workflows after 60 days of inactivity | The state file records a monthly heartbeat, guaranteeing at least one commit a month. |
| Dependencies break | Standard library only. Nothing to `pip install`, no supply-chain risk, nothing to update. |
| Typos in `sources.toml` | Strict validation (unknown keys, bad URLs, invalid regexes, duplicate ids) runs in CI on every push and pull request. |
| Leaked webhook URLs | All webhooks live in one encrypted secret; values are masked in logs and never echoed in errors. |

## Workflows

| Workflow | When | What |
|---|---|---|
| `post.yml` | hourly, and on demand | posts new items, commits state |
| `check-sources.yml` | Mondays, and on demand | live health report of every source; never posts |
| `ci.yml` | every push and pull request | compiles, validates `sources.toml`, runs the 62 offline tests on Python 3.11–3.13 |

## Adding or changing a source

Add a block to `sources.toml`:

```toml
[[source]]
id = "my-new-blog"          # also its key in DISCORD_WEBHOOKS
name = "My New Blog"
category = "math"
feed = "https://example.com/feed/"
site = "https://example.com/"
```

Then add its webhook to the `DISCORD_WEBHOOKS` secret. A site without a feed only needs `site` pointing at its blog or news listing page. Useful options: `include_keywords` (post only matching titles, for broad feeds), `max_per_run`, `mode = "digest"` for busy sources, and `enabled = false` to keep an entry documented but inactive. Every key is described at the top of `sources.toml`; see also [CONTRIBUTING.md](CONTRIBUTING.md).

## Running locally

Requires Python 3.11+ and nothing else.

```bash
python -m feedbot lint                       # validate configuration
python -m feedbot check --only quera,riverlane
python -m feedbot run --dry-run --only gil-kalai
python -m unittest discover -s tests -v
```

## License

MIT. See [LICENSE](LICENSE). Post titles and links belong to their authors; this bot only points to them.
