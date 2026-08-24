"""Tests for lead-matched meteorology.

The substitution is a join on three keys, one of which is derived. That is
precisely the shape of bug this project has been bitten by before — a silent
misalignment that produces a plausible table — so the mapping, the fallback and
the provenance flag are all pinned here.
"""

from __future__ import annotations

import datetime as dt

import polars as pl
import pytest

from airshed.features.build import apply_lead_matched_meteo, lead_day_for


def test_lead_day_mapping_is_never_optimistic():
    # A horizon must map to a lead day whose *shortest* lead is >= the horizon,
    # or the model would be scored against a fresher forecast than it claims.
    for h in (24, 48, 72):
        assert 24 * lead_day_for(h) >= h
    assert lead_day_for(24) == 1
    assert lead_day_for(48) == 2
    assert lead_day_for(72) == 3


def test_lead_day_rounds_up_for_off_grid_horizons():
    assert lead_day_for(1) == 1
    assert lead_day_for(25) == 2
    assert lead_day_for(47) == 2


@pytest.fixture
def cfg(monkeypatch):
    class Cfg:
        def source(self, name):
            return {
                "lead_matched_hourly": ["temperature_2m", "wind_speed_10m"],
                "lead_matched_days": [1, 2, 3],
            }

    return Cfg()


def _sup() -> pl.DataFrame:
    t0 = dt.datetime(2025, 11, 5, tzinfo=dt.timezone.utc)
    rows = []
    for h in (24, 48, 72):
        rows.append(
            {
                "station_id": "DL001",
                "issue_time": t0,
                "target_time": t0 + dt.timedelta(hours=h),
                "horizon_h": h,
                "y": 100.0,
                # Short-lead values, all identical so any change is the swap.
                "met_temperature_2m_tgt": 20.0,
                "met_wind_speed_10m_tgt": 5.0,
            }
        )
    return pl.DataFrame(rows)


def _lead_frame() -> pl.DataFrame:
    t0 = dt.datetime(2025, 11, 5, tzinfo=dt.timezone.utc)
    rows = []
    for h, day in ((24, 1), (48, 2), (72, 3)):
        rows.append(
            {
                "station_id": "DL001",
                "time": t0 + dt.timedelta(hours=h),
                "lead_day": day,
                # Encoded so the test can prove which lead day landed where.
                "temperature_2m": 20.0 + day,
                "wind_speed_10m": 5.0 + day,
            }
        )
    return pl.DataFrame(rows)


def test_each_horizon_takes_its_own_lead_day(monkeypatch, cfg):
    monkeypatch.setattr(
        "airshed.features.build.read_range", lambda *a, **k: _lead_frame()
    )
    out = apply_lead_matched_meteo(_sup(), cfg=cfg).sort("horizon_h")

    # 24 h must read lead day 1, 48 h day 2, 72 h day 3 — never each other's.
    assert out["met_temperature_2m_tgt"].to_list() == [21.0, 22.0, 23.0]
    assert out["met_wind_speed_10m_tgt"].to_list() == [6.0, 7.0, 8.0]
    assert out["met_lead_matched"].all()


def test_missing_lead_row_keeps_short_lead_value_and_is_flagged(monkeypatch, cfg):
    # Drop the 72 h lead day. That row must survive with its old value rather
    # than vanish: losing rows would change the evaluation set and make the
    # comparison against the archive-trained model no longer like-for-like.
    partial = _lead_frame().filter(pl.col("lead_day") != 3)
    monkeypatch.setattr("airshed.features.build.read_range", lambda *a, **k: partial)
    out = apply_lead_matched_meteo(_sup(), cfg=cfg).sort("horizon_h")

    assert out.height == 3
    assert out["met_temperature_2m_tgt"].to_list() == [21.0, 22.0, 20.0]
    assert out["met_lead_matched"].to_list() == [True, True, False]


def test_no_cached_lead_data_leaves_frame_unchanged(monkeypatch, cfg):
    monkeypatch.setattr(
        "airshed.features.build.read_range", lambda *a, **k: pl.DataFrame()
    )
    out = apply_lead_matched_meteo(_sup(), cfg=cfg)
    assert out["met_temperature_2m_tgt"].to_list() == [20.0, 20.0, 20.0]
    assert not out["met_lead_matched"].any()


def test_helper_columns_do_not_leak_into_the_frame(monkeypatch, cfg):
    monkeypatch.setattr(
        "airshed.features.build.read_range", lambda *a, **k: _lead_frame()
    )
    out = apply_lead_matched_meteo(_sup(), cfg=cfg)
    assert "lead_day" not in out.columns
    assert not [c for c in out.columns if c.endswith("_lm")]


def test_provenance_column_is_excluded_from_model_features():
    # It is near-constant within a run and would let a model split on which
    # dataset a row came from.
    from airshed.models.corrector import EXCLUDE

    assert "met_lead_matched" in EXCLUDE


def test_native_lead_day_expression_matches_the_python_helper(monkeypatch, cfg):
    """The join uses a Polars expression, not `lead_day_for`, for speed.

    Two implementations of the same mapping is exactly how a silent
    misalignment gets in, so they are checked against each other here.
    """
    monkeypatch.setattr(
        "airshed.features.build.read_range", lambda *a, **k: _lead_frame()
    )
    horizons = pl.Series("horizon_h", [1, 24, 25, 47, 48, 72, 96], dtype=pl.Int32)
    native = ((horizons + 23) // 24).clip(lower_bound=1).cast(pl.Int32).to_list()
    assert native == [lead_day_for(h) for h in horizons.to_list()]
