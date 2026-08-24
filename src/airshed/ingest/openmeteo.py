"""Shared Open-Meteo request/parse plumbing.

All three Open-Meteo endpoints we use (air quality, forecast, historical
forecast) speak the same JSON dialect, so the parsing lives here once and
`cams.py` / `meteo.py` only decide *which* endpoint and *which* variables.
"""

from __future__ import annotations

import datetime as dt
import json
import logging
from collections.abc import Iterable, Sequence
from typing import Any

import polars as pl

from ..net import get_json

log = logging.getLogger(__name__)

# Open-Meteo takes comma-separated coordinate lists. Keep batches modest so a
# single failed request costs little and URLs stay under any proxy limit.
BATCH_SIZE = 20

# Response variables that are not real measurements.
_META_KEYS = {"time", "interval"}


class Point:
    """A named location to sample. `key` ends up as a column in the output."""

    __slots__ = ("key", "lat", "lon", "extra")

    def __init__(self, key: str, lat: float, lon: float, **extra: Any) -> None:
        self.key = key
        self.lat = lat
        self.lon = lon
        self.extra = extra


def fetch_hourly(
    url: str,
    points: Sequence[Point],
    hourly: Sequence[str],
    key_column: str = "station_id",
    extra_params: dict[str, Any] | None = None,
) -> pl.DataFrame:
    """Fetch hourly variables for many points and return one long dataframe.

    Output columns: `key_column`, `time` (UTC, tz-aware), one column per
    requested variable, plus `lat`/`lon` of the grid cell Open-Meteo actually
    served (which is not the requested point — see R7 about resolution).
    """
    frames: list[pl.DataFrame] = []
    for batch in _batched(points, BATCH_SIZE):
        params: dict[str, Any] = {
            "latitude": ",".join(f"{p.lat:.4f}" for p in batch),
            "longitude": ",".join(f"{p.lon:.4f}" for p in batch),
            "hourly": ",".join(hourly),
            "timezone": "UTC",
            **(extra_params or {}),
        }
        payload = get_json(url, params=params)
        locations = payload if isinstance(payload, list) else [payload]
        if len(locations) != len(batch):
            raise RuntimeError(
                f"asked for {len(batch)} locations, got {len(locations)} back from {url}"
            )
        for point, loc in zip(batch, locations, strict=True):
            frames.append(_parse_location(loc, point, key_column))

    if not frames:
        return pl.DataFrame()
    return pl.concat(frames, how="vertical_relaxed")


def _parse_location(loc: dict[str, Any], point: Point, key_column: str) -> pl.DataFrame:
    hourly = loc.get("hourly")
    if not hourly or not hourly.get("time"):
        log.warning("empty hourly block for %s", point.key)
        return pl.DataFrame()

    data: dict[str, Any] = {"time": hourly["time"]}
    for name, values in hourly.items():
        if name in _META_KEYS:
            continue
        data[name] = values

    df = pl.DataFrame(data)
    df = df.with_columns(
        pl.col("time")
        .str.to_datetime(format="%Y-%m-%dT%H:%M", time_unit="us")
        .dt.replace_time_zone("UTC")
    )
    # Cast every value column to Float64 so an all-null variable does not come
    # back as Null dtype and poison a later concat.
    df = df.with_columns(
        [pl.col(c).cast(pl.Float64) for c in df.columns if c != "time"]
    )
    df = df.with_columns(
        pl.lit(point.key).alias(key_column),
        pl.lit(float(loc.get("latitude", point.lat))).alias("cell_lat"),
        pl.lit(float(loc.get("longitude", point.lon))).alias("cell_lon"),
    )
    for name, value in point.extra.items():
        df = df.with_columns(pl.lit(value).alias(name))
    return df


def station_points(cfg) -> list[Point]:
    """One Point per configured CAAQMS station."""
    return [Point(s.id, s.lat, s.lon, station_name=s.name) for s in cfg.stations]


def resolve_cells(
    url: str,
    points: Sequence[Point],
    extra_params: dict[str, Any] | None = None,
    cache_key: str | None = None,
) -> dict[str, tuple[float, float]]:
    """Ask the API which grid cell each point actually lands in.

    One cheap one-day request. The response reports the *served* cell centre,
    which is authoritative — guessing the model lattice by rounding would merge
    two genuinely different cells whenever our guess is offset from the real
    grid.

    The answer only changes when the model grid or the station list changes, so
    it is cached on disk: re-running a backfill should not spend requests
    rediscovering it, least of all while we are being rate-limited.
    """
    cache_path = _cell_cache_path(cache_key) if cache_key else None
    if cache_path and cache_path.is_file():
        cached = json.loads(cache_path.read_text(encoding="utf-8"))
        if set(cached) == {p.key for p in points}:
            log.info("using cached cell map %s", cache_path.name)
            return {k: (v[0], v[1]) for k, v in cached.items()}

    mapping: dict[str, tuple[float, float]] = {}
    for batch in _batched(points, BATCH_SIZE):
        params: dict[str, Any] = {
            "latitude": ",".join(f"{p.lat:.4f}" for p in batch),
            "longitude": ",".join(f"{p.lon:.4f}" for p in batch),
            "hourly": "temperature_2m",
            "forecast_days": 1,
            "timezone": "UTC",
            **(extra_params or {}),
        }
        payload = get_json(url, params=params)
        locations = payload if isinstance(payload, list) else [payload]
        for point, loc in zip(batch, locations, strict=True):
            mapping[point.key] = (
                round(float(loc["latitude"]), 4),
                round(float(loc["longitude"]), 4),
            )

    if cache_path:
        cache_path.parent.mkdir(parents=True, exist_ok=True)
        cache_path.write_text(
            json.dumps({k: list(v) for k, v in mapping.items()}, indent=1), encoding="utf-8"
        )
    return mapping


def _cell_cache_path(cache_key: str):
    from ..config import load_config

    return load_config().processed_dir / f"cells_{cache_key}.json"


def fetch_hourly_by_cell(
    url: str,
    points: Sequence[Point],
    hourly: Sequence[str],
    cells: dict[str, tuple[float, float]],
    key_column: str = "station_id",
    extra_params: dict[str, Any] | None = None,
) -> pl.DataFrame:
    """Fetch once per distinct grid cell, then expand back to every point.

    Fifty stations across Delhi NCR resolve to a single-digit number of model
    cells, so requesting per station asks the same question ten times over —
    which is what was earning us HTTP 429s. The returned values are identical
    either way; only the request count changes.
    """
    by_cell: dict[tuple[float, float], list[str]] = {}
    for key, cell in cells.items():
        by_cell.setdefault(cell, []).append(key)

    cell_points = [
        Point(f"{lat:.4f},{lon:.4f}", lat, lon) for (lat, lon) in sorted(by_cell)
    ]
    log.info("%d points -> %d distinct model cells", len(points), len(cell_points))

    cell_df = fetch_hourly(
        url=url,
        points=cell_points,
        hourly=hourly,
        key_column="_cell_key",
        extra_params=extra_params,
    )
    if cell_df.is_empty():
        return cell_df

    lookup = pl.DataFrame(
        {
            key_column: list(cells.keys()),
            "_cell_key": [f"{lat:.4f},{lon:.4f}" for (lat, lon) in cells.values()],
        }
    )
    expanded = lookup.join(cell_df, on="_cell_key", how="left").drop("_cell_key")

    extras = {k: v for p in points for k, v in p.extra.items() if p.key in cells}
    if extras:
        meta = pl.DataFrame(
            [{key_column: p.key, **p.extra} for p in points if p.key in cells]
        )
        expanded = expanded.join(meta, on=key_column, how="left")
    return expanded


def date_chunks(
    start: dt.date, end: dt.date, days: int = 60
) -> Iterable[tuple[dt.date, dt.date]]:
    """Split a long range into request-sized windows."""
    cur = start
    while cur <= end:
        stop = min(cur + dt.timedelta(days=days - 1), end)
        yield cur, stop
        cur = stop + dt.timedelta(days=1)


def _batched(seq: Sequence[Point], size: int) -> Iterable[Sequence[Point]]:
    for i in range(0, len(seq), size):
        yield seq[i : i + size]
