"""The Phase 1 gate.

    "Can you reconstruct every input feature for any past week on demand, from
     local storage, with no network call?"

Answered by doing it — one week in November, one week in June — with the
network calls physically blocked for the duration. A gate that is answered by
reading the code is not answered.
"""

from __future__ import annotations

import datetime as dt
import socket
from contextlib import contextmanager

import polars as pl
from rich.console import Console
from rich.table import Table

from .config import load_config
from .features import build as feat
from .features import splits as split_mod

REQUIRED = {
    "target": ["pm25", "pm25_clean"],
    "cams": ["cams_pm2_5", "cams_pm10", "cams_dust"],
    "meteorology": [
        "met_boundary_layer_height",
        "met_lapse_2m_925",
        "met_wind_speed_10m",
        "met_u10",
        "met_v10",
        "met_ventilation_index",
        "met_temperature_2m",
        "met_relative_humidity_2m",
    ],
    "observed visibility": ["metar_visibility_km", "metar_dew_point_depression_c"],
    "fires": ["fire_count_24h", "fire_frp_24h", "fires_available"],
    "observation history": ["obs_lag_24h", "obs_mean_24h", "obs_gap_h"],
    "calendar": ["hour_sin", "doy_sin", "weekday_ist"],
}


class NetworkUsedError(RuntimeError):
    """Raised when code under `no_network()` tries to open a socket."""


@contextmanager
def no_network():
    """Make any outbound connection fail loudly.

    This is the actual test. Without it, "reads from local storage" is a claim
    about intent rather than a fact about behaviour.
    """
    real = socket.socket.connect

    def blocked(self, address):  # noqa: ANN001
        raise NetworkUsedError(f"network call attempted to {address}")

    socket.socket.connect = blocked
    try:
        yield
    finally:
        socket.socket.connect = real


def reconstruct_week(start: str, console: Console) -> tuple[bool, pl.DataFrame]:
    """Rebuild every feature for the seven days from `start`, offline."""
    start_d = dt.date.fromisoformat(start)
    end_d = start_d + dt.timedelta(days=6)
    console.print(f"\n[bold]reconstructing {start_d} .. {end_d}[/] with the network blocked")

    with no_network():
        base = feat.build_base(start_d, end_d)

    cfg = load_config()
    expected = len(cfg.stations) * 24 * 7
    ok = True

    if base.height != expected:
        console.print(f"  [red]index wrong size:[/] {base.height} rows, expected {expected}")
        ok = False
    else:
        console.print(f"  index complete: {base.height:,} rows ({len(cfg.stations)} stations x 168 h)")

    dupes = base.height - base.select(["station_id", "time"]).unique().height
    if dupes:
        console.print(f"  [red]{dupes} duplicated (station, time) pairs[/]")
        ok = False

    table = Table(show_header=True)
    table.add_column("group")
    table.add_column("column")
    table.add_column("non-null", justify="right")
    table.add_column("coverage", justify="right")
    for group, cols in REQUIRED.items():
        for col in cols:
            if col not in base.columns:
                table.add_row(group, col, "[red]MISSING[/]", "-")
                ok = False
                continue
            n = base[col].is_not_null().sum()
            pct = n / base.height if base.height else 0.0
            mark = "[green]" if pct > 0 else "[red]"
            table.add_row(group, col, f"{n:,}", f"{mark}{pct:.1%}[/]")
            if pct == 0:
                ok = False
    console.print(table)
    return ok, base


def run_gate(winter_start: str, summer_start: str, console: Console | None = None) -> bool:
    console = console or Console()
    results = []
    for label, start in (("winter", winter_start), ("summer", summer_start)):
        try:
            ok, base = reconstruct_week(start, console)
        except NetworkUsedError as exc:
            console.print(f"  [red]R8 violation:[/] {exc}")
            ok, base = False, pl.DataFrame()
        results.append((label, start, ok))

        if not base.is_empty():
            with no_network():
                sup = feat.build_supervised(base)
            if sup.is_empty():
                console.print("  [red]supervised table empty[/] — no targets available")
            else:
                per_h = (
                    sup.group_by("horizon_h")
                    .agg(pl.len().alias("rows"))
                    .sort("horizon_h")
                )
                console.print(f"  supervised rows by horizon: {per_h.to_dicts()}")
                labelled = split_mod.assign_split(sup)
                console.print(
                    "  split assignment: "
                    + str(
                        labelled.group_by("split")
                        .agg(pl.len().alias("rows"))
                        .sort("split")
                        .to_dicts()
                    )
                )

    console.print("\n[bold]Phase 1 gate[/]")
    all_ok = True
    for label, start, ok in results:
        mark = "[green]PASS[/]" if ok else "[red]FAIL[/]"
        console.print(f"  {mark}  {label} week from {start}")
        all_ok &= ok
    if not all_ok:
        console.print(
            "\n[yellow]Gate not met.[/] Every feature group must be reconstructible "
            "offline before Phase 2 starts."
        )
    return all_ok
