"""The correction layer: LightGBM on top of the CAMS forecast.

This is the project's central claim in code. CAMS supplies the future; this
learns what CAMS gets wrong about Delhi.

Three design choices worth stating plainly:

**It learns the residual, not the concentration.** The target is
`observed - CAMS`, so the physics forecast is the starting point rather than
one feature among fifty. If the model learns nothing at all, its output decays
to raw CAMS instead of to the training mean — a much safer failure.

**One model per (horizon, quantile).** R4 forbids recursive rollout, so each
horizon gets its own head; and a quantile objective needs a separate fit per
quantile. With three horizons and three quantiles that is nine small models,
which is still seconds to train and keeps every head honest about its own
error distribution.

**Observation history is optional.** `use_obs_history=False` builds the
CAMS-plus-weather variant the ablation needs, so the contribution of knowing
recent PM2.5 can be measured rather than assumed.
"""

from __future__ import annotations

import logging

import lightgbm as lgb
import numpy as np
import polars as pl

from .base import Model, Quantiles

log = logging.getLogger(__name__)

QUANTILES = (0.1, 0.5, 0.9)

# Small trees, heavy regularisation: the training set is one winter, and a
# model with unlimited depth will memorise individual episodes and report a
# beautiful validation number that means nothing in the next November.
DEFAULT_PARAMS = {
    "objective": "quantile",
    "learning_rate": 0.05,
    "num_leaves": 31,
    "min_data_in_leaf": 40,
    "feature_fraction": 0.8,
    "bagging_fraction": 0.8,
    "bagging_freq": 1,
    "lambda_l2": 1.0,
    "verbosity": -1,
    "num_threads": 0,
}
DEFAULT_ROUNDS = 400

# Columns that must never reach the model: identifiers, timestamps, the target
# itself, and anything derived from the target at target time.
EXCLUDE = {
    "y", "y_vis", "station_id", "issue_time", "target_time", "horizon_h",
    "split", "block_label", "station_name", "cams_source_class",
    "quality_flag", "pm25", "pm25_clean", "obs_n",
    "cell_lat", "cell_lon",
    # Provenance, not physics. It is near-constant within any one run and would
    # let a model split on which dataset a row came from.
    "met_lead_matched",
}


class CorrectorModel(Model):
    """LightGBM residual corrector with direct multi-horizon quantile heads."""

    name = "corrector"
    requires = ("cams_pm2_5_tgt",)

    def __init__(
        self,
        use_obs_history: bool = True,
        use_meteo: bool = True,
        params: dict | None = None,
        num_rounds: int = DEFAULT_ROUNDS,
        name: str | None = None,
        anchor_col: str = "cams_pm2_5_tgt",
        drop_prefixes: tuple[str, ...] = (),
    ) -> None:
        self.use_obs_history = use_obs_history
        self.use_meteo = use_meteo
        # The physics forecast this model corrects. Configurable so the same
        # class can correct a visibility diagnostic instead of PM2.5, without
        # overwriting the CAMS column the head still needs as a predictor.
        self.anchor_col = anchor_col
        self.requires = (anchor_col,)
        # Feature families to withhold. Used to build a deliberately blinded
        # variant so an ablation can attribute a gain to one kind of
        # information rather than to "more columns helped".
        self.drop_prefixes = tuple(drop_prefixes)
        self.params = {**DEFAULT_PARAMS, **(params or {})}
        self.num_rounds = num_rounds
        if name:
            self.name = name
        self._models: dict[tuple[int, float], lgb.Booster] = {}
        self._features: list[str] = []

    # -- features ----------------------------------------------------------
    def feature_columns(self, X: pl.DataFrame) -> list[str]:
        cols = []
        for c in X.columns:
            if c in EXCLUDE:
                continue
            if not X.schema[c].is_numeric() and X.schema[c] != pl.Boolean:
                continue
            if not self.use_obs_history and c.startswith("obs_"):
                continue
            if not self.use_meteo and (c.startswith(("met_", "metar_")) or c.startswith("fire_")):
                continue
            if self.drop_prefixes and c.startswith(self.drop_prefixes):
                continue
            cols.append(c)
        return cols

    def _matrix(self, X: pl.DataFrame) -> np.ndarray:
        return (
            X.select(self._features)
            .with_columns([pl.col(c).cast(pl.Float64) for c in self._features])
            .to_numpy()
        )

    # -- fit / predict -----------------------------------------------------
    def fit(self, X: pl.DataFrame, y: np.ndarray) -> Model:
        self._check(X)
        self._features = self.feature_columns(X)
        if not self._features:
            raise ValueError("no usable feature columns")

        cams = X[self.anchor_col].to_numpy().astype(float)
        residual = np.asarray(y, dtype=float) - cams
        matrix = self._matrix(X)
        horizons = X["horizon_h"].to_numpy()

        for h in np.unique(horizons):
            sel = horizons == h
            usable = sel & np.isfinite(residual) & np.isfinite(cams)
            if usable.sum() < 200:
                log.warning("horizon %s has only %d usable rows — skipped", h, usable.sum())
                continue
            dataset_x = matrix[usable]
            dataset_y = residual[usable]
            for q in QUANTILES:
                booster = lgb.train(
                    {**self.params, "alpha": q},
                    lgb.Dataset(dataset_x, label=dataset_y, feature_name=self._features),
                    num_boost_round=self.num_rounds,
                )
                self._models[(int(h), q)] = booster
            log.info("fitted horizon %sh on %d rows", h, usable.sum())
        return self

    def predict(self, X: pl.DataFrame) -> Quantiles:
        self._check(X)
        if not self._models:
            raise RuntimeError("call fit first")

        cams = X[self.anchor_col].to_numpy().astype(float)
        matrix = self._matrix(X)
        horizons = X["horizon_h"].to_numpy()
        out = {q: np.full(len(cams), np.nan) for q in QUANTILES}

        for h in np.unique(horizons):
            sel = horizons == h
            for q in QUANTILES:
                booster = self._models.get((int(h), q))
                if booster is None:
                    # No head for this horizon: fall back to raw CAMS rather
                    # than to nothing. Degrading to the physics forecast is the
                    # correct failure mode for a correction layer.
                    out[q][sel] = cams[sel]
                    continue
                out[q][sel] = cams[sel] + booster.predict(matrix[sel])

        return Quantiles(
            q10=np.clip(out[0.1], 0.0, None),
            q50=np.clip(out[0.5], 0.0, None),
            q90=np.clip(out[0.9], 0.0, None),
        ).sorted()

    # -- interpretation ----------------------------------------------------
    def importance(self, horizon: int | None = None, top: int = 20) -> pl.DataFrame:
        """Gain-based importance of the median head — the attribution starting point."""
        horizons = sorted({h for (h, q) in self._models if q == 0.5})
        if not horizons:
            return pl.DataFrame()
        h = horizon if horizon is not None else horizons[-1]
        booster = self._models[(h, 0.5)]
        return (
            pl.DataFrame(
                {
                    "feature": booster.feature_name(),
                    "gain": booster.feature_importance("gain"),
                }
            )
            .sort("gain", descending=True)
            .head(top)
        )
