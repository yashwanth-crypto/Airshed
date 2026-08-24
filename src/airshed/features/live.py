"""Features for the live forecast.

The historical path reads `cams_archive` and `meteo_archive`, both keyed by
valid time. The live path cannot: `cams_runs` and `meteo_runs` are partitioned
by *issue* date, because one partition is one model run covering five days
ahead. This module bridges that difference so the same feature builder, and
therefore the same trained model, serves both.

The asymmetry that matters is unchanged from training. Observations are read up
to **issue time** and no further; CAMS and meteorology are read at **target
time**, because a forecast for t+72 genuinely is in hand at t. Getting that
backwards would be leakage in training and impossible in production.
"""

from __future__ import annotations

import datetime as dt
import logging

import polars as pl

from ..config import Config, load_config
from ..store import latest_run
from .build import (
    _add_calendar_features,
    _add_observation_features,
    _join_cpcb,
    _join_fires,
    _join_metar,
    CAMS_COLS,
    MET_COLS,
)

log = logging.getLogger(__name__)


def build_live_base(cfg: Config | None = None) -> tuple[pl.DataFrame, dt.datetime]:
    """Hourly frame spanning the latest run, with observations up to issue time."""
    cfg = cfg or load_config()
    cams = latest_run("cams_runs")
    meteo = latest_run("meteo_runs")
    if cams.is_empty() or meteo.is_empty():
        raise RuntimeError(
            "no archived forecast runs — run `airshed archive` (and schedule it daily)"
        )

    issue = cams["issue_time"].max()
    start = (issue - dt.timedelta(days=5)).date()
    end = cams["time"].max().date()

    stations = [s.id for s in cfg.stations]
    times = pl.datetime_range(
        dt.datetime.combine(start, dt.time.min, tzinfo=dt.timezone.utc),
        dt.datetime.combine(end, dt.time(23, 0), tzinfo=dt.timezone.utc),
        interval="1h", time_zone="UTC", eager=True,
    )
    base = (
        pl.DataFrame({"station_id": stations})
        .join(pl.DataFrame({"time": times}), how="cross")
        .sort(["station_id", "time"])
    )

    base = _join_cpcb(base, start, end)
    base = _join_run(base, cams, CAMS_COLS, "cams_")
    base = _join_run(base, meteo, MET_COLS, "met_")
    base = _join_metar(base, start, end)
    base = _join_fires(base, start, end, cfg)

    from .upwind import upwind_features

    base = upwind_features(base, cfg=cfg, start=start, end=end)
    base = _add_observation_features(base)
    base = _add_calendar_features(base)
    return base.sort(["station_id", "time"]), issue


def _join_run(
    base: pl.DataFrame, run: pl.DataFrame, columns: list[str], prefix: str
) -> pl.DataFrame:
    keep = [c for c in columns if c in run.columns]
    trimmed = (
        run.select(["station_id", "time"] + keep)
        .unique(subset=["station_id", "time"], keep="last")
        .rename({c: f"{prefix}{c}" for c in keep})
    )
    out = base.join(trimmed, on=["station_id", "time"], how="left")
    missing = [f"{prefix}{c}" for c in columns if c not in run.columns]
    if missing:
        out = out.with_columns([pl.lit(None, dtype=pl.Float64).alias(c) for c in missing])
    if prefix == "cams_":
        out = out.with_columns(pl.lit("live_forecast").alias("cams_source_class"))
    return out


def build_live_supervised(
    cfg: Config | None = None,
    horizons: list[int] | None = None,
) -> tuple[pl.DataFrame, dt.datetime]:
    """One row per (station, horizon) for the run in hand.

    Issue time is the latest hour for which observations exist, not the run's
    nominal stamp: a forecast made at 06:00 with data only to 03:00 is really a
    forecast issued at 03:00, and pretending otherwise would quietly give the
    live model fresher history than it has.
    """
    cfg = cfg or load_config()
    horizons = horizons or cfg.horizons
    base, run_issue = build_live_base(cfg)

    observed = base.filter(pl.col("pm25_clean").is_not_null())
    if observed.is_empty():
        raise RuntimeError("no recent observations — run `airshed ingest live`")
    issue = min(observed["time"].max(), base["time"].max())

    at_issue = base.filter(pl.col("time") == issue).drop("time")
    forecast_cols = [
        c for c in base.columns
        if c.startswith(("cams_", "met_"))
        or c in {"fire_count_24h", "fire_frp_24h", "fire_count_72h", "fires_available",
                 "hour_sin", "hour_cos", "doy_sin", "doy_cos", "hour_ist",
                 "weekday_ist", "month"}
    ]
    targets = base.select(["station_id", "time"] + forecast_cols).rename(
        {"time": "target_time"} | {c: f"{c}_tgt" for c in forecast_cols}
    )

    rows = []
    for h in horizons:
        target_time = issue + dt.timedelta(hours=h)
        frame = at_issue.with_columns(
            pl.lit(issue).cast(pl.Datetime("us", "UTC")).alias("issue_time"),
            pl.lit(target_time).cast(pl.Datetime("us", "UTC")).alias("target_time"),
            pl.lit(h, dtype=pl.Int32).alias("horizon_h"),
        )
        rows.append(frame.join(targets, on=["station_id", "target_time"], how="inner"))

    sup = pl.concat(rows, how="vertical_relaxed")
    log.info(
        "live supervised: %d rows, issued %s (run stamped %s)", sup.height, issue, run_issue
    )
    return sup.sort(["station_id", "horizon_h"]), issue
