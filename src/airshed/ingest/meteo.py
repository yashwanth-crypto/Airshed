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
from ..store import missing_dates, partition_path, write_partitioned
from .openmeteo import (
    Point,
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
LEADMATCHED_DATASET = "meteo_leadmatched"


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


def backfill_new_stations(
    start: dt.date | str,
    end: dt.date | str,
    cfg: Config | None = None,
    station_ids: list[str] | None = None,
    chunk_days: int = 10,
) -> list[Path]:
    """Fetch archived meteorology for stations the partitions do not carry.

    `repair.expand` copies a cell-mate's rows and needs no network, which is the
    right tool when a new station lands inside a cell we already fetched. It is
    the wrong tool here. GFS cells are ~0.11 deg, and the 2026-08 expansion put
    stations 0.25-0.44 deg from the nearest served cell — Meerut is four cells
    out. Copying would have handed those stations weather from up to 49 km away
    and nothing would have complained.

    So: resolve the real cells for these stations, fetch them, merge. The cost
    is only the genuinely new cells, because `fetch_hourly_by_cell` requests one
    series per distinct cell however many stations share it.
    """
    cfg = cfg or load_config()
    src = cfg.source("meteo")
    start_d, end_d = _as_date(start), _as_date(end)

    if station_ids is None:
        from .repair import missing_stations

        station_ids = missing_stations(ARCHIVE_DATASET, cfg)
    if not station_ids:
        log.info("%s already carries every configured station", ARCHIVE_DATASET)
        return []

    by_id = {s.id: s for s in cfg.stations}
    points = [
        Point(s.id, s.lat, s.lon, station_name=s.name)
        for s in (by_id[i] for i in station_ids if i in by_id)
    ]
    # A fresh cell map for these points only, and deliberately not the cached
    # one: the cache is keyed to the full station set and adding stations
    # invalidates it, so reusing it here would silently reintroduce the wrong
    # cell for exactly the stations this function exists to fix.
    cells = resolve_cells(src["training_url"], points, {"models": src["model"]})
    log.info(
        "meteo for %d new station(s) over %d distinct cell(s)",
        len(points), len(set(cells.values())),
    )

    # Smaller chunks than `backfill` uses. That path asks for 16 cells; the
    # 2026-08 expansion spread the new stations over more cells than that, and
    # 30 days x 28 variables x ~20 cells in one request read-timed out and lost
    # the whole run. Requests are cheap; a run that dies at day 500 is not.
    written: list[Path] = []
    for chunk_start, chunk_end in date_chunks(start_d, end_d, days=chunk_days):
        log.info("meteo (new stations) %s..%s", chunk_start, chunk_end)
        try:
            fresh = _fetch_window(src, points, cells, chunk_start, chunk_end)
        except Exception as exc:
            # Keep going and report the hole. Losing 500 good days because day
            # 501 timed out is the failure mode this backfill already hit once.
            log.error(
                "meteo chunk %s..%s failed, continuing: %s",
                chunk_start, chunk_end, str(exc)[:200],
            )
            continue
        if fresh.is_empty():
            log.warning("meteo empty for %s..%s", chunk_start, chunk_end)
            continue
        for day, part in fresh.with_columns(
            pl.col("time").dt.date().alias("_d")
        ).group_by("_d", maintain_order=True):
            day = day[0] if isinstance(day, tuple) else day
            path = partition_path(ARCHIVE_DATASET, day)
            part = part.drop("_d")
            if path.is_file():
                merged = pl.concat(
                    [pl.read_parquet(path), part], how="diagonal_relaxed"
                )
            else:
                merged = part
            # Existing rows win: a refetch must not rewrite audited history.
            merged = merged.unique(subset=["station_id", "time"], keep="first")
            written += write_partitioned(merged, ARCHIVE_DATASET)
    log.info("merged new stations into %d partition(s)", len(written))
    return written


def archive_run(cfg: Config | None = None, forecast_days: int | None = None) -> list[Path]:
    """Cache the live run. Cron job partner to `cams.archive_run` (R8)."""
    df = fetch_serving(cfg=cfg, forecast_days=forecast_days)
    if df.is_empty():
        return []
    return write_partitioned(df, RUNS_DATASET, partition_col="issue_time")


# ---------------------------------------------------------------------------
# Lead-matched training meteorology
# ---------------------------------------------------------------------------
# `fetch_training` returns the best available forecast for each past hour, which
# is a short-lead one. A model trained on that and served a real 72 h forecast is
# reading a cleaner input in training than it will ever get in production — the
# same failure mode R1 names for ERA5, one level down. The Previous Runs API
# serves, for a valid hour on day D, the value from the run initialised D-N, so
# training can see the forecast at the lead it will actually be used at.
#
# Not every variable is available this way, and the ones that are not include
# the most important one. See `lead_matched_unavailable` in config.toml: BLH,
# visibility and every pressure-level variable have no `_previous_dayN` form.
# Those columns stay short-lead here and the gap closes only forward, as
# `meteo_runs` accumulates. Nothing about that is hidden: `lead_day` is on every
# row, and the feature builder marks which columns it actually replaced.


def fetch_previous_runs(
    start: dt.date | str,
    end: dt.date | str,
    cfg: Config | None = None,
    lead_days: list[int] | None = None,
    station_ids: list[str] | None = None,
    cells: dict[str, tuple[float, float]] | None = None,
) -> pl.DataFrame:
    """Archived forecasts at *known lead*, long-format on `lead_day`.

    One row per (station_id, time, lead_day). `lead_day = N` means the value
    came from the run initialised N days before the valid day, so the true lead
    is `24N + hour_of_day` — a range, not a point, and deliberately the
    pessimistic end of it: lead_day 3 covers 72-95 h, never less than 72.
    """
    cfg = cfg or load_config()
    src = cfg.source("meteo")
    days = lead_days or list(src["lead_matched_days"])
    variables = list(src["lead_matched_hourly"])
    start_d, end_d = _as_date(start), _as_date(end)

    points = station_points(cfg)
    if station_ids is not None:
        wanted = set(station_ids)
        points = [p for p in points if p.key in wanted]
    if cells is None:
        # Resolving costs a request, so a caller looping over chunks must pass
        # `cells` in rather than letting every chunk rediscover the same map.
        # Not doing that is what earned a 429 and stalled this backfill at
        # 2026-02-12: 56 chunks meant 56 redundant probe calls.
        #
        # No `cache_key` for a subset: the cached map is keyed to the full
        # station set, and reusing it here would resolve these stations against
        # cells chosen for someone else.
        cells = (
            resolve_cells(src["previous_runs_url"], points, {"models": src["model"]})
            if station_ids is not None
            else resolve_cells(
                src["previous_runs_url"], points, {"models": src["model"]},
                cache_key="meteo",
            )
        )
    hourly = [f"{v}_previous_day{d}" for d in days for v in variables]

    frames = []
    for chunk_start, chunk_end in date_chunks(start_d, end_d, days=CHUNK_DAYS):
        log.info("meteo previous-runs %s..%s", chunk_start, chunk_end)
        wide = fetch_hourly_by_cell(
            url=src["previous_runs_url"],
            points=points,
            hourly=hourly,
            cells=cells,
            extra_params={
                "start_date": chunk_start.isoformat(),
                "end_date": chunk_end.isoformat(),
                "models": src["model"],
            },
        )
        if wide.is_empty():
            log.warning("meteo previous-runs empty for %s..%s", chunk_start, chunk_end)
            continue
        frames.append(_to_long(wide, variables, days))

    if not frames:
        return pl.DataFrame()
    return pl.concat(frames, how="vertical_relaxed").sort(
        ["station_id", "time", "lead_day"]
    )


def _to_long(wide: pl.DataFrame, variables: list[str], days: list[int]) -> pl.DataFrame:
    """One wide frame of `<var>_previous_dayN` columns -> one row per lead day.

    Stacking rather than widening keeps the column names identical to
    `meteo_archive`, so the feature builder can substitute one for the other
    without a second naming convention to keep straight.
    """
    keep = [c for c in ("station_id", "time", "cell_lat", "cell_lon") if c in wide.columns]
    out = []
    for d in days:
        cols = {f"{v}_previous_day{d}": v for v in variables if f"{v}_previous_day{d}" in wide.columns}
        if not cols:
            continue
        part = wide.select(keep + list(cols)).rename(cols).with_columns(
            pl.lit(d, dtype=pl.Int32).alias("lead_day"),
            pl.lit("previous_run").alias("source_class"),
        )
        out.append(_derive(part))
    if not out:
        return pl.DataFrame()
    return pl.concat(out, how="vertical_relaxed")


def backfill_previous_runs(
    start: dt.date | str,
    end: dt.date | str,
    cfg: Config | None = None,
    skip_existing: bool = True,
    station_ids: list[str] | None = None,
    chunk_days: int = CHUNK_DAYS,
) -> list[Path]:
    """Cache lead-matched meteorology, chunked and resumable like `backfill`.

    `station_ids` switches to merge mode: fetch only those stations and add them
    to whatever each partition already holds. Without it, `skip_existing` would
    see every partition present and fetch nothing at all for a station added
    after the first backfill — which is how a new station ends up silently
    absent from one dataset while looking complete in the others.
    """
    cfg = cfg or load_config()
    start_d, end_d = _as_date(start), _as_date(end)
    merging = station_ids is not None

    # Resolve the cell map exactly once for the whole backfill.
    src = cfg.source("meteo")
    points = station_points(cfg)
    if merging:
        wanted = set(station_ids)
        points = [p for p in points if p.key in wanted]
        cells = resolve_cells(src["previous_runs_url"], points, {"models": src["model"]})
    else:
        cells = resolve_cells(
            src["previous_runs_url"], points, {"models": src["model"]}, cache_key="meteo"
        )

    written: list[Path] = []
    for chunk_start, chunk_end in date_chunks(start_d, end_d, days=chunk_days):
        if (
            not merging
            and skip_existing
            and not missing_dates(LEADMATCHED_DATASET, chunk_start, chunk_end)
        ):
            log.info("meteo lead-matched %s..%s already cached", chunk_start, chunk_end)
            continue
        try:
            df = fetch_previous_runs(
                chunk_start, chunk_end, cfg=cfg, station_ids=station_ids, cells=cells
            )
        except Exception as exc:
            log.error(
                "lead-matched chunk %s..%s failed, continuing: %s",
                chunk_start, chunk_end, str(exc)[:200],
            )
            continue
        if df.is_empty():
            continue
        if not merging:
            written += write_partitioned(df, LEADMATCHED_DATASET)
            continue
        for day, part in df.with_columns(
            pl.col("time").dt.date().alias("_d")
        ).group_by("_d", maintain_order=True):
            day = day[0] if isinstance(day, tuple) else day
            path = partition_path(LEADMATCHED_DATASET, day)
            part = part.drop("_d")
            merged = (
                pl.concat([pl.read_parquet(path), part], how="diagonal_relaxed")
                if path.is_file()
                else part
            )
            merged = merged.unique(
                subset=["station_id", "time", "lead_day"], keep="first"
            )
            written += write_partitioned(merged, LEADMATCHED_DATASET)
    return written


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
