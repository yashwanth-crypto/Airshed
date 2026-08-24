"""Metric correctness, checked against values computed by hand.

An evaluation bug is worse than a model bug: it changes which model you pick.
"""

from __future__ import annotations

import numpy as np
import polars as pl

from airshed.eval.metrics import EPISODE_PM25, horizon_table, score
from airshed.models.base import Quantiles


def _q(values, spread: float = 10.0) -> Quantiles:
    v = np.asarray(values, dtype=float)
    return Quantiles(q10=v - spread, q50=v, q90=v + spread)


def test_rmse_mae_and_bias_match_hand_computation():
    y = np.array([100.0, 200.0, 300.0])
    pred = _q([110.0, 180.0, 300.0])  # errors +10, -20, 0
    s = score(y, pred)
    assert s["mae"] == (10 + 20 + 0) / 3
    assert s["rmse"] == np.sqrt((100 + 400 + 0) / 3)
    assert s["bias"] == (10 - 20 + 0) / 3


def test_bias_sign_means_under_forecast_when_negative():
    y = np.array([100.0, 100.0])
    s = score(y, _q([80.0, 80.0]))
    assert s["bias"] < 0, "predicting below the truth must give a negative bias"


def test_perfect_prediction_scores_zero_error():
    y = np.array([50.0, 250.0])
    s = score(y, _q(y))
    assert s["rmse"] == 0.0 and s["mae"] == 0.0 and s["bias"] == 0.0


def test_nans_are_excluded_rather_than_propagated():
    y = np.array([100.0, np.nan, 300.0])
    s = score(y, _q([100.0, 999.0, 300.0]))
    assert s["n"] == 2
    assert s["rmse"] == 0.0


def test_skill_is_zero_against_itself_and_positive_when_better():
    y = np.array([100.0, 200.0, 300.0])
    baseline = _q([150.0, 250.0, 350.0])
    assert score(y, baseline, baseline)["skill_vs_baseline"] == 0.0
    better = score(y, _q(y), baseline)["skill_vs_baseline"]
    assert better == 1.0


def test_skill_is_negative_when_worse_than_the_baseline():
    """R2: a model that loses to persistence must show it, not hide it."""
    y = np.array([100.0, 200.0])
    baseline = _q([105.0, 205.0])
    worse = score(y, _q([300.0, 400.0]), baseline)["skill_vs_baseline"]
    assert worse < 0


def test_interval_coverage_counts_truths_inside_the_band():
    y = np.array([100.0, 100.0, 100.0, 100.0])
    pred = Quantiles(
        q10=np.array([90.0, 90.0, 200.0, 200.0]),
        q50=np.array([100.0] * 4),
        q90=np.array([110.0, 110.0, 300.0, 300.0]),
    )
    assert score(y, pred)["coverage_80"] == 0.5


def test_episode_metrics_only_consider_episode_hours():
    y = np.array([10.0, 20.0, EPISODE_PM25 + 50])
    pred = _q([10.0, 20.0, 100.0])
    s = score(y, pred)
    assert s["episode_n"] == 1
    assert s["episode_recall"] == 0.0  # predicted 100, well below the threshold
    assert s["episode_rmse"] == 200.0


def test_episode_recall_rewards_calling_the_episode():
    y = np.array([EPISODE_PM25 + 10, EPISODE_PM25 + 20])
    s = score(y, _q([EPISODE_PM25 + 5, 100.0]))
    assert s["episode_recall"] == 0.5


def test_a_model_can_look_good_overall_and_miss_every_episode():
    """Exactly why R5 bans overall accuracy from results tables."""
    quiet = np.full(500, 40.0)
    severe = np.full(10, 400.0)
    y = np.concatenate([quiet, severe])
    pred = _q(np.concatenate([quiet, np.full(10, 60.0)]))
    s = score(y, pred)
    assert s["rmse"] < 60          # looks respectable
    assert s["episode_recall"] == 0.0  # caught nothing that mattered


def test_horizon_table_reports_each_horizon_and_an_overall_row():
    frame = pl.DataFrame({"y": [100.0] * 6, "horizon_h": [24, 24, 48, 48, 72, 72]})
    pred = _q([110.0] * 6)
    table = horizon_table(frame, pred)
    assert sorted(table["horizon_h"].to_list()) == [0, 24, 48, 72]
    assert table.filter(pl.col("horizon_h") == 0)["n"][0] == 6


def test_horizon_table_scores_each_horizon_separately():
    frame = pl.DataFrame({"y": [100.0, 100.0], "horizon_h": [24, 72]})
    pred = _q([100.0, 200.0])
    table = horizon_table(frame, pred)
    assert table.filter(pl.col("horizon_h") == 24)["rmse"][0] == 0.0
    assert table.filter(pl.col("horizon_h") == 72)["rmse"][0] == 100.0
