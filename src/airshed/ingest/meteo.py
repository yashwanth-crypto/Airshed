"""Forecast meteorology from Open-Meteo.

**R1 lives here.** Two code paths, deliberately separate, never merged:

`serving`  `api.open-meteo.com/v1/forecast` — the live run, what the model sees
           in production. Cached as `meteo_runs`, partitioned by issue date.

`training` `historical-forecast-api.open-meteo.com/v1/forecast` — *archived past
           forecast runs*, not reanalysis. Cached as `meteo_archive`.

ERA5 is not reachable from this module and must not become reachable. Training
on reanalysis and serving on forecasts is the distribution mismatch that R1
exists to prevent: reanalysis knows what the atmosphere actually did, a
forecast does not, and a model trained on the former quietly leans on
information it will never have at inference.

The model is pinned to **GFS**, not `best_match`, because
`boundary_layer_height` is served by the GFS family alone — ECMWF, ICON and JMA
all return it empty over Delhi — and mixing height is the variable the whole
coupling argument rests on. It also only exists in the archive from
**2024-09-15** onward. Both facts are measured, and the evidence tables are in
`docs/notes/data-findings.md`.

Earlier history therefore gets `lapse_2m_925` (surface-to-925 hPa temperature
difference) as the documented BLH proxy, and every row carries `blh_available`
so nothing silently trains on a column that is null for half its span.
`check_variables()` re-runs the availability check before any feature starts
depending on a variable.
"""

from __future__ import annotations

import datetime as dt
import logging
from pathlib import Path

import polars as pl

from ..config import Config, load_config
from ..store import missing_dates, write_partitioned
from .openmeteo import (
    date_chunks,
    fetch_hourly,
    fetch_hourly_by_cell,
    resolve_cells,
    station_points,
)

log = logging.getLogger(__name__)

RUNS_DATASET = "meteo_runs"
CHUNK_DAYS = 30
ARCHIVE_DATASET = "meteo_archive"


def fetch_training(
    start: dt.date | str,
    end: dt.date | str,
    cfg: Config | None = None,
) -> pl.DataFrame:
    """Archived past forecast runs over a date range. The R1-compliant training source."""
    cfg = cfg or load_config()
    src = cfg.source("meteo")
    start_d, end_d = _as_date(start), _as_date(end)

    points = station_points(cfg)
    cells = resolve_cells(
        src["training_url"], points, {"models": src["model"]}, cache_key="meteo"
    )

    frames = []
    for chunk_start, chunk_end in date_chunks(start_d, end_d, days=CHUNK_DAYS):
        log.info("meteo training %s..%s", chunk_start, chunk_end)
        frames.append(_fetch_window(src, points, cells, chunk_start, chunk_end))
    frames = [f for f in frames if not f.is_empty()]
    if not frames:
        return pl.DataFrame()
    return pl.concat(frames, how="vertical_relaxed")


def _fetch_window(src, points, cells, chunk_start, chunk_end) -> pl.DataFrame:
    df = fetch_hourly_by_cell(
        url=src["training_url"],
        points=points,
        hourly=src["hourly"],
        cells=cells,
        extra_params={
            "start_date": chunk_start.isoformat(),
            "end_date": chunk_end.isoformat(),
            "models": src["model"],
        },
    )
    if df.is_empty():
        return df
    return _derive(df).with_columns(pl.lit("archived_forecast").alias("source_class"))


def fetch_serving(
    cfg: Config | None = None,
    forecast_days: int | None = None,
    issue_time: dt.datetime | None = None,
) -> pl.DataFrame:
    """The live forecast run. What production reads."""
    cfg = cfg or load_config()
    src = cfg.source("meteo")
    issue = (issue_time or _default_issue_time(cfg)).astimezone(dt.timezone.utc)

    df = fetch_hourly(
        url=src["serving_url"],
        points=station_points(cfg),
        hourly=src["hourly"],
        extra_params={
            "forecast_days": forecast_days or src["forecast_days"],
            "models": src["model"],
        },
    )
    if df.is_empty():
        return df
    df = _derive(df).with_columns(
        pl.lit(issue).cast(pl.Datetime("us", "UTC")).alias("issue_time"),
        pl.lit("live_forecast").alias("source_class"),
    )
    return df.with_columns(
        ((pl.col("time") - pl.col("issue_time")).dt.total_minutes() // 60)
        .cast(pl.Int32)
        .alias("lead_h")
    ).sort(["station_id", "time"])


def fetch(start: dt.date | str, end: dt.date | str, **kwargs) -> pl.DataFrame:
    return fetch_training(start, end, **kwargs)


def backfill(
    start: dt.date | str,
    end: dt.date | str,
    cfg: Config | None = None,
    skip_existing: bool = True,
) -> list[Path]:
    """Cache archived forecast meteorology, one chunk at a time and resumable."""
    cfg = cfg or load_config()
    src = cfg.source("meteo")
    start_d, end_d = _as_date(start), _as_date(end)
    points = station_points(cfg)
    cells = resolve_cells(
        src["training_url"], points, {"models": src["model"]}, cache_key="meteo"
    )

    written: list[Path] = []
    for chunk_start, chunk_end in date_chunks(start_d, end_d, days=CHUNK_DAYS):
        if skip_existing and not missing_dates(ARCHIVE_DATASET, chunk_start, chunk_end):
            log.info("meteo %s..%s already cached", chunk_start, chunk_end)
            continue
        log.info("meteo training %s..%s", chunk_start, chunk_end)
        df = _fetch_window(src, points, cells, chunk_start, chunk_end)
        if df.is_empty():
            log.warning("meteo empty for %s..%s", chunk_start, chunk_end)
            continue
        written += write_partitioned(df, ARCHIVE_DATASET)
    return written


def archive_run(cfg: Config | None = None, forecast_days: int | None = None) -> list[Path]:
    """Cache the live run. Cron job partner to `cams.archive_run` (R8)."""
    df = fetch_serving(cfg=cfg, forecast_days=forecast_days)
    if df.is_empty():
        return []
    return write_partitioned(df, RUNS_DATASET, partition_col="issue_time")


def check_variables(cfg: Config | None = None) -> dict[str, float]:
    """Fraction of non-null hours per variable on the training endpoint.

    Run this before trusting a variable. A variable that comes back all-null on
    the chosen model needs a documented proxy, not a silent zero.
    """
    cfg = cfg or load_config()
    src = cfg.source("meteo")
    end = dt.date.today() - dt.timedelta(days=10)
    start = end - dt.timedelta(days=3)
    station = cfg.stations[0]
    from .openmeteo import Point

    df = fetch_hourly(
        url=src["training_url"],
        points=[Point(station.id, station.lat, station.lon)],
        hourly=src["hourly"],
        extra_params={
            "start_date": start.isoformat(),
            "end_date": end.isoformat(),
            "models": src["model"],
        },
    )
    if df.is_empty():
        return {}
    n = df.height
    return {
        c: round(1.0 - df[c].null_count() / n, 4)
        for c in src["hourly"]
        if c in df.columns
    }


# ---------------------------------------------------------------------------
def _derive(df: pl.DataFrame) -> pl.DataFrame:
    """Add wind components and a ventilation index.

    Wind direction in degrees is discontinuous at 360/0 and a tree model will
    happily split it in the wrong place, so u/v go alongside it. Ventilation
    (mixing height x transport wind) is the single most useful scalar for
    "does today's emission stay here or leave" and is exactly the coupling this
    project claims to model, so it is computed once, here, not per model.
    """
    out = df
    if {"wind_speed_10m", "wind_direction_10m"} <= set(out.columns):
        rad = (pl.col("wind_direction_10m") * 3.141592653589793 / 180.0)
        out = out.with_columns(
            (-pl.col("wind_speed_10m") * rad.sin()).alias("u10"),
            (-pl.col("wind_speed_10m") * rad.cos()).alias("v10"),
        )
    if {"boundary_layer_height", "wind_speed_10m"} <= set(out.columns):
        # km/h -> m/s, then m * m/s = m^2/s
        out = out.with_columns(
            (pl.col("boundary_layer_height") * pl.col("wind_speed_10m") / 3.6).alias(
                "ventilation_index"
            )
        )

    # Low-level stability. T(2m) - T(925 hPa) is positive when the surface is
    # warmer than the air ~750 m above it (mixing) and negative under an
    # inversion (trapping). This is the BLH proxy for the pre-2024-09 archive,
    # where boundary_layer_height does not exist, and it is a useful predictor
    # in its own right afterwards — winter episodes in Delhi are inversions.
    if {"temperature_2m", "temperature_925hPa"} <= set(out.columns):
        out = out.with_columns(
            (pl.col("temperature_2m") - pl.col("temperature_925hPa")).alias("lapse_2m_925"),
            (pl.col("temperature_925hPa") > pl.col("temperature_2m")).alias("inversion"),
        )
    if {"temperature_925hPa", "temperature_850hPa"} <= set(out.columns):
        out = out.with_columns(
            (pl.col("temperature_925hPa") - pl.col("temperature_850hPa")).alias("lapse_925_850")
        )

    # Says plainly whether real mixing height was available for this row, so a
    # model can be told "use BLH where it exists, the proxy where it does not"
    # instead of silently learning from a column that is null for half the years.
    if "boundary_layer_height" in out.columns:
        out = out.with_columns(
            pl.col("boundary_layer_height").is_not_null().alias("blh_available")
        )
    return out.sort(["station_id", "time"])


def _default_issue_time(cfg: Config) -> dt.datetime:
    hour = int(cfg.forecast["issue_hour_utc"])
    return dt.datetime.now(dt.timezone.utc).replace(
        hour=hour, minute=0, second=0, microsecond=0
    )


def _as_date(value: dt.date | str) -> dt.date:
    return dt.date.fromisoformat(value) if isinstance(value, str) else value
