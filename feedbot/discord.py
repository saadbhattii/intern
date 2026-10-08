"""Discord webhook delivery. The bot posts titles and links only, never summaries.

Two message styles (set `style` under [discord] in sources.toml):
- "card" (default): a branded embed. Source name and icon on top, the headline as
  a clickable title, a colour stripe per category, a footer with the brand and
  category, the publish time, and an optional preview image. Every channel looks
  like one product, whatever the source's own page looks like.
- "link": the bold title plus the bare link, so Discord draws the site's own
  preview card underneath.

Every message carries the source's name and avatar, so posts in a shared
category channel are easy to tell apart.

Delivery guarantees:
- `?wait=true` makes Discord confirm the message was stored before an item is
  marked as seen. No confirmation, no "seen": a failed post is retried next run.
- 429 responses honour Discord's retry_after; rate-limit bucket headers are
  respected proactively.
- 401/403/404 mean the webhook was deleted or revoked; that is a permanent error.
- Mentions are blocked by default. Only role ids explicitly configured under
  [discord.roles] can ever be pinged, so a hostile headline cannot ping anyone.
"""

from __future__ import annotations

import json
import re
import time
from urllib.parse import urlsplit

from .http import post_json
from .util import Item, format_date, truncate

MESSAGE_LIMIT = 1900  # Discord allows 2000 characters of content; keep headroom
EMBED_DESCRIPTION_LIMIT = 3900  # Discord allows 4096
EMBEDS_PER_MESSAGE = 10
EMBED_TOTAL_LIMIT = 5800  # Discord allows 6000 characters across all embeds of one message
SUPPRESS_EMBEDS = 1 << 2
DEFAULT_COLOR = 0x4F7CAC
_MD_SPECIAL = re.compile(r"([\\\[\]*_~`|<>#])")
_FORBIDDEN_NAME = re.compile(r"discord|clyde|everyone|here", re.I)


class WebhookError(Exception):
    def __init__(self, message: str, permanent: bool = False):
        super().__init__(message)
        self.permanent = permanent


# --------------------------------------------------------------------------
# Text helpers
# --------------------------------------------------------------------------

def escape_markdown(text: str) -> str:
    text = text.replace("\n", " ").replace("@", "@\u200b")
    return _MD_SPECIAL.sub(r"\\\1", text)


def safe_url(url: str) -> str:
    """Characters that would terminate a masked markdown link are percent-encoded."""
    return url.replace(" ", "%20").replace("(", "%28").replace(")", "%29").replace("<", "%3C").replace(">", "%3E")


def bare_url(url: str) -> str:
    """A URL Discord will auto-link and preview: no spaces, no wrapping brackets."""
    return url.strip().replace(" ", "%20").replace("<", "%3C").replace(">", "%3E")


def safe_username(name: str) -> str:
    cleaned = _FORBIDDEN_NAME.sub("", name).replace("`", "").strip()
    cleaned = re.sub(r"\s{2,}", " ", cleaned)
    return truncate(cleaned or "Within Quantum", 80)


def favicon_url(site_url: str, size: int = 128) -> str:
    """The website's own icon, served at a size Discord can use as an avatar."""
    host = urlsplit(site_url).netloc
    if host.startswith("www."):
        host = host[4:]
    if not host:
        return ""
    return f"https://www.google.com/s2/favicons?domain={host}&sz={size}"


# --------------------------------------------------------------------------
# "link" style (title plus bare link; Discord draws the site's own preview)
# --------------------------------------------------------------------------

def format_item(item: Item) -> str:
    title = escape_markdown(truncate(item.title, 250))
    return f"**{title}**\n{bare_url(item.link)}"


def format_digest(source_name: str, items: list[Item], skipped: int = 0) -> list[str]:
    lines = []
    for item in items:
        date = format_date(item.published)
        suffix = f" ({date})" if date else ""
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


# --------------------------------------------------------------------------
# "card" style (branded embeds)
# --------------------------------------------------------------------------

def card_embed(item: Item, *, source_name: str, source_url: str, avatar: str, category_name: str,
               color: int, brand: str, image: str = "", image_mode: str = "thumbnail") -> dict:
    embed: dict = {
        "title": truncate(item.title.replace("\n", " "), 256),
        "url": item.link,
        "color": color or DEFAULT_COLOR,
        "author": {"name": truncate(source_name, 256)},
        "footer": {"text": truncate(f"{brand} | {category_name}", 2048)},
    }
    if source_url:
        embed["author"]["url"] = source_url
    if avatar:
        embed["author"]["icon_url"] = avatar
    if item.published:
        embed["timestamp"] = item.published.isoformat()
    if image and image_mode in ("thumbnail", "large"):
        embed["thumbnail" if image_mode == "thumbnail" else "image"] = {"url": image}
    return embed


def list_embeds(title: str, lines: list[str], *, color: int, footer: str, url: str = "",
                avatar: str = "") -> list[dict]:
    """A titled list split across as many embeds as needed (one description each)."""
    embeds: list[dict] = []
    current: list[str] = []
    size = 0
    for line in lines:
        line = truncate(line, 1000)
        if current and size + len(line) + 1 > EMBED_DESCRIPTION_LIMIT:
            embeds.append(current)  # type: ignore[arg-type]
            current, size = [], 0
        current.append(line)
        size += len(line) + 1
    if current or not embeds:
        embeds.append(current)  # type: ignore[arg-type]
    out = []
    for index, chunk in enumerate(embeds):
        embed = {"description": "\n".join(chunk) or "Nothing new.", "color": color or DEFAULT_COLOR,
                 "footer": {"text": truncate(footer, 2048)}}
        if index == 0:
            embed["title"] = truncate(title, 256)
            if url:
                embed["url"] = url
            if avatar:
                embed["thumbnail"] = {"url": avatar}
        out.append(embed)
    return out


def digest_embeds(items: list[Item], *, source_name: str, source_url: str, avatar: str,
                  category_name: str, color: int, brand: str, skipped: int = 0) -> list[dict]:
    lines = []
    for item in items:
        date = format_date(item.published)
        suffix = f" ({date})" if date else ""
        lines.append(f"- [{escape_markdown(truncate(item.title, 200))}]({safe_url(item.link)}){suffix}")
    if skipped:
        lines.append(f"+{skipped} older posts not shown")
    title = f"{len(items)} new from {source_name}"
    return list_embeds(title, lines, color=color, footer=f"{brand} | {category_name}", url=source_url,
                       avatar=avatar)


def group_embeds(embeds: list[dict]) -> list[list[dict]]:
    """Pack embeds into messages within Discord's per-message limits."""
    messages: list[list[dict]] = []
    current: list[dict] = []
    size = 0
    for embed in embeds:
        length = _embed_length(embed)
        if current and (len(current) >= EMBEDS_PER_MESSAGE or size + length > EMBED_TOTAL_LIMIT):
            messages.append(current)
            current, size = [], 0
        current.append(embed)
        size += length
    if current:
        messages.append(current)
    return messages


def _embed_length(embed: dict) -> int:
    total = len(embed.get("title", "")) + len(embed.get("description", ""))
    total += len(embed.get("footer", {}).get("text", "")) + len(embed.get("author", {}).get("name", ""))
    return total


# --------------------------------------------------------------------------
# Client
# --------------------------------------------------------------------------

class DiscordClient:
    def __init__(self, dry_run: bool = False, min_interval: float = 0.6, sleep=time.sleep, post=post_json,
                 request=None):
        self.dry_run = dry_run
        self.min_interval = min_interval
        self._sleep = sleep
        self._post = post
        self._request = request or post_json
        self._last_sent: dict[str, float] = {}
        self._blocked_until: dict[str, float] = {}
        self.sent_count = 0

    # Backwards-compatible plain-text send.
    def send(self, webhook_url: str, content: str, username: str, previews: bool = True,
             avatar_url: str = "") -> str | None:
        payload: dict = {"content": content}
        if not previews:
            payload["flags"] = SUPPRESS_EMBEDS
        return self.deliver(webhook_url, payload, username=username, avatar_url=avatar_url)

    def deliver(self, webhook_url: str, payload: dict, *, username: str, avatar_url: str = "",
                ping_role: str = "") -> str | None:
        """POST a message and return its id once Discord has confirmed it."""
        body = dict(payload)
        body["username"] = safe_username(username)
        if avatar_url:
            body["avatar_url"] = avatar_url
        if ping_role:
            mention = f"<@&{ping_role}>"
            body["content"] = f"{mention} {body.get('content', '')}".strip()
            body["allowed_mentions"] = {"parse": [], "roles": [ping_role]}
        else:
            body["allowed_mentions"] = {"parse": []}
        if self.dry_run:
            print(f"  [dry-run] as {body['username']!r}: " + _describe(body))
            self.sent_count += 1
            return "dry-run"
        url = webhook_url + ("&" if "?" in webhook_url else "?") + "wait=true"
        status, response = self._call(webhook_url, lambda: self._post(url, body))
        self.sent_count += 1
        try:
            return str(json.loads(response.decode("utf-8", "replace")).get("id") or "") or None
        except (ValueError, AttributeError):
            return None

    def edit(self, webhook_url: str, message_id: str, payload: dict) -> bool:
        """Edit a message this webhook sent. Returns False if the message no longer exists."""
        if self.dry_run:
            print(f"  [dry-run] edit message {message_id}: " + _describe(payload))
            return True
        url = f"{webhook_url.split('?')[0]}/messages/{message_id}"
        body = dict(payload, allowed_mentions={"parse": []})
        try:
            self._call(webhook_url, lambda: self._request(url, body, method="PATCH"))
        except WebhookError as exc:
            if "HTTP 404" in str(exc) or "Unknown Message" in str(exc):
                return False
            raise
        return True

    def delete(self, webhook_url: str, message_id: str) -> None:
        if self.dry_run:
            print(f"  [dry-run] delete message {message_id}")
            return
        url = f"{webhook_url.split('?')[0]}/messages/{message_id}"
        try:
            self._call(webhook_url, lambda: self._request(url, None, method="DELETE"))
        except WebhookError:
            pass  # already gone: nothing to do

    def _call(self, webhook_url: str, do) -> tuple[int, bytes]:
        self._pace(webhook_url)
        last_problem = "unknown error"
        for attempt in range(6):
            status, body, headers = do()
            self._last_sent[webhook_url] = time.monotonic()
            self._note_bucket(webhook_url, headers)
            if 200 <= status < 300:
                return status, body
            if status == 429:
                retry_after = _retry_after(body, headers)
                last_problem = f"rate limited ({retry_after:.1f}s)"
                self._sleep(retry_after)
                continue
            if status == 404 and b"10008" in body:  # Unknown Message (edit/delete of a removed message)
                raise WebhookError("HTTP 404: Unknown Message")
            if status in (401, 403, 404):
                raise WebhookError(f"webhook rejected with HTTP {status} (deleted or revoked?)", permanent=True)
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


def _describe(body: dict) -> str:
    parts = []
    if body.get("content"):
        parts.append(body["content"].replace("\n", " | "))
    for embed in body.get("embeds", []):
        parts.append("[card] " + (embed.get("title") or embed.get("description", "")[:80]).replace("\n", " "))
    return "\n    ".join(parts) if parts else "(empty)"


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
