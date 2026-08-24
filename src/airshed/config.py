"""Configuration loading.

One `config.toml` at the repo root is the single source of truth for station
coordinates, GRAP thresholds, grid bounds and endpoints. Nothing else in the
package is allowed to hardcode those (CLAUDE.md, Conventions).
"""

from __future__ import annotations

import tomllib
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path
from typing import Any


def repo_root() -> Path:
    """Walk up from this file until we find config.toml."""
    here = Path(__file__).resolve()
    for parent in here.parents:
        if (parent / "config.toml").is_file():
            return parent
    raise FileNotFoundError("config.toml not found in any parent of " + str(here))


@dataclass(frozen=True, slots=True)
class Station:
    """A CAAQMS ground station.

    `openaq_id` is 0 when the OpenAQ location id has not been resolved yet;
    the S3 archive backend cannot fetch such a station.
    """

    id: str
    name: str
    city: str
    agency: str
    lat: float
    lon: float
    openaq_id: int = 0

    @property
    def resolved(self) -> bool:
        return self.openaq_id > 0

    @property
    def label(self) -> str:
        return f"{self.name}, {self.city}"


@dataclass(frozen=True, slots=True)
class UpwindStation(Station):
    """A monitor outside NCR, used as a leading indicator only.

    Carries its distance and bearing from Delhi so transport features do not
    have to recompute geometry on every call.
    """

    distance_km: float = 0.0
    bearing_deg: float = 0.0


@dataclass(frozen=True, slots=True)
class GrapStage:
    stage: int
    name: str
    aqi_min: int
    aqi_max: int


@dataclass(frozen=True, slots=True)
class Breakpoint:
    """One CPCB AQI sub-index bracket."""

    c_lo: float
    c_hi: float
    i_lo: int
    i_hi: int


class Config:
    """Thin typed wrapper over the parsed TOML.

    Raw sections stay reachable through `cfg.raw` so a new key does not need a
    code change here before it can be used.
    """

    def __init__(self, raw: dict[str, Any], root: Path) -> None:
        self.raw = raw
        self.root = root

    # -- sections -----------------------------------------------------------
    @property
    def domain(self) -> dict[str, Any]:
        return self.raw["domain"]

    @property
    def forecast(self) -> dict[str, Any]:
        return self.raw["forecast"]

    def source(self, name: str) -> dict[str, Any]:
        return self.raw["sources"][name]

    # -- stations -----------------------------------------------------------
    @property
    def stations(self) -> list[Station]:
        return [Station(**s) for s in self.raw["stations_meta"]["stations"]]

    def station(self, station_id: str) -> Station:
        for s in self.stations:
            if s.id == station_id:
                return s
        raise KeyError(f"unknown station id: {station_id}")

    @property
    def upwind_stations(self) -> list[UpwindStation]:
        """Stations outside NCR that feed transport features.

        Deliberately separate from `stations`: these are never forecast
        targets, never part of the city average, and never held out in LOSO.
        """
        raw = self.raw.get("upwind", {}).get("upwind_stations", [])
        return [UpwindStation(**s) for s in raw]

    def stations_in(self, city: str) -> list[Station]:
        return [s for s in self.stations if s.city.lower() == city.lower()]

    # -- thresholds ---------------------------------------------------------
    @property
    def grap_stages(self) -> list[GrapStage]:
        return [GrapStage(**s) for s in self.raw["grap"]["stages"]]

    @property
    def pm25_breakpoints(self) -> list[Breakpoint]:
        return [Breakpoint(**b) for b in self.raw["aqi"]["pm25_breakpoints"]]

    @property
    def horizons(self) -> list[int]:
        return list(self.forecast["horizons_h"])

    # -- storage ------------------------------------------------------------
    @property
    def data_root(self) -> Path:
        return self.root / self.raw["storage"]["root"]

    @property
    def raw_dir(self) -> Path:
        return self.root / self.raw["storage"]["raw"]

    @property
    def processed_dir(self) -> Path:
        return self.root / self.raw["storage"]["processed"]

    # -- grid ---------------------------------------------------------------
    def grid_points(self) -> list[tuple[float, float]]:
        """Regular (lat, lon) grid over the NCR box.

        R7: this is the downscaling *target* resolution, not CAMS resolution.
        """
        d = self.domain
        step = d["grid_step_deg"]
        lats = _arange(d["lat_min"], d["lat_max"], step)
        lons = _arange(d["lon_min"], d["lon_max"], step)
        return [(round(la, 4), round(lo, 4)) for la in lats for lo in lons]

    def bbox(self) -> tuple[float, float, float, float]:
        """(lat_min, lon_min, lat_max, lon_max)."""
        d = self.domain
        return (d["lat_min"], d["lon_min"], d["lat_max"], d["lon_max"])


def _arange(lo: float, hi: float, step: float) -> list[float]:
    out: list[float] = []
    n = 0
    while lo + n * step <= hi + 1e-9:
        out.append(lo + n * step)
        n += 1
    return out


@lru_cache(maxsize=4)
def load_config(path: str | None = None) -> Config:
    root = Path(path).parent.resolve() if path else repo_root()
    cfg_path = Path(path) if path else root / "config.toml"
    with open(cfg_path, "rb") as fh:
        raw = tomllib.load(fh)
    return Config(raw, root)
