"""Model interface and leakage tests.

The most dangerous bug in this layer is a feature that contains the answer.
It does not crash, it does not look wrong, it just produces a wonderful RMSE
that evaporates in production.
"""

from __future__ import annotations

import datetime as dt

import numpy as np
import polars as pl
import pytest

from airshed.models.base import Quantiles
from airshed.models.baselines import (
    PersistenceDailyModel,
    PersistenceModel,
    RawCAMSModel,
    ScaledCAMSModel,
)
from airshed.models.corrector import CorrectorModel

UTC = dt.timezone.utc
rng = np.random.default_rng(0)


def _supervised(n: int = 3000) -> pl.DataFrame:
    horizons = np.tile([24, 48, 72], n // 3)
    n = len(horizons)
    cams = rng.uniform(20, 150, n)
    truth = cams * 2.5 + rng.normal(0, 10, n)  # a learnable scale bias
    return pl.DataFrame(
        {
            "station_id": ["A"] * n,
            "issue_time": [dt.datetime(2025, 11, 1, tzinfo=UTC)] * n,
            "target_time": [dt.datetime(2025, 11, 2, tzinfo=UTC)] * n,
            "horizon_h": horizons.astype(np.int32),
            "cams_pm2_5_tgt": cams,
            "obs_lag_1h": truth + rng.normal(0, 30, n),
            "obs_lag_24h": truth + rng.normal(0, 50, n),
            "met_wind_speed_10m": rng.uniform(0, 20, n),
            "y": truth,
        }
    )


def test_every_model_returns_ordered_quantiles():
    df = _supervised()
    y = df["y"].to_numpy()
    for model in (
        PersistenceModel(), PersistenceDailyModel(), RawCAMSModel(),
        ScaledCAMSModel(), CorrectorModel(),
    ):
        pred = model.fit(df, y).predict(df)
        assert len(pred) == df.height
        assert np.all(pred.q10 <= pred.q50 + 1e-9), model.name
        assert np.all(pred.q50 <= pred.q90 + 1e-9), model.name


def test_predictions_are_never_negative():
    df = _supervised()
    y = df["y"].to_numpy()
    for model in (PersistenceModel(), RawCAMSModel(), CorrectorModel()):
        pred = model.fit(df, y).predict(df)
        assert pred.q10.min() >= 0.0, model.name


def test_persistence_uses_the_lag_not_the_current_observation():
    """Using the value at issue time would give persistence a peek production never gets."""
    assert "obs_lag_1h" in PersistenceModel.requires
    assert "pm25_clean" not in PersistenceModel.requires


def test_raw_cams_reads_the_target_hour_not_issue_hour():
    assert RawCAMSModel.requires == ("cams_pm2_5_tgt",)


def test_raw_cams_passes_the_forecast_through_unchanged():
    df = _supervised()
    pred = RawCAMSModel().fit(df, df["y"].to_numpy()).predict(df)
    assert np.allclose(pred.q50, df["cams_pm2_5_tgt"].to_numpy())


def test_scaled_cams_learns_a_sensible_factor():
    df = _supervised()
    model = ScaledCAMSModel().fit(df, df["y"].to_numpy())
    assert 2.0 < model.scale < 3.0  # truth is 2.5x CAMS by construction


def test_corrector_never_sees_the_target_or_its_relatives():
    df = _supervised().with_columns(
        pl.lit(1.0).alias("pm25_clean"),
        pl.lit(1.0).alias("pm25"),
    )
    features = CorrectorModel().feature_columns(df)
    for leaky in ("y", "pm25", "pm25_clean"):
        assert leaky not in features, f"{leaky} would leak the answer"


def test_corrector_can_be_built_without_observation_history():
    df = _supervised()
    features = CorrectorModel(use_obs_history=False).feature_columns(df)
    assert not [c for c in features if c.startswith("obs_")]
    assert "cams_pm2_5_tgt" in features


def test_corrector_can_be_built_without_meteorology():
    df = _supervised()
    features = CorrectorModel(use_meteo=False).feature_columns(df)
    assert not [c for c in features if c.startswith("met_")]


def test_corrector_learns_a_bias_it_can_actually_see():
    df = _supervised()
    y = df["y"].to_numpy()
    corrected = CorrectorModel(num_rounds=120).fit(df, y).predict(df)
    raw = RawCAMSModel().fit(df, y).predict(df)
    rmse = lambda p: float(np.sqrt(np.mean((p.q50 - y) ** 2)))  # noqa: E731
    assert rmse(corrected) < rmse(raw) / 2


def test_unseen_horizon_falls_back_to_raw_cams_not_to_nothing():
    df = _supervised()
    model = CorrectorModel(num_rounds=40).fit(df, df["y"].to_numpy())
    future = df.head(10).with_columns(pl.lit(96, dtype=pl.Int32).alias("horizon_h"))
    pred = model.predict(future)
    assert np.allclose(pred.q50, future["cams_pm2_5_tgt"].to_numpy())


def test_predicting_before_fitting_is_an_error():
    with pytest.raises(RuntimeError):
        CorrectorModel().predict(_supervised())


def test_missing_required_column_is_reported_clearly():
    df = _supervised().drop("cams_pm2_5_tgt")
    with pytest.raises(KeyError):
        RawCAMSModel().fit(df, df["y"].to_numpy())


def test_quantiles_sorted_repairs_crossing():
    q = Quantiles(q10=np.array([50.0]), q50=np.array([10.0]), q90=np.array([30.0])).sorted()
    assert q.q10[0] == 10.0 and q.q50[0] == 30.0 and q.q90[0] == 50.0
