"""Timestamp parsing across sources.

Every source stamps time differently, and a mismatch here does not raise — it
produces nulls that a later drop silently removes. The live OpenAQ feed
vanished exactly this way: the S3 archive writes "+05:30", the v3 API writes
"Z", and one format string read only the first.
"""

from __future__ import annotations

import datetime as dt

import polars as pl

from airshed.ingest import cpcb

UTC = dt.timezone.utc


def _raw(stamps: list[str]) -> pl.DataFrame:
    return pl.DataFrame(
        {
            "datetime": stamps,
            "value": [100.0] * len(stamps),
            "station_id": ["DL001"] * len(stamps),
        }
    )


def test_ist_offset_stamps_from_the_bulk_archive_parse():
    out = cpcb._to_hourly(_raw(["2025-11-01T00:45:00+05:30"]))
    assert out.height == 1
    # 00:45 IST is 19:15 UTC the previous day, floored to the UTC hour.
    assert out["time"][0] == dt.datetime(2025, 10, 31, 19, 0, tzinfo=UTC)


def test_zulu_stamps_from_the_live_api_parse():
    out = cpcb._to_hourly(_raw(["2026-08-24T03:30:00Z"]))
    assert out.height == 1
    assert out["time"][0] == dt.datetime(2026, 8, 24, 3, 0, tzinfo=UTC)


def test_both_formats_together_produce_one_frame():
    """The live sync concatenates archive-shaped and API-shaped rows."""
    out = cpcb._to_hourly(_raw(["2026-08-24T03:30:00Z", "2026-08-24T09:45:00+05:30"]))
    assert out.height == 2, "a format mismatch would silently drop one of these"


def test_an_unparseable_stamp_does_not_take_the_others_with_it():
    out = cpcb._to_hourly(_raw(["2026-08-24T03:30:00Z", "not a timestamp"]))
    assert out.height == 1


# ---------------------------------------------------------------------------
# FIRMS: an all-failed fetch must not look like an out-of-season empty
# ---------------------------------------------------------------------------
import pytest  # noqa: E402

from airshed.ingest import fires  # noqa: E402


def test_day_range_respects_the_documented_cap():
    """FIRMS answers 400 "Expects [1..5]" above five days."""
    assert fires.MAX_DAY_RANGE <= 5


def test_every_request_failing_raises_rather_than_reporting_no_fires(monkeypatch):
    """The dangerous case: a bad key looks exactly like a quiet season."""
    monkeypatch.setenv("FIRMS_MAP_KEY", "dummy")

    def boom(*args, **kwargs):
        raise RuntimeError("400 Bad Request")

    monkeypatch.setattr(fires, "get_text", boom)
    with pytest.raises(RuntimeError, match="every FIRMS request failed"):
        fires.fetch("2025-11-05", "2025-11-08")


def test_a_genuinely_empty_season_returns_empty_without_raising(monkeypatch):
    monkeypatch.setenv("FIRMS_MAP_KEY", "dummy")
    monkeypatch.setattr(fires, "get_text", lambda *a, **k: "latitude,longitude\n")
    out = fires.fetch("2026-06-05", "2026-06-08")
    assert out.is_empty()


def test_the_map_key_is_redacted_from_messages():
    assert "secret123" not in fires._redact("failed for key secret123", "secret123")


def test_product_family_is_chosen_per_chunk_not_per_range(monkeypatch):
    """A backfill spanning the NRT cutoff must not ask NRT for old data.

    This is how the November 2025 stubble peak went missing: the range ended
    today, so every chunk — including year-old ones — was requested from the
    near-real-time products, which do not retain that far back. The result was
    an empty return that looked like a quiet season.
    """
    monkeypatch.setenv("FIRMS_MAP_KEY", "dummy")
    asked: list[str] = []

    def record(url, *args, **kwargs):
        asked.append(url)
        return "latitude,longitude\n"

    monkeypatch.setattr(fires, "get_text", record)
    # A range whose end is today but whose start is a year back.
    today = dt.date.today()
    fires.fetch(today - dt.timedelta(days=330), today)

    old_chunk = [u for u in asked if str(today - dt.timedelta(days=330)) in u]
    assert old_chunk, "the oldest chunk was never requested"
    assert any("_SP" in u for u in old_chunk), (
        "year-old data must be requested from the standard-processing archive"
    )
    recent = [u for u in asked if str(today - dt.timedelta(days=2)) in u]
    if recent:
        assert any("NRT" in u for u in recent), "recent data should use NRT"


def test_chunks_never_exceed_the_api_day_cap():
    spans = [span for _start, span in fires._chunks(dt.date(2025, 11, 1), dt.date(2025, 11, 30))]
    assert spans and max(spans) <= fires.MAX_DAY_RANGE
    assert sum(spans) == 30, "chunks must tile the range exactly, with no gaps"
