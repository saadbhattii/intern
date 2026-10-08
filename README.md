# Intern

A bot follows about 190 sources (researchers' blogs, labs, companies, universities, journals and analysts) and posts each new article as its title and a link to the original, with a link preview.

It runs entirely on GitHub Actions. The full source list is in [`sources.toml`](sources.toml) and rendered as a table in [`docs/SOURCES.md`](docs/SOURCES.md).

## How it works

Every 30 minutes, the **Post new articles** workflow:

1. Reads `sources.toml` and fetches every source that has a Discord channel, in parallel and with timeouts.
2. Compares what it found with `state/state.json` to find articles that are genuinely new.
3. Posts them to the right section channel (and the all-updates channel), records them in `state/journal.json`, and updates the sources message if the list changed.
4. Commits the updated state back to the repository.

For each source the bot tries its feed address first, then any feed the website advertises, then the article links on its news page, and finally its homepage if the news page has moved.

## Design decisions

| Problem | What the bot does |
|---|---|
| Adding sources floods channels with old posts | The first time a source is seen, everything it currently lists is recorded and nothing is posted. Only later articles are announced. |
| GitHub runs are delayed or skipped | No time windows. The state file remembers exactly what was posted, so a late run simply catches up. |
| A site changes its URLs and everything looks new | If most of a sizeable feed suddenly looks new, it is recorded silently and reported. Dated items older than 14 days are never posted. |
| A post fails to send | Items are marked as posted only after Discord confirms delivery, so failed ones are retried next run. |
| One broken site or deleted webhook | Every source is isolated. A private status channel gets one alert after 3 failures in a row, and one when it recovers. |
| The same story from several outlets | Matching links or near-identical headlines within 72 hours are posted once per channel. |
| Everything showed Discord's own icon | Each message carries the source's name and website icon (or a custom image). |
| Some posts had no preview | About 10 seconds after posting, the bot reads each post back; any post Discord did not preview gets a preview-shaped box. |
| Busy sources | At most 5 posts per source per run, each as its own message so each gets a preview. |
| A hostile headline tries to ping @everyone | Mentions are blocked. Only role ids you list under `[discord.roles]` can ever be pinged. |
| Two runs overlap | Posting and briefings share one concurrency group, so only one job writes state at a time. |
| State lost or corrupted | State files are committed to git, written atomically, and a corrupt file is set aside instead of crashing. |
| Dependencies break | Standard library only. Nothing to install. |
| Typos in `sources.toml` | Strict validation runs in CI on every push and pull request. |

## Workflows

| Workflow | When | What |
|---|---|---|
| `post.yml` | every 30 minutes, and on demand | posts new articles, updates the sources message, commits state |
| `briefing.yml` | daily 03:00 UTC (08:00 PKT), weekly Sunday 15:00 UTC (20:00 PKT), and on demand | posts the daily briefing or weekly roundup, once per period |
| `check-sources.yml` | Mondays, and on demand | health report of every source; never posts |
| `ci.yml` | every push and pull request | validates `sources.toml` and runs the offline tests on Python 3.11 to 3.13 |

## Adding or changing a source

```toml
[[source]]
id = "my-new-blog"
name = "My New Blog"
category = "math"
feed = "https://example.com/feed/"
site = "https://example.com/"
```

If the section already has a webhook, that is all. Useful options: `include_keywords` (post only matching titles), `max_per_run`, `mode = "digest"` for busy sources, `avatar`, and `enabled = false`. Every key is described at the top of `sources.toml`. Run **Check sources** afterwards to confirm it works; anyone can suggest a source with the "New source" issue template.

## Running locally

Requires Python 3.11 or newer and nothing else.

```bash
python -m feedbot lint
python -m feedbot check --only quera,riverlane
python -m feedbot run --dry-run --only gil-kalai
python -m feedbot briefing --period daily --dry-run
python -m feedbot directory --dry-run
python -m unittest discover -s tests -v
```

## License

MIT. See [LICENSE](LICENSE). Titles and links belong to their publishers; this bot only points to them.
