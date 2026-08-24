"""The coupled multi-output core.

BUILD_PLAN asks for PM2.5, boundary-layer height and visibility predicted
jointly, "the architecture, not a feature column".

**One target had to change, and the reason matters.** There is no boundary-layer
height *observation* anywhere in our data — BLH exists only as GFS model output.
Training a model to predict it would be training a model to reproduce the
forecast we already have, which is circular and would score beautifully while
meaning nothing. So BLH stays what it honestly is: a shared **driver**, read
from the forecast at target time and fed to every head.

The two series we can actually verify against instruments are PM2.5 (CPCB) and
visibility (METAR). Those are the joint outputs, and they are the right pair:
they are the same physical state — a shallow moist layer trapping aerosol —
measured by two independent instruments. Getting them to agree is the concrete
form of the coupling claim.

**How the coupling works.** A chained architecture, not two models in a
trenchcoat:

1. the visibility head predicts observed visibility at the target hour, from
   meteorology, CAMS and the recent history of *both* series;
2. the PM2.5 head then receives that **predicted** visibility as a feature.

So the PM2.5 forecast is conditioned on the forecast atmospheric state, which
is what "coupled" has to mean if it is to mean anything. Predicted visibility
for the training rows is generated **out of fold** with time-blocked splits: a
model that trains on its own in-sample predictions learns to trust them far
more than it should, and the failure only appears at deployment.
"""

from __future__ import annotations

import logging

import numpy as np
import polars as pl

from .base import Model, Quantiles
from .corrector import CorrectorModel

log = logging.getLogger(__name__)

VIS_TARGET = "y_vis"
VIS_FEATURE = "pred_visibility_km"
N_FOLDS = 3


class CoupledCorrector(Model):
    """PM2.5 and visibility predicted jointly, PM2.5 conditioned on visibility."""

    name = "coupled"
    requires = ("cams_pm2_5_tgt",)

    def __init__(
        self,
        num_rounds: int = 400,
        n_folds: int = N_FOLDS,
        name: str | None = None,
    ) -> None:
        self.num_rounds = num_rounds
        self.n_folds = n_folds
        if name:
            self.name = name
        self.vis_model = CorrectorModel(
            num_rounds=num_rounds, name="visibility-head", anchor_col="vis_anchor_km"
        )
        self.pm_model = CorrectorModel(num_rounds=num_rounds, name="pm25-head")
        self._fitted = False

    # -- the visibility head ------------------------------------------------
    def _fit_visibility(self, X: pl.DataFrame, y_vis: np.ndarray) -> None:
        """Visibility is predicted directly, so its 'CAMS anchor' is the model's
        own visibility diagnostic — poor in absolute terms but a legitimate
        starting point for a residual learner."""
        frame = X.with_columns(pl.Series("vis_anchor_km", self._vis_anchor(X)))
        self.vis_model.fit(frame, y_vis)

    def _vis_anchor(self, X: pl.DataFrame) -> np.ndarray:
        if "met_visibility_tgt" in X.columns:
            # Open-Meteo reports visibility in metres; METAR truth is in km.
            return X["met_visibility_tgt"].to_numpy().astype(float) / 1000.0
        return np.full(X.height, 5.0)

    def _predict_visibility(self, X: pl.DataFrame) -> np.ndarray:
        frame = X.with_columns(pl.Series("vis_anchor_km", self._vis_anchor(X)))
        return self.vis_model.predict(frame).q50

    # -- fit ----------------------------------------------------------------
    def fit(self, X: pl.DataFrame, y: np.ndarray) -> Model:
        self._check(X)
        if VIS_TARGET not in X.columns:
            raise KeyError(
                f"{self.name} needs a {VIS_TARGET!r} column — build the supervised "
                "table with extra_targets={'y_vis': 'metar_visibility_km'}"
            )
        y_vis = X[VIS_TARGET].to_numpy().astype(float)

        # Out-of-fold predicted visibility for the PM2.5 head's training rows.
        oof = np.full(X.height, np.nan)
        for train_idx, held_idx in self._time_folds(X):
            if len(train_idx) < 500 or len(held_idx) == 0:
                continue
            fold = CoupledCorrector(num_rounds=max(60, self.num_rounds // 4))
            fold._fit_visibility(X[train_idx], y_vis[train_idx])
            oof[held_idx] = fold._predict_visibility(X[held_idx])
        log.info("out-of-fold visibility available for %d rows", int(np.isfinite(oof).sum()))

        # Final visibility head, trained on everything.
        self._fit_visibility(X, y_vis)

        # Fall back to the in-sample prediction only where a fold produced
        # nothing, so the column is never silently full of NaN.
        filled = np.where(np.isfinite(oof), oof, self._predict_visibility(X))
        pm_frame = self._with_vis(X, filled)
        self.pm_model.fit(pm_frame, y)
        self._fitted = True
        return self

    def predict(self, X: pl.DataFrame) -> Quantiles:
        if not self._fitted:
            raise RuntimeError("call fit first")
        vis = self._predict_visibility(X)
        return self.pm_model.predict(self._with_vis(X, vis))

    def predict_visibility(self, X: pl.DataFrame) -> Quantiles:
        """The visibility forecast in its own right — the second output."""
        if not self._fitted:
            raise RuntimeError("call fit first")
        frame = X.with_columns(pl.Series("vis_anchor_km", self._vis_anchor(X)))
        return self.vis_model.predict(frame)

    # -- helpers ------------------------------------------------------------
    def _with_vis(self, X: pl.DataFrame, vis: np.ndarray) -> pl.DataFrame:
        return X.with_columns(pl.Series(VIS_FEATURE, vis))

    def _time_folds(self, X: pl.DataFrame) -> list[tuple[np.ndarray, np.ndarray]]:
        """Contiguous time blocks, never random rows (R3 applies inside training too)."""
        order = np.argsort(X["issue_time"].to_numpy())
        blocks = np.array_split(order, self.n_folds)
        folds = []
        for i in range(self.n_folds):
            held = blocks[i]
            train = np.concatenate([blocks[j] for j in range(self.n_folds) if j != i])
            folds.append((train, held))
        return folds

    def importance(self, horizon: int | None = None, top: int = 20) -> pl.DataFrame:
        return self.pm_model.importance(horizon=horizon, top=top)
