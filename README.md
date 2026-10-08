# Within Quantum

The bot behind the **Within Quantum** Discord server: a read-only news feed for quantum computing, mathematics, theoretical computer science and AI. It follows about 190 sources (researchers' blogs, labs, companies, universities, journals and analysts) and posts each new article as a clean card with the title and a link to the original. No summaries, no AI rewriting, no discussion.

It runs entirely on GitHub Actions. The full source list is in [`sources.toml`](sources.toml) and rendered as a table in [`docs/SOURCES.md`](docs/SOURCES.md).

## What readers see

- **One channel per section** (quantum researchers, hardware, error correction, AI, and so on). Every post shows the source's own name and icon, so posts in a shared channel are easy to tell apart.
- **Branded cards**: the headline as a link, a colour per section, the source and publish time, a small preview image, and a "Within Quantum | Section" footer.
- **No repeats**: when several outlets report the same story within 3 days, a section channel shows it once.
- **#all-updates** (optional): everything in one channel, also without repeats across sections.
- **#daily-briefing** (optional): one message every morning listing yesterday's posts by section, and a weekly roundup on Sundays.
- **#sources** (optional): a list of every source that updates itself when the list changes.
- **Opt-in pings** (optional): readers pick up a role such as "Hardware alerts" and get pinged when that section has news, at most once per run.

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
| Busy sources | At most 5 posts per source per run; more than 3 at once become one list card. |
| A hostile headline tries to ping @everyone | Mentions are blocked. Only role ids you list under `[discord.roles]` can ever be pinged. |
| Two runs overlap | Posting and briefings share one concurrency group, so only one job writes state at a time. |
| State lost or corrupted | State files are committed to git, written atomically, and a corrupt file is set aside instead of crashing. |
| Dependencies break | Standard library only. Nothing to install. |
| Typos in `sources.toml` | Strict validation runs in CI on every push and pull request. |

## Setting up the server

### 1. Discord (done once, in the Discord app)

- **Read-only**: in each category's channel permissions, deny **Send Messages** (and **Add Reactions** if you prefer) for `@everyone`. Webhooks still post.
- **Channels**: one per section (you already have these). Optional extras: `#all-updates`, `#daily-briefing`, `#sources`, and a **private** `#bot-status` visible only to admins.
- **Webhooks**: in each channel, **Edit Channel > Integrations > Webhooks > New Webhook > Copy Webhook URL**. You do not need to set names or pictures on the webhooks; the bot sets them per post.
- **Community features** (Server Settings > Enable Community): rules screening, a welcome screen, and Onboarding so newcomers can pick sections and roles. Pin a short "what this server is" message in `#sources` or a welcome channel.

### 2. The webhook secret

```bash
python -m feedbot webhooks-template > webhooks.json   # every key, empty
python -m feedbot lint --webhooks webhooks.json       # checks the file without printing URLs
gh secret set DISCORD_WEBHOOKS < webhooks.json
```

Fill in what you use and leave the rest empty:

```json
{
  "category:quantum-researchers": "https://discord.com/api/webhooks/...",
  "category:hardware": "https://discord.com/api/webhooks/...",
  "firehose": "https://discord.com/api/webhooks/...",
  "briefing": "https://discord.com/api/webhooks/...",
  "weekly": "",
  "directory": "https://discord.com/api/webhooks/..."
}
```

| Key | Channel |
|---|---|
| `category:<id>` | the section's channel (one per category in `sources.toml`) |
| `firehose` | `#all-updates` |
| `briefing` | `#daily-briefing` |
| `weekly` | the weekly roundup, if you want it in a separate channel |
| `directory` | `#sources` |

A source posts to its own webhook if one is set, otherwise to its section's (`category:<id>`), otherwise to `default`. Sources with no matching webhook are skipped. `weekly` falls back to `briefing` when empty.

Add a second secret, `DISCORD_STATUS_WEBHOOK`, pointing at the private `#bot-status` channel, for failure and recovery alerts. Never point it at a public channel.

### 3. Appearance and pings (`[discord]` in `sources.toml`)

```toml
[discord]
brand = "Within Quantum"
style = "card"              # or "link": title plus bare link with Discord's own preview
avatars = "auto"            # each source's website icon; "off" for Discord's default
preview_image = "thumbnail" # "thumbnail", "large" or "none"
dedupe_hours = 72

[discord.colors]
hardware = "#2E8B8B"        # one per section

[discord.roles]
hardware = "123456789012345678"   # opt-in ping role for that section
```

To give a source its own picture instead of its website icon, add `avatar = "https://..."` to its `[[source]]` block. You can also keep images in the repository (for example `assets/avatars/ibm.png`) and set `asset_base_url` to the repository's raw address; the repository must be public for Discord to load them.

To find a role id: enable **Developer Mode** (User Settings > Advanced), then right-click the role and choose **Copy Role ID**.

### 4. First run

Run **Actions > Post new articles > Run workflow**. The first run records what each source currently lists and posts nothing; from then on new articles appear within about 30 minutes. To see a real card straight away, run it with `only` set to a source id and `force_latest` set to `1`.

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

## Troubleshooting

**A source shows ❌ in Check sources.** `HTTP 403` means the site blocks GitHub's servers; disable it. `no article links found` lists the links the page does have, so you can set a `link_pattern`. Repeated timeouts usually mean the site is slow or blocking.

**No picture on a card.** The source's feed and page offer no preview image. Set `preview_image = "none"` for a uniform look, or give the source an `avatar`.

**A briefing did not arrive.** Briefings only list what was posted, so a quiet day sends nothing. Each period is sent at most once; run the workflow with `force` to resend.

**The sources message was deleted.** The next posting run notices and posts a fresh copy.

**"Could not push state".** Another commit landed during the run and five retries failed. The next run recovers automatically.

**Merge conflict in `state/`.** The bot commits state every 30 minutes. Always `git pull` before editing, and if a conflict happens in `state/`, keep the bot's version: `git checkout --theirs state/`.

## License

MIT. See [LICENSE](LICENSE). Titles and links belong to their publishers; this bot only points to them.
