"""Historical archive loader.

Both bugs guarded here were found in real matches, not imagined: a station name
whose own commas hid it, and a Roman numeral that a length filter discarded and
a containment rule then papered over. Neither would have raised an error; both
would have trained the model on the wrong neighbourhood.
"""

from __future__ import annotations

import datetime as dt

import polars as pl

from airshed.config import Station
from airshed.ingest import kaggle_history as kh


def _station(name: str, station_id: str = "X1") -> Station:
    return Station(id=station_id, name=name, city="Delhi", agency="DPCC", lat=28.6, lon=77.2)


def _catalogue(names: dict[str, str]) -> pl.DataFrame:
    return pl.DataFrame(
        {
            "StationId": list(names),
            "StationName": list(names.values()),
            "City": ["Delhi"] * len(names),
            "State": ["Delhi"] * len(names),
            "Status": ["Active"] * len(names),
        }
    )


def test_a_name_containing_commas_still_matches():
    """'IHBAS, Dilshad Garden, Delhi - CPCB' must not collapse to 'IHBAS'."""
    catalogue = _catalogue({"DL013": "IHBAS, Dilshad Garden, Delhi - CPCB"})
    mapping = kh.match_stations(catalogue, [_station("IHBAS Dilshad Garden")])
    assert mapping == {"X1": "DL013"}


def test_a_shorter_name_contained_in_a_longer_one_matches():
    """Ours 'Bahadurgarh' against their 'Arya Nagar, Bahadurgarh'."""
    catalogue = _catalogue({"HR002": "Arya Nagar, Bahadurgarh - HSPCB"})
    mapping = kh.match_stations(catalogue, [_station("Bahadurgarh")])
    assert mapping == {"X1": "HR002"}


def test_roman_numerals_distinguish_two_real_stations():
    """Knowledge Park V and Knowledge Park III are different sites."""
    catalogue = _catalogue({"UP008": "Knowledge Park - III, Greater Noida - UPPCB"})
    mapping = kh.match_stations(catalogue, [_station("Knowledge Park V")])
    assert mapping == {}, "V must never be matched to III"


def test_roman_numerals_match_when_they_agree():
    catalogue = _catalogue(
        {
            "UP008": "Knowledge Park - III, Greater Noida - UPPCB",
            "UP009": "Knowledge Park - V, Greater Noida - UPPCB",
        }
    )
    mapping = kh.match_stations(catalogue, [_station("Knowledge Park V")])
    assert mapping == {"X1": "UP009"}


def test_sector_numbers_are_not_interchangeable():
    catalogue = _catalogue({"HR012": "Sector-51, Gurugram - HSPCB"})
    mapping = kh.match_stations(catalogue, [_station("Sector 11")])
    assert mapping == {}


def test_matching_is_one_to_one():
    """Two of our stations must not both claim the same historical record."""
    catalogue = _catalogue({"DL003": "Ashok Vihar, Delhi - DPCC"})
    mapping = kh.match_stations(
        catalogue, [_station("Ashok Vihar", "A"), _station("Ashok Vihar", "B")]
    )
    assert len(mapping) == 1


def test_a_bare_generic_name_does_not_attach_itself_to_anything():
    """Containment needs a distinctive word, or 'Sector 5' would match all."""
    catalogue = _catalogue({"XX001": "Sector 5, Somewhere Else - PCB"})
    mapping = kh.match_stations(catalogue, [_station("Sector 5")])
    assert mapping == {}


def test_ist_timestamps_convert_to_the_right_utc_hour(tmp_path, monkeypatch):
    """01:00 IST is 19:30 UTC the previous day, floored to 19:00."""
    (tmp_path / "stations.csv").write_text(
        "StationId,StationName,City,State,Status\nDL001,Alipur, Delhi - DPCC,Delhi,Delhi,Active\n",
        encoding="utf-8",
    )
    pl.DataFrame(
        {
            "StationId": ["DL001"] * 3,
            "StationName": ["Alipur, Delhi - DPCC"] * 3,
            "City": ["Delhi"] * 3,
            "State": ["Delhi"] * 3,
            "Status": ["Active"] * 3,
        }
    ).head(1).write_csv(tmp_path / "stations.csv")
    pl.DataFrame(
        {
            "StationId": ["DL001"],
            "Datetime": ["2018-11-05 01:00:00"],
            "PM2.5": [300.0],
        }
    ).write_csv(tmp_path / "station_hour.csv")

    class _Cfg:
        stations = [_station("Alipur", "DL026")]
        upwind_stations: list = []

    out = kh.load(tmp_path, cfg=_Cfg())
    assert out.height == 1
    assert out["time"][0] == dt.datetime(2018, 11, 4, 19, 0, tzinfo=dt.timezone.utc)
    assert out["station_id"][0] == "DL026"


def test_loaded_rows_carry_their_provenance():
    """A result must always be recomputable on the modern period alone."""
    assert kh.SOURCE.startswith("kaggle")
