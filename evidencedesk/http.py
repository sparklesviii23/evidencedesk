"""Shared HTTP client: per-host rate limits, retries with exponential backoff, Retry-After, redacted errors."""
from __future__ import annotations

import logging
import random
import threading
import time
from dataclasses import dataclass

import httpx

from . import __version__
from .credentials import get, redact

log = logging.getLogger("evidencedesk.http")

USER_AGENT = f"EvidenceDesk/{__version__} (local systematic-review workbench)"


class SourceError(Exception):
    def __init__(self, message: str, kind: str = "error", http_status: int | None = None, retryable=False):
        super().__init__(redact(message))
        self.kind = kind  # error | auth | access | rate_limit | network | not_found | bad_query
        self.http_status = http_status
        self.retryable = retryable


class RateLimiter:
    def __init__(self):
        self._next: dict[str, float] = {}
        self._lock = threading.Lock()

    def wait(self, key: str, per_sec: float) -> None:
        with self._lock:
            t = time.monotonic()
            nxt = self._next.get(key, 0.0)
            delay = max(0.0, nxt - t)
            self._next[key] = max(t, nxt) + 1.0 / per_sec
        if delay:
            time.sleep(delay)


_limiter = RateLimiter()
_client: httpx.Client | None = None
_client_lock = threading.Lock()


def client() -> httpx.Client:
    global _client
    with _client_lock:
        if _client is None:
            email = get("contact", "email")
            ua = USER_AGENT + (f" mailto:{email}" if email else "")
            _client = httpx.Client(timeout=httpx.Timeout(60.0, connect=20.0), follow_redirects=True,
                                   headers={"User-Agent": ua})
        return _client


def reset_client():
    global _client
    with _client_lock:
        if _client is not None:
            _client.close()
        _client = None


@dataclass
class RetryPolicy:
    attempts: int = 5
    base: float = 1.5
    cap: float = 60.0


def request(method: str, url: str, *, source: str, per_sec: float = 3.0, policy: RetryPolicy | None = None,
            on_retry=None, expect_json: bool = False, **kw) -> httpx.Response:
    policy = policy or RetryPolicy()
    last: Exception | None = None
    for attempt in range(1, policy.attempts + 1):
        _limiter.wait(source, per_sec)
        try:
            r = client().request(method, url, **kw)
        except (httpx.TimeoutException, httpx.NetworkError, httpx.RemoteProtocolError) as e:
            last = SourceError(f"{type(e).__name__}: {e}", "network", retryable=True)
        else:
            if r.status_code < 400:
                if expect_json:
                    try:
                        r.json()
                    except ValueError:
                        last = SourceError(f"Invalid JSON from {source} (HTTP {r.status_code})", "error",
                                           r.status_code, retryable=True)
                        _sleep(attempt, policy, None, on_retry, source, last)
                        continue
                return r
            body = r.text[:400]
            if r.status_code in (401, 403):
                raise SourceError(f"HTTP {r.status_code} from {source}: {body}",
                                  "auth" if r.status_code == 401 else "access", r.status_code)
            if r.status_code == 404:
                raise SourceError(f"HTTP 404 from {source}: {body}", "not_found", 404)
            if r.status_code == 400:
                raise SourceError(f"HTTP 400 from {source} (query rejected): {body}", "bad_query", 400)
            if r.status_code == 429 or r.status_code >= 500:
                last = SourceError(f"HTTP {r.status_code} from {source}: {body}",
                                   "rate_limit" if r.status_code == 429 else "error", r.status_code, True)
                _sleep(attempt, policy, r.headers.get("Retry-After"), on_retry, source, last)
                continue
            raise SourceError(f"HTTP {r.status_code} from {source}: {body}", "error", r.status_code)
        _sleep(attempt, policy, None, on_retry, source, last)
    assert last is not None
    raise last


def _sleep(attempt, policy, retry_after, on_retry, source, err):
    if attempt >= policy.attempts:
        return
    try:
        delay = float(retry_after) if retry_after else None
    except ValueError:
        delay = None
    if delay is None:
        delay = min(policy.cap, policy.base ** attempt) + random.uniform(0, 0.5)
    log.warning("retry %s attempt %d in %.1fs: %s", source, attempt, delay, err)
    if on_retry:
        on_retry(attempt, str(err))
    time.sleep(min(delay, policy.cap))


def download(url: str, *, source: str, per_sec: float = 2.0, max_bytes: int = 150_000_000, deadline: float = 180.0,
             headers: dict | None = None) -> tuple[int, dict, bytes]:
    """Streamed GET with a total wall-clock deadline and size cap (a trickling server cannot hang a worker)."""
    _limiter.wait(source, per_sec)
    t0 = time.monotonic()
    try:
        with client().stream("GET", url, headers=headers or {}) as r:
            if r.status_code in (401, 403):
                body = r.read()[:400].decode("utf-8", "replace")
                raise SourceError(f"HTTP {r.status_code} from {source}: {body}", "access", r.status_code)
            if r.status_code == 404:
                raise SourceError(f"HTTP 404 from {source}", "not_found", 404)
            if r.status_code >= 400:
                raise SourceError(f"HTTP {r.status_code} from {source}", "error", r.status_code)
            buf = bytearray()
            for chunk in r.iter_bytes():
                buf += chunk
                if len(buf) > max_bytes:
                    raise SourceError(f"{source}: file exceeds {max_bytes} bytes", "error", r.status_code)
                if time.monotonic() - t0 > deadline:
                    raise SourceError(f"{source}: download exceeded {deadline:.0f}s", "network", r.status_code)
            return r.status_code, dict(r.headers), bytes(buf)
    except (httpx.TimeoutException, httpx.NetworkError, httpx.RemoteProtocolError) as e:
        raise SourceError(f"{type(e).__name__}: {e}", "network")
