"""GRAP stage mapping and the CPCB National AQI.

Scope note: this file currently implements the **deterministic** mapping —
concentration to AQI to GRAP stage — because Phase 1 needs it to label
historical episodes and to state how rare Stage III/IV actually are. Turning a
predicted *distribution* into per-stage probabilities is Phase 3 work and is
not started here (see `docs/BUILD_PLAN.md`).

Two things that are easy to get wrong and are therefore enforced:

* The CPCB AQI is computed from a **24-hour rolling average**, not an
  instantaneous value. An hourly PM2.5 of 300 does not mean AQI 500.
* GRAP is invoked on **Delhi's city-wide average AQI**, not per station
  (`config.toml`, `grap.aggregate`). A single bad station is not Stage IV.
"""

from __future__ import annotations

import numpy as np
import polars as pl

from .config import Config, load_config

AQI_MAX = 500
MIN_HOURS_FOR_AQI = 16  # CPCB requires 16 of 24 hours for a valid daily value


def pm25_to_aqi(pm25: pl.Expr, cfg: Config | None = None) -> pl.Expr:
    """CPCB PM2.5 sub-index from a **24-hour average** concentration.

    Linear interpolation inside the bracket the concentration falls in.
    Above the top bracket the index is clamped to 500 — CPCB does not define
    an AQI beyond that, and extrapolating invents a number no policy uses.
    """
    cfg = cfg or load_config()
    brackets = cfg.pm25_breakpoints
    expr = pl.lit(None, dtype=pl.Float64)
    for b in reversed(brackets):
        sub = (
            (b.i_hi - b.i_lo) / (b.c_hi - b.c_lo) * (pm25 - b.c_lo) + b.i_lo
        )
        expr = pl.when(pm25 <= b.c_hi).then(sub).otherwise(expr)
    top = brackets[-1]
    expr = pl.when(pm25 > top.c_hi).then(pl.lit(float(AQI_MAX))).otherwise(expr)
    return expr.clip(0, AQI_MAX).round(0)


def add_rolling_aqi(
    df: pl.DataFrame,
    value_col: str = "pm25_clean",
    over: str = "station_id",
    cfg: Config | None = None,
) -> pl.DataFrame:
    """Add `pm25_24h` and `aqi_pm25` to an hourly frame.

    The rolling window needs `MIN_HOURS_FOR_AQI` real observations, so a
    station that reported four hours out of twenty-four produces a null AQI
    rather than a confident-looking wrong one (R6).
    """
    cfg = cfg or load_config()
    out = df.sort([over, "time"]).with_columns(
        pl.col(value_col)
        .rolling_mean(24, min_samples=MIN_HOURS_FOR_AQI)
        .over(over)
        .alias("pm25_24h")
    )
    return out.with_columns(pm25_to_aqi(pl.col("pm25_24h"), cfg).alias("aqi_pm25"))


def aqi_to_stage(aqi: pl.Expr, cfg: Config | None = None) -> pl.Expr:
    """GRAP stage (0-4) from AQI. 0 means no stage invoked."""
    cfg = cfg or load_config()
    expr = pl.lit(0, dtype=pl.Int32)
    for stage in cfg.grap_stages:
        expr = (
            pl.when(aqi.is_between(stage.aqi_min, stage.aqi_max))
            .then(pl.lit(stage.stage, dtype=pl.Int32))
            .otherwise(expr)
        )
    return pl.when(aqi.is_null()).then(None).otherwise(expr)


def city_aqi(
    df: pl.DataFrame,
    cfg: Config | None = None,
    aqi_col: str = "aqi_pm25",
    min_stations: int = 5,
) -> pl.DataFrame:
    """Collapse per-station AQI to the city-wide value GRAP is actually keyed to.

    An hour covered by fewer than `min_stations` reporting stations yields a
    null city AQI. During an outage the surviving stations are not a random
    sample of the city, so averaging three of them is not a city average.
    """
    cfg = cfg or load_config()
    agg = (
        df.group_by("time")
        .agg(
            pl.col(aqi_col).mean().alias("city_aqi"),
            pl.col(aqi_col).max().alias("worst_station_aqi"),
            pl.col(aqi_col).count().alias("n_stations"),
        )
        .sort("time")
    )
    agg = agg.with_columns(
        pl.when(pl.col("n_stations") >= min_stations)
        .then(pl.col("city_aqi"))
        .otherwise(None)
        .alias("city_aqi")
    )
    return agg.with_columns(
        aqi_to_stage(pl.col("city_aqi"), cfg).alias("grap_stage")
    )


def stage_frequency(city: pl.DataFrame, cfg: Config | None = None) -> pl.DataFrame:
    """How often each stage actually occurs — the imbalance R5 warns about.

    Read this before believing any classification metric. Stage IV is rare
    enough that a model predicting "never Stage IV" scores well on accuracy,
    which is exactly why accuracy is banned from our results tables.
    """
    cfg = cfg or load_config()
    names = {s.stage: s.name for s in cfg.grap_stages} | {0: "None"}
    total = city.filter(pl.col("grap_stage").is_not_null()).height
    return (
        city.filter(pl.col("grap_stage").is_not_null())
        .group_by("grap_stage")
        .agg(pl.len().alias("hours"))
        .with_columns(
            (pl.col("hours") / max(total, 1) * 100).round(2).alias("pct_of_hours"),
            pl.col("grap_stage").replace_strict(names, default="?").alias("stage_name"),
        )
        .sort("grap_stage")
    )


# ---------------------------------------------------------------------------
# Phase 3: from a predicted distribution to a stage probability
# ---------------------------------------------------------------------------
def aqi_to_pm25(aqi: float, cfg: Config | None = None) -> float:
    """Invert the CPCB sub-index: the 24 h PM2.5 that produces this AQI.

    GRAP thresholds are written in AQI, our model predicts µg/m³, and the
    mapping is piecewise linear, so the inverse is exact rather than fitted.
    Above the top bracket the AQI saturates at 500, so the inverse is only
    defined up to that concentration and is clamped there.
    """
    cfg = cfg or load_config()
    for b in cfg.pm25_breakpoints:
        if b.i_lo <= aqi <= b.i_hi:
            span = b.i_hi - b.i_lo
            if span == 0:
                return b.c_lo
            return b.c_lo + (aqi - b.i_lo) * (b.c_hi - b.c_lo) / span
    top = cfg.pm25_breakpoints[-1]
    return top.c_hi if aqi > top.i_hi else cfg.pm25_breakpoints[0].c_lo


def stage_bounds(cfg: Config | None = None) -> list[tuple[int, str, float, float]]:
    """(stage, name, lower µg/m³, upper µg/m³) for each GRAP stage."""
    cfg = cfg or load_config()
    out = []
    for stage in cfg.grap_stages:
        lo = aqi_to_pm25(stage.aqi_min, cfg)
        hi = aqi_to_pm25(stage.aqi_max, cfg)
        out.append((stage.stage, stage.name, lo, hi))
    return out


def _cdf(q10: np.ndarray, q50: np.ndarray, q90: np.ndarray, x: float) -> np.ndarray:
    """P(X <= x) from three quantiles, by interpolation.

    The predictive distribution is only known at three points, so this
    interpolates linearly between them and decays exponentially outside. It is
    an approximation and is stated as one: the alternative — assuming a normal
    distribution — is worse, because PM2.5 error is strongly right-skewed and a
    normal tail would systematically understate the probability of a severe
    stage, which is the one number a decision-maker must not be misled about.
    """
    q10 = np.asarray(q10, dtype=float)
    q50 = np.asarray(q50, dtype=float)
    q90 = np.asarray(q90, dtype=float)
    lo_w = np.maximum(q50 - q10, 1e-6)
    hi_w = np.maximum(q90 - q50, 1e-6)

    p = np.empty_like(q50)
    below = x <= q10
    mid_lo = (x > q10) & (x <= q50)
    mid_hi = (x > q50) & (x <= q90)
    above = x > q90

    # Exponential tails anchored so the density is continuous at q10 and q90.
    p[below] = 0.10 * np.exp((x - q10[below]) / lo_w[below])
    p[mid_lo] = 0.10 + 0.40 * (x - q10[mid_lo]) / lo_w[mid_lo]
    p[mid_hi] = 0.50 + 0.40 * (x - q50[mid_hi]) / hi_w[mid_hi]
    p[above] = 1.0 - 0.10 * np.exp(-(x - q90[above]) / hi_w[above])
    return np.clip(p, 0.0, 1.0)


def stage_probabilities(pred, cfg: Config | None = None) -> pl.DataFrame:
    """Probability of each GRAP stage, and of *at least* each stage.

    `p_at_least_3` is the number a decision-maker actually acts on: the
    question is never "will it be exactly Very Poor", it is "how likely is it
    to reach Severe or worse".
    """
    cfg = cfg or load_config()
    bounds = stage_bounds(cfg)
    cols: dict[str, np.ndarray] = {}

    for stage, _name, lo, hi in bounds:
        p_below_lo = _cdf(pred.q10, pred.q50, pred.q90, lo)
        p_below_hi = _cdf(pred.q10, pred.q50, pred.q90, hi)
        cols[f"p_stage_{stage}"] = np.clip(p_below_hi - p_below_lo, 0.0, 1.0)
        cols[f"p_at_least_{stage}"] = 1.0 - p_below_lo

    cols["p_stage_0"] = np.clip(1.0 - cols["p_at_least_1"], 0.0, 1.0)
    cols["expected_stage"] = sum(
        stage * cols[f"p_stage_{stage}"] for stage, _n, _l, _h in bounds
    )
    return pl.DataFrame(cols)


def observed_stage(pm25_24h: np.ndarray, cfg: Config | None = None) -> np.ndarray:
    """The stage that actually occurred, from an observed 24 h mean."""
    cfg = cfg or load_config()
    frame = pl.DataFrame({"c": np.asarray(pm25_24h, dtype=float)})
    aqi = frame.select(pm25_to_aqi(pl.col("c"), cfg).alias("aqi"))
    stage = aqi.select(aqi_to_stage(pl.col("aqi"), cfg).alias("s"))["s"]
    return stage.to_numpy()


def city_average_stations(cfg: Config | None = None) -> list[str]:
    """Station ids that define the statutory city average.

    Single source of truth for the scoping, shared by `features.build` (which
    applies it) and the GRAP report (which has to state it). Two places deciding
    what "Delhi's average" means is how the report and the model end up
    describing different quantities.
    """
    cfg = cfg or load_config()
    cities = cfg.raw.get("grap", {}).get("city_average_cities")
    if not cities:
        return [s.id for s in cfg.stations]
    wanted = {c.strip().lower() for c in cities}
    return [s.id for s in cfg.stations if s.city.strip().lower() in wanted]
