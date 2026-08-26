"""The local Parquet store.

R8: every ingest writes here and every downstream reader reads from here. No
module outside `ingest/` may call a remote API. A source outage must degrade
the demo to "stale cache" and never to "no data".

Layout:

    data/raw/<dataset>/date=YYYY-MM-DD/part.parquet

Partition date is the UTC date of the row's time column, so a partition is
self-contained and a re-fetch of one day overwrites exactly one file.
"""

from __future__ import annotations

import datetime as dt
import os
import shutil
from collections.abc import Sequence
from pathlib import Path

import polars as pl

from .config import load_config

PART_NAME = "part.parquet"


def dataset_dir(dataset: str, base: Path | None = None) -> Path:
    root = base if base is not None else load_config().raw_dir
    return root / dataset


def partition_path(dataset: str, day: dt.date, base: Path | None = None) -> Path:
    return dataset_dir(dataset, base) / f"date={day.isoformat()}" / PART_NAME


def write_partitioned(
    df: pl.DataFrame,
    dataset: str,
    time_col: str = "time",
    base: Path | None = None,
    partition_col: str | None = None,
    merge_on: Sequence[str] | None = None,
) -> list[Path]:
    """Write `df` partitioned by the UTC date of `partition_col` (default `time_col`).

    Idempotent: a partition that already exists is replaced wholesale, never
    appended to, so re-running a fetch cannot duplicate rows.

    A forecast-run dataset partitions by *issue* date instead of valid time, so
    that one model run is one file and re-fetching a run replaces it cleanly.

    **Replacing wholesale is only safe when the caller holds the whole day.**
    A backfill does; a rolling-window sync does not, and replacing a day with
    the slice of it that happens to fall inside the window deletes the rest.
    That is not hypothetical: a 12 h live sync ran every 30 minutes and quietly
    ate the CPCB store from behind, leaving 2026-08-25 with a single hour of the
    24 it had held that morning, and 16 of the last 41 days short.

    Pass `merge_on` with the columns that identify a row -- for observations,
    `("station_id", "time")` -- and each partition is unioned with what is
    already on disk instead, the incoming row winning any collision so a revised
    value still replaces a provisional one.
    """
    if df.is_empty():
        return []
    part_col = partition_col or time_col
    _require_utc(df, time_col)
    if part_col != time_col:
        _require_utc(df, part_col)

    written: list[Path] = []
    with_date = df.with_columns(pl.col(part_col).dt.date().alias("_part_date"))
    for (day,), part in with_date.group_by(["_part_date"], maintain_order=True):
        out = partition_path(dataset, day, base)
        out.parent.mkdir(parents=True, exist_ok=True)
        fresh = part.drop("_part_date")
        if merge_on and out.is_file():
            fresh = _merge_with_existing(fresh, out, merge_on)
        tmp = out.with_suffix(".parquet.tmp")
        fresh.sort(time_col).write_parquet(tmp, compression="zstd")
        os.replace(tmp, out)
        written.append(out)
    return written


def _merge_with_existing(
    fresh: pl.DataFrame, path: Path, merge_on: Sequence[str]
) -> pl.DataFrame:
    """Union a partial write with the partition already on disk.

    Incoming rows are kept ahead of cached ones so a collision resolves to the
    newer value -- OpenAQ revises a provisional reading often enough that
    preferring the cached copy would freeze the first number we ever saw.

    A partition unreadable for any reason is treated as absent rather than
    fatal: losing the merge costs one window of history, and refusing to write
    costs every window after it.
    """
    try:
        cached = pl.read_parquet(path)
    except Exception:  # pragma: no cover - corrupt or half-written file
        return fresh
    if cached.is_empty():
        return fresh
    keys = [c for c in merge_on if c in fresh.columns and c in cached.columns]
    if len(keys) != len(merge_on):
        # Without every key column the union cannot be deduplicated safely, and
        # silently dropping rows would be worse than the replace it fixes.
        return fresh
    combined = pl.concat([fresh, cached], how="diagonal_relaxed")
    return combined.unique(subset=list(keys), keep="first")


def read_range(
    dataset: str,
    start: dt.date | dt.datetime | str,
    end: dt.date | dt.datetime | str,
    base: Path | None = None,
    columns: list[str] | None = None,
) -> pl.DataFrame:
    """Read one dataset over a closed date range. Missing days are skipped."""
    start_d, end_d = _as_date(start), _as_date(end)
    paths = [
        p
        for day in _days(start_d, end_d)
        if (p := partition_path(dataset, day, base)).is_file()
    ]
    if not paths:
        return pl.DataFrame()
    frames = [pl.read_parquet(p, columns=columns) for p in paths]
    # Diagonal: partitions written at different times may carry different
    # columns — a provenance field added later, for instance. Missing columns
    # become null rather than raising, so the store can evolve without a
    # rewrite of everything already on disk.
    return pl.concat(frames, how="diagonal_relaxed")


def available_dates(dataset: str, base: Path | None = None) -> list[dt.date]:
    d = dataset_dir(dataset, base)
    if not d.is_dir():
        return []
    out = []
    for child in d.iterdir():
        if child.is_dir() and child.name.startswith("date=") and (child / PART_NAME).is_file():
            out.append(dt.date.fromisoformat(child.name.removeprefix("date=")))
    return sorted(out)


def missing_dates(
    dataset: str,
    start: dt.date | str,
    end: dt.date | str,
    base: Path | None = None,
) -> list[dt.date]:
    """Days in [start, end] with no partition on disk — what a backfill owes."""
    have = set(available_dates(dataset, base))
    return [d for d in _days(_as_date(start), _as_date(end)) if d not in have]


def coverage(dataset: str, base: Path | None = None) -> dict[str, object]:
    """Cheap summary for the "last synced" indicator and for CLI status."""
    days = available_dates(dataset, base)
    if not days:
        return {"dataset": dataset, "days": 0, "first": None, "last": None, "rows": 0}
    rows = 0
    for day in days:
        rows += pl.scan_parquet(partition_path(dataset, day, base)).select(
            pl.len()
        ).collect().item()
    return {
        "dataset": dataset,
        "days": len(days),
        "first": days[0].isoformat(),
        "last": days[-1].isoformat(),
        "rows": rows,
    }


def latest_run(dataset: str, base: Path | None = None) -> pl.DataFrame:
    """The most recent forecast run in a run-partitioned dataset.

    `cams_runs` and `meteo_runs` are partitioned by *issue* date, so the usual
    read-by-valid-date does not apply: one partition is one model run covering
    five days of future valid times. This returns the newest one whole.
    """
    days = available_dates(dataset, base)
    if not days:
        return pl.DataFrame()
    return pl.read_parquet(partition_path(dataset, days[-1], base))


def drop_dataset(dataset: str, base: Path | None = None) -> None:
    d = dataset_dir(dataset, base)
    if d.is_dir():
        shutil.rmtree(d)


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------
def _require_utc(df: pl.DataFrame, time_col: str) -> None:
    if time_col not in df.columns:
        raise ValueError(f"dataframe has no time column {time_col!r}")
    dtype = df.schema[time_col]
    if not isinstance(dtype, pl.Datetime):
        raise TypeError(f"{time_col} must be Datetime, got {dtype}")
    if dtype.time_zone != "UTC":
        raise TypeError(
            f"{time_col} must be tz-aware UTC, got time_zone={dtype.time_zone!r}. "
            "Internals are UTC everywhere; convert to IST only for display."
        )


def _as_date(value: dt.date | dt.datetime | str) -> dt.date:
    if isinstance(value, str):
        return dt.date.fromisoformat(value[:10])
    if isinstance(value, dt.datetime):
        return value.date()
    return value


def _days(start: dt.date, end: dt.date) -> list[dt.date]:
    if end < start:
        raise ValueError(f"end {end} is before start {start}")
    return [start + dt.timedelta(days=i) for i in range((end - start).days + 1)]
