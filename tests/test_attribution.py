"""Attribution correctness.

SHAP values are only useful if they add up and if they are grouped honestly.
Both are checked here, along with the property that matters most for the UI:
every feature the model actually uses must land in a named human category, or
the explanation quietly routes real signal into "other".
"""

from __future__ import annotations

import datetime as dt

import numpy as np
import polars as pl

from airshed import attribution
from airshed.models.corrector import CorrectorModel

UTC = dt.timezone.utc
rng = np.random.default_rng(7)


def _frame(n: int = 1500) -> pl.DataFrame:
    horizons = np.tile([24, 72], n // 2)
    n = len(horizons)
    cams = rng.uniform(30, 160, n)
    blh = rng.uniform(50, 1200, n)
    # Truth depends on CAMS and, strongly, on how shallow the mixing layer is.
    truth = cams * 1.6 + (1000.0 / np.maximum(blh, 50)) * 40 + rng.normal(0, 5, n)
    return pl.DataFrame(
        {
            "station_id": ["A"] * n,
            "issue_time": [dt.datetime(2025, 11, 1, tzinfo=UTC)] * n,
            "target_time": [dt.datetime(2025, 11, 2, tzinfo=UTC)] * n,
            "horizon_h": horizons.astype(np.int32),
            "cams_pm2_5_tgt": cams,
            "met_boundary_layer_height_tgt": blh,
            "obs_lag_24h": truth + rng.normal(0, 25, n),
            "fire_count_24h": rng.uniform(0, 50, n),
            "y": truth,
        }
    )


def _fitted():
    df = _frame()
    model = CorrectorModel(num_rounds=120).fit(df, df["y"].to_numpy())
    return model, df


def test_every_feature_maps_to_a_named_driver():
    """Nothing real should end up in 'other' — that is where explanations go to die."""
    model, df = _fitted()
    ungrouped = [f for f in model.feature_columns(df) if attribution.group_of(f) == "other"]
    assert ungrouped == []


def test_known_features_land_in_the_expected_group():
    assert attribution.group_of("fire_count_24h") == "upwind fires"
    assert attribution.group_of("fires_available") == "upwind fires"
    assert attribution.group_of("met_boundary_layer_height_tgt") == "shallow mixing"
    assert attribution.group_of("met_ventilation_index_tgt") == "shallow mixing"
    assert attribution.group_of("met_wind_speed_10m_tgt") == "wind and transport"
    assert attribution.group_of("cams_pm2_5_tgt") == "regional forecast (CAMS)"
    assert attribution.group_of("obs_lag_24h") == "recent local levels"
    assert attribution.group_of("doy") == "time of day and season"
    assert attribution.group_of("hour_sin") == "time of day and season"


def test_contributions_sum_to_the_predicted_residual():
    """The defining property of SHAP: contributions plus base equal the output.

    Compared against the booster's raw residual output, not the finished
    forecast. `predict` clips at zero and sorts the three quantiles afterwards,
    and those steps are not part of what SHAP decomposes — testing against the
    post-processed number would be testing the wrong layer and would fail on
    exactly the rows where clipping bites.
    """
    model, df = _fitted()
    detail = attribution.contributions(model, df, horizon=72)
    per_row = (
        detail.group_by("row").agg(pl.col("contribution").sum().alias("total")).sort("row")
    )
    rows = df.filter(pl.col("horizon_h") == 72)
    raw_residual = model._models[(72, 0.5)].predict(model._matrix(rows))
    base = detail["base_value"][0]
    reconstructed = per_row["total"].to_numpy() + base
    assert np.allclose(reconstructed, raw_residual, atol=1e-6)


def test_the_dominant_driver_is_the_one_that_generated_the_data():
    """Mixing height drives the synthetic truth, so it must top the table."""
    model, df = _fitted()
    table = attribution.drivers(model, df, horizon=72, top=3)
    assert table["driver"][0] == "shallow mixing"


def test_explain_row_produces_a_sentence_a_person_can_read():
    model, df = _fitted()
    out = attribution.explain_row(model, df, horizon=72, row_index=0)
    assert out["horizon_h"] == 72
    assert isinstance(out["sentence"], str) and len(out["sentence"]) > 20
    assert "µg/m³" in out["sentence"]


def test_asking_for_an_unfitted_horizon_fails_loudly():
    model, df = _fitted()
    try:
        attribution.contributions(model, df, horizon=48)
    except KeyError:
        return
    raise AssertionError("expected a KeyError for a horizon with no head")
