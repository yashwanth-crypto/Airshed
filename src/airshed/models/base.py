"""The model interface every forecaster implements.

Predictions are distributions, not point estimates (CLAUDE.md, Conventions).
A single number for Thursday's PM2.5 hides exactly the information a GRAP
decision needs: how likely the threshold is to be crossed. Even persistence
returns quantiles here, so the ablation compares like with like and so nothing
downstream has to special-case a baseline.
"""

from __future__ import annotations

from dataclasses import dataclass

import numpy as np
import polars as pl


@dataclass(frozen=True, slots=True)
class Quantiles:
    """10th / 50th / 90th percentile predictions, aligned row-wise with the input."""

    q10: np.ndarray
    q50: np.ndarray
    q90: np.ndarray

    def __post_init__(self) -> None:
        n = len(self.q50)
        if not (len(self.q10) == len(self.q90) == n):
            raise ValueError("quantile arrays must be the same length")

    def __len__(self) -> int:
        return len(self.q50)

    @property
    def interval_width(self) -> np.ndarray:
        return self.q90 - self.q10

    def to_frame(self) -> pl.DataFrame:
        return pl.DataFrame({"q10": self.q10, "q50": self.q50, "q90": self.q90})

    def sorted(self) -> Quantiles:
        """Enforce q10 <= q50 <= q90.

        Independently fitted quantile models can cross, especially in the tail
        where severe episodes live. Sorting is the standard, honest repair: it
        cannot make calibration worse and it stops a q90 below the median from
        producing a negative-width interval on the exact rows that matter most.
        """
        stacked = np.sort(np.vstack([self.q10, self.q50, self.q90]), axis=0)
        return Quantiles(q10=stacked[0], q50=stacked[1], q90=stacked[2])


class Model:
    """Base class. Subclasses override `fit` and `predict`."""

    name: str = "model"
    #: Set by subclasses that need specific columns present in X.
    requires: tuple[str, ...] = ()

    def fit(self, X: pl.DataFrame, y: np.ndarray) -> Model:  # noqa: ARG002
        return self

    def predict(self, X: pl.DataFrame) -> Quantiles:
        raise NotImplementedError

    def _check(self, X: pl.DataFrame) -> None:
        missing = [c for c in self.requires if c not in X.columns]
        if missing:
            raise KeyError(f"{self.name} needs columns {missing}, which are absent")

    def __repr__(self) -> str:
        return f"<{type(self).__name__} {self.name!r}>"
