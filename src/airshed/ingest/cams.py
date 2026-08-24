"""CAMS PM2.5 forecasts via Open-Meteo — the spine of the system.

CAMS supplies the *future*: predicted transport, regional build-up, upwind
loading. Our model supplies the *local*: bias correction and sub-grid detail.
See CLAUDE.md, "THE CORE ARCHITECTURAL DECISION".

Two datasets, deliberately kept apart:

`cams_runs`   One partition per forecast **issue date**. Rows carry `issue_time`
              and `lead_h`, so a 72 h-lead value is identifiable as such. This
              is the honest training source and it only accumulates forward
              from the day we start archiving. Populate it from cron.

`cams_archive`  Open-Meteo's air-quality archive, back to 2013. Open-Meteo
              builds this from CAMS output at short lead, so a row here is
              *not* a 72 h forecast: it is roughly analysis-quality. It gives
              us years of history immediately, at the cost of a train/serve
              distribution gap.

That gap is the same hazard R1 names for ERA5, so it is handled the same way:
never silently. Every row carries `source_class`, the feature builder records
which one it used, and `docs/notes/cams-lead-time.md` states the caveat.
Ablations must report archive-trained and run-trained results separately once
`cams_runs` holds enough days.
"""

from __future__ import annotations

import datetime as dt
import logging
from pathlib import Path

import polars as pl

from ..config import Config, load_config
from ..store import write_partitioned
from ..store import missing_dates
from .openmeteo import (
    date_chunks,
    fetch_hourly,
    fetch_hourly_by_cell,
    resolve_cells,
    station_points,
)

log = logging.getLogger(__name__)

RUNS_DATASET = "cams_runs"
CHUNK_DAYS = 30
ARCHIVE_DATASET = "cams_archive"


def fetch_run(
    cfg: Config | None = None,
    forecast_days: int | None = None,
    issue_time: dt.datetime | None = None,
) -> pl.DataFrame:
    """Fetch the current CAMS forecast run for every station.

    `issue_time` defaults to the current UTC day at the configured issue hour.
    It is a label for the run, not a claim about CAMS initialisation time; what
    matters downstream is that `lead_h` is monotone within a run and that no
    feature built for horizon h ever reads a row with `lead_h < h`.
    """
    cfg = cfg or load_config()
    src = cfg.source("cams")
    days = forecast_days or src["forecast_days"]
    issue = issue_time or _default_issue_time(cfg)

    df = fetch_hourly(
        url=src["url"],
        points=station_points(cfg),
        hourly=src["hourly"],
        extra_params={"forecast_days": days, "domains": src["domains"]},
    )
    if df.is_empty():
        return df
    return _stamp(df, issue_time=issue, source_class="live_forecast")


def fetch_archive(
    start: dt.date | str,
    end: dt.date | str,
    cfg: Config | None = None,
) -> pl.DataFrame:
    """Fetch archived CAMS values over a past date range (short-lead — see module docstring)."""
    cfg = cfg or load_config()
    src = cfg.source("cams")
    start_d, end_d = _as_date(start), _as_date(end)

    points = station_points(cfg)
    cells = resolve_cells(
        src["url"], points, {"domains": src["domains"]}, cache_key="cams"
    )

    frames = []
    for chunk_start, chunk_end in date_chunks(start_d, end_d, days=CHUNK_DAYS):
        log.info("cams archive %s..%s", chunk_start, chunk_end)
        frames.append(_fetch_window(src, points, cells, chunk_start, chunk_end))
    frames = [f for f in frames if not f.is_empty()]
    if not frames:
        return pl.DataFrame()
    return pl.concat(frames, how="vertical_relaxed")


def _fetch_window(src, points, cells, chunk_start, chunk_end) -> pl.DataFrame:
    df = fetch_hourly_by_cell(
        url=src["url"],
        points=points,
        hourly=src["hourly"],
        cells=cells,
        extra_params={
            "start_date": chunk_start.isoformat(),
            "end_date": chunk_end.isoformat(),
            "domains": src["domains"],
        },
    )
    if df.is_empty():
        return df
    return _stamp(df, issue_time=None, source_class="archive_short_lead")


# Uniform entry point (CLAUDE.md, Conventions): fetch(start, end, **kwargs).
def fetch(start: dt.date | str, end: dt.date | str, **kwargs) -> pl.DataFrame:
    return fetch_archive(start, end, **kwargs)


def backfill(
    start: dt.date | str,
    end: dt.date | str,
    cfg: Config | None = None,
    skip_existing: bool = True,
) -> list[Path]:
    """Fetch and cache the archive for a date range.

    Writes chunk by chunk rather than accumulating a year in memory and saving
    at the end: a rate-limit failure at month eleven then costs one month, not
    eleven, and re-running resumes instead of restarting. `skip_existing` makes
    the whole command idempotent and cheap to repeat.
    """
    cfg = cfg or load_config()
    src = cfg.source("cams")
    start_d, end_d = _as_date(start), _as_date(end)
    points = station_points(cfg)
    cells = resolve_cells(
        src["url"], points, {"domains": src["domains"]}, cache_key="cams"
    )

    written: list[Path] = []
    for chunk_start, chunk_end in date_chunks(start_d, end_d, days=CHUNK_DAYS):
        if skip_existing and not missing_dates(ARCHIVE_DATASET, chunk_start, chunk_end):
            log.info("cams archive %s..%s already cached", chunk_start, chunk_end)
            continue
        log.info("cams archive %s..%s", chunk_start, chunk_end)
        df = _fetch_window(src, points, cells, chunk_start, chunk_end)
        if df.is_empty():
            log.warning("cams archive empty for %s..%s", chunk_start, chunk_end)
            continue
        written += write_partitioned(df, ARCHIVE_DATASET)
    return written


def archive_run(
    cfg: Config | None = None,
    forecast_days: int | None = None,
) -> list[Path]:
    """Cache today's forecast run. This is the cron job (R8)."""
    df = fetch_run(cfg=cfg, forecast_days=forecast_days)
    if df.is_empty():
        log.warning("cams run returned nothing")
        return []
    return write_partitioned(df, RUNS_DATASET, partition_col="issue_time")


# ---------------------------------------------------------------------------
def _stamp(df: pl.DataFrame, issue_time: dt.datetime | None, source_class: str) -> pl.DataFrame:
    """Attach run provenance. `lead_h` is null when the run time is unknown."""
    if issue_time is None:
        return df.with_columns(
            pl.lit(None, dtype=pl.Datetime("us", "UTC")).alias("issue_time"),
            pl.lit(None, dtype=pl.Int32).alias("lead_h"),
            pl.lit(source_class).alias("source_class"),
        ).sort(["station_id", "time"])

    issue = issue_time.astimezone(dt.timezone.utc)
    return df.with_columns(
        pl.lit(issue).cast(pl.Datetime("us", "UTC")).alias("issue_time"),
        pl.lit(source_class).alias("source_class"),
    ).with_columns(
        ((pl.col("time") - pl.col("issue_time")).dt.total_minutes() // 60)
        .cast(pl.Int32)
        .alias("lead_h")
    ).sort(["station_id", "time"])


def _default_issue_time(cfg: Config) -> dt.datetime:
    hour = int(cfg.forecast["issue_hour_utc"])
    now = dt.datetime.now(dt.timezone.utc)
    return now.replace(hour=hour, minute=0, second=0, microsecond=0)


def _as_date(value: dt.date | str) -> dt.date:
    return dt.date.fromisoformat(value) if isinstance(value, str) else value
