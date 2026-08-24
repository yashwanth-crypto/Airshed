"""Forecast models.

`baselines.py` is written before `corrector.py`. Always (CLAUDE.md).

Every model here implements the same three methods, so the ablation harness
cannot tell them apart:

    fit(X, y) -> self
    predict(X) -> Quantiles      # 10th / 50th / 90th percentile, never a point
    feature_names -> list[str]
"""

from .base import Model, Quantiles  # noqa: F401
from .baselines import PersistenceModel, RawCAMSModel  # noqa: F401
