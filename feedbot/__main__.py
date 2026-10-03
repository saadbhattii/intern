"""Command line interface.

    python -m feedbot run                 # the scheduled job
    python -m feedbot run --dry-run       # print what would be posted, change nothing
    python -m feedbot check               # live health check of every source (never posts)
    python -m feedbot lint                # offline validation of sources.toml (used by CI)
    python -m feedbot docs                # regenerate docs/SOURCES.md
    python -m feedbot webhooks-template   # JSON skeleton for the DISCORD_WEBHOOKS secret

Exit codes: 0 success (individual source failures do NOT fail the run; they are
reported in the summary and alerted after repeated failures), 2 configuration
error, 3 state could not be saved.
"""

from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path

from .config import Config, ConfigError, load_config, parse_webhooks
from .discord import DiscordClient
from .runner import run, send_alerts, summary_markdown, write_step_summary
from .state import State
from .tools import check_sources, sources_markdown, webhook_template


def _ids(value: str | None) -> set[str] | None:
    if not value:
        return None
    ids = {part.strip() for part in value.replace(" ", ",").split(",") if part.strip()}
    return ids or None


def _load(args) -> Config:
    try:
        return load_config(args.config)
    except ConfigError as exc:
        print("Configuration errors:", file=sys.stderr)
        for error in exc.errors:
            print(f"  - {error}", file=sys.stderr)
            if os.environ.get("GITHUB_ACTIONS"):
                print(f"::error file={args.config}::{error}")
        sys.exit(2)


def _mask(values) -> None:
    """Tell GitHub Actions to redact these strings from all log output."""
    if not os.environ.get("GITHUB_ACTIONS"):
        return
    for value in values:
        if value:
            print(f"::add-mask::{value}")


def cmd_run(args) -> int:
    config = _load(args)
    only = _ids(args.only)
    if only:
        unknown = only - {s.id for s in config.sources}
        if unknown:
            print(f"Unknown source id(s): {', '.join(sorted(unknown))}", file=sys.stderr)
            return 2
    raw = os.environ.get("DISCORD_WEBHOOKS", "")
    webhooks = parse_webhooks(raw, config)
    status_hook = os.environ.get("DISCORD_STATUS_WEBHOOK", "").strip() or None
    _mask(list(webhooks.mapping.values()) + [status_hook])
    if args.dry_run and not raw.strip():
        webhooks.problems.clear()  # expected: dry runs print instead of posting
    for problem in webhooks.problems:
        print(f"::warning::{problem}" if os.environ.get("GITHUB_ACTIONS") else f"warning: {problem}")

    state = State.load(args.state)
    client = DiscordClient(dry_run=args.dry_run)
    report = run(config, webhooks, state, only=only, dry_run=args.dry_run,
                 force_latest=max(0, args.force_latest), client=client)

    for outcome in report.outcomes:
        if outcome.status in ("posted", "failed", "seeded"):
            print(f"[{outcome.status:>8}] {outcome.source.id}: {outcome.detail[:200]}")
    print(f"done: {sum(o.posted for o in report.outcomes)} posted, "
          f"{sum(1 for o in report.outcomes if o.status == 'failed')} failed, "
          f"{len(report.outcomes)} sources")

    markdown = summary_markdown(report, "Dry run (nothing posted or saved)" if args.dry_run else "Feed run")
    write_step_summary(markdown)

    if args.dry_run:
        return 0
    try:
        state.save()
    except OSError as exc:
        print(f"::error::could not save state: {exc}")
        return 3
    send_alerts(report.alerts, status_hook, DiscordClient())
    return 0


def cmd_check(args) -> int:
    config = _load(args)
    markdown, problems = check_sources(config, _ids(args.only))
    print(markdown)
    write_step_summary(markdown)
    if args.output:
        Path(args.output).write_text(markdown, encoding="utf-8")
    return 1 if (problems and args.strict) else 0


def cmd_lint(args) -> int:
    config = _load(args)
    print(f"sources.toml OK: {len(config.sources)} sources in {len({s.category for s in config.sources})} categories")
    if args.webhooks:
        try:
            raw = Path(args.webhooks).read_text(encoding="utf-8")
        except OSError as exc:
            print(f"cannot read {args.webhooks}: {exc}", file=sys.stderr)
            return 2
        hooks = parse_webhooks(raw, config)
        for problem in hooks.problems:
            print(f"warning: {problem}")
        covered = sum(1 for s in config.sources if s.enabled and hooks.for_source(s))
        enabled = sum(1 for s in config.sources if s.enabled)
        print(f"webhooks OK: {len(hooks.mapping)} valid entries; {covered}/{enabled} enabled sources will post")
    return 0


def cmd_docs(args) -> int:
    config = _load(args)
    text = sources_markdown(config)
    target = Path(args.output)
    if args.check:
        current = target.read_text(encoding="utf-8") if target.exists() else ""
        if current != text:
            print(f"{target} is out of date; run: python -m feedbot docs", file=sys.stderr)
            return 1
        print(f"{target} is up to date")
        return 0
    target.parent.mkdir(parents=True, exist_ok=True)
    if not target.exists() or target.read_text(encoding="utf-8") != text:
        target.write_text(text, encoding="utf-8")
        print(f"wrote {target}")
    return 0


def cmd_template(args) -> int:
    sys.stdout.write(webhook_template(_load(args)))
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="feedbot", description="Post research blog titles and links to Discord.")
    parser.add_argument("--config", default="sources.toml", help="path to sources.toml")
    sub = parser.add_subparsers(dest="command", required=True)

    p_run = sub.add_parser("run", help="fetch sources and post new items")
    p_run.add_argument("--state", default="state/state.json")
    p_run.add_argument("--only", help="comma-separated source ids")
    p_run.add_argument("--dry-run", action="store_true", help="print instead of posting; do not save state")
    p_run.add_argument("--force-latest", type=int, default=0, metavar="N",
                       help="post the newest N items of each selected source even if already seen (testing)")
    p_run.set_defaults(func=cmd_run)

    p_check = sub.add_parser("check", help="live health check of sources (never posts)")
    p_check.add_argument("--only", help="comma-separated source ids")
    p_check.add_argument("--output", help="also write the report to this file")
    p_check.add_argument("--strict", action="store_true", help="exit 1 if any source has problems")
    p_check.set_defaults(func=cmd_check)

    p_lint = sub.add_parser("lint", help="validate configuration offline")
    p_lint.add_argument("--webhooks", help="optional path to a local webhooks JSON file to validate")
    p_lint.set_defaults(func=cmd_lint)

    p_docs = sub.add_parser("docs", help="generate docs/SOURCES.md")
    p_docs.add_argument("--output", default="docs/SOURCES.md")
    p_docs.add_argument("--check", action="store_true", help="fail if the file is out of date")
    p_docs.set_defaults(func=cmd_docs)

    p_tpl = sub.add_parser("webhooks-template", help="print a JSON skeleton for DISCORD_WEBHOOKS")
    p_tpl.set_defaults(func=cmd_template)

    args = parser.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
