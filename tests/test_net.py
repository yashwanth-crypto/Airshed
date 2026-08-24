"""Tests for the HTTP layer's failure handling.

The distinction that matters here is between a 429 meaning "slow down" and a 429
meaning "come back tomorrow". They are the same status code and want opposite
handling, and getting it wrong turns a clear stop into five rounds of
exponential backoff against a quota that cannot recover today.
"""

from __future__ import annotations

import httpx
import pytest

from airshed import net


class _Resp:
    def __init__(self, status_code: int, text: str = ""):
        self.status_code = status_code
        self.text = text

    def raise_for_status(self):
        if self.status_code >= 400:
            raise httpx.HTTPStatusError(
                f"{self.status_code}", request=None, response=None
            )


@pytest.fixture(autouse=True)
def _no_sleep(monkeypatch):
    monkeypatch.setattr(
        net, "_limiter_for", lambda url: type("L", (), {"wait": lambda s: None})()
    )
    # tenacity's backoff is baked into the decorator at import time, so patching
    # `wait_exponential` in the module does nothing. Replace the Retrying
    # object's sleep instead, or the burst-retry test below really does wait
    # about half a minute.
    monkeypatch.setattr(net.get.retry, "sleep", lambda _s: None)


def test_daily_quota_429_is_not_retried(monkeypatch):
    calls = []

    def fake_get(url, **kw):
        calls.append(url)
        return _Resp(429, '{"error":true,"reason":"Daily API request limit exceeded. Please try again tomorrow."}')

    monkeypatch.setattr(net.httpx, "get", fake_get)
    with pytest.raises(net.DailyQuotaExceeded):
        net.get("https://previous-runs-api.open-meteo.com/v1/forecast")
    # Exactly one attempt: backing off against a spent daily allowance only
    # makes the failure slower, never more likely to succeed.
    assert len(calls) == 1


def test_burst_429_is_still_retried(monkeypatch):
    calls = []

    def fake_get(url, **kw):
        calls.append(url)
        return _Resp(429, "too many requests, slow down")

    monkeypatch.setattr(net.httpx, "get", fake_get)
    with pytest.raises(httpx.HTTPStatusError):
        net.get("https://api.example.com/thing")
    assert len(calls) > 1


def test_quota_message_names_the_host(monkeypatch):
    monkeypatch.setattr(
        net.httpx, "get",
        lambda url, **kw: _Resp(429, "Daily limit reached"),
    )
    with pytest.raises(net.DailyQuotaExceeded) as exc:
        net.get("https://air-quality-api.open-meteo.com/v1/air-quality")
    # Which provider ran out is the first thing you need to know, and this
    # project talks to four of them.
    assert "air-quality-api.open-meteo.com" in str(exc.value)


@pytest.mark.parametrize(
    "body,expected",
    [
        ('{"reason":"Daily API request limit exceeded."}', True),
        ("Daily limit reached", True),
        ("please try again tomorrow", True),
        ("rate limited, retry in 2s", False),
        ("", False),
    ],
)
def test_quota_detection(body, expected):
    assert net._is_daily_quota(_Resp(429, body)) is expected
