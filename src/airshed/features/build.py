"""Align every source onto one tz-aware hourly UTC index, then build the
supervised table.

Two stages, deliberately separate:

`build_base`        one row per (station, hour) over a *complete* hourly index,
                    every source left-joined on. Missing data stays null.

`build_supervised`  one row per (station, issue_time, horizon) — the direct
                    multi-horizon layout R4 requires. No recursive rollout
                    exists anywhere in this codebase.

Three correctness rules this module exists to enforce:

1. The index is built complete *before* any lag is taken. Lags are positional
   shifts, so on a gappy frame `shift(24)` would silently mean "24 rows back",
   which during an outage is not 24 hours. On a complete index it is exact.

2. Observation features are read strictly at or before `issue_time`; forecast
   features (CAMS, meteorology) are read at `target_time`, because a forecast
   for t+72 genuinely is available at t. That asymmetry is the whole design,
   and mixing it up is leakage.

   There is a second, quieter version of the same hazard. A forecast for t+72
   is available at t, but *which* forecast? The archives return the best
   available forecast for each past hour, which is a short-lead one, so the
   column is cleaner in training than production will ever be.
   `apply_lead_matched_meteo` substitutes the forecast that was genuinely
   available `horizon_h` hours earlier, for the variables that have one.

3. Nothing is forward-filled across a gap. Rolling windows require a minimum
   number of real observations and return null otherwise (R6).
"""

from __future__ import annotations

import datetime as dt
import logging

import polars as pl

from ..config import Config, load_config
from ..store import read_range

log = logging.getLogger(__name__)

# Rolling windows need this fraction of the window to be real observations,
# otherwise the window is null. This is the explicit "do not bridge a gap" rule.
MIN_COVERAGE = 0.6

OBS_LAGS_H = [1, 2, 3, 6, 12, 24, 48, 72]
OBS_WINDOWS_H = [3, 24, 72]

CAMS_COLS = [
    "pm2_5", "pm10", "carbon_monoxide", "nitrogen_dioxide",
    "sulphur_dioxide", "ozone", "dust", "aerosol_optical_depth",
]
MET_COLS = [
    "temperature_2m", "relative_humidity_2m", "dew_point_2m", "surface_pressure",
    "wind_speed_10m", "wind_direction_10m", "wind_gusts_10m", "wind_speed_100m",
    "wind_direction_100m", "boundary_layer_height", "precipitation", "cloud_cover",
    "shortwave_radiation", "cape", "visibility", "u10", "v10", "ventilation_index",
    "temperature_925hPa", "temperature_850hPa", "wind_speed_925hPa",
    "wind_direction_925hPa", "geopotential_height_925hPa",
    "lapse_2m_925", "lapse_925_850", "inversion", "blh_available",
]
METAR_COLS = [
    "visibility_km", "visibility_km_min", "dew_point_depression_c",
    "relative_humidity", "temp_c", "n_obs",
]


def hourly_index(
    start: dt.date | str,
    end: dt.date | str,
    stations: list[str],
) -> pl.DataFrame:
    """Complete (station_id, time) grid, hourly, UTC, end-date inclusive."""
    start_d = _as_date(start)
    end_d = _as_date(end)
    first = dt.datetime.combine(start_d, dt.time.min, tzinfo=dt.timezone.utc)
    last = dt.datetime.combine(end_d, dt.time(23, 0), tzinfo=dt.timezone.utc)
    times = pl.datetime_range(first, last, interval="1h", time_zone="UTC", eager=True)
    return (
        pl.DataFrame({"station_id": stations})
        .join(pl.DataFrame({"time": times}), how="cross")
        .sort(["station_id", "time"])
    )


def build_base(
    start: dt.date | str,
    end: dt.date | str,
    cfg: Config | None = None,
    cams_dataset: str = "cams_archive",
    meteo_dataset: str = "meteo_archive",
    use_upwind: bool = True,
) -> pl.DataFrame:
    """Join every cached source onto the complete hourly index.

    Reads only from the local store — no network (R8). A source with no cached
    partitions contributes all-null columns rather than vanishing, so a caller
    can see what is missing instead of silently training on fewer features.
    """
    cfg = cfg or load_config()
    station_ids = [s.id for s in cfg.stations]
    base = hourly_index(start, end, station_ids)

    base = _join_cpcb(base, start, end)
    base = _join_cams(base, start, end, cams_dataset)
    base = _join_meteo(base, start, end, meteo_dataset)
    base = _join_metar(base, start, end)
    base = _join_fires(base, start, end, cfg)
    if use_upwind:
        # Corridor transport, joined on time and broadcast to every station:
        # the upwind network is 65-340 km away and resolves the airshed, not
        # differences between neighbourhoods.
        from .upwind import upwind_features

        base = upwind_features(base, cfg=cfg, start=start, end=end)

    base = _add_observation_features(base)
    base = _add_calendar_features(base)
    return base.sort(["station_id", "time"])


def build_supervised(
    base: pl.DataFrame,
    cfg: Config | None = None,
    horizons: list[int] | None = None,
    target: str = "pm25_clean",
    forecast_cols: list[str] | None = None,
    extra_targets: dict[str, str] | None = None,
) -> pl.DataFrame:
    """One row per (station, issue_time, horizon) — direct multi-horizon (R4).

    Observation-derived columns keep their value at `issue_time`. Forecast
    columns are re-read at `target_time` and suffixed `_tgt`, since that is the
    value a real forecast issued at `issue_time` would have had for the target
    hour. Rows whose target is missing are dropped: an absent observation is
    not a zero (R6).
    """
    cfg = cfg or load_config()
    horizons = horizons or cfg.horizons
    if base.is_empty():
        return base

    fc_cols = forecast_cols or _default_forecast_cols(base)
    fc_cols = [c for c in fc_cols if c in base.columns]

    # Secondary targets for the coupled model — observed series read at the
    # target hour, exactly like `y`. They are targets, never inputs: a
    # visibility *observation* from the future is not available at issue time,
    # and only the primary target's absence drops a row.
    extra = {k: v for k, v in (extra_targets or {}).items() if v in base.columns}
    targets = base.select(
        ["station_id", "time", target] + list(extra.values()) + fc_cols
    ).rename(
        {"time": "target_time", target: "y"}
        | {src: name for name, src in extra.items()}
        | {c: f"{c}_tgt" for c in fc_cols}
    )

    out = []
    for h in horizons:
        issued = base.with_columns(
            (pl.col("time") + pl.duration(hours=h)).alias("target_time"),
            pl.lit(h, dtype=pl.Int32).alias("horizon_h"),
        ).rename({"time": "issue_time"})
        joined = issued.join(targets, on=["station_id", "target_time"], how="inner")
        out.append(joined)

    frame = pl.concat(out, how="vertical_relaxed")
    before = frame.height
    frame = frame.drop_nulls("y")
    log.info(
        "supervised rows %d -> %d after dropping missing targets", before, frame.height
    )
    return frame.sort(["station_id", "issue_time", "horizon_h"])


def lead_day_for(horizon_h: int) -> int:
    """Which Previous-Runs lead day covers a horizon. 24 h -> 1, 48 h -> 2, 72 h -> 3.

    `lead_day = N` spans true leads of 24N to 24N+23 hours, so this mapping is
    never optimistic: a 72 h horizon is scored against a forecast that is at
    least 72 hours old, sometimes 95.
    """
    return max(1, -(-int(horizon_h) // 24))


def apply_lead_matched_meteo(
    sup: pl.DataFrame,
    cfg: Config | None = None,
    dataset: str = "meteo_leadmatched",
) -> pl.DataFrame:
    """Replace short-lead forecast meteorology with the value at the real lead.

    `build_base` reads `meteo_archive`, which returns the best available
    forecast for each past hour — a short-lead one. Every `met_*_tgt` column is
    therefore cleaner in training than the corresponding column will be in
    production, and a 72 h score built on it is not a 72 h score. This swaps in
    the forecast that was genuinely available `horizon_h` hours earlier.

    Only the columns listed in `lead_matched_hourly` can be swapped. BLH,
    visibility and the pressure-level variables have no `_previous_dayN` form
    at all, so they keep their short-lead values and the residual optimism
    stays real. `met_lead_matched` records, per row, whether the swap happened
    — so nothing downstream can mistake a fallback row for a corrected one.
    """
    cfg = cfg or load_config()
    if sup.is_empty():
        return sup

    src = cfg.source("meteo")
    variables = [v for v in src.get("lead_matched_hourly", []) if f"met_{v}_tgt" in sup.columns]
    if not variables:
        log.warning("no lead-matchable meteorology columns present; frame unchanged")
        return sup.with_columns(pl.lit(False).alias("met_lead_matched"))

    start = sup["target_time"].min().date()
    end = sup["target_time"].max().date()
    lead = read_range(dataset, start, end)
    if lead.is_empty():
        log.warning(
            "no cached lead-matched meteorology (%s) for %s..%s; frame unchanged",
            dataset, start, end,
        )
        return sup.with_columns(pl.lit(False).alias("met_lead_matched"))

    derived = [c for c in ("u10", "v10") if c in lead.columns]
    keep = [v for v in variables if v in lead.columns] + derived
    lead = (
        lead.select(["station_id", "time", "lead_day"] + keep)
        .unique(subset=["station_id", "time", "lead_day"], keep="last")
        .rename({c: f"met_{c}_lm" for c in keep})
        .rename({"time": "target_time"})
    )

    # Expressed natively rather than through `lead_day_for`: a Python callback
    # per row costs minutes on a multi-million-row supervised table. The two
    # must agree, and `tests/test_leadmatch.py` holds them to it.
    out = sup.with_columns(
        (((pl.col("horizon_h") + 23) // 24).clip(lower_bound=1))
        .cast(pl.Int32)
        .alias("lead_day")
    ).join(lead, on=["station_id", "target_time", "lead_day"], how="left")

    # A miss keeps the short-lead value rather than dropping the row: losing
    # rows would silently change the evaluation set and make the comparison
    # against the archive-trained model no longer like-for-like.
    probe = f"met_{keep[0]}_lm"
    out = out.with_columns(pl.col(probe).is_not_null().alias("met_lead_matched"))
    out = out.with_columns(
        [
            pl.coalesce([pl.col(f"met_{c}_lm"), pl.col(f"met_{c}_tgt")]).alias(f"met_{c}_tgt")
            for c in keep
            if f"met_{c}_tgt" in out.columns
        ]
    )

    matched = int(out["met_lead_matched"].sum())
    log.info(
        "lead-matched meteorology: %d/%d rows (%.1f%%), %d columns replaced",
        matched, out.height, 100.0 * matched / out.height, len(keep),
    )
    return out.drop([f"met_{c}_lm" for c in keep] + ["lead_day"])


# ---------------------------------------------------------------------------
# joins
# ---------------------------------------------------------------------------
def _join_cpcb(base: pl.DataFrame, start, end) -> pl.DataFrame:
    obs = read_range("cpcb", start, end)
    if obs.is_empty():
        log.warning("no cached CPCB observations for %s..%s", start, end)
        return base.with_columns(
            pl.lit(None, dtype=pl.Float64).alias("pm25"),
            pl.lit(None, dtype=pl.Float64).alias("pm25_clean"),
            pl.lit(None, dtype=pl.UInt32).alias("obs_n"),
            pl.lit(None, dtype=pl.Utf8).alias("quality_flag"),
        )
    obs = obs.select(
        "station_id", "time", "pm25", "pm25_clean",
        pl.col("n_obs").alias("obs_n"), "quality_flag",
    ).unique(subset=["station_id", "time"], keep="first")
    return base.join(obs, on=["station_id", "time"], how="left")


def _join_cams(base: pl.DataFrame, start, end, dataset: str) -> pl.DataFrame:
    cams = read_range(dataset, start, end)
    if cams.is_empty():
        log.warning("no cached CAMS (%s) for %s..%s", dataset, start, end)
        return base.with_columns(
            [pl.lit(None, dtype=pl.Float64).alias(f"cams_{c}") for c in CAMS_COLS]
            + [pl.lit(None, dtype=pl.Utf8).alias("cams_source_class")]
        )
    keep = [c for c in CAMS_COLS if c in cams.columns]
    cams = (
        cams.select(["station_id", "time", "source_class"] + keep)
        .unique(subset=["station_id", "time"], keep="last")
        .rename({c: f"cams_{c}" for c in keep} | {"source_class": "cams_source_class"})
    )
    return base.join(cams, on=["station_id", "time"], how="left")


def _join_meteo(base: pl.DataFrame, start, end, dataset: str) -> pl.DataFrame:
    met = read_range(dataset, start, end)
    if met.is_empty():
        log.warning("no cached meteorology (%s) for %s..%s", dataset, start, end)
        return base.with_columns(
            [pl.lit(None, dtype=pl.Float64).alias(f"met_{c}") for c in MET_COLS]
        )
    keep = [c for c in MET_COLS if c in met.columns]
    met = (
        met.select(["station_id", "time"] + keep)
        .unique(subset=["station_id", "time"], keep="last")
        .rename({c: f"met_{c}" for c in keep})
    )
    return base.join(met, on=["station_id", "time"], how="left")


def _join_metar(base: pl.DataFrame, start, end) -> pl.DataFrame:
    """METAR is one airport, joined on time alone and broadcast to all stations.

    That is a real approximation: VIDP visibility is not Rohini's visibility.
    It is kept because it is the only *measured* visibility we have, and it is
    named `metar_*` so no one mistakes it for a per-station observation.
    """
    metar = read_range("metar", start, end)
    if metar.is_empty():
        log.warning("no cached METAR for %s..%s", start, end)
        return base.with_columns(
            [pl.lit(None, dtype=pl.Float64).alias(f"metar_{c}") for c in METAR_COLS]
        )
    primary = metar["metar_station"].mode().to_list()[0]
    keep = [c for c in METAR_COLS if c in metar.columns]
    metar = (
        metar.filter(pl.col("metar_station") == primary)
        .select(["time"] + keep)
        .unique(subset=["time"], keep="first")
        .rename({c: f"metar_{c}" for c in keep})
    )
    return base.join(metar, on="time", how="left")


def _join_fires(base: pl.DataFrame, start, end, cfg: Config) -> pl.DataFrame:
    """Regional fire load, joined on time.

    `fires_available` separates "no detections" from "no data" — in June both
    look like zero, and only one of them means the air is clean.
    """
    fires = read_range("fires", start, end)
    if fires.is_empty():
        # Same columns as the populated case — a feature that disappears when a
        # source is missing breaks the model signature between train and serve.
        return base.with_columns(
            pl.lit(0.0).alias("fire_count_1h"),
            pl.lit(0.0).alias("fire_frp_1h"),
            pl.lit(0.0).alias("fire_count_24h"),
            pl.lit(0.0).alias("fire_frp_24h"),
            pl.lit(0.0).alias("fire_count_72h"),
            pl.lit(False).alias("fires_available"),
        )
    hourly = (
        fires.with_columns(pl.col("time").dt.truncate("1h").alias("time"))
        .group_by("time")
        .agg(
            pl.len().cast(pl.Float64).alias("fire_count_1h"),
            pl.col("frp").sum().alias("fire_frp_1h"),
        )
    )
    out = base.join(hourly, on="time", how="left").with_columns(
        pl.col("fire_count_1h").fill_null(0.0),
        pl.col("fire_frp_1h").fill_null(0.0),
        pl.lit(True).alias("fires_available"),
    )
    # Smoke takes a day or two to arrive, so the useful predictor is the recent
    # cumulative burn, not this hour's detections.
    return out.with_columns(
        pl.col("fire_count_1h").rolling_sum(24, min_samples=1).over("station_id").alias("fire_count_24h"),
        pl.col("fire_frp_1h").rolling_sum(24, min_samples=1).over("station_id").alias("fire_frp_24h"),
        pl.col("fire_count_1h").rolling_sum(72, min_samples=1).over("station_id").alias("fire_count_72h"),
    )


# ---------------------------------------------------------------------------
# derived features
# ---------------------------------------------------------------------------
def _add_observation_features(df: pl.DataFrame) -> pl.DataFrame:
    """Lags and rolling statistics of the observed series.

    Safe only because the index is complete: shift(k) is exactly k hours.
    """
    out = df.with_columns(
        [
            pl.col("pm25_clean").shift(k).over("station_id").alias(f"obs_lag_{k}h")
            for k in OBS_LAGS_H
        ]
    )
    for w in OBS_WINDOWS_H:
        min_samples = max(1, int(w * MIN_COVERAGE))
        out = out.with_columns(
            pl.col("pm25_clean")
            .rolling_mean(w, min_samples=min_samples)
            .over("station_id")
            .alias(f"obs_mean_{w}h"),
            pl.col("pm25_clean")
            .rolling_std(w, min_samples=min_samples)
            .over("station_id")
            .alias(f"obs_std_{w}h"),
        )

    # Observed visibility history, on the same footing as PM2.5 history.
    # BUILD_PLAN's coupled core requires each series' recent history to be
    # available to all of them: fog and haze are the same physical state seen
    # through two instruments, and a model that can see last night's visibility
    # collapse knows something about this morning's PM2.5 that CAMS does not.
    if "metar_visibility_km" in out.columns:
        out = out.with_columns(
            [
                pl.col("metar_visibility_km").shift(k).over("station_id").alias(f"vis_lag_{k}h")
                for k in (1, 3, 24)
            ]
        )
        out = out.with_columns(
            pl.col("metar_visibility_km")
            .rolling_min(24, min_samples=int(24 * MIN_COVERAGE))
            .over("station_id")
            .alias("vis_min_24h"),
            pl.col("metar_visibility_km")
            .rolling_mean(24, min_samples=int(24 * MIN_COVERAGE))
            .over("station_id")
            .alias("vis_mean_24h"),
        )

    # Staleness: how many hours since this station last reported. A model that
    # cannot see this will treat a three-day-old lag as if it were fresh.
    out = out.with_columns(
        pl.int_range(pl.len()).over("station_id").alias("_row"),
    )
    out = out.with_columns(
        pl.when(pl.col("pm25_clean").is_not_null())
        .then(pl.col("_row"))
        .otherwise(None)
        .forward_fill()
        .over("station_id")
        .alias("_last_ok")
    )
    out = out.with_columns(
        (pl.col("_row") - pl.col("_last_ok")).cast(pl.Int32).alias("obs_gap_h"),
        (pl.col("pm25_clean") - pl.col("pm25_clean").shift(24).over("station_id")).alias(
            "obs_delta_24h"
        ),
    ).drop(["_row", "_last_ok"])
    return out


def _add_calendar_features(df: pl.DataFrame) -> pl.DataFrame:
    """Calendar terms in IST, because human activity follows local clock time.

    This is the only place local time is allowed to appear, and it produces
    numbers, not timestamps — every stored timestamp stays UTC.
    """
    ist = pl.col("time").dt.convert_time_zone("Asia/Kolkata")
    two_pi = 2 * 3.141592653589793
    return df.with_columns(
        ist.dt.hour().alias("hour_ist"),
        ist.dt.weekday().alias("weekday_ist"),
        ist.dt.month().alias("month"),
        ist.dt.ordinal_day().alias("doy"),
        (two_pi * ist.dt.hour() / 24).sin().alias("hour_sin"),
        (two_pi * ist.dt.hour() / 24).cos().alias("hour_cos"),
        (two_pi * ist.dt.ordinal_day() / 365.25).sin().alias("doy_sin"),
        (two_pi * ist.dt.ordinal_day() / 365.25).cos().alias("doy_cos"),
    )


def _default_forecast_cols(base: pl.DataFrame) -> list[str]:
    """Columns legitimately knowable at issue time for a future hour."""
    return [
        c
        for c in base.columns
        if c.startswith(("cams_", "met_"))
        or c in {"fire_count_24h", "fire_frp_24h", "fire_count_72h", "fires_available"}
        or c in {"hour_sin", "hour_cos", "doy_sin", "doy_cos", "hour_ist", "weekday_ist", "month"}
    ]


def _as_date(value: dt.date | str) -> dt.date:
    return dt.date.fromisoformat(value) if isinstance(value, str) else value


CITY_ID = "CITY"


def _scope_to_city(base: pl.DataFrame, cfg: Config) -> pl.DataFrame:
    """Keep only the stations that define the statutory city average."""
    cities = cfg.raw.get("grap", {}).get("city_average_cities")
    if not cities:
        # No setting means "every station", which is what this did before the
        # setting existed. Say so rather than changing behaviour silently.
        log.warning(
            "grap.city_average_cities is unset — averaging all %d stations, "
            "which is not the quantity GRAP is keyed to",
            base["station_id"].n_unique(),
        )
        return base

    # Shared with the GRAP report rather than reimplemented, so the model and
    # the document that describes it cannot end up scoping differently.
    from ..grap import city_average_stations

    keep = city_average_stations(cfg)
    if not keep:
        log.warning("no configured station matches grap.city_average_cities=%s", cities)
        return base.clear()
    log.info(
        "city average scoped to %d of %d stations (%s)",
        len(keep), len(cfg.stations), ", ".join(cities),
    )
    return base.filter(pl.col("station_id").is_in(keep))


def _require_coverage(base: pl.DataFrame, min_coverage: float) -> pl.DataFrame:
    """Drop stations reporting for less than `min_coverage` of the window.

    Default 0.0, which keeps every station — the right behaviour for GRAP, where
    CPCB averages whatever is reporting and a drifting basket is faithful to how
    the policy actually works.

    A controlled experiment needs the opposite. The coupling proof uses city
    PM2.5 as a *covariate*, and 7 Delhi stations came online in early 2026, so
    the basket changed by roughly a quarter partway through the evaluation
    window. A covariate whose definition drifts mid-series is a measurement
    problem whichever way it moves the answer.
    """
    if min_coverage <= 0:
        return base
    hours = base["time"].n_unique()
    if not hours:
        return base
    counts = (
        base.filter(pl.col("pm25_clean").is_not_null())
        .group_by("station_id")
        .agg(pl.len().alias("n"))
    )
    keep = counts.filter(pl.col("n") >= min_coverage * hours)["station_id"].to_list()
    dropped = base["station_id"].n_unique() - len(keep)
    if not keep:
        log.warning("no station meets %.0f%% coverage; keeping all", 100 * min_coverage)
        return base
    log.info(
        "stable basket: kept %d station(s), dropped %d below %.0f%% coverage",
        len(keep), dropped, 100 * min_coverage,
    )
    return base.filter(pl.col("station_id").is_in(keep))


def build_city_base(
    base: pl.DataFrame,
    cfg: Config | None = None,
    min_stations: int = 5,
    min_coverage: float = 0.0,
    stations: list[str] | None = None,
) -> pl.DataFrame:
    """Collapse the station frame to the city-wide series GRAP is keyed to.

    GRAP is invoked on Delhi's city-wide average AQI, not on any one station,
    and that AQI is computed from a 24-hour mean. Modelling the city quantity
    directly avoids having to combine correlated station forecasts into one
    number afterwards -- an aggregation that needs a covariance we do not have
    and would get wrong.

    **Only the cities in `grap.city_average_cities` are averaged.** CAQM keys
    GRAP to Delhi's own AQI, so averaging in Gurugram, Ghaziabad or Bhiwadi
    computes a different quantity and then compares it against statutory Delhi
    thresholds. The station model still trains on every station; the scoping
    applies to this aggregate alone.

    Hours covered by fewer than `min_stations` reporting stations produce a
    null target: during an outage the surviving stations are not a random
    sample of the city (R6).
    """
    cfg = cfg or load_config()
    if base.is_empty():
        return base

    if stations is not None:
        # An explicit basket overrides the GRAP scoping. Used by the coupling
        # experiment, where the question is which stations best describe the
        # air at one airport, not which ones the policy is keyed to.
        base = base.filter(pl.col("station_id").is_in(stations))
        if base.is_empty():
            log.warning("explicit station basket matched no rows")
            return base
    else:
        base = _scope_to_city(base, cfg)
        if base.is_empty():
            log.warning("no stations left after scoping to the GRAP city average")
            return base
    base = _require_coverage(base, min_coverage)

    numeric = [
        c for c, dtype in base.schema.items()
        if dtype.is_numeric() and c not in {"obs_n"}
    ]
    city = (
        base.group_by("time")
        .agg(
            [pl.col(c).mean().alias(c) for c in numeric]
            + [
                pl.col("pm25_clean").count().alias("n_stations"),
                pl.col("pm25_clean").max().alias("worst_station_pm25"),
            ]
        )
        .sort("time")
        .with_columns(pl.lit(CITY_ID).alias("station_id"))
    )
    city = city.with_columns(
        pl.when(pl.col("n_stations") >= min_stations)
        .then(pl.col("pm25_clean"))
        .otherwise(None)
        .alias("pm25_clean")
    )

    # The GRAP-relevant quantity: the 24-hour running mean, which is what the
    # CPCB AQI is defined on. Requires 16 of 24 hours, per the CPCB rule.
    city = city.with_columns(
        pl.col("pm25_clean").rolling_mean(24, min_samples=16).alias("city_pm25_24h"),
        pl.col("cams_pm2_5").rolling_mean(24, min_samples=16).alias("cams_pm25_24h"),
    )
    city = _add_observation_features(city)
    return _add_calendar_features(city)
