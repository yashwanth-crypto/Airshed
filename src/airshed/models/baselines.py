"""The two baselines every result is measured against.

`PersistenceModel` — "tomorrow is like today". R2 exists because this is a
brutally strong AQI baseline that published models routinely fail to beat, and
a result that does not clear it at a given horizon is a negative result.

`RawCAMSModel` — the CAMS forecast passed through unchanged. This is the number
an external, internationally-used physics model already publishes for Delhi.
Beating it by a measured margin is the project's central claim, so it has to be
in the table as an opponent, not as an input we quietly absorb.

Both learn their uncertainty from the training residuals rather than assuming a
spread, so their intervals are honest and comparable with the corrector's.
"""

from __future__ import annotations

import numpy as np
import polars as pl

from .base import Model, Quantiles

# Empirical residual quantiles are taken per horizon: a 72 h persistence error
# is far wider than a 24 h one, and one pooled interval would flatter the long
# horizons while punishing the short ones.
DEFAULT_SPREAD = (0.1, 0.9)


class _EmpiricalInterval(Model):
    """Shared machinery: a median rule plus residual quantiles by horizon."""

    def __init__(self) -> None:
        self._lo: dict[int, float] = {}
        self._hi: dict[int, float] = {}
        self._lo_global = 0.0
        self._hi_global = 0.0

    def _median(self, X: pl.DataFrame) -> np.ndarray:
        raise NotImplementedError

    def fit(self, X: pl.DataFrame, y: np.ndarray) -> Model:
        self._check(X)
        centre = self._median(X)
        resid = np.asarray(y, dtype=float) - centre
        finite = np.isfinite(resid)

        horizons = (
            X["horizon_h"].to_numpy()
            if "horizon_h" in X.columns
            else np.zeros(len(resid), dtype=int)
        )
        lo_q, hi_q = DEFAULT_SPREAD
        for h in np.unique(horizons):
            sel = finite & (horizons == h)
            if sel.sum() < 30:  # too few to estimate a tail from
                continue
            self._lo[int(h)] = float(np.quantile(resid[sel], lo_q))
            self._hi[int(h)] = float(np.quantile(resid[sel], hi_q))

        if finite.any():
            self._lo_global = float(np.quantile(resid[finite], lo_q))
            self._hi_global = float(np.quantile(resid[finite], hi_q))
        return self

    def predict(self, X: pl.DataFrame) -> Quantiles:
        self._check(X)
        centre = self._median(X)
        horizons = (
            X["horizon_h"].to_numpy()
            if "horizon_h" in X.columns
            else np.zeros(len(centre), dtype=int)
        )
        lo = np.array([self._lo.get(int(h), self._lo_global) for h in horizons])
        hi = np.array([self._hi.get(int(h), self._hi_global) for h in horizons])
        centre = np.clip(centre, 0.0, None)

        # The median is the baseline's rule, exactly — raw CAMS must stay raw.
        # When a baseline is badly biased its residual quantiles can land wholly
        # on one side of the centre, and sorting the three values would quietly
        # promote `centre + residual_q10` into the median, turning "raw CAMS"
        # into a bias-corrected CAMS and flattering the thing we must beat.
        # Clamping instead keeps q10 <= q50 <= q90 while leaving the median
        # untouched; the bias shows up in the bias column, where it belongs.
        #
        # PM2.5 cannot be negative either, and that clip is physics, not a fudge.
        return Quantiles(
            q10=np.minimum(np.clip(centre + lo, 0.0, None), centre),
            q50=centre,
            q90=np.maximum(np.clip(centre + hi, 0.0, None), centre),
        )


class PersistenceModel(_EmpiricalInterval):
    """Tomorrow equals the last observation available at issue time (R2).

    Uses `obs_lag_1h`, not the observation at issue time itself: at 05:30 IST a
    real operational forecast has last hour's reading, not this hour's. Using
    the latter would give persistence a peek the deployed system never gets,
    and an unbeatable baseline that cheats is worse than no baseline.
    """

    name = "persistence"
    requires = ("obs_lag_1h",)

    def _median(self, X: pl.DataFrame) -> np.ndarray:
        return X["obs_lag_1h"].to_numpy().astype(float)


class PersistenceDailyModel(_EmpiricalInterval):
    """Same hour yesterday — persistence with the diurnal cycle respected.

    Delhi PM2.5 swings by a factor of three between afternoon and dawn, so
    plain persistence at a 24 h horizon is partly measuring the daily cycle.
    This variant removes that excuse from the comparison.
    """

    name = "persistence-daily"
    requires = ("obs_lag_24h",)

    def _median(self, X: pl.DataFrame) -> np.ndarray:
        return X["obs_lag_24h"].to_numpy().astype(float)


class RawCAMSModel(_EmpiricalInterval):
    """CAMS forecast for the target hour, passed through untouched.

    Reads `cams_pm2_5_tgt` — the CAMS value at the *target* time, which is what
    a forecast issued earlier would legitimately have had. Comparing against
    CAMS at issue time instead would be comparing against a different question.
    """

    name = "raw-cams"
    requires = ("cams_pm2_5_tgt",)

    def _median(self, X: pl.DataFrame) -> np.ndarray:
        return X["cams_pm2_5_tgt"].to_numpy().astype(float)


class ScaledCAMSModel(_EmpiricalInterval):
    """CAMS multiplied by a single fitted scale factor.

    Deliberately the crudest possible correction: one number, fitted on the
    training split. It exists to answer the obvious objection to the whole
    project — "CAMS is just low by a constant, multiply it and go home". If the
    LightGBM corrector cannot beat this, the extra machinery is not earning its
    place and the ablation should say so.
    """

    name = "scaled-cams"
    requires = ("cams_pm2_5_tgt",)

    def __init__(self) -> None:
        super().__init__()
        self.scale = 1.0

    def fit(self, X: pl.DataFrame, y: np.ndarray) -> Model:
        self._check(X)
        cams = X["cams_pm2_5_tgt"].to_numpy().astype(float)
        obs = np.asarray(y, dtype=float)
        ok = np.isfinite(cams) & np.isfinite(obs) & (cams > 0)
        # Ratio of means, not mean of ratios: the latter is dominated by hours
        # when CAMS is near zero and the ratio explodes.
        self.scale = float(obs[ok].mean() / cams[ok].mean()) if ok.any() else 1.0
        return super().fit(X, y)

    def _median(self, X: pl.DataFrame) -> np.ndarray:
        return X["cams_pm2_5_tgt"].to_numpy().astype(float) * self.scale
