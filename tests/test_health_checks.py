"""The checks that exist because a real failure hid from every other one.

A hollowed-out partition satisfies `available_dates`, counts as cached by
`backfill(skip_existing=True)`, and lets the forecast serve on lag features
built from almost nothing. Nothing in the health check noticed until a chart
drew no line.
"""

from __future__ import annotations

import datetime as dt
import logging

import polars as pl
import pytest

from airshed import cli, store


def _obs(days: dict[dt.date, int]) -> pl.DataFrame:
    """One row per hour per day, `hours` of them."""
    rows = []
    for day, hours in days.items():
        for h in range(hours):
            rows.append(dt.datetime(day.year, day.month, day.day, h, tzinfo=dt.timezone.utc))
    return pl.DataFrame({"time": rows, "station_id": ["DL001"] * len(rows)})


def _week(hours_per_day: int) -> dict[dt.date, int]:
    today = dt.date.today()
    return {today - dt.timedelta(days=i): hours_per_day for i in range(1, 8)}


def test_full_days_report_clean(monkeypatch):
    monkeypatch.setattr(store, "read_range", lambda *a, **k: _obs(_week(24)))
    assert cli._report_observation_completeness() == 0


def test_a_hollowed_out_day_is_reported(monkeypatch):
    """The 2026-08-25 case: the day exists, and holds one hour of 24."""
    days = _week(24)
    victim = dt.date.today() - dt.timedelta(days=1)
    days[victim] = 1
    monkeypatch.setattr(store, "read_range", lambda *a, **k: _obs(days))
    assert cli._report_observation_completeness() == 1


def test_an_ordinary_station_dropout_is_not_an_alarm(monkeypatch):
    """23 h days are routine. An alarm that fires on those is one nobody reads."""
    days = _week(24)
    days[dt.date.today() - dt.timedelta(days=2)] = 23
    monkeypatch.setattr(store, "read_range", lambda *a, **k: _obs(days))
    assert cli._report_observation_completeness() == 0


def test_no_observations_at_all_fails_loudly(monkeypatch):
    monkeypatch.setattr(store, "read_range", lambda *a, **k: pl.DataFrame())
    assert cli._report_observation_completeness() == 2


# -- the write-side guard ---------------------------------------------------
def _frame(hours: int) -> pl.DataFrame:
    times = [
        dt.datetime(2024, 11, 1, h, tzinfo=dt.timezone.utc) for h in range(hours)
    ]
    return pl.DataFrame({"time": times, "value": [1.0] * hours})


def test_a_shrinking_replace_says_so(tmp_path, caplog):
    store.write_partitioned(_frame(24), "demo", base=tmp_path)
    with caplog.at_level(logging.WARNING, logger="airshed.store"):
        store.write_partitioned(_frame(2), "demo", base=tmp_path)
    assert any("smaller" in r.getMessage() for r in caplog.records), (
        "a replace that deletes most of a partition must not be silent"
    )


def test_a_normal_replace_is_quiet(tmp_path, caplog):
    """Rewriting a day with the same shape is the common case and must not warn."""
    store.write_partitioned(_frame(24), "demo", base=tmp_path)
    with caplog.at_level(logging.WARNING, logger="airshed.store"):
        store.write_partitioned(_frame(24), "demo", base=tmp_path)
    assert not caplog.records


def test_a_merging_write_does_not_warn(tmp_path, caplog):
    """Merging cannot lose rows, so the warning would be noise on every tick."""
    store.write_partitioned(_frame(24), "demo", base=tmp_path)
    with caplog.at_level(logging.WARNING, logger="airshed.store"):
        store.write_partitioned(
            _frame(2), "demo", base=tmp_path, merge_on=("time",)
        )
    assert not caplog.records
    assert store.read_range("demo", "2024-11-01", "2024-11-01", base=tmp_path).height == 24
