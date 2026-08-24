"""Why is Thursday bad?

The forecast number is not the product. A control-room officer deciding whether
to invoke GRAP Stage III needs to know *what is driving it* — because a spike
caused by upwind fires calls for different action than one caused by a shallow
inversion over local traffic, and because a forecast nobody can interrogate is
a forecast nobody will act on.

SHAP values come from LightGBM directly (`pred_contrib=True`), which computes
exact tree SHAP — no extra dependency, and no sampling approximation.

One subtlety that shapes how this reads: the model predicts the **residual**
against CAMS, so a contribution here answers "why is this hour different from
what the physics model said", not "why is the air dirty". Those are different
questions and conflating them would be misleading. The regional load itself is
reported separately as the CAMS baseline, so the two parts of the story stay
distinct:

    forecast = CAMS regional forecast + our local correction
                                        ^ this is what SHAP explains
"""

from __future__ import annotations

import logging

import numpy as np
import polars as pl

log = logging.getLogger(__name__)

# Feature prefixes grouped into causes a human would recognise. Order matters:
# the first matching rule wins, so more specific prefixes come first.
GROUPS: list[tuple[str, tuple[str, ...]]] = [
    ("upwind fires", ("fire",)),  # fire_count_24h and fires_available alike
    ("shallow mixing", ("met_boundary_layer_height", "met_ventilation_index",
                        "met_lapse", "met_inversion", "met_blh")),
    ("wind and transport", ("met_wind", "met_u10", "met_v10", "met_gusts",
                            "nbr_pm25_wind")),
    ("neighbouring stations", ("nbr_",)),
    ("humidity and fog", ("met_relative_humidity", "met_dew_point", "metar_",
                          "vis_", "pred_visibility")),
    ("regional forecast (CAMS)", ("cams_",)),
    ("recent local levels", ("obs_",)),
    ("rain and clearing", ("met_precipitation", "met_cloud", "met_shortwave")),
    ("time of day and season", ("hour", "doy", "weekday", "month", "season")),
    ("other weather", ("met_",)),
]

# Phrasing for a driver that pushes the forecast up or down.
DIRECTION = {True: "raising", False: "lowering"}


def group_of(feature: str) -> str:
    for name, prefixes in GROUPS:
        if feature.startswith(prefixes):
            return name
    return "other"


def contributions(
    model,
    X: pl.DataFrame,
    horizon: int,
    quantile: float = 0.5,
) -> pl.DataFrame:
    """Signed SHAP contribution of every feature, for one horizon's median head.

    Returns one row per input row per feature — long format, so it can be
    grouped, summed or filtered without recomputing anything.
    """
    inner = getattr(model, "inner", model)          # unwrap CalibratedModel
    inner = getattr(inner, "pm_model", inner)       # unwrap CoupledCorrector
    booster = inner._models.get((int(horizon), quantile))
    if booster is None:
        raise KeyError(f"no head fitted for horizon {horizon} quantile {quantile}")

    rows = X.filter(pl.col("horizon_h") == horizon)
    if rows.is_empty():
        return pl.DataFrame()

    matrix = inner._matrix(rows)
    shap = booster.predict(matrix, pred_contrib=True)
    features = inner._features
    # Last column is the model's base value, not a feature.
    values, base = shap[:, :-1], shap[:, -1]

    n_rows, n_features = values.shape
    return pl.DataFrame(
        {
            "row": np.repeat(np.arange(n_rows), n_features),
            "feature": features * n_rows,
            "contribution": values.reshape(-1),
        }
    ).with_columns(
        pl.col("feature")
        .map_elements(group_of, return_dtype=pl.Utf8)
        .alias("driver"),
        pl.lit(float(np.mean(base))).alias("base_value"),
    )


def drivers(
    model,
    X: pl.DataFrame,
    horizon: int,
    top: int = 4,
) -> pl.DataFrame:
    """The named drivers of the correction, aggregated to human categories.

    Individual feature importances are not an explanation — nobody acts on
    "met_wind_speed_100m contributed +8.3". Summing to a category does produce
    something actionable, and summing is exactly what SHAP values permit.
    """
    detail = contributions(model, X, horizon)
    if detail.is_empty():
        return detail
    return (
        detail.group_by("driver")
        .agg(
            pl.col("contribution").mean().alias("mean_contribution"),
            pl.col("contribution").abs().mean().alias("mean_magnitude"),
        )
        .sort("mean_magnitude", descending=True)
        .head(top)
    )


def explain_row(
    model,
    X: pl.DataFrame,
    horizon: int,
    row_index: int = 0,
    top: int = 3,
) -> dict:
    """A single forecast, explained in words and numbers."""
    detail = contributions(model, X, horizon)
    if detail.is_empty():
        return {}
    one = detail.filter(pl.col("row") == row_index)
    by_driver = (
        one.group_by("driver")
        .agg(pl.col("contribution").sum().alias("contribution"))
        .sort(pl.col("contribution").abs(), descending=True)
        .head(top)
    )
    rows = X.filter(pl.col("horizon_h") == horizon)
    context = rows.row(row_index, named=True)

    parts = []
    for item in by_driver.iter_rows(named=True):
        if abs(item["contribution"]) < 1.0:
            continue
        parts.append(
            f"{item['driver']} ({DIRECTION[item['contribution'] > 0]} it by "
            f"{abs(item['contribution']):.0f} µg/m³)"
        )

    cams = context.get("cams_pm2_5_tgt")
    return {
        "station_id": context.get("station_id"),
        "issue_time": context.get("issue_time"),
        "target_time": context.get("target_time"),
        "horizon_h": horizon,
        "cams_baseline": cams,
        "drivers": by_driver.to_dicts(),
        "sentence": _sentence(parts, cams),
    }


def _sentence(parts: list[str], cams: float | None) -> str:
    if not parts:
        return "No single driver dominates; the forecast tracks the regional CAMS signal."
    lead = "CAMS puts the regional level at "
    prefix = f"{lead}{cams:.0f} µg/m³. " if cams is not None else ""
    if len(parts) == 1:
        return prefix + f"Our correction is driven by {parts[0]}."
    return prefix + "Our correction is driven mainly by " + ", then ".join(parts) + "."
