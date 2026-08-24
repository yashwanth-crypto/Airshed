"""Tests for station discovery and the GRAP city-average scope.

Both exist because of a specific way this project can be quietly wrong: adding a
station is a one-line config change that silently redefines what "Delhi's
city-wide average" means, and a co-located duplicate silently makes
leave-one-station-out look better than it is. Neither failure raises anything.
"""

from __future__ import annotations

import datetime as dt

import polars as pl
import pytest

from airshed.features.build import _scope_to_city
from airshed.ingest import cpcb


# -- co-location -----------------------------------------------------------
def test_colocated_candidates_are_dropped_from_the_emitted_block():
    """A twin at zero distance breaks leave-one-station-out (R7).

    "Pusa, Delhi - IMD" and "Pusa, Delhi - DPCC" are both live and share a
    coordinate to the metre. Holding one out and predicting it from a perfect
    copy is not spatial skill.
    """
    records = [
        {
            "openaq_id": 6356, "openaq_name": "Pusa, Delhi - DPCC", "name": "Pusa",
            "agency": "DPCC", "provider": "CPCB", "lat": 28.639645, "lon": 77.146262,
            "first": "2025-02-18", "last": "2026-08-24", "km_from_centre": 6.8,
            "nearest_configured": "DL024", "nearest_name": "Pusa",
            "nearest_km": 0.0, "colocated": True,
        },
        {
            "openaq_id": 6254594, "openaq_name": "Talkatora Garden, Delhi - DPCC",
            "name": "Talkatora Garden", "agency": "DPCC", "provider": "N/A",
            "lat": 28.62181, "lon": 77.194463, "first": "2026-02-27",
            "last": "2026-08-24", "km_from_centre": 1.7,
            "nearest_configured": "DL021", "nearest_name": "Mandir Marg",
            "nearest_km": 1.75, "colocated": False,
        },
    ]
    emitted = cpcb.emit_new_station_lines(records)
    assert "Talkatora Garden" in emitted
    assert "Pusa" not in emitted
    assert emitted.count("{ id =") == 1


def test_a_genuine_neighbour_is_kept():
    # NISE Gwal Pahari and TERI Gram are 590 m apart and are two real stations.
    # The threshold must catch identical coordinates without eating these.
    assert 0.59 > cpcb.COLOCATED_KM


# -- id allocation ---------------------------------------------------------
def test_new_ids_continue_the_series_and_never_reuse(monkeypatch):
    """Ids are the partition key for every Parquet file already written.

    Renumbering to make a tidy sequence would orphan history, so allocation only
    ever appends past the current maximum.
    """
    class FakeStation:
        def __init__(self, sid, city):
            self.id, self.city = sid, city

    class FakeCfg:
        stations = [FakeStation("DL001", "Delhi"), FakeStation("DL037", "Delhi"),
                    FakeStation("HR007", "Gurugram")]

    records = [
        {"openaq_name": "Talkatora Garden, Delhi - DPCC"},
        {"openaq_name": "Some Site, Delhi - DPCC"},
        {"openaq_name": "Another, Gurugram - HSPCB"},
    ]
    ids = cpcb.next_station_ids(records, cfg=FakeCfg())
    assert ids == ["DL038", "DL039", "HR008"]
    assert not ({"DL001", "DL037", "HR007"} & set(ids))


def test_unknown_city_gets_its_own_prefix_rather_than_a_wrong_one():
    class FakeCfg:
        stations = []

    ids = cpcb.next_station_ids([{"openaq_name": "Somewhere, Atlantis - XYZ"}], cfg=FakeCfg())
    assert ids == ["NC001"]


# -- name cleaning ---------------------------------------------------------
@pytest.mark.parametrize(
    "raw,expected",
    [
        ("Talkatora Garden, Delhi - DPCC", "Talkatora Garden"),
        ("Knowledge Park - III, Greater Noida - UPPCB", "Knowledge Park - III"),
        ("Murthal, Sonipat - HSPCB", "Murthal"),
    ],
)
def test_clean_name_strips_city_and_agency(raw, expected):
    assert cpcb._clean_name(raw) == expected


def test_clean_name_keeps_roman_numerals():
    """"Knowledge Park - III" must not collapse to "Knowledge Park".

    data-findings section 8 records "Knowledge Park V" matching "Knowledge Park
    III"; both are now configured, so losing the numeral would merge two real
    stations into one name.
    """
    assert "III" in cpcb._clean_name("Knowledge Park - III, Greater Noida - UPPCB")


# -- GRAP city scope -------------------------------------------------------
class _Station:
    def __init__(self, sid, city):
        self.id, self.city = sid, city


class _Cfg:
    def __init__(self, cities):
        self.stations = [
            _Station("DL001", "Delhi"), _Station("DL002", "New Delhi"),
            _Station("HR001", "Gurugram"), _Station("RJ001", "Bhiwadi"),
        ]
        self.raw = {"grap": {"city_average_cities": cities}} if cities is not None else {"grap": {}}


def _base():
    t = dt.datetime(2026, 1, 1, tzinfo=dt.timezone.utc)
    return pl.DataFrame(
        {"station_id": ["DL001", "DL002", "HR001", "RJ001"], "time": [t] * 4,
         "pm25_clean": [400.0, 400.0, 100.0, 50.0]}
    )


def test_city_average_excludes_stations_outside_delhi():
    """GRAP is invoked on Delhi's AQI, not the NCR ring's.

    With Bhiwadi and Gurugram averaged in, a Delhi Stage-IV morning reads as
    Stage II and the decision layer forecasts the wrong quantity accurately.
    """
    scoped = _scope_to_city(_base(), _Cfg(["Delhi", "New Delhi"]))
    assert sorted(scoped["station_id"].to_list()) == ["DL001", "DL002"]
    assert scoped["pm25_clean"].mean() == 400.0


def test_unscoped_config_keeps_every_station_and_says_so(caplog):
    # Absent setting must not change behaviour silently — it warns instead.
    with caplog.at_level("WARNING"):
        scoped = _scope_to_city(_base(), _Cfg(None))
    assert scoped.height == 4
    assert "city_average_cities" in caplog.text


def test_city_matching_ignores_case_and_padding():
    scoped = _scope_to_city(_base(), _Cfg(["  delhi  ", "NEW DELHI"]))
    assert scoped.height == 2
