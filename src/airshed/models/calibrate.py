"""Conformal calibration of prediction intervals.

Phase 2 left the full model's 10-90 band holding the truth 54% of the time
instead of 80%. A quantile objective optimises pinball loss, which does not
guarantee coverage on data drawn from a different month than training — and in
Delhi every month is a different month.

Conformalised quantile regression fixes this with one number. On a calibration
split the model has never trained on, compute for each row how far outside the
interval the truth fell:

    E = max(q10 - y,  y - q90)

E is negative when the truth is comfortably inside, positive when it escaped.
Take the (1 - alpha) empirical quantile of E and widen every interval by it.
The result carries a finite-sample coverage guarantee under exchangeability,
and when the model is already well calibrated the correction is near zero.

Exchangeability is not strictly true for a time series, so the guarantee is
approximate here. It is still the right tool: the adjustment is fitted on held-
out data, it cannot silently overfit, and the resulting coverage is *measured*
on the test split rather than assumed. A per-horizon correction is used because
a 72 h interval needs far more widening than a 24 h one.
"""

from __future__ import annotations

import logging

import numpy as np
import polars as pl

from .base import Model, Quantiles

log = logging.getLogger(__name__)

TARGET_COVERAGE = 0.8


class CalibratedModel(Model):
    """Wraps any model and widens its intervals to hit the target coverage.

    The median is never touched. Calibration is about honesty over the spread,
    not about changing the forecast.
    """

    def __init__(self, inner: Model, target_coverage: float = TARGET_COVERAGE) -> None:
        self.inner = inner
        self.target_coverage = target_coverage
        self.name = f"{inner.name}+cal"
        self._pad: dict[int, float] = {}
        self._pad_global = 0.0

    def fit(self, X: pl.DataFrame, y: np.ndarray) -> Model:
        self.inner.fit(X, y)
        return self

    def calibrate(self, X: pl.DataFrame, y: np.ndarray) -> CalibratedModel:
        """Fit the widening on a split the model did not train on.

        The conformity score is **normalised by the model's own interval
        width** rather than measured in µg/m³. Forecast error here scales with
        concentration — being 40 µg/m³ out at 350 is ordinary, at 30 it is not
        — so a single additive pad fitted on calm hours leaves the episode
        hours uncovered, which are precisely the hours a GRAP decision turns
        on. A multiplicative factor widens each interval in proportion to the
        uncertainty the model already expressed.
        """
        pred = self.inner.predict(X)
        y = np.asarray(y, dtype=float)
        half = np.maximum((pred.q90 - pred.q10) / 2.0, 1.0)
        scores = np.maximum(pred.q10 - y, y - pred.q90) / half
        horizons = (
            X["horizon_h"].to_numpy()
            if "horizon_h" in X.columns
            else np.zeros(len(y), dtype=int)
        )
        finite = np.isfinite(scores)
        if not finite.any():
            log.warning("no finite conformity scores; calibration skipped")
            return self

        self._pad_global = float(np.quantile(scores[finite], self.target_coverage))
        for h in np.unique(horizons):
            sel = finite & (horizons == h)
            if sel.sum() < 100:
                continue
            self._pad[int(h)] = float(np.quantile(scores[sel], self.target_coverage))
            log.info("horizon %sh: widening intervals by %.1f", h, self._pad[int(h)])
        return self

    def predict(self, X: pl.DataFrame) -> Quantiles:
        pred = self.inner.predict(X)
        horizons = (
            X["horizon_h"].to_numpy()
            if "horizon_h" in X.columns
            else np.zeros(len(pred), dtype=int)
        )
        factor = np.array([self._pad.get(int(h), self._pad_global) for h in horizons])
        # The stored factor is in units of the model's own half-width, so the
        # widening is proportional to the uncertainty already expressed.
        # A negative factor means the intervals were too wide and should
        # shrink; that is legitimate, but never past the median.
        pad = factor * np.maximum((pred.q90 - pred.q10) / 2.0, 1.0)
        return Quantiles(
            q10=np.minimum(np.clip(pred.q10 - pad, 0.0, None), pred.q50),
            q50=pred.q50,
            q90=np.maximum(pred.q90 + pad, pred.q50),
        )

    def importance(self, *args, **kwargs):
        return self.inner.importance(*args, **kwargs)
