"""AQI and GRAP mapping against the published CPCB / CAQM tables."""

from __future__ import annotations

import datetime as dt

import polars as pl

from airshed import grap

UTC = dt.timezone.utc


def _aqi(values: list[float]) -> list[float | None]:
    df = pl.DataFrame({"c": values})
    return df.select(grap.pm25_to_aqi(pl.col("c")).alias("aqi"))["aqi"].to_list()


def test_breakpoint_boundaries_match_the_cpcb_table():
    # (24-hour PM2.5 in ug/m3, expected AQI) straight off the CPCB table.
    assert _aqi([0, 30, 60, 90, 120, 250]) == [0, 50, 100, 200, 300, 400]


def test_index_is_linear_inside_a_bracket():
    # Midpoint of the 30-60 bracket. CPCB prints the bracket as 31-60 -> 51-100;
    # we use continuous breakpoints (30-60 -> 51-100), so the midpoint is 75.5
    # and rounds to 76. Documented because the half-unit offset is exactly the
    # kind of thing that later looks like a bug.
    assert _aqi([45]) == [76]


def test_aqi_is_clamped_at_500_not_extrapolated():
    assert _aqi([380, 600, 2000]) == [500, 500, 500]


def test_stage_thresholds_match_the_caqm_schedule():
    df = pl.DataFrame({"aqi": [150.0, 201.0, 300.0, 301.0, 400.0, 401.0, 450.0, 451.0, 499.0]})
    stages = df.select(grap.aqi_to_stage(pl.col("aqi")).alias("s"))["s"].to_list()
    assert stages == [0, 1, 1, 2, 2, 3, 3, 4, 4]


def test_null_aqi_gives_null_stage_not_stage_zero():
    df = pl.DataFrame({"aqi": [None]}, schema={"aqi": pl.Float64})
    assert df.select(grap.aqi_to_stage(pl.col("aqi")).alias("s"))["s"].to_list() == [None]


def _hourly(n: int, value: float, station: str = "A") -> pl.DataFrame:
    times = pl.datetime_range(
        dt.datetime(2024, 11, 1, tzinfo=UTC),
        dt.datetime(2024, 11, 1, tzinfo=UTC) + dt.timedelta(hours=n - 1),
        interval="1h",
        time_zone="UTC",
        eager=True,
    )
    return pl.DataFrame(
        {"station_id": [station] * n, "time": times, "pm25_clean": [value] * n}
    )


def test_aqi_uses_a_24_hour_average_not_the_current_hour():
    df = grap.add_rolling_aqi(_hourly(24, 300.0))
    # The first hours have too few observations to form a 24 h mean.
    assert df["aqi_pm25"][0] is None
    assert df["aqi_pm25"][23] is not None


def test_a_station_reporting_a_few_hours_gets_no_aqi():
    sparse = _hourly(24, 300.0).with_columns(
        pl.when(pl.col("time").dt.hour() < 20)
        .then(None)
        .otherwise(pl.col("pm25_clean"))
        .alias("pm25_clean")
    )
    df = grap.add_rolling_aqi(sparse)
    assert df["aqi_pm25"].drop_nulls().is_empty()


def test_city_aqi_needs_a_quorum_of_stations():
    frames = [_hourly(30, 300.0, station=s) for s in ("A", "B", "C")]
    df = grap.add_rolling_aqi(pl.concat(frames))
    city = grap.city_aqi(df, min_stations=5)
    assert city["city_aqi"].drop_nulls().is_empty(), "3 stations must not stand in for a city"
    city_ok = grap.city_aqi(df, min_stations=3)
    assert not city_ok["city_aqi"].drop_nulls().is_empty()


# ---------------------------------------------------------------------------
# Phase 3: distribution -> stage probability
# ---------------------------------------------------------------------------
import numpy as np  # noqa: E402

from airshed.models.base import Quantiles  # noqa: E402


def test_aqi_to_pm25_inverts_pm25_to_aqi():
    for concentration in (15.0, 45.0, 75.0, 105.0, 200.0, 300.0):
        aqi = _aqi([concentration])[0]
        back = grap.aqi_to_pm25(aqi)
        assert abs(back - concentration) < 2.0, concentration


def test_stage_bounds_are_ordered_and_match_the_schedule():
    bounds = grap.stage_bounds()
    assert [s for s, _n, _lo, _hi in bounds] == [1, 2, 3, 4]
    lows = [lo for _s, _n, lo, _hi in bounds]
    assert lows == sorted(lows)
    # Stage III starts at the Severe threshold, AQI 401 -> 250 ug/m3.
    assert abs(bounds[2][2] - 250.0) < 1.0


def _q(q10, q50, q90) -> Quantiles:
    return Quantiles(np.array([q10]), np.array([q50]), np.array([q90]))


def test_cdf_is_monotone_and_bounded():
    q = _q(80.0, 150.0, 300.0)
    previous = -1.0
    for x in (10, 80, 120, 150, 200, 300, 500, 900):
        p = grap._cdf(q.q10, q.q50, q.q90, float(x))[0]
        assert 0.0 <= p <= 1.0
        assert p >= previous - 1e-9, f"CDF decreased at {x}"
        previous = p


def test_cdf_matches_the_quantiles_it_was_built_from():
    q = _q(80.0, 150.0, 300.0)
    assert abs(grap._cdf(q.q10, q.q50, q.q90, 80.0)[0] - 0.10) < 1e-6
    assert abs(grap._cdf(q.q10, q.q50, q.q90, 150.0)[0] - 0.50) < 1e-6
    assert abs(grap._cdf(q.q10, q.q50, q.q90, 300.0)[0] - 0.90) < 1e-6


def test_stage_probabilities_are_a_distribution():
    probs = grap.stage_probabilities(_q(80.0, 150.0, 300.0))
    total = sum(probs[f"p_stage_{s}"][0] for s in range(5))
    assert 0.85 <= total <= 1.0  # above the top bracket is not a stage


def test_at_least_probabilities_decrease_with_severity():
    probs = grap.stage_probabilities(_q(80.0, 150.0, 300.0))
    values = [probs[f"p_at_least_{s}"][0] for s in (1, 2, 3, 4)]
    assert values == sorted(values, reverse=True)


def test_a_clean_forecast_gives_almost_no_severe_probability():
    probs = grap.stage_probabilities(_q(10.0, 20.0, 35.0))
    assert probs["p_at_least_3"][0] < 0.01
    assert probs["p_stage_0"][0] > 0.9


def test_a_severe_forecast_is_confident_about_severe():
    probs = grap.stage_probabilities(_q(260.0, 300.0, 360.0))
    assert probs["p_at_least_3"][0] > 0.9


def test_a_wide_interval_spreads_probability_across_stages():
    """Uncertainty must show up as spread, not as a confident middle answer.

    Both forecasts have the same median, sitting exactly on the Stage III
    boundary, so both give it about even odds. What must differ is the tails:
    the uncertain forecast has to carry more weight into both Stage IV and the
    milder stages.
    """
    narrow = grap.stage_probabilities(_q(240.0, 250.0, 260.0))
    wide = grap.stage_probabilities(_q(80.0, 250.0, 500.0))
    assert abs(wide["p_at_least_3"][0] - narrow["p_at_least_3"][0]) < 0.05
    assert wide["p_at_least_4"][0] > narrow["p_at_least_4"][0]
    assert wide["p_stage_1"][0] > narrow["p_stage_1"][0]


def test_observed_stage_agrees_with_the_deterministic_mapping():
    # Checked against the CPCB sub-index: 100 ug/m3 is AQI 234 (Poor, stage 1),
    # 200 is AQI 362 (Very Poor, 2), 280 is AQI 424 (Severe, 3), 350 is 477.
    values = np.array([20.0, 100.0, 200.0, 280.0, 350.0])
    stages = grap.observed_stage(values)
    assert stages.tolist() == [0, 1, 2, 3, 4]
