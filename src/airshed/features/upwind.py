"""Transport features from the upwind corridor.

Delhi's severe episodes are substantially imported. Smoke lifted off a Punjab
field takes roughly 12 to 36 hours to reach the city, which makes an upwind
monitor a *leading indicator* — information about Delhi's future that no amount
of Delhi's own history contains. That is the entire argument for treating this
as an airshed rather than a city.

Three features, each doing a job local data cannot:

`upwind_pm25`
    Corridor concentration, weighted by how well the wind aligns with each
    station's bearing from Delhi and by distance. Under a north-westerly this
    is Punjab; under an easterly it is close to nothing, which is correct.

`upwind_transport_h`
    Distance divided by transport wind speed — how long the air now over the
    corridor will take to arrive. Calm air means a long transit and dispersal;
    a strong north-westerly means the corridor arrives tomorrow largely intact.

`upwind_pm25_advected`
    The corridor concentration sampled at `t - transport_h`: an estimate of what
    is *currently arriving*, rather than what is currently upwind. This is the
    Lagrangian version of the same idea and is the one a trajectory model would
    compute properly.

Lags at 12, 24 and 36 hours are supplied alongside, so the model can learn the
arrival delay from data rather than trusting our travel-time arithmetic.

The features are city-wide: they are joined on time alone and broadcast to
every NCR station. That is honest — the corridor is 65 to 340 km away, and
claiming it resolves differences between Rohini and Okhla would be nonsense.
"""

from __future__ import annotations

import logging

import numpy as np
import polars as pl

from ..config import Config, load_config
from ..store import read_range

log = logging.getLogger(__name__)

# Corridor influence decays over this distance. Much longer than the intra-NCR
# kernel because transport, not proximity, is what is being modelled.
LENGTH_SCALE_KM = 200.0
# Sharpness of the upwind cone; 2 gives a lobe about 90 degrees wide.
WIND_POWER = 2.0
# Transport speed is capped so a dead calm does not imply a 500-hour transit.
MIN_WIND_KMH = 3.0
MAX_TRANSPORT_H = 96.0
LAGS_H = (12, 24, 36)


def upwind_features(
    base: pl.DataFrame,
    cfg: Config | None = None,
    start: str | None = None,
    end: str | None = None,
) -> pl.DataFrame:
    """Attach corridor transport features to an hourly frame."""
    cfg = cfg or load_config()
    stations = cfg.upwind_stations
    if not stations:
        return _empty(base)

    times = base["time"]
    obs = read_range("cpcb_upwind", start or times.min(), end or times.max())
    if obs.is_empty():
        log.warning("no cached upwind observations — transport features unavailable")
        return _empty(base)

    # Corridor concentration per station per hour.
    wide = (
        obs.filter(pl.col("quality_flag") == "ok")
        .select(["station_id", "time", "pm25"])
        .pivot(index="time", on="station_id", values="pm25", aggregate_function="mean")
        .sort("time")
    )
    order = [s.id for s in stations if s.id in wide.columns]
    if not order:
        return _empty(base)
    matrix = np.column_stack([wide[c].cast(pl.Float64).to_numpy() for c in order])
    by_id = {s.id: s for s in stations}
    distance = np.array([by_id[c].distance_km for c in order], dtype=float)
    bearing = np.array([by_id[c].bearing_deg for c in order], dtype=float)
    decay = np.exp(-distance / LENGTH_SCALE_KM)

    # City-mean wind for each hour: the corridor is a regional feature, so a
    # regional wind is the right thing to weight it with.
    wind = (
        base.group_by("time")
        .agg(
            pl.col("met_wind_direction_10m").mean().alias("dir"),
            pl.col("met_wind_speed_10m").mean().alias("speed"),
        )
        .sort("time")
    )
    frame = wind.join(wide.select("time"), on="time", how="inner")
    idx = {t: i for i, t in enumerate(wide["time"].to_list())}
    rows = [idx[t] for t in frame["time"].to_list()]
    values = matrix[rows]
    wind_dir = frame["dir"].to_numpy().astype(float)
    wind_speed = np.clip(frame["speed"].to_numpy().astype(float), MIN_WIND_KMH, None)

    # Weight: aligned with the wind, and near.
    alignment = np.clip(np.cos(np.radians(wind_dir[:, None] - bearing[None, :])), 0.0, None)
    weights = (alignment**WIND_POWER) * decay[None, :]
    weights = np.where(np.isfinite(values), weights, 0.0)
    totals = weights.sum(axis=1)
    corridor = np.where(
        totals > 1e-6,
        np.nansum(np.where(np.isfinite(values), values, 0.0) * weights, axis=1)
        / np.maximum(totals, 1e-9),
        np.nan,
    )

    # Transport time of the weighted corridor, in hours.
    weighted_km = np.where(
        totals > 1e-6,
        (weights * distance[None, :]).sum(axis=1) / np.maximum(totals, 1e-9),
        np.nan,
    )
    transport_h = np.clip(weighted_km / wind_speed, 0.0, MAX_TRANSPORT_H)
    # Fraction of the corridor that is upwind at all: 0 means the wind is not
    # bringing anything from Punjab, whatever the concentration there.
    exposure = np.clip(alignment.max(axis=1), 0.0, 1.0)

    out = pl.DataFrame(
        {
            "time": frame["time"],
            "upwind_pm25": corridor,
            "upwind_transport_h": transport_h,
            "upwind_alignment": exposure,
            "upwind_distance_km": weighted_km,
        }
    ).sort("time")

    # Advected term: what the corridor held when the air now arriving left it.
    advected = _advect(out["time"].to_numpy(), corridor, transport_h)
    out = out.with_columns(pl.Series("upwind_pm25_advected", advected))
    out = out.with_columns(
        [pl.col("upwind_pm25").shift(k).alias(f"upwind_pm25_lag_{k}h") for k in LAGS_H]
    )

    # NaN means "the wind was not bringing anything from the corridor this
    # hour" — a real, common state, not a computation failure. Store it as
    # null: polars treats NaN as a value, so leaving it in would poison every
    # downstream mean and correlation while looking like data.
    # `upwind_alignment` keeps its zero, because zero exposure is a fact.
    nan_cols = [c for c in out.columns if c not in ("time", "upwind_alignment")]
    out = out.with_columns([pl.col(c).fill_nan(None) for c in nan_cols])

    joined = base.join(out.with_columns(pl.col("time").cast(base.schema["time"])), on="time", how="left")
    log.info(
        "upwind features: %.1f%% of rows have a corridor value",
        100.0 * joined["upwind_pm25"].is_not_null().mean(),
    )
    return joined


def _advect(times: np.ndarray, corridor: np.ndarray, transport_h: np.ndarray) -> np.ndarray:
    """Sample the corridor series `transport_h` hours before each timestamp.

    Index arithmetic is safe here only because the series is a complete hourly
    index; on a gappy series "24 rows back" would not be 24 hours (the same
    trap the feature builder avoids elsewhere).
    """
    n = len(corridor)
    out = np.full(n, np.nan)
    steps = np.where(np.isfinite(transport_h), np.round(transport_h), np.nan)
    for i in range(n):
        k = steps[i]
        if not np.isfinite(k):
            continue
        j = i - int(k)
        if 0 <= j < n:
            out[i] = corridor[j]
    return out


def _empty(base: pl.DataFrame) -> pl.DataFrame:
    columns = ["upwind_pm25", "upwind_transport_h", "upwind_alignment",
               "upwind_distance_km", "upwind_pm25_advected"]
    columns += [f"upwind_pm25_lag_{k}h" for k in LAGS_H]
    return base.with_columns([pl.lit(None, dtype=pl.Float64).alias(c) for c in columns])
