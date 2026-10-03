"""HTTP helpers built on urllib, hardened for unattended runs.

Design notes
- Every request has a timeout and a response-size cap, so one slow or huge
  page can never hang or exhaust a run.
- Transient failures (timeouts, 5xx, 429) are retried with backoff; permanent
  ones (404, 410, 403 ...) fail fast so they are reported, not hammered.
- Conditional GET (ETag / Last-Modified) makes hourly polling cheap and polite.
"""

from __future__ import annotations

import gzip
import http.client
import json
import re
import socket
import ssl
import time
import urllib.error
import urllib.request
import zlib
from dataclasses import dataclass, field

USER_AGENT = (
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/126.0 Safari/537.36 research-feed-bot/1.0"
)
# Some firewalls reject browser-like agents that lack real browser fingerprints,
# others reject anything that is not a browser. On HTTP 403 the request is
# retried once with this honest feed-reader identity before giving up.
FEED_READER_AGENT = "research-feed-bot/1.0 (RSS reader; +https://github.com/topics/rss-reader)"
MAX_BYTES = 8 * 1024 * 1024
RETRYABLE_STATUS = {408, 425, 429, 500, 502, 503, 504, 520, 521, 522, 523, 524}
_NETWORK_ERRORS = (
    urllib.error.URLError,
    socket.timeout,
    TimeoutError,
    ConnectionError,
    ssl.SSLError,
    http.client.HTTPException,
    OSError,
)
_CHARSET_RE = re.compile(rb"""<meta[^>]+charset=["']?([A-Za-z0-9_\-]+)""", re.I)


class FetchError(Exception):
    def __init__(self, message: str, status: int | None = None):
        super().__init__(message)
        self.status = status


@dataclass
class Response:
    url: str
    status: int
    body: bytes
    headers: dict = field(default_factory=dict)

    def text(self) -> str:
        charset = None
        ctype = self.headers.get("content-type", "")
        m = re.search(r"charset=([\w\-]+)", ctype, re.I)
        if m:
            charset = m.group(1)
        if not charset:
            m2 = _CHARSET_RE.search(self.body[:4096])
            if m2:
                charset = m2.group(1).decode("ascii", "ignore")
        for enc in (charset, "utf-8", "latin-1"):
            if not enc:
                continue
            try:
                return self.body.decode(enc)
            except (LookupError, UnicodeDecodeError):
                continue
        return self.body.decode("utf-8", "replace")


def _decompress(raw: bytes, encoding: str | None) -> bytes:
    enc = (encoding or "").lower()
    try:
        if "gzip" in enc or raw[:2] == b"\x1f\x8b":
            return gzip.decompress(raw)
        if "deflate" in enc:
            try:
                return zlib.decompress(raw)
            except zlib.error:
                return zlib.decompress(raw, -zlib.MAX_WBITS)
    except (OSError, zlib.error, EOFError) as exc:
        raise FetchError(f"could not decompress response: {exc}") from None
    return raw


def _sleep_for_retry(attempt: int, retry_after: str | None) -> None:
    delay = (2, 6, 12)[min(attempt, 2)]
    if retry_after:
        try:
            delay = max(delay, min(float(retry_after), 30.0))
        except ValueError:
            pass
    time.sleep(delay)


def fetch(
    url: str,
    *,
    etag: str | None = None,
    last_modified: str | None = None,
    timeout: float = 25.0,
    attempts: int = 3,
) -> Response:
    headers = {
        "User-Agent": USER_AGENT,
        "Accept": (
            "application/rss+xml, application/atom+xml, application/xml;q=0.9, "
            "text/xml;q=0.9, text/html;q=0.8, */*;q=0.5"
        ),
        "Accept-Encoding": "gzip, deflate",
        "Accept-Language": "en-US,en;q=0.8",
    }
    if etag:
        headers["If-None-Match"] = etag
    if last_modified:
        headers["If-Modified-Since"] = last_modified

    tried_alternate_agent = False
    attempt = 0
    while attempt < attempts:
        request = urllib.request.Request(url, headers=headers)
        try:
            with urllib.request.urlopen(request, timeout=timeout) as resp:
                raw = resp.read(MAX_BYTES + 1)
                if len(raw) > MAX_BYTES:
                    raise FetchError("response larger than 8 MB")
                resp_headers = {k.lower(): v for k, v in resp.headers.items()}
                body = _decompress(raw, resp_headers.get("content-encoding"))
                return Response(resp.geturl(), resp.status, body, resp_headers)
        except urllib.error.HTTPError as exc:
            if exc.code == 304:
                return Response(url, 304, b"", {})
            if exc.code == 403 and not tried_alternate_agent:
                tried_alternate_agent = True
                headers = dict(headers, **{"User-Agent": FEED_READER_AGENT, "Accept": "*/*"})
                continue  # does not consume a retry attempt
            if exc.code in RETRYABLE_STATUS and attempt < attempts - 1:
                _sleep_for_retry(attempt, exc.headers.get("Retry-After") if exc.headers else None)
                attempt += 1
                continue
            raise FetchError(f"HTTP {exc.code}", status=exc.code) from None
        except FetchError:
            raise
        except _NETWORK_ERRORS as exc:
            if attempt < attempts - 1:
                _sleep_for_retry(attempt, None)
                attempt += 1
                continue
            reason = getattr(exc, "reason", exc)
            raise FetchError(f"network error: {reason}") from None
    raise FetchError("request failed")  # pragma: no cover (loop always returns/raises)


def post_json(url: str, payload: dict, timeout: float = 20.0) -> tuple[int, bytes, dict]:
    """POST JSON. Never raises for HTTP/network errors; status 0 means network failure."""
    data = json.dumps(payload).encode("utf-8")
    request = urllib.request.Request(
        url,
        data=data,
        headers={"Content-Type": "application/json", "User-Agent": USER_AGENT},
        method="POST",
    )
    try:
        with urllib.request.urlopen(request, timeout=timeout) as resp:
            return resp.status, resp.read(65536), {k.lower(): v for k, v in resp.headers.items()}
    except urllib.error.HTTPError as exc:
        body = b""
        try:
            body = exc.read(65536)
        except Exception:  # noqa: BLE001 - body is diagnostic only
            pass
        headers = {k.lower(): v for k, v in (exc.headers.items() if exc.headers else [])}
        return exc.code, body, headers
    except _NETWORK_ERRORS:
        return 0, b"", {}
