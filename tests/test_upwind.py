"""Upwind corridor features.

Two things can be silently wrong here and neither would crash: the wind
convention (does a north-westerly read Punjab or read Agra?) and the separation
between the upwind network and the NCR network (an upwind station leaking into
the city average would quietly redefine "Delhi AQI").
"""

from __future__ import annotations

import datetime as dt

import numpy as np
import polars as pl

from airshed.config import UpwindStation, load_config
from airshed.features import upwind

UTC = dt.timezone.utc

# Two corridor stations: one north-west of Delhi (the stubble direction), one
# south-east (never upwind during an episode).
NW = UpwindStation(id="UWNW", name="northwest", city="Punjab", agency="X",
                   lat=30.9, lon=75.9, openaq_id=1, distance_km=280.0, bearing_deg=330.0)
SE = UpwindStation(id="UWSE", name="southeast", city="UP", agency="X",
                   lat=27.2, lon=78.0, openaq_id=2, distance_km=200.0, bearing_deg=150.0)


class _Cfg:
    def __init__(self, stations):
        self._stations = stations

    @property
    def upwind_stations(self):
        return self._stations


def _base(wind_from_deg: float, hours: int = 6) -> pl.DataFrame:
    times = pl.datetime_range(
        dt.datetime(2025, 11, 5, tzinfo=UTC),
        dt.datetime(2025, 11, 5, tzinfo=UTC) + dt.timedelta(hours=hours - 1),
        interval="1h", time_zone="UTC", eager=True,
    )
    return pl.DataFrame(
        {
            "station_id": ["DL001"] * hours,
            "time": times,
            "met_wind_direction_10m": [wind_from_deg] * hours,
            "met_wind_speed_10m": [20.0] * hours,
        }
    )


def _observations(hours: int = 6) -> pl.DataFrame:
    times = pl.datetime_range(
        dt.datetime(2025, 11, 5, tzinfo=UTC),
        dt.datetime(2025, 11, 5, tzinfo=UTC) + dt.timedelta(hours=hours - 1),
        interval="1h", time_zone="UTC", eager=True,
    )
    frames = []
    for station, value in ((NW, 500.0), (SE, 50.0)):
        frames.append(
            pl.DataFrame(
                {
                    "station_id": [station.id] * hours,
                    "time": times,
                    "pm25": [value] * hours,
                    "quality_flag": ["ok"] * hours,
                }
            )
        )
    return pl.concat(frames)


def _run(wind_from_deg: float, monkeypatch, hours: int = 6) -> pl.DataFrame:
    monkeypatch.setattr(upwind, "read_range", lambda *a, **k: _observations(hours))
    return upwind.upwind_features(_base(wind_from_deg, hours), cfg=_Cfg([NW, SE]))


def test_northwesterly_reads_the_punjab_station(monkeypatch):
    """A 330-degree wind comes from the north-west, where the smoke is."""
    out = _run(330.0, monkeypatch)
    value = out["upwind_pm25"].drop_nulls()[0]
    assert abs(value - 500.0) < 1.0


def test_southeasterly_does_not_read_punjab(monkeypatch):
    out = _run(150.0, monkeypatch)
    value = out["upwind_pm25"].drop_nulls()[0]
    assert abs(value - 50.0) < 1.0, "a south-easterly must not import Punjab"


def test_crosswind_gives_no_corridor_exposure(monkeypatch):
    """Wind perpendicular to both stations means nothing is arriving."""
    out = _run(60.0, monkeypatch)
    assert float(out["upwind_alignment"].max()) < 0.35


def test_alignment_is_one_when_wind_points_straight_down_the_corridor(monkeypatch):
    out = _run(330.0, monkeypatch)
    assert float(out["upwind_alignment"].max()) > 0.99


def test_transport_time_falls_as_wind_strengthens(monkeypatch):
    monkeypatch.setattr(upwind, "read_range", lambda *a, **k: _observations())
    slow = upwind.upwind_features(
        _base(330.0).with_columns(pl.lit(10.0).alias("met_wind_speed_10m")), cfg=_Cfg([NW, SE])
    )
    fast = upwind.upwind_features(
        _base(330.0).with_columns(pl.lit(40.0).alias("met_wind_speed_10m")), cfg=_Cfg([NW, SE])
    )
    assert float(slow["upwind_transport_h"].max()) > float(fast["upwind_transport_h"].max())


def test_transport_time_is_capped_in_dead_calm(monkeypatch):
    monkeypatch.setattr(upwind, "read_range", lambda *a, **k: _observations())
    calm = upwind.upwind_features(
        _base(330.0).with_columns(pl.lit(0.0).alias("met_wind_speed_10m")), cfg=_Cfg([NW, SE])
    )
    assert float(calm["upwind_transport_h"].max()) <= upwind.MAX_TRANSPORT_H


def test_missing_corridor_data_yields_null_columns_not_a_crash(monkeypatch):
    monkeypatch.setattr(upwind, "read_range", lambda *a, **k: pl.DataFrame())
    out = upwind.upwind_features(_base(330.0), cfg=_Cfg([NW, SE]))
    assert "upwind_pm25" in out.columns
    assert out["upwind_pm25"].null_count() == out.height


def test_no_upwind_stations_configured_is_survivable():
    out = upwind.upwind_features(_base(330.0), cfg=_Cfg([]))
    assert out["upwind_pm25_advected"].null_count() == out.height


def test_advection_samples_the_series_at_the_travel_lag():
    times = np.arange(50)
    corridor = times.astype(float) * 10.0  # value == 10 * hour index
    transport = np.full(50, 5.0)
    advected = upwind._advect(times, corridor, transport)
    # At index 20, the air arriving left the corridor 5 hours earlier.
    assert advected[20] == corridor[15]
    assert np.isnan(advected[2]), "no source before the series starts"


def test_upwind_network_is_disjoint_from_the_ncr_network():
    """An upwind station must never become a forecast target (R7 and GRAP)."""
    cfg = load_config()
    ncr = {s.id for s in cfg.stations}
    up = {s.id for s in cfg.upwind_stations}
    assert up and ncr
    assert not (ncr & up)
    ncr_openaq = {s.openaq_id for s in cfg.stations}
    assert not (ncr_openaq & {s.openaq_id for s in cfg.upwind_stations})


def test_every_upwind_station_lies_upwind_of_delhi():
    """Configured corridor stations must sit in the north-west sector."""
    for station in load_config().upwind_stations:
        assert 280 <= station.bearing_deg <= 360, station.name
        assert 60 <= station.distance_km <= 350, station.name
