"""Wind-aware station graph.

Distance-weighted interpolation says a station 10 km north and a station 10 km
south should influence a receptor equally. On a November morning with a
north-westerly carrying stubble smoke into Delhi, that is simply false: the
upwind station is telling you what is about to arrive, the downwind one is
telling you what has already left.

So edge weight is a function of distance **and** of how well the wind aligns
with the inter-station bearing:

    w(j -> i, t) = exp(-d_ij / L) * max(0, cos(wind_from_i(t) - bearing(i, j)))^p

`wind_direction_10m` is meteorological convention — the direction the wind is
coming *from*. When that equals the bearing from receptor i to neighbour j, j
sits directly upwind of i and gets full weight; at right angles it gets none.
This is the structure a distance kernel cannot express, and it is why the graph
exists (AirPhyNet, TransNet, and the ST-GNN literature make the same argument).

Two features come out of it per station-hour:

* `nbr_pm25_wind` — upwind-weighted neighbour concentration, the transport term;
* `nbr_pm25_dist` — plain distance-weighted neighbour concentration, so the
  ablation can show whether the wind term adds anything over ordinary spatial
  smoothing. If it does not, we should say so rather than keep the machinery.

`exclude` is what makes leave-one-station-out honest: a held-out station must
not appear in its own neighbourhood (R7).
"""

from __future__ import annotations

import logging

import numpy as np
import polars as pl

from ..config import Config, Station, load_config

log = logging.getLogger(__name__)

# Distance over which neighbour influence decays by 1/e. Delhi NCR is roughly
# 60 km across, so 15 km keeps a receptor's neighbourhood local without
# isolating the outer stations.
LENGTH_SCALE_KM = 15.0
# Sharpness of the upwind cone. 2 gives a lobe roughly 90 degrees wide.
WIND_POWER = 2.0
EARTH_R_KM = 6371.0088


def haversine_km(lat1, lon1, lat2, lon2):
    p1, p2 = np.radians(lat1), np.radians(lat2)
    dp = p2 - p1
    dl = np.radians(lon2 - lon1)
    a = np.sin(dp / 2) ** 2 + np.cos(p1) * np.cos(p2) * np.sin(dl / 2) ** 2
    return 2 * EARTH_R_KM * np.arcsin(np.sqrt(a))


def bearing_deg(lat1, lon1, lat2, lon2):
    """Initial bearing from point 1 to point 2, degrees clockwise from north."""
    p1, p2 = np.radians(lat1), np.radians(lat2)
    dl = np.radians(lon2 - lon1)
    y = np.sin(dl) * np.cos(p2)
    x = np.cos(p1) * np.sin(p2) - np.sin(p1) * np.cos(p2) * np.cos(dl)
    return (np.degrees(np.arctan2(y, x)) + 360.0) % 360.0


def geometry(stations: list[Station]) -> tuple[np.ndarray, np.ndarray]:
    """(distance_km, bearing_deg) matrices, indexed [receptor, neighbour]."""
    lats = np.array([s.lat for s in stations])
    lons = np.array([s.lon for s in stations])
    lat_i, lat_j = lats[:, None], lats[None, :]
    lon_i, lon_j = lons[:, None], lons[None, :]
    return haversine_km(lat_i, lon_i, lat_j, lon_j), bearing_deg(lat_i, lon_i, lat_j, lon_j)


def neighbour_features(
    base: pl.DataFrame,
    cfg: Config | None = None,
    exclude: list[str] | None = None,
    length_scale_km: float = LENGTH_SCALE_KM,
    wind_power: float = WIND_POWER,
    value_col: str = "pm25_clean",
) -> pl.DataFrame:
    """Add upwind and distance-weighted neighbour concentrations.

    Both are computed from observations at the *same hour*, so they are known
    at issue time and carry no information from the future.
    """
    cfg = cfg or load_config()
    exclude = set(exclude or [])
    stations = cfg.stations
    index = {s.id: k for k, s in enumerate(stations)}
    n = len(stations)

    dist, bearing = geometry(stations)
    decay = np.exp(-dist / length_scale_km)
    np.fill_diagonal(decay, 0.0)  # a station is never its own neighbour

    # Reshape into (time x station) matrices by pivoting rather than iterating
    # rows: leave-one-station-out refits this for every held-out station, and a
    # per-row Python loop over half a million rows turns a two-minute
    # evaluation into an hour.
    wind_col = "met_wind_direction_10m"
    times = base["time"].unique().sort()
    order = [s.id for s in stations]

    def matrix(column: str) -> np.ndarray:
        wide = base.pivot(index="time", on="station_id", values=column, aggregate_function="first")
        wide = wide.sort("time")
        out = np.full((wide.height, n), np.nan)
        for name in wide.columns:
            if name == "time":
                continue
            si = index.get(name)
            if si is not None:
                out[:, si] = wide[name].cast(pl.Float64).to_numpy()
        return out

    values = matrix(value_col)
    winds = matrix(wind_col) if wind_col in base.columns else np.full_like(values, np.nan)

    # A held-out station must not appear in its own neighbourhood (R7).
    for station_id in exclude:
        si = index.get(station_id)
        if si is not None:
            values[:, si] = np.nan
    del order

    usable = np.array([s.id not in exclude for s in stations])
    wind_out = np.full((len(times), n), np.nan)
    dist_out = np.full((len(times), n), np.nan)
    count_out = np.zeros((len(times), n))

    for t in range(len(times)):
        row_values = values[t]
        valid = np.isfinite(row_values) & usable
        if not valid.any():
            continue

        # Distance-only weighting: the control.
        w_dist = decay[:, valid]
        totals = w_dist.sum(axis=1)
        dist_out[t] = np.where(totals > 0, w_dist @ row_values[valid] / np.maximum(totals, 1e-9), np.nan)
        count_out[t] = valid.sum()

        # Wind-aware weighting: the claim.
        wind_from = winds[t]
        if np.isfinite(wind_from).any():
            # Fall back to the city-mean wind where a receptor's own wind is
            # missing, so an isolated gap does not silently disable transport.
            filled = np.where(np.isfinite(wind_from), wind_from, np.nanmean(wind_from))
            alignment = np.cos(np.radians(filled[:, None] - bearing))
            upwind = np.clip(alignment, 0.0, None) ** wind_power
            w_wind = (decay * upwind)[:, valid]
            totals_w = w_wind.sum(axis=1)
            wind_out[t] = np.where(
                totals_w > 1e-6, w_wind @ row_values[valid] / np.maximum(totals_w, 1e-9), np.nan
            )

    long = pl.DataFrame(
        {
            "time": np.repeat(times.to_numpy(), n),
            "station_id": [s.id for s in stations] * len(times),
            "nbr_pm25_wind": wind_out.reshape(-1),
            "nbr_pm25_dist": dist_out.reshape(-1),
            "nbr_count": count_out.reshape(-1),
        }
    ).with_columns(pl.col("time").cast(base.schema["time"]))

    out = base.join(long, on=["station_id", "time"], how="left")
    log.info(
        "neighbour features: %.1f%% of rows have an upwind neighbour",
        100.0 * out["nbr_pm25_wind"].is_not_null().mean(),
    )
    return out


def edge_table(
    cfg: Config | None = None,
    wind_from_deg: float = 315.0,
    length_scale_km: float = LENGTH_SCALE_KM,
    wind_power: float = WIND_POWER,
    top: int = 15,
) -> pl.DataFrame:
    """Strongest edges under a given wind — for inspecting what the graph does.

    Default is a north-westerly, the direction that carries Punjab stubble
    smoke into Delhi.
    """
    cfg = cfg or load_config()
    stations = cfg.stations
    dist, bearing = geometry(stations)
    decay = np.exp(-dist / length_scale_km)
    np.fill_diagonal(decay, 0.0)
    upwind = np.clip(np.cos(np.radians(wind_from_deg - bearing)), 0.0, None) ** wind_power
    weight = decay * upwind

    rows = []
    for i, receptor in enumerate(stations):
        for j, neighbour in enumerate(stations):
            if weight[i, j] <= 0:
                continue
            rows.append(
                {
                    "receptor": receptor.name,
                    "upwind_neighbour": neighbour.name,
                    "distance_km": round(float(dist[i, j]), 1),
                    "bearing_deg": round(float(bearing[i, j]), 0),
                    "weight": round(float(weight[i, j]), 4),
                }
            )
    return pl.DataFrame(rows).sort("weight", descending=True).head(top)


def bearing_between(a: Station, b: Station) -> float:
    return float(bearing_deg(a.lat, a.lon, b.lat, b.lon))


def distance_between(a: Station, b: Station) -> float:
    return float(haversine_km(a.lat, a.lon, b.lat, b.lon))
