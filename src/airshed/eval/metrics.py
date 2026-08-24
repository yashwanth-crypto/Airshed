"""Forecast metrics, broken down by horizon.

Deliberately included:

* **bias**, not just error magnitude. CAMS's problem is a scale bias, and a
  metric set that only reports RMSE cannot show whether we fixed it.
* **skill against persistence**, as a column on every row, because R2 says
  persistence belongs in every results table and a reader should not have to do
  the division themselves.
* **interval coverage**, because a 10-90 interval that contains the truth 40%
  of the time is not an uncertainty estimate, and quantile models fail this way
  quietly.
* **episode recall**, because the mean error over 14 000 quiet hours tells you
  nothing about the twenty hours that trigger a GRAP stage (R5).
"""

from __future__ import annotations

import numpy as np
import polars as pl

from ..models.base import Quantiles

# An "episode hour" for reporting purposes: PM2.5 above this is comfortably
# into GRAP territory and is what the system exists to catch. Kept separate
# from the statutory GRAP thresholds in config.toml, which apply to a 24 h
# city-wide AQI rather than a station-hour.
EPISODE_PM25 = 250.0


def score(
    y_true: np.ndarray,
    pred: Quantiles,
    baseline: Quantiles | None = None,
) -> dict[str, float]:
    """Metrics for one set of predictions."""
    y = np.asarray(y_true, dtype=float)
    p = np.asarray(pred.q50, dtype=float)
    ok = np.isfinite(y) & np.isfinite(p)
    if not ok.any():
        return {"n": 0}

    err = p[ok] - y[ok]
    out = {
        "n": int(ok.sum()),
        "rmse": float(np.sqrt(np.mean(err**2))),
        "mae": float(np.mean(np.abs(err))),
        "bias": float(np.mean(err)),
        "coverage_80": _coverage(y, pred, ok),
        "interval_width": float(np.mean(pred.q90[ok] - pred.q10[ok])),
        "pinball": _pinball(y[ok], pred, ok),
    }

    episode = ok & (y >= EPISODE_PM25)
    out["episode_n"] = int(episode.sum())
    if episode.any():
        ep_err = p[episode] - y[episode]
        out["episode_rmse"] = float(np.sqrt(np.mean(ep_err**2)))
        out["episode_bias"] = float(np.mean(ep_err))
        # Of the hours that really were episodes, how many did we call?
        out["episode_recall"] = float(np.mean(p[episode] >= EPISODE_PM25))

    if baseline is not None:
        b = np.asarray(baseline.q50, dtype=float)
        both = ok & np.isfinite(b)
        if both.any():
            base_rmse = float(np.sqrt(np.mean((b[both] - y[both]) ** 2)))
            model_rmse = float(np.sqrt(np.mean((p[both] - y[both]) ** 2)))
            out["baseline_rmse"] = base_rmse
            # Skill score: 1 means perfect, 0 means no better than the
            # baseline, negative means worse. Negative is a real result and
            # must be reported as one (R2).
            out["skill_vs_baseline"] = (
                float(1.0 - model_rmse / base_rmse) if base_rmse > 0 else float("nan")
            )
    return out


def horizon_table(
    frame: pl.DataFrame,
    pred: Quantiles,
    baseline: Quantiles | None = None,
    target: str = "y",
    horizon_col: str = "horizon_h",
) -> pl.DataFrame:
    """Per-horizon metrics — the shape every results table in this project takes.

    A single pooled number across 24/48/72 h hides the thing we most need to
    see, which is where skill decays.
    """
    y = frame[target].to_numpy().astype(float)
    horizons = frame[horizon_col].to_numpy()

    rows = []
    for h in sorted(np.unique(horizons)):
        sel = horizons == h
        sub_pred = Quantiles(pred.q10[sel], pred.q50[sel], pred.q90[sel])
        sub_base = (
            Quantiles(baseline.q10[sel], baseline.q50[sel], baseline.q90[sel])
            if baseline is not None
            else None
        )
        row = {"horizon_h": int(h), **score(y[sel], sub_pred, sub_base)}
        rows.append(row)

    overall = {"horizon_h": 0, **score(y, pred, baseline)}
    rows.append(overall)
    return pl.DataFrame(rows)


def _coverage(y: np.ndarray, pred: Quantiles, ok: np.ndarray) -> float:
    """Fraction of truths inside the 10-90 interval. Should be near 0.80."""
    inside = (y[ok] >= pred.q10[ok]) & (y[ok] <= pred.q90[ok])
    return float(np.mean(inside))


def _pinball(y: np.ndarray, pred: Quantiles, ok: np.ndarray) -> float:
    """Mean pinball loss over the three quantiles — one number for the whole
    distribution, so a model cannot win by nailing the median while producing
    nonsense intervals."""
    total = 0.0
    for q, values in ((0.1, pred.q10[ok]), (0.5, pred.q50[ok]), (0.9, pred.q90[ok])):
        diff = y - values
        total += float(np.mean(np.maximum(q * diff, (q - 1) * diff)))
    return total / 3.0
