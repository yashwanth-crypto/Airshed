"""Feature-building correctness.

These tests exist because misalignment and leakage are silent: the pipeline
runs, the numbers look plausible, and the model is wrong. Each test below
corresponds to a specific way that has happened to real projects.
"""

from __future__ import annotations

import datetime as dt

import polars as pl

from airshed.features import build as feat

UTC = dt.timezone.utc


def _base(n_hours: int = 200, stations=("A", "B")) -> pl.DataFrame:
    times = pl.datetime_range(
        dt.datetime(2024, 11, 1, tzinfo=UTC),
        dt.datetime(2024, 11, 1, tzinfo=UTC) + dt.timedelta(hours=n_hours - 1),
        interval="1h",
        time_zone="UTC",
        eager=True,
    )
    frames = []
    for i, s in enumerate(stations):
        frames.append(
            pl.DataFrame(
                {
                    "station_id": [s] * n_hours,
                    "time": times,
                    "pm25_clean": [float(h + i * 1000) for h in range(n_hours)],
                    "cams_pm2_5": [float(h) * 0.5 for h in range(n_hours)],
                }
            )
        )
    return pl.concat(frames).sort(["station_id", "time"])


def test_hourly_index_is_complete_and_unique():
    idx = feat.hourly_index("2024-11-01", "2024-11-07", ["A", "B", "C"])
    assert idx.height == 3 * 24 * 7
    assert idx.select(["station_id", "time"]).unique().height == idx.height
    assert idx.schema["time"].time_zone == "UTC"


def test_index_is_end_inclusive():
    idx = feat.hourly_index("2024-11-01", "2024-11-01", ["A"])
    assert idx.height == 24
    assert idx["time"].max() == dt.datetime(2024, 11, 1, 23, tzinfo=UTC)


def test_lag_is_exactly_k_hours_not_k_rows():
    """The bug this guards: shifting a gappy frame means 'k rows', not 'k hours'."""
    df = feat._add_observation_features(_base())
    row = df.filter((pl.col("station_id") == "A") & (pl.col("time") == dt.datetime(2024, 11, 2, 12, tzinfo=UTC)))
    # value at t equals hours since 2024-11-01T00; lag 24 must be exactly 24 less
    assert row["pm25_clean"].item() - row["obs_lag_24h"].item() == 24.0


def test_lags_stay_null_across_a_gap_instead_of_reaching_further_back():
    base = _base()
    holed = base.with_columns(
        pl.when(pl.col("time").dt.hour().is_between(6, 17))
        .then(None)
        .otherwise(pl.col("pm25_clean"))
        .alias("pm25_clean")
    )
    df = feat._add_observation_features(holed)
    missing = df.filter(pl.col("pm25_clean").is_null())
    # A lag pointing into the hole must be null, never the nearest live value.
    assert missing["obs_lag_1h"].null_count() > 0
    lagged = df.filter(pl.col("time") == dt.datetime(2024, 11, 2, 7, tzinfo=UTC))
    assert lagged.filter(pl.col("station_id") == "A")["obs_lag_1h"].item() is None


def test_rolling_window_refuses_to_bridge_a_long_outage():
    base = _base()
    outage = base.with_columns(
        pl.when(pl.col("time") > dt.datetime(2024, 11, 2, 0, tzinfo=UTC))
        .then(None)
        .otherwise(pl.col("pm25_clean"))
        .alias("pm25_clean")
    )
    df = feat._add_observation_features(outage)
    late = df.filter(pl.col("time") == dt.datetime(2024, 11, 3, 12, tzinfo=UTC))
    assert late["obs_mean_24h"].to_list() == [None, None]


def test_obs_gap_counts_hours_since_last_real_observation():
    base = _base()
    holed = base.with_columns(
        pl.when(pl.col("time") > dt.datetime(2024, 11, 2, 0, tzinfo=UTC))
        .then(None)
        .otherwise(pl.col("pm25_clean"))
        .alias("pm25_clean")
    )
    df = feat._add_observation_features(holed)
    row = df.filter(
        (pl.col("station_id") == "A") & (pl.col("time") == dt.datetime(2024, 11, 2, 5, tzinfo=UTC))
    )
    assert row["obs_gap_h"].item() == 5


def test_lags_do_not_leak_across_stations():
    df = feat._add_observation_features(_base(n_hours=50))
    first_b = df.filter(pl.col("station_id") == "B").head(1)
    assert first_b["obs_lag_1h"].item() is None, "station B must not lag into station A"


def test_calendar_features_use_ist_not_utc():
    df = feat._add_calendar_features(_base(n_hours=24, stations=("A",)))
    midnight_utc = df.filter(pl.col("time") == dt.datetime(2024, 11, 1, 0, tzinfo=UTC))
    # 00:00 UTC is 05:30 IST — the hour must read 5, not 0.
    assert midnight_utc["hour_ist"].item() == 5


def test_supervised_target_is_the_observation_h_hours_later():
    base = feat._add_calendar_features(feat._add_observation_features(_base()))
    sup = feat.build_supervised(base, horizons=[24, 48, 72])
    for h in (24, 48, 72):
        row = sup.filter(
            (pl.col("station_id") == "A")
            & (pl.col("horizon_h") == h)
            & (pl.col("issue_time") == dt.datetime(2024, 11, 2, 0, tzinfo=UTC))
        )
        assert row.height == 1
        assert row["target_time"].item() == dt.datetime(2024, 11, 2, tzinfo=UTC) + dt.timedelta(hours=h)
        # pm25_clean encodes hours since the series start, so the target must
        # be exactly h greater than the value at issue time.
        assert row["y"].item() - row["pm25_clean"].item() == float(h)


def test_forecast_columns_are_read_at_target_time_observations_at_issue_time():
    base = feat._add_calendar_features(feat._add_observation_features(_base()))
    sup = feat.build_supervised(base, horizons=[24])
    row = sup.filter(
        (pl.col("station_id") == "A")
        & (pl.col("issue_time") == dt.datetime(2024, 11, 2, 0, tzinfo=UTC))
    )
    # CAMS is a forecast: its value must come from the target hour.
    assert row["cams_pm2_5_tgt"].item() - row["cams_pm2_5"].item() == 12.0  # 24 h * 0.5
    # Observation history must stay pinned to issue time.
    assert row["pm25_clean"].item() == row["obs_lag_24h"].item() + 24.0


def test_rows_with_a_missing_target_are_dropped_not_zero_filled():
    base = _base()
    base = base.with_columns(
        pl.when(pl.col("time") > dt.datetime(2024, 11, 5, 0, tzinfo=UTC))
        .then(None)
        .otherwise(pl.col("pm25_clean"))
        .alias("pm25_clean")
    )
    base = feat._add_calendar_features(feat._add_observation_features(base))
    sup = feat.build_supervised(base, horizons=[24])
    assert sup["y"].null_count() == 0
    assert sup["target_time"].max() <= dt.datetime(2024, 11, 5, 0, tzinfo=UTC)


def test_no_duplicate_rows_in_supervised_table():
    base = feat._add_calendar_features(feat._add_observation_features(_base()))
    sup = feat.build_supervised(base, horizons=[24, 48, 72])
    keys = sup.select(["station_id", "issue_time", "horizon_h"])
    assert keys.unique().height == sup.height
