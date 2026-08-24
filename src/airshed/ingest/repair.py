"""Backfill gridded data for stations added after the fact.

Open-Meteo data is fetched once per model grid cell and expanded to every
station in that cell, so a station added later needs no new request: its values
are already on disk under a station that shares its cell. Copying them is exact,
not an approximation — `fetch_hourly_by_cell` would have produced the identical
rows.

Only valid for gridded model output. Never use it for CPCB or METAR, where two
stations are two instruments and the values genuinely differ.
"""

from __future__ import annotations

import logging

import polars as pl

from ..config import Config, load_config
from ..store import available_dates, partition_path, write_partitioned

log = logging.getLogger(__name__)

GRIDDED = {"cams_archive": "cams", "meteo_archive": "meteo"}


def missing_stations(dataset: str, cfg: Config | None = None) -> list[str]:
    """Configured stations with no rows in the most recent partition."""
    cfg = cfg or load_config()
    days = available_dates(dataset)
    if not days:
        return []
    latest = pl.read_parquet(partition_path(dataset, days[-1]), columns=["station_id"])
    present = set(latest["station_id"].unique().to_list())
    return [s.id for s in cfg.stations if s.id not in present]


def expand(dataset: str, cfg: Config | None = None) -> int:
    """Copy each missing station's cell-mate rows across every partition.

    The cell mapping is read out of the cached Parquet itself — every row
    already records the `cell_lat`/`cell_lon` the API served it from — so this
    needs no network call and works while we are rate-limited.
    """
    cfg = cfg or load_config()
    if dataset not in GRIDDED:
        raise ValueError(f"{dataset} is not gridded model output — refetch it instead")

    wanted = missing_stations(dataset, cfg)
    if not wanted:
        log.info("%s already covers every configured station", dataset)
        return 0

    days = available_dates(dataset)
    sample = pl.read_parquet(
        partition_path(dataset, days[-1]), columns=["station_id", "cell_lat", "cell_lon"]
    ).unique(subset=["station_id"])
    known = {
        row["station_id"]: (row["cell_lat"], row["cell_lon"])
        for row in sample.iter_rows(named=True)
    }

    # A new station belongs to whichever served cell centre is nearest — the
    # same nearest-grid-point rule the API applies, so the copied values are
    # what a fresh request would have returned.
    by_id = {s.id: s for s in cfg.stations}
    donors: dict[str, str] = {}
    for station_id in wanted:
        station = by_id.get(station_id)
        if station is None or not known:
            continue
        donor, dist = min(
            ((k, _dist2(station.lat, station.lon, *cell)) for k, cell in known.items()),
            key=lambda kv: kv[1],
        )
        donors[station_id] = donor
        log.info(
            "%s will take %s values from %s (cell centre %.4f deg away)",
            station_id, dataset, donor, dist**0.5,
        )

    if not donors:
        return 0

    written = 0
    for day in available_dates(dataset):
        path = partition_path(dataset, day)
        part = pl.read_parquet(path)
        present = set(part["station_id"].unique().to_list())
        additions = [
            part.filter(pl.col("station_id") == donor).with_columns(
                pl.lit(new_id).alias("station_id")
            )
            for new_id, donor in donors.items()
            if new_id not in present and donor in present
        ]
        if not additions:
            continue
        merged = pl.concat([part, *additions], how="vertical_relaxed")
        if "station_name" in merged.columns:
            names = {s.id: s.name for s in cfg.stations}
            merged = merged.with_columns(
                pl.col("station_id").replace_strict(names, default=None).alias("station_name")
            )
        write_partitioned(merged, dataset)
        written += 1
    log.info("expanded %d partitions of %s", written, dataset)
    return written


def _dist2(lat: float, lon: float, cell_lat: float, cell_lon: float) -> float:
    """Squared degree distance. Only used to rank candidates, never reported."""
    return (lat - cell_lat) ** 2 + (lon - cell_lon) ** 2
