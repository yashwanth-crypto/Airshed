"""Wind-aware graph correctness.

The failure mode this guards against is a sign error in the wind convention.
`wind_direction_10m` is the direction the wind blows *from*; get it backwards
and the model reads the downwind station as the upwind one, which is not a
crash, not a visible error, and exactly wrong.
"""

from __future__ import annotations

import datetime as dt

import numpy as np
import polars as pl

from airshed.config import Station
from airshed.models import graph

UTC = dt.timezone.utc

# A cross centred on Delhi: one station 10 km to each compass point.
CENTRE = Station(id="C", name="centre", city="Delhi", agency="X", lat=28.60, lon=77.20)
NORTH = Station(id="N", name="north", city="Delhi", agency="X", lat=28.69, lon=77.20)
SOUTH = Station(id="S", name="south", city="Delhi", agency="X", lat=28.51, lon=77.20)
EAST = Station(id="E", name="east", city="Delhi", agency="X", lat=28.60, lon=77.30)


class _Cfg:
    """Minimal stand-in for Config — only `stations` is used by the graph."""

    def __init__(self, stations):
        self.stations = stations


def test_haversine_matches_known_distance():
    # One degree of latitude is about 111 km.
    assert 110 < graph.haversine_km(28.0, 77.0, 29.0, 77.0) < 112


def test_bearing_points_the_right_way():
    assert abs(graph.bearing_deg(28.6, 77.2, 28.7, 77.2) - 0.0) < 1.0     # north
    assert abs(graph.bearing_deg(28.6, 77.2, 28.6, 77.3) - 90.0) < 1.0    # east
    assert abs(graph.bearing_deg(28.6, 77.2, 28.5, 77.2) - 180.0) < 1.0   # south


def test_geometry_matrices_are_indexed_receptor_then_neighbour():
    dist, bearing = graph.geometry([CENTRE, NORTH])
    assert dist[0, 0] == 0.0
    assert 9 < dist[0, 1] < 11
    assert abs(bearing[0, 1]) < 1.0  # from centre, north lies at bearing 0


def _frame(wind_from_deg: float) -> pl.DataFrame:
    """One hour, four stations, only the neighbours reporting."""
    time = dt.datetime(2025, 11, 5, 6, tzinfo=UTC)
    return pl.DataFrame(
        {
            "station_id": ["C", "N", "S", "E"],
            "time": [time] * 4,
            "pm25_clean": [None, 400.0, 100.0, 250.0],
            "met_wind_direction_10m": [wind_from_deg] * 4,
        }
    ).with_columns(pl.col("time").dt.replace_time_zone("UTC"))


def _centre_value(frame: pl.DataFrame, column: str) -> float:
    cfg = _Cfg([CENTRE, NORTH, SOUTH, EAST])
    out = graph.neighbour_features(frame, cfg=cfg)
    return out.filter(pl.col("station_id") == "C")[column][0]


def test_northerly_wind_reads_the_northern_station():
    """Wind from 0 degrees means air arrives from the north."""
    value = _centre_value(_frame(0.0), "nbr_pm25_wind")
    assert abs(value - 400.0) < 1.0, "a northerly must weight the north station"


def test_southerly_wind_reads_the_southern_station():
    value = _centre_value(_frame(180.0), "nbr_pm25_wind")
    assert abs(value - 100.0) < 1.0


def test_easterly_wind_reads_the_eastern_station():
    value = _centre_value(_frame(90.0), "nbr_pm25_wind")
    assert abs(value - 250.0) < 1.0


def test_wind_weighting_differs_from_plain_distance_weighting():
    """If these agreed, the wind term would be decoration."""
    frame = _frame(0.0)
    windy = _centre_value(frame, "nbr_pm25_wind")
    plain = _centre_value(frame, "nbr_pm25_dist")
    assert abs(windy - plain) > 50.0


def test_distance_weighting_ignores_wind_direction():
    north = _centre_value(_frame(0.0), "nbr_pm25_dist")
    south = _centre_value(_frame(180.0), "nbr_pm25_dist")
    assert abs(north - south) < 1e-6


def test_excluded_station_is_absent_from_its_own_neighbourhood():
    """The leave-one-station-out guarantee (R7)."""
    time = dt.datetime(2025, 11, 5, 6, tzinfo=UTC)
    frame = pl.DataFrame(
        {
            "station_id": ["C", "N"],
            "time": [time] * 2,
            "pm25_clean": [999.0, 100.0],
            "met_wind_direction_10m": [0.0, 0.0],
        }
    ).with_columns(pl.col("time").dt.replace_time_zone("UTC"))
    cfg = _Cfg([CENTRE, NORTH])
    out = graph.neighbour_features(frame, cfg=cfg, exclude=["C"])
    # North's neighbourhood must not contain the excluded centre station.
    north_value = out.filter(pl.col("station_id") == "N")["nbr_pm25_dist"][0]
    assert north_value is None or np.isnan(north_value)


def test_a_station_never_reads_its_own_value():
    time = dt.datetime(2025, 11, 5, 6, tzinfo=UTC)
    frame = pl.DataFrame(
        {
            "station_id": ["C", "N"],
            "time": [time] * 2,
            "pm25_clean": [999.0, 100.0],
            "met_wind_direction_10m": [0.0, 0.0],
        }
    ).with_columns(pl.col("time").dt.replace_time_zone("UTC"))
    cfg = _Cfg([CENTRE, NORTH])
    out = graph.neighbour_features(frame, cfg=cfg)
    centre = out.filter(pl.col("station_id") == "C")["nbr_pm25_dist"][0]
    assert abs(centre - 100.0) < 1e-6, "centre must see only its neighbour, not itself"


def test_edge_table_lists_only_upwind_neighbours():
    edges = graph.edge_table(wind_from_deg=315.0, top=20)
    # Every strong edge under a north-westerly must point up-wind, i.e. the
    # neighbour lies to the north-west of the receptor.
    assert edges.height > 0
    for bearing in edges["bearing_deg"].to_list():
        assert 225 <= bearing <= 405 or bearing <= 45, bearing
