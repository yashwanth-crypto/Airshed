"""Tests for the corrector's quantile heads.

`quantiles=(0.5,)` exists so a hyperparameter trial can fit one head instead of
three. It introduced a silent corruption that is worth a permanent test: the
unfitted heads fell back to the anchor, `Quantiles.sorted()` then reordered the
triple per row, and the anchor landed in the median slot -- so every prediction
came back as uncorrected CAMS.

Nothing raised. A 24-configuration search scored every trial identically and
read as a tidy "the defaults were already fine" null result. It was only caught
because an exact tie to three decimals across 24 configurations is not a thing
that happens.
"""

from __future__ import annotations

import numpy as np
import polars as pl
import pytest

from airshed.models.corrector import QUANTILES, CorrectorModel


def _frame(n: int = 400, seed: int = 0) -> pl.DataFrame:
    rng = np.random.default_rng(seed)
    cams = rng.uniform(20, 200, n)
    # A learnable residual: observed sits well above the anchor, by an amount
    # the feature explains. A model that ignores it returns the anchor.
    feat = rng.uniform(0, 1, n)
    y = cams + 40 * feat + rng.normal(0, 2, n)
    return pl.DataFrame({
        "station_id": ["DL001"] * n,
        "horizon_h": np.full(n, 24, dtype=np.int32),
        "cams_pm2_5_tgt": cams,
        "obs_lag_1h": rng.uniform(20, 200, n),
        "signal": feat,
        "y": y,
    })


def _fit(quantiles):
    train = _frame(seed=1)
    m = CorrectorModel(name="t", num_rounds=60, quantiles=quantiles)
    m.fit(train, train["y"].to_numpy().astype(float))
    return m, _frame(seed=2)


def test_median_only_still_corrects_the_anchor():
    """The bug: q50 came back as the raw anchor, so the model did nothing."""
    m, test = _fit((0.5,))
    pred = m.predict(test)
    anchor = test["cams_pm2_5_tgt"].to_numpy()
    y = test["y"].to_numpy()

    assert not np.allclose(pred.q50, anchor), "median collapsed onto the anchor"
    corrected = np.sqrt(np.mean((pred.q50 - y) ** 2))
    raw = np.sqrt(np.mean((anchor - y) ** 2))
    assert corrected < raw * 0.6, f"corrected {corrected:.1f} vs anchor {raw:.1f}"


def test_median_only_matches_the_full_fit_closely():
    """If the shortcut ranked configurations differently it would be useless."""
    y = _frame(seed=2)["y"].to_numpy()
    rmses = {}
    for qs in ((0.5,), QUANTILES):
        m, test = _fit(qs)
        rmses[qs] = float(np.sqrt(np.mean((m.predict(test).q50 - y) ** 2)))
    a, b = rmses[(0.5,)], rmses[QUANTILES]
    assert abs(a - b) / b < 0.05, f"median-only {a:.2f} vs full {b:.2f}"


def test_unfitted_quantiles_mirror_the_median_not_the_anchor():
    m, test = _fit((0.5,))
    pred = m.predict(test)
    # Mirroring keeps sorting a no-op. The interval is degenerate, which is
    # correct: one head cannot describe a spread, and pretending otherwise
    # would hand a caller an interval that means nothing.
    assert np.allclose(pred.q10, pred.q50)
    assert np.allclose(pred.q90, pred.q50)


def test_a_full_fit_still_produces_a_real_interval():
    m, test = _fit(QUANTILES)
    pred = m.predict(test)
    assert (pred.q90 >= pred.q50).all() and (pred.q50 >= pred.q10).all()
    assert float(np.mean(pred.q90 - pred.q10)) > 0, "interval collapsed"


def test_default_is_all_three_heads():
    # Anything served must return an interval. The shortcut is for search only.
    assert CorrectorModel().quantiles == QUANTILES
