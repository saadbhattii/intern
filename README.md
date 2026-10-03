# Intern

Posts **new article titles and links** from 120+ research blogs, company blogs and research organizations in quantum computing, mathematics, theoretical computer science and AI to Discord, with **one channel per source or category**. No summaries, no AI, no link previews. Runs entirely on GitHub Actions.

The full list of sources lives in [`sources.toml`](sources.toml) and is rendered as a table in [`docs/SOURCES.md`](docs/SOURCES.md).

---

## How it works

Every hour a GitHub Actions workflow:

1. Reads `sources.toml`.
2. Fetches every source that has a Discord webhook configured (in parallel, with timeouts).
3. Works out which items are genuinely new by comparing against `state/state.json`.
4. Posts each new item's title and link to that source's channel.
5. Commits the updated state back to the repository.

For each source, the bot tries, in order: the listed feed URL(s); feeds the site advertises in its HTML; and finally the article links on the site's blog or news listing page (for sites like IBM or Riverlane that publish no feed). A moved or mistyped feed URL therefore degrades gracefully instead of breaking.

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

## Setup

You need a GitHub repository (public is fine; public repos get unlimited Actions minutes) and a Discord server where you can manage webhooks.

### 1. Create the repository

Push this folder to a new GitHub repository. In **Settings → Actions → General**, make sure Actions are enabled. Under **Workflow permissions**, choose **Read and write permissions** if your organization defaults to read-only; the posting workflow requests write access explicitly, but an organization policy can override it.

### 2. Create Discord channels and webhooks

You don't need all 120+ channels on day one. Start with the sources you care about; others are simply skipped until they have a webhook.

For each source: create a channel, then **Edit Channel → Integrations → Webhooks → New Webhook → Copy Webhook URL**.

Discord allows 50 channels per category and 500 per server. The bot's categories (researchers, hardware, QEC, ...) map naturally onto Discord categories.

### 3. Build the webhook mapping

```bash
python -m feedbot webhooks-template > webhooks.json   # one empty entry per source
# paste each webhook URL next to its source id, leave the rest ""
python -m feedbot lint --webhooks webhooks.json       # validates without printing URLs
```

`webhooks.json` is listed in `.gitignore`. Never commit it.

The mapping supports fallbacks, looked up in this order:

```json
{
  "shtetl-optimized": "https://discord.com/api/webhooks/...",
  "category:hardware": "https://discord.com/api/webhooks/...",
  "default": ""
}
```

A source uses its own webhook if present, otherwise its category's webhook, otherwise `default`. So you can start with one channel per category and split later.

### 4. Add the secrets

In **Settings → Secrets and variables → Actions → New repository secret**:

| Secret | Required | Value |
|---|---|---|
| `DISCORD_WEBHOOKS` | yes | the entire contents of `webhooks.json` |
| `DISCORD_STATUS_WEBHOOK` | no | a webhook for an ops channel that receives failure and recovery alerts |

Or with the GitHub CLI: `gh secret set DISCORD_WEBHOOKS < webhooks.json`

### 5. First run

Go to **Actions → Post new articles → Run workflow**. The first run posts nothing: it records what each source currently lists. From then on, new posts appear within about an hour of publication.

To see real output immediately, run the workflow with `only` = `shtetl-optimized` and `force_latest` = `1`. To preview everything without posting or saving state, tick **dry_run**.

Then run **Actions → Check sources** once. Its summary shows, for every source, whether a feed was found, whether it fell back to scraping, how many items it returned, and when it last posted.

## Workflows

| Workflow | When | What |
|---|---|---|
| `post.yml` | hourly, and on demand | posts new items, commits state |
| `check-sources.yml` | Mondays, and on demand | live health report of every source; never posts |
| `ci.yml` | every push and pull request | compiles, validates `sources.toml`, runs the offline test suite on Python 3.11–3.13 |

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
