"""Pre-2025 CPCB history from the public Kaggle archive.

OpenAQ has a gap between 2018-02 and 2025-02 for these stations, which left the
whole project resting on a single winter — too little to tell a 1% effect from
noise, and only 18 Stage IV hours to judge severe-episode recall on. This loader
adds **2015-01 to 2020-07**, roughly five and a half more winters, from the
`rohanrao/air-quality-data-in-india` dataset.

Three things the file gets wrong for our purposes, all handled here:

**Station ids collide but do not correspond.** Their `DL001` is Alipur; ours is
Anand Vihar. Matching is by *name*, never by id, and the match is reported so a
wrong pairing is visible rather than silent.

**Timestamps are naive IST.** No offset, no timezone. Converted to UTC before
the hourly index is formed — the same trap the OpenAQ loader hits with its
`+05:30` stamps, and truncating first would smear every hour across two.

**IST is a half-hour offset from UTC.** An hourly local stamp converts to :30
past the UTC hour, so timestamps are floored to the UTC hour before use. The
label therefore sits within half an hour of the interval it summarises — the
same convention the live loader produces from 15-minute data, and the price of
a country whose clock is offset by thirty minutes.

**A different era is not automatically comparable.** These years predate the
boundary-layer-height archive, so features built on them fall back to the
documented lapse-rate proxy with `blh_available = false`. Rows carry a `source`
column so any result can be recomputed on the modern period alone.
"""

from __future__ import annotations

import datetime as dt
import logging
import re
from pathlib import Path

import polars as pl

from ..config import Config, Station, load_config
from ..store import write_partitioned
from . import _stationmatch as sm
from .cpcb import DATASET, UPWIND_DATASET, flag_quality

log = logging.getLogger(__name__)

SOURCE = "kaggle_cpcb_2015_2020"
DEFAULT_DIR = Path("data/manual")
IST = "Asia/Kolkata"


def match_stations(
    catalogue: pl.DataFrame,
    stations: list[Station],
) -> dict[str, str]:
    """Map our station id -> their StationId, by name similarity."""
    candidates = list(zip(catalogue["StationId"].to_list(), catalogue["StationName"].to_list(), strict=True))
    mapping: dict[str, str] = {}
    used: set[str] = set()
    for station in stations:
        found = sm.best_match(station.name, station.city, candidates, used)
        if found is not None and found[1] >= sm.MIN_SCORE:
            their_id, s, their_name = found
            mapping[station.id] = their_id
            used.add(their_id)
            log.info("%-6s %-30s <- %-8s %s", station.id, station.name, their_id, their_name)
        else:
            best_txt = f"{found[1]:.2f}: {found[2]!r}" if found else "no candidate"
            log.warning(
                "%-6s %-30s has no historical counterpart (best %s)",
                station.id, station.name, best_txt,
            )
    return mapping


def load(
    directory: Path | str = DEFAULT_DIR,
    cfg: Config | None = None,
    upwind: bool = False,
) -> pl.DataFrame:
    """Read the archive and return hourly observations in our own schema."""
    cfg = cfg or load_config()
    directory = Path(directory)
    catalogue_path = directory / "stations.csv"
    hourly_path = directory / "station_hour.csv"
    for path in (catalogue_path, hourly_path):
        if not path.is_file():
            raise FileNotFoundError(
                f"{path} not found — extract stations.csv and station_hour.csv "
                "from the Kaggle download into this directory"
            )

    catalogue = pl.read_csv(catalogue_path)
    targets = list(cfg.upwind_stations) if upwind else list(cfg.stations)
    mapping = match_stations(catalogue, targets)
    if not mapping:
        log.warning("no stations matched — nothing to load")
        return pl.DataFrame()

    reverse = {their: ours for ours, their in mapping.items()}
    frame = (
        pl.scan_csv(hourly_path)
        .filter(pl.col("StationId").is_in(list(reverse)))
        .select(["StationId", "Datetime", "PM2.5"])
        .collect()
    )
    if frame.is_empty():
        return pl.DataFrame()

    hourly = (
        frame.rename({"PM2.5": "pm25"})
        .drop_nulls("pm25")
        .with_columns(
            pl.col("StationId").replace_strict(reverse).alias("station_id"),
            # Naive local time -> UTC. The archive is stamped in IST with no
            # offset; reading it as UTC would shift every observation by 5.5
            # hours and quietly destroy every diurnal feature built on it.
            pl.col("Datetime")
            .str.to_datetime(format="%Y-%m-%d %H:%M:%S", time_unit="us", strict=False)
            .dt.replace_time_zone(IST)
            .dt.convert_time_zone("UTC")
            # IST is UTC+5:30, so an on-the-hour local stamp lands at :30 past
            # the UTC hour. Left there, not one of these rows would ever join
            # to the hourly index and the whole archive would silently vanish
            # into a left join. Floor to the UTC hour, exactly as the live
            # loader does with its 15-minute data; the residual half-hour
            # convention is documented in the module docstring.
            .dt.truncate("1h")
            .alias("time"),
        )
        .drop_nulls("time")
        .group_by(["station_id", "time"])
        .agg(
            pl.col("pm25").mean().alias("pm25"),
            pl.col("pm25").min().alias("pm25_min"),
            pl.col("pm25").max().alias("pm25_max"),
            pl.len().alias("n_obs"),
        )
        .sort(["station_id", "time"])
    )
    # Identical quality rules to the live loader: different cleaning per era
    # would put a step change into the training data that looks like a change
    # in the air rather than a change in us.
    flagged = flag_quality(hourly).with_columns(pl.lit(SOURCE).alias("source"))
    log.info(
        "loaded %d hourly rows for %d stations, %s..%s",
        flagged.height, flagged["station_id"].n_unique(),
        flagged["time"].min(), flagged["time"].max(),
    )
    return flagged


def backfill(
    directory: Path | str = DEFAULT_DIR,
    cfg: Config | None = None,
    upwind: bool = False,
    overwrite_from: dt.date | None = None,
) -> list[Path]:
    """Write the historical archive into the store.

    Refuses to touch partitions the live loader already owns unless told to:
    the two sources overlap nowhere by design (2015-2020 against 2025 onward),
    and silently overwriting modern ground truth with a five-year-old archive
    would be a very hard bug to find.
    """
    cfg = cfg or load_config()
    df = load(directory, cfg=cfg, upwind=upwind)
    if df.is_empty():
        return []

    dataset = UPWIND_DATASET if upwind else DATASET
    cutoff = overwrite_from or dt.date(2021, 1, 1)
    before = df.height
    df = df.filter(pl.col("time").dt.date() < cutoff)
    if df.height < before:
        log.info(
            "dropped %d rows at or after %s to avoid overwriting live data",
            before - df.height, cutoff,
        )
    return write_partitioned(df, dataset)
