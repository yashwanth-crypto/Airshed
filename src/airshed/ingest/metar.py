"""METAR surface observations for Delhi from the Iowa State IEM ASOS archive.

Visibility here is a *measurement*, not a model diagnostic, which makes it the
independent check on the pollution-fog coupling the project claims to forecast.
Open-Meteo also serves a `visibility` field, but it is a model output derived
from the same physics we are correcting, so it cannot validate anything.

Reports arrive at :00 and :30 plus SPECIs, so this module aggregates to the
hourly UTC index and records `n_obs`. Hours with no report stay absent — R6
applies to METAR exactly as it does to CPCB.
"""

from __future__ import annotations

import datetime as dt
import io
import logging
from pathlib import Path

import polars as pl

from ..config import Config, load_config
from ..net import get_text
from ..store import write_partitioned

log = logging.getLogger(__name__)

DATASET = "metar"

MILES_TO_KM = 1.609344
KNOTS_TO_MS = 0.514444
INCH_TO_MM = 25.4


def fetch(
    start: dt.date | str,
    end: dt.date | str,
    cfg: Config | None = None,
    stations: list[str] | None = None,
) -> pl.DataFrame:
    """Hourly METAR for the configured airports over [start, end]."""
    cfg = cfg or load_config()
    src = cfg.source("metar")
    start_d, end_d = _as_date(start), _as_date(end)
    # IEM's end date is exclusive.
    stop = end_d + dt.timedelta(days=1)

    frames = []
    for station in stations or src["stations"]:
        params = {
            "station": station,
            "data": src["elements"],
            "year1": start_d.year,
            "month1": start_d.month,
            "day1": start_d.day,
            "year2": stop.year,
            "month2": stop.month,
            "day2": stop.day,
            "tz": "UTC",
            "format": "onlycomma",
            "latlon": "no",
            "missing": "M",
            "trace": "0.0001",
            "report_type": [3, 4],
        }
        text = get_text(src["url"], params=params)
        df = _parse(text, station)
        if df.is_empty():
            log.warning("no METAR rows for %s over %s..%s", station, start_d, end_d)
            continue
        frames.append(df)

    if not frames:
        return pl.DataFrame()
    return pl.concat(frames, how="vertical_relaxed").sort(["metar_station", "time"])


def backfill(
    start: dt.date | str,
    end: dt.date | str,
    cfg: Config | None = None,
) -> list[Path]:
    df = fetch(start, end, cfg=cfg)
    if df.is_empty():
        return []
    return write_partitioned(df, DATASET)


# ---------------------------------------------------------------------------
def _parse(text: str, station: str) -> pl.DataFrame:
    lines = [ln for ln in text.splitlines() if ln and not ln.startswith("#")]
    if len(lines) < 2:
        return pl.DataFrame()

    raw = pl.read_csv(
        io.StringIO("\n".join(lines)),
        null_values=["M", "", "None"],
        infer_schema_length=0,  # everything as string, cast explicitly below
    )
    if "valid" not in raw.columns:
        return pl.DataFrame()

    numeric = [c for c in ("vsby", "tmpf", "dwpf", "relh", "drct", "sknt", "p01i", "mslp") if c in raw.columns]
    obs = raw.with_columns(
        pl.col("valid")
        .str.to_datetime(format="%Y-%m-%d %H:%M", time_unit="us", strict=False)
        .dt.replace_time_zone("UTC")
        .alias("obs_time"),
        *[pl.col(c).cast(pl.Float64, strict=False) for c in numeric],
    ).drop_nulls("obs_time")

    obs = obs.with_columns(
        (pl.col("vsby") * MILES_TO_KM).alias("visibility_km"),
        ((pl.col("tmpf") - 32.0) * 5.0 / 9.0).alias("temp_c"),
        ((pl.col("dwpf") - 32.0) * 5.0 / 9.0).alias("dew_point_c"),
        (pl.col("sknt") * KNOTS_TO_MS).alias("wind_speed_ms"),
        (pl.col("p01i") * INCH_TO_MM).alias("precip_mm"),
        pl.col("obs_time").dt.truncate("1h").alias("time"),
    )

    return (
        obs.group_by(["time"])
        .agg(
            pl.col("visibility_km").mean().alias("visibility_km"),
            pl.col("visibility_km").min().alias("visibility_km_min"),
            pl.col("temp_c").mean().alias("temp_c"),
            pl.col("dew_point_c").mean().alias("dew_point_c"),
            pl.col("relh").mean().alias("relative_humidity"),
            pl.col("wind_speed_ms").mean().alias("wind_speed_ms"),
            pl.col("drct").mean().alias("wind_direction_deg"),
            pl.col("precip_mm").max().alias("precip_mm"),
            pl.col("mslp").mean().alias("mslp_hpa"),
            pl.len().alias("n_obs"),
        )
        .with_columns(
            pl.lit(station).alias("metar_station"),
            # Dew-point depression: small values mean fog is close, which is the
            # mechanism linking a shallow moist layer to a visibility collapse.
            (pl.col("temp_c") - pl.col("dew_point_c")).alias("dew_point_depression_c"),
        )
        .sort("time")
    )


def _as_date(value: dt.date | str) -> dt.date:
    return dt.date.fromisoformat(value) if isinstance(value, str) else value
