# Contributing

Suggestions for new sources are welcome. The bar for inclusion: the blog is written by researchers (or is a research organization's own blog), it is read and cited by people in the field, and it has posted in the last year. No paper feeds (arXiv, journal tables of contents).

## Suggesting a source

Open an issue with the **New source** template, or a pull request that adds a `[[source]]` block to `sources.toml`.

## Before opening a pull request

```bash
python -m feedbot lint                        # must pass
python -m feedbot check --only your-source-id # should show ✅
python -m unittest discover -s tests          # must pass
```

CI runs the first and last on every pull request.

## Conventions

- `id`: lowercase words joined by dashes, stable forever (it is the key in the webhook secret and in the state file). Renaming an id makes the source start fresh.
- `feed`: prefer the site's own RSS/Atom feed. If unsure, leave `feed` out and set `site`; the bot will discover a feed or fall back to the listing page.
- `site`: for sites without a feed, point at the blog/news **listing** page, not the homepage.
- `include_keywords`: use for broad feeds (a magazine, a whole company blog) so only relevant titles post.
- `high_volume = true` and `mode = "digest"` for anything posting several times a day.
- Keep `note` short and factual.

You don't need to edit `docs/SOURCES.md`; it is regenerated automatically.

## Code changes

Standard library only. Please keep it that way: zero dependencies is a reliability feature. Add a test in `tests/` for any behaviour change; tests must not use the network.
