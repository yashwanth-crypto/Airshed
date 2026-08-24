"""The downscaled surface: predictions on a regular grid over NCR.

The grid is 0.05° (~5.5 km), finer than the station network and far finer than
CAMS's ~40 km. R7 governs what may be claimed about it: the regional signal
comes from CAMS, the sub-grid structure comes from the station network through
the wind-aware kernel, and the honest error bar on any grid cell is the
leave-one-station-out number, not the model's own confidence.

Every cell carries `distance_to_station_km`. A cell 2 km from a monitor and a
cell 25 km out in Bhiwadi are not equally knowable, and the UI must render that
difference rather than paint a uniformly confident map.

**Not yet included:** road density and satellite AOD as auxiliary predictors.
BUILD_PLAN lists both. CAMS aerosol optical depth is already ingested and is
available per cell; road density needs an OSM extract we have not built. Until
it is in, between-station structure comes from the wind kernel alone, and this
docstring is the record of that gap.
"""

from __future__ import annotations

import logging

import numpy as np
import polars as pl

from ..config import Config, load_config
from . import graph

log = logging.getLogger(__name__)


def grid_frame(cfg: Config | None = None) -> pl.DataFrame:
    """The regular lat/lon grid, with each cell's distance to the nearest station."""
    cfg = cfg or load_config()
    points = cfg.grid_points()
    lats = np.array([p[0] for p in points])
    lons = np.array([p[1] for p in points])

    station_lats = np.array([s.lat for s in cfg.stations])
    station_lons = np.array([s.lon for s in cfg.stations])
    distances = graph.haversine_km(
        lats[:, None], lons[:, None], station_lats[None, :], station_lons[None, :]
    )
    return pl.DataFrame(
        {
            "lat": lats,
            "lon": lons,
            "distance_to_station_km": distances.min(axis=1).round(2),
            "nearest_station": [cfg.stations[i].id for i in distances.argmin(axis=1)],
        }
    )


def interpolate(
    station_values: dict[str, float],
    wind_from_deg: float,
    cfg: Config | None = None,
    length_scale_km: float = graph.LENGTH_SCALE_KM,
    wind_power: float = graph.WIND_POWER,
) -> pl.DataFrame:
    """Spread station values onto the grid with the wind-aware kernel.

    `station_values` is whatever the temporal model predicted at each station
    for one target hour. The kernel is the same one leave-one-station-out was
    validated with, so the reported skill applies to this surface rather than
    to a different method that merely looks similar.
    """
    cfg = cfg or load_config()
    grid = grid_frame(cfg)
    usable = [s for s in cfg.stations if np.isfinite(station_values.get(s.id, np.nan))]
    if not usable:
        return grid.with_columns(
            pl.lit(None, dtype=pl.Float64).alias("pm25"),
            pl.lit(None, dtype=pl.Float64).alias("pm25_idw"),
        )

    values = np.array([station_values[s.id] for s in usable])
    lat_s = np.array([s.lat for s in usable])
    lon_s = np.array([s.lon for s in usable])
    lat_g = grid["lat"].to_numpy()
    lon_g = grid["lon"].to_numpy()

    dist = graph.haversine_km(lat_g[:, None], lon_g[:, None], lat_s[None, :], lon_s[None, :])
    bearing = graph.bearing_deg(lat_g[:, None], lon_g[:, None], lat_s[None, :], lon_s[None, :])
    decay = np.exp(-dist / length_scale_km)

    upwind = np.clip(np.cos(np.radians(wind_from_deg - bearing)), 0.0, None) ** wind_power
    w_wind = decay * upwind
    totals_wind = w_wind.sum(axis=1)
    # Where no station lies upwind, fall back to distance weighting rather than
    # leaving a hole: a cell with no upwind monitor is less certain, not unknown.
    wind_vals = np.where(
        totals_wind > 1e-6,
        (w_wind @ values) / np.maximum(totals_wind, 1e-9),
        np.nan,
    )
    idw_vals = (decay @ values) / np.maximum(decay.sum(axis=1), 1e-9)

    return grid.with_columns(
        pl.Series("pm25", np.where(np.isfinite(wind_vals), wind_vals, idw_vals)),
        pl.Series("pm25_idw", idw_vals),
        pl.lit(float(wind_from_deg)).alias("wind_from_deg"),
    )


def surface_for_hour(
    base: pl.DataFrame,
    time,
    cfg: Config | None = None,
    value_col: str = "pm25_clean",
) -> pl.DataFrame:
    """Build the surface for one hour straight from observed station values.

    Used for the replay view and for eyeballing the kernel; the live path
    passes model predictions to `interpolate` instead.
    """
    cfg = cfg or load_config()
    hour = base.filter(pl.col("time") == time)
    if hour.is_empty():
        raise ValueError(f"no rows at {time}")
    values = {
        row["station_id"]: row[value_col]
        for row in hour.iter_rows(named=True)
        if row[value_col] is not None
    }
    wind = hour["met_wind_direction_10m"].drop_nulls()
    wind_from = float(wind.mean()) if wind.len() else 0.0
    return interpolate(values, wind_from, cfg=cfg)
