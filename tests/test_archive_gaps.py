"""Tests for how the ground-truth archive handles failure versus absence.

R6 says a station gap is missing data, not something to interpolate. The
corollary this file guards is subtler: *our* inability to fetch must never be
recorded as the station's absence. They look identical downstream — both produce
no rows — and only one of them is a fact about Delhi's air.

The failure that motivated these tests: a partially-failed day wrote a partition
containing whatever happened to succeed. Every later backfill saw a partition on
disk, treated the day as cached and complete, and never asked again. A transient
network problem became permanent invisible data loss.
"""

from __future__ import annotations

import datetime as dt

import httpx
import polars as pl
import pytest

from airshed.ingest import cpcb


class _Resp:
    def __init__(self, status_code, content=b""):
        self.status_code = status_code
        self.content = content


class _Client:
    """Fake archive client with a scripted sequence of outcomes per key."""

    def __init__(self, script):
        self.script = script
        self.calls = []

    def get(self, url):
        self.calls.append(url)
        outcome = self.script.pop(0) if self.script else _Resp(404)
        if isinstance(outcome, Exception):
            raise outcome
        return outcome


class _Station:
    id = "DL001"
    openaq_id = 235


DAY = dt.date(2026, 1, 15)


# -- failure vs absence ----------------------------------------------------
def test_a_404_is_a_real_gap_and_stays_a_gap():
    # The station was offline. That is data about Delhi, and must not be
    # retried forever or reported as our failure.
    client = _Client([_Resp(404)])
    out = cpcb._fetch_day(client, "https://archive", _Station(), DAY)
    assert out is None
    assert len(client.calls) == 1, "a 404 must not be retried"


def test_a_transport_failure_is_retried_then_reported_as_a_failure():
    client = _Client([httpx.ConnectTimeout("tls"), httpx.ConnectTimeout("tls"),
                      httpx.ConnectTimeout("tls")])
    out = cpcb._fetch_day(client, "https://archive", _Station(), DAY)
    assert out is cpcb.FETCH_FAILED
    assert len(client.calls) == cpcb.ARCHIVE_RETRIES


def test_a_transient_failure_that_recovers_returns_data():
    """One SSL timeout used to cost the whole station-day."""
    csv = b"datetime,parameter,value\n2026-01-15T00:00:00+05:30,pm25,120\n"
    import gzip

    client = _Client([httpx.ConnectTimeout("tls"), _Resp(200, gzip.compress(csv))])
    out = cpcb._fetch_day(client, "https://archive", _Station(), DAY)
    assert out is not None and out is not cpcb.FETCH_FAILED
    assert out.height == 1


def test_a_server_error_counts_as_failure_not_absence():
    # A 5xx is the archive having a bad day, not the station being offline.
    client = _Client([_Resp(503)])
    out = cpcb._fetch_day(client, "https://archive", _Station(), DAY)
    assert out is cpcb.FETCH_FAILED


# -- refusing to persist a crippled day ------------------------------------
def test_backfill_refuses_to_write_when_most_fetches_failed(monkeypatch, caplog):
    """The important one. A written partition is treated as complete forever."""
    written = []
    monkeypatch.setattr(
        cpcb, "fetch_archive",
        lambda *a, **k: pl.DataFrame({"station_id": ["DL001"], "time": [1], "pm25": [5.0]}),
    )
    cpcb.fetch_archive.last_failure_rate = 0.9
    monkeypatch.setattr(cpcb, "write_partitioned", lambda df, ds, **k: written.append(ds) or [ds])
    monkeypatch.setattr(cpcb, "missing_dates", lambda *a, **k: [DAY])

    with caplog.at_level("ERROR"):
        cpcb.backfill("2026-01-15", "2026-01-15", skip_existing=False)
    assert written == [], "a 90%-failed day must not be persisted"
    assert "NOT writing" in caplog.text


def test_backfill_writes_when_failures_are_tolerable(monkeypatch):
    written = []
    monkeypatch.setattr(
        cpcb, "fetch_archive",
        lambda *a, **k: pl.DataFrame({"station_id": ["DL001"], "time": [1], "pm25": [5.0]}),
    )
    cpcb.fetch_archive.last_failure_rate = 0.01
    monkeypatch.setattr(cpcb, "write_partitioned", lambda df, ds, **k: written.append(ds) or [ds])
    monkeypatch.setattr(cpcb, "missing_dates", lambda *a, **k: [DAY])

    cpcb.backfill("2026-01-15", "2026-01-15", skip_existing=False)
    assert written, "a healthy fetch must still be written"


def test_failure_threshold_is_not_so_loose_it_lets_a_broken_day_through():
    # A quarter of stations missing already distorts a city average badly.
    assert 0 < cpcb.MAX_FETCH_FAILURE_RATE <= 0.5
