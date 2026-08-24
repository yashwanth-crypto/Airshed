"""HTTP access shared by the ingest modules.

Only `ingest/` may import this. Everything else reads the local store (R8).
"""

from __future__ import annotations

import logging
import time
from typing import Any

import httpx
from tenacity import (
    retry,
    retry_if_exception_type,
    stop_after_attempt,
    wait_exponential,
)

log = logging.getLogger(__name__)

USER_AGENT = "airshed/0.1 (SIH26082 research prototype)"
DEFAULT_TIMEOUT = httpx.Timeout(60.0, connect=20.0)

RETRYABLE = (httpx.TransportError, httpx.HTTPStatusError)


class RateLimiter:
    """Minimum spacing between calls. The free endpoints are a shared resource."""

    def __init__(self, min_interval_s: float = 0.35) -> None:
        self.min_interval_s = min_interval_s
        self._last = 0.0

    def wait(self) -> None:
        gap = time.monotonic() - self._last
        if gap < self.min_interval_s:
            time.sleep(self.min_interval_s - gap)
        self._last = time.monotonic()


_limiter = RateLimiter()

# Per-host minimum spacing. Open-Meteo tolerates a brisk pace; OpenAQ's free
# tier allows roughly 60 requests a minute and answers 429 above that, so it
# gets its own slower limiter rather than dragging every other source down.
_HOST_INTERVALS = {
    "api.openaq.org": 1.1,
}
_host_limiters: dict[str, RateLimiter] = {}


def _limiter_for(url: str) -> RateLimiter:
    from urllib.parse import urlparse

    host = urlparse(url).netloc
    interval = _HOST_INTERVALS.get(host)
    if interval is None:
        return _limiter
    if host not in _host_limiters:
        _host_limiters[host] = RateLimiter(interval)
    return _host_limiters[host]


@retry(
    retry=retry_if_exception_type(RETRYABLE),
    wait=wait_exponential(multiplier=2, min=2, max=60),
    stop=stop_after_attempt(5),
    reraise=True,
)
def get(
    url: str,
    params: dict[str, Any] | None = None,
    headers: dict[str, str] | None = None,
    timeout: httpx.Timeout | None = None,
) -> httpx.Response:
    """GET with backoff. 4xx other than 429 fail fast — they are our bug."""
    _limiter_for(url).wait()
    hdrs = {"User-Agent": USER_AGENT, **(headers or {})}
    resp = httpx.get(url, params=params, headers=hdrs, timeout=timeout or DEFAULT_TIMEOUT)
    if resp.status_code == 429 or resp.status_code >= 500:
        log.warning("retryable %s from %s", resp.status_code, url)
        resp.raise_for_status()
    if resp.status_code >= 400:
        raise httpx.HTTPError(
            f"{resp.status_code} from {url}: {resp.text[:300]}"
        )
    return resp


def get_json(
    url: str,
    params: dict[str, Any] | None = None,
    headers: dict[str, str] | None = None,
) -> Any:
    return get(url, params=params, headers=headers).json()


def get_text(
    url: str,
    params: dict[str, Any] | None = None,
    headers: dict[str, str] | None = None,
) -> str:
    return get(url, params=params, headers=headers).text
