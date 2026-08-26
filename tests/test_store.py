"""Store correctness: UTC discipline, idempotency, honest gaps."""

from __future__ import annotations

import datetime as dt

import polars as pl
import pytest

from airshed import store


def _frame(n_hours: int = 48, value: float = 1.0) -> pl.DataFrame:
    times = pl.datetime_range(
        dt.datetime(2024, 11, 1, tzinfo=dt.timezone.utc),
        dt.datetime(2024, 11, 1, tzinfo=dt.timezone.utc) + dt.timedelta(hours=n_hours - 1),
        interval="1h",
        time_zone="UTC",
        eager=True,
    )
    return pl.DataFrame({"time": times, "value": [value] * n_hours})


def test_roundtrip_partitions_by_utc_date(tmp_path):
    paths = store.write_partitioned(_frame(48), "demo", base=tmp_path)
    assert len(paths) == 2  # 48 hours spans exactly two UTC days
    got = store.read_range("demo", "2024-11-01", "2024-11-02", base=tmp_path)
    assert got.height == 48
    assert got.schema["time"].time_zone == "UTC"


def test_rewrite_replaces_rather_than_appends(tmp_path):
    store.write_partitioned(_frame(24, value=1.0), "demo", base=tmp_path)
    store.write_partitioned(_frame(24, value=2.0), "demo", base=tmp_path)
    got = store.read_range("demo", "2024-11-01", "2024-11-01", base=tmp_path)
    assert got.height == 24, "re-running a fetch must not duplicate rows"
    assert got["value"].unique().to_list() == [2.0]


def test_naive_timestamps_are_rejected(tmp_path):
    naive = _frame(4).with_columns(pl.col("time").dt.replace_time_zone(None))
    with pytest.raises(TypeError):
        store.write_partitioned(naive, "demo", base=tmp_path)


def test_non_utc_timestamps_are_rejected(tmp_path):
    ist = _frame(4).with_columns(pl.col("time").dt.convert_time_zone("Asia/Kolkata"))
    with pytest.raises(TypeError):
        store.write_partitioned(ist, "demo", base=tmp_path)


def test_missing_days_are_absent_not_empty_rows(tmp_path):
    store.write_partitioned(_frame(24), "demo", base=tmp_path)
    got = store.read_range("demo", "2024-11-01", "2024-11-05", base=tmp_path)
    assert got.height == 24
    assert store.missing_dates("demo", "2024-11-01", "2024-11-05", base=tmp_path) == [
        dt.date(2024, 11, d) for d in (2, 3, 4, 5)
    ]


def test_partition_by_issue_date_keeps_a_run_in_one_file(tmp_path):
    df = _frame(72).with_columns(
        pl.lit(dt.datetime(2024, 11, 1, tzinfo=dt.timezone.utc))
        .cast(pl.Datetime("us", "UTC"))
        .alias("issue_time")
    )
    paths = store.write_partitioned(df, "runs", base=tmp_path, partition_col="issue_time")
    assert len(paths) == 1, "one forecast run must land in exactly one partition"


# -- partial writes ---------------------------------------------------------
def _hours(day: dt.date, hours: list[int], station: str = "DL001", value: float = 1.0):
    times = [
        dt.datetime(day.year, day.month, day.day, h, tzinfo=dt.timezone.utc)
        for h in hours
    ]
    return pl.DataFrame(
        {
            "time": times,
            "station_id": [station] * len(times),
            "value": [value] * len(times),
        }
    )


def test_a_partial_write_without_merge_still_replaces_the_day(tmp_path):
    """The existing contract, pinned so the merge option cannot change it.

    A backfill holds the whole day and must be able to replace it outright,
    including dropping a station that turned out to be bad.
    """
    day = dt.date(2024, 11, 1)
    store.write_partitioned(_hours(day, list(range(24))), "demo", base=tmp_path)
    store.write_partitioned(_hours(day, [23]), "demo", base=tmp_path)
    got = store.read_range("demo", day, day, base=tmp_path)
    assert got.height == 1


def test_a_rolling_window_write_keeps_the_hours_outside_it(tmp_path):
    """The bug this exists to stop, in miniature.

    The live sync fetches a rolling 12 h window every 30 minutes. Replacing the
    day wholesale meant each tick deleted whatever part of that day the window
    had rolled past: the store held 2026-08-25 complete in the morning and one
    hour of it by evening, and the dashboard's replay chart drew nothing because
    a single point is not a line.
    """
    day = dt.date(2024, 11, 1)
    store.write_partitioned(_hours(day, list(range(24))), "demo", base=tmp_path)
    store.write_partitioned(
        _hours(day, [22, 23]), "demo", base=tmp_path, merge_on=("station_id", "time")
    )
    got = store.read_range("demo", day, day, base=tmp_path)
    assert got.height == 24, "hours outside the window must survive the write"
    assert sorted(got["time"].dt.hour().to_list()) == list(range(24))


def test_a_merged_write_prefers_the_incoming_value(tmp_path):
    """OpenAQ revises provisional readings; the newer number has to win."""
    day = dt.date(2024, 11, 1)
    store.write_partitioned(_hours(day, [10], value=1.0), "demo", base=tmp_path)
    store.write_partitioned(
        _hours(day, [10], value=2.0), "demo", base=tmp_path,
        merge_on=("station_id", "time"),
    )
    got = store.read_range("demo", day, day, base=tmp_path)
    assert got.height == 1
    assert got["value"].to_list() == [2.0]


def test_a_merged_write_keeps_other_stations(tmp_path):
    """A window that returned only one station must not evict the others."""
    day = dt.date(2024, 11, 1)
    store.write_partitioned(
        pl.concat([_hours(day, [5], "DL001"), _hours(day, [5], "DL002")]),
        "demo", base=tmp_path,
    )
    store.write_partitioned(
        _hours(day, [5], "DL001", value=9.0), "demo", base=tmp_path,
        merge_on=("station_id", "time"),
    )
    got = store.read_range("demo", day, day, base=tmp_path)
    assert sorted(got["station_id"].to_list()) == ["DL001", "DL002"]
    assert got.filter(pl.col("station_id") == "DL001")["value"].to_list() == [9.0]
