"""Render the downscaled PM2.5 surface for one hour.

    python scripts/plot_surface.py 2025-11-12T02:00
"""

from __future__ import annotations

import datetime as dt
import sys
from pathlib import Path

import matplotlib

matplotlib.use("Agg")
import matplotlib.pyplot as plt  # noqa: E402
import numpy as np  # noqa: E402
import polars as pl  # noqa: E402

from airshed.config import load_config  # noqa: E402
from airshed.features import build as feat  # noqa: E402
from airshed.models import surface  # noqa: E402

OUT = Path("docs/results")


def main(stamp: str) -> int:
    cfg = load_config()
    when = dt.datetime.fromisoformat(stamp).replace(tzinfo=dt.timezone.utc)
    day = when.date()
    base = feat.build_base(day, day, cfg=cfg)
    grid = surface.surface_for_hour(base, when, cfg=cfg)

    hour = base.filter(pl.col("time") == when)
    observed = {
        r["station_id"]: r["pm25_clean"]
        for r in hour.iter_rows(named=True)
        if r["pm25_clean"] is not None
    }
    wind_from = float(grid["wind_from_deg"][0])

    lats = np.sort(grid["lat"].unique().to_numpy())
    lons = np.sort(grid["lon"].unique().to_numpy())
    field = (
        grid.sort(["lat", "lon"])["pm25"].to_numpy().reshape(len(lats), len(lons))
    )
    dist = (
        grid.sort(["lat", "lon"])["distance_to_station_km"]
        .to_numpy()
        .reshape(len(lats), len(lons))
    )

    fig, (ax, ax2) = plt.subplots(1, 2, figsize=(15, 6.2))

    mesh = ax.pcolormesh(lons, lats, field, cmap="inferno_r", shading="auto")
    fig.colorbar(mesh, ax=ax, label="PM2.5 (µg/m³)")
    for station in cfg.stations:
        value = observed.get(station.id)
        ax.scatter(
            station.lon, station.lat, s=26,
            c="white" if value is None else "#00e5ff",
            edgecolors="black", linewidths=0.6, zorder=3,
        )
    # Arrow shows where the air is coming from.
    ax.annotate(
        "", xy=(0.12, 0.88), xytext=(0.12 + 0.07 * np.sin(np.radians(wind_from)),
                                     0.88 + 0.07 * np.cos(np.radians(wind_from))),
        xycoords="axes fraction",
        arrowprops={"arrowstyle": "-|>", "color": "white", "lw": 2},
    )
    ax.text(0.12, 0.955, f"wind from {wind_from:.0f}°", transform=ax.transAxes,
            color="white", ha="center", fontsize=9)
    ax.set_title(
        f"Wind-aware downscaled surface — {when:%Y-%m-%d %H:%M} UTC", loc="left"
    )
    ax.set_xlabel("longitude")
    ax.set_ylabel("latitude")

    conf = ax2.pcolormesh(lons, lats, dist, cmap="viridis", shading="auto")
    fig.colorbar(conf, ax=ax2, label="km to nearest station")
    ax2.scatter(
        [s.lon for s in cfg.stations], [s.lat for s in cfg.stations],
        s=26, c="white", edgecolors="black", linewidths=0.6, zorder=3,
    )
    ax2.set_title("How far the nearest monitor is (R7: not all cells are equal)", loc="left")
    ax2.set_xlabel("longitude")

    fig.tight_layout()
    OUT.mkdir(parents=True, exist_ok=True)
    out = OUT / f"surface_{when:%Y%m%dT%H%M}.png"
    fig.savefig(out, dpi=140)
    print(f"stations reporting: {len(observed)}")
    print(f"surface range {np.nanmin(field):.0f}-{np.nanmax(field):.0f} µg/m³")
    print(f"wrote {out}")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1]))
