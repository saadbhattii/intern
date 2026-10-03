"""Discord webhook delivery: the bot posts titles and links only.

- Discord shows its normal link preview (the site's own title, image and
  description card) under each link. The bot itself never writes summaries.
- `?wait=true` makes Discord confirm the message was stored before we mark an
  item as seen. No confirmation, no "seen": a failed post is retried next run.
- 429 responses are honoured using Discord's retry_after; the per-webhook bucket
  headers are respected proactively so we rarely hit 429 at all.
- 401/403/404 mean the webhook was deleted or revoked; that is reported as a
  permanent error instead of being retried forever.
"""

from __future__ import annotations

import json
import re
import time

from .http import post_json
from .util import Item, format_date, truncate

MESSAGE_LIMIT = 1900  # Discord allows 2000; keep headroom
_MD_SPECIAL = re.compile(r"([\\\[\]*_~`|<>#])")
_FORBIDDEN_NAME = re.compile(r"discord|clyde|everyone|here", re.I)


class WebhookError(Exception):
    def __init__(self, message: str, permanent: bool = False):
        super().__init__(message)
        self.permanent = permanent


def escape_markdown(text: str) -> str:
    text = text.replace("\n", " ").replace("@", "@\u200b")
    return _MD_SPECIAL.sub(r"\\\1", text)


def safe_url(url: str) -> str:
    """Characters that would terminate a masked markdown link are percent-encoded."""
    return url.replace(" ", "%20").replace("(", "%28").replace(")", "%29").replace("<", "%3C").replace(">", "%3E")


def safe_username(name: str) -> str:
    cleaned = _FORBIDDEN_NAME.sub("", name).replace("`", "").strip()
    cleaned = re.sub(r"\s{2,}", " ", cleaned)
    return truncate(cleaned or "Research Feed", 80)


def format_item(item: Item) -> str:
    title = escape_markdown(truncate(item.title, 250))
    lines = [f"**[{title}]({safe_url(item.link)})**"]
    date = format_date(item.published)
    if date:
        lines.append(f"-# {date}")
    return "\n".join(lines)


def format_digest(source_name: str, items: list[Item], skipped: int = 0) -> list[str]:
    """One compact list message (split into several if it would exceed the limit)."""
    lines = []
    for item in items:
        date = format_date(item.published)
        suffix = f" · {date}" if date else ""
        lines.append(f"- [{escape_markdown(truncate(item.title, 200))}]({safe_url(item.link)}){suffix}")
    header = f"**{escape_markdown(source_name)}**: {len(items)} new"
    if skipped:
        header += f" (+{skipped} older not shown)"
    return chunk_lines([header] + lines)


def chunk_lines(lines: list[str], limit: int = MESSAGE_LIMIT) -> list[str]:
    chunks: list[str] = []
    current = ""
    for line in lines:
        line = truncate(line, limit)
        candidate = f"{current}\n{line}" if current else line
        if len(candidate) > limit:
            chunks.append(current)
            current = line
        else:
            current = candidate
    if current:
        chunks.append(current)
    return chunks


class DiscordClient:
    def __init__(self, dry_run: bool = False, min_interval: float = 0.6, sleep=time.sleep, post=post_json):
        self.dry_run = dry_run
        self.min_interval = min_interval
        self._sleep = sleep
        self._post = post
        self._last_sent: dict[str, float] = {}
        self._blocked_until: dict[str, float] = {}
        self.sent_count = 0

    def send(self, webhook_url: str, content: str, username: str) -> None:
        if self.dry_run:
            print(f"  [dry-run] as {username!r}:\n    " + content.replace("\n", "\n    "))
            self.sent_count += 1
            return
        self._pace(webhook_url)
        url = webhook_url + ("&" if "?" in webhook_url else "?") + "wait=true"
        payload = {
            "content": content,
            "username": safe_username(username),
            "allowed_mentions": {"parse": []},
        }
        last_problem = "unknown error"
        for attempt in range(6):
            status, body, headers = self._post(url, payload)
            self._last_sent[webhook_url] = time.monotonic()
            self._note_bucket(webhook_url, headers)
            if 200 <= status < 300:
                self.sent_count += 1
                return
            if status == 429:
                retry_after = _retry_after(body, headers)
                last_problem = f"rate limited ({retry_after:.1f}s)"
                self._sleep(retry_after)
                continue
            if status in (401, 403, 404):
                raise WebhookError(
                    f"webhook rejected with HTTP {status} (deleted or revoked?)", permanent=True
                )
            if status == 0 or status >= 500:
                last_problem = "network error" if status == 0 else f"HTTP {status}"
                self._sleep(min(2 ** attempt, 20))
                continue
            detail = body[:200].decode("utf-8", "replace")
            raise WebhookError(f"HTTP {status}: {detail}")
        raise WebhookError(f"gave up after retries: {last_problem}")

    def _pace(self, webhook_url: str) -> None:
        now = time.monotonic()
        wait = 0.0
        last = self._last_sent.get(webhook_url)
        if last is not None:
            wait = max(wait, self.min_interval - (now - last))
        blocked = self._blocked_until.get(webhook_url)
        if blocked:
            wait = max(wait, blocked - now)
        if wait > 0:
            self._sleep(min(wait, 60))

    def _note_bucket(self, webhook_url: str, headers: dict) -> None:
        try:
            if headers.get("x-ratelimit-remaining") == "0":
                reset_after = float(headers.get("x-ratelimit-reset-after", "1"))
                self._blocked_until[webhook_url] = time.monotonic() + min(reset_after, 60)
            else:
                self._blocked_until.pop(webhook_url, None)
        except ValueError:
            pass


def _retry_after(body: bytes, headers: dict) -> float:
    value = None
    try:
        value = float(json.loads(body.decode("utf-8", "replace")).get("retry_after"))
    except (ValueError, AttributeError, TypeError):
        value = None
    if value is None:
        try:
            value = float(headers.get("retry-after", "2"))
        except ValueError:
            value = 2.0
    return max(0.5, min(value, 60.0))