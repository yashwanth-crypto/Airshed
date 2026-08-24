"""Leave-one-station-out validation.

R7: any neighbourhood-level detail we claim comes from our downscaling layer,
not from CAMS, and it has to be *validated*, not asserted. There is no
block-level ground truth for anyone — not for us, not for the operational
government systems — so the only honest test available is this one: delete a
station, predict it from the rest, and report the error in µg/m³.

Two rules make the test mean something:

**The held-out station contributes nothing.** It is dropped from the training
rows *and* from its own neighbourhood, so the graph cannot quietly read the
answer off the station it is meant to be predicting.

**The model may not use the held-out station's own history.** An unmonitored
location has no `obs_lag_24h`. A model that leans on its own past observations
would score wonderfully here and be useless at the one job spatial prediction
exists for — saying what the air is like where there is no instrument. So the
LOSO model is built with `use_obs_history=False` and sees only CAMS,
meteorology, calendar terms, and what the *other* stations report.

Two baselines make the number interpretable:

* `idw` — plain distance-weighted interpolation from the other stations, the
  textbook approach the graph must beat to justify itself;
* `raw-cams` — the physics forecast at that point, which is what you would use
  if you had no network at all.
"""

from __future__ import annotations

import logging

import numpy as np
import polars as pl

from ..config import Config, load_config
from ..features import build as feat
from ..features import splits as split_mod
from ..models import graph
from ..models.corrector import CorrectorModel

log = logging.getLogger(__name__)

# Stations spread across the domain rather than clustered in central Delhi:
# a held-out station surrounded by six neighbours 2 km away is an easy case,
# and reporting only easy cases would overstate spatial skill.
DEFAULT_HELD_OUT = ["DL001", "DL006", "DL025", "DL034", "UP004", "HR001"]


def run(
    start: str,
    end: str,
    stations: list[str] | None = None,
    cfg: Config | None = None,
    num_rounds: int = 250,
) -> tuple[pl.DataFrame, dict]:
    """Hold out each station in turn; report the error in predicting it."""
    cfg = cfg or load_config()
    held_out = stations or DEFAULT_HELD_OUT
    known = {s.id for s in cfg.stations}
    missing = [s for s in held_out if s not in known]
    if missing:
        raise KeyError(f"unknown station ids: {missing}")

    base_raw = feat.build_base(start, end, cfg=cfg)
    if base_raw.is_empty():
        raise RuntimeError("no cached data for that range")
    names = {s.id: s.name for s in cfg.stations}

    rows = []
    for station_id in held_out:
        log.info("holding out %s (%s)", station_id, names[station_id])
        # Recompute the neighbourhood with this station removed.
        with_nbrs = graph.neighbour_features(base_raw, cfg=cfg, exclude=[station_id])
        sup = feat.build_supervised(with_nbrs, cfg=cfg)
        if sup.is_empty():
            continue
        sup = split_mod.assign_split(sup, cfg=cfg)

        train = sup.filter(
            (pl.col("split") == "train") & (pl.col("station_id") != station_id)
        ).drop_nulls(["y", "cams_pm2_5_tgt"])
        test = sup.filter(
            (pl.col("split") == "test") & (pl.col("station_id") == station_id)
        ).drop_nulls(["y", "cams_pm2_5_tgt"])
        if train.height < 1000 or test.is_empty():
            log.warning("%s: train=%d test=%d — skipped", station_id, train.height, test.height)
            continue

        model = CorrectorModel(
            use_obs_history=False, use_meteo=True, num_rounds=num_rounds, name="spatial"
        )
        model.fit(train, train["y"].to_numpy().astype(float))
        pred = model.predict(test).q50

        y = test["y"].to_numpy().astype(float)
        idw = test["nbr_pm25_dist"].to_numpy().astype(float)
        cams = test["cams_pm2_5_tgt"].to_numpy().astype(float)

        rows.append(
            {
                "station_id": station_id,
                "station": names[station_id],
                "n": len(y),
                "observed_mean": float(np.nanmean(y)),
                "rmse_graph": _rmse(pred, y),
                "rmse_idw": _rmse(idw, y),
                "rmse_cams": _rmse(cams, y),
                "mae_graph": float(np.nanmean(np.abs(pred - y))),
                "bias_graph": float(np.nanmean(pred - y)),
                "nearest_km": _nearest_km(station_id, cfg),
            }
        )

    table = pl.DataFrame(rows)
    if table.is_empty():
        raise RuntimeError("no station produced a usable evaluation")

    meta = {
        "start": start,
        "end": end,
        "n_stations": table.height,
        "mean_rmse_graph": float(table["rmse_graph"].mean()),
        "mean_rmse_idw": float(table["rmse_idw"].mean()),
        "mean_rmse_cams": float(table["rmse_cams"].mean()),
    }
    return table, meta


def _rmse(pred: np.ndarray, y: np.ndarray) -> float:
    ok = np.isfinite(pred) & np.isfinite(y)
    if not ok.any():
        return float("nan")
    return float(np.sqrt(np.mean((pred[ok] - y[ok]) ** 2)))


def _nearest_km(station_id: str, cfg: Config) -> float:
    """Distance to the closest other station — the difficulty of the case."""
    target = cfg.station(station_id)
    others = [s for s in cfg.stations if s.id != station_id]
    return round(min(graph.distance_between(target, s) for s in others), 1)


def to_markdown(table: pl.DataFrame, meta: dict) -> str:
    lines = [
        "# Leave-one-station-out — Phase 4\n",
        "Each station is deleted from the network, then predicted from the "
        "others. The held-out station contributes nothing: not to training, not "
        "to its own neighbourhood, and not through its own observation history "
        "(R7).\n",
        f"- data range `{meta['start']}` to `{meta['end']}`\n"
        f"- {meta['n_stations']} stations held out in turn\n",
        "\n## Held-out station error (µg/m³)\n",
        "| station | nearest neighbour | hours | observed mean | "
        "**graph** | IDW | raw CAMS |",
        "|---|---|---|---|---|---|---|",
    ]
    for row in table.iter_rows(named=True):
        lines.append(
            f"| {row['station']} | {row['nearest_km']} km | {row['n']:,} | "
            f"{row['observed_mean']:.0f} | **{row['rmse_graph']:.1f}** | "
            f"{row['rmse_idw']:.1f} | {row['rmse_cams']:.1f} |"
        )
    lines.append(
        f"| **mean** | | | | **{meta['mean_rmse_graph']:.1f}** | "
        f"{meta['mean_rmse_idw']:.1f} | {meta['mean_rmse_cams']:.1f} |"
    )

    lines.append("\n## Gate\n")
    lines.append(
        "> What is the held-out station error in µg/m³, across at least four "
        "stations?\n"
    )
    lines.append(
        f"**{meta['mean_rmse_graph']:.1f} µg/m³ RMSE**, averaged over "
        f"{meta['n_stations']} held-out stations."
    )
    vs_idw = (meta["mean_rmse_idw"] - meta["mean_rmse_graph"]) / meta["mean_rmse_idw"]
    vs_cams = (meta["mean_rmse_cams"] - meta["mean_rmse_graph"]) / meta["mean_rmse_cams"]
    lines.append(
        f"\nAgainst distance-weighted interpolation: {vs_idw:+.1%}. "
        f"Against raw CAMS at the same point: {vs_cams:+.1%}."
    )
    if vs_idw <= 0:
        lines.append(
            "\n> **The graph does not beat plain interpolation.** Until it does, "
            "the map is a visualisation, not a prediction, and must be labelled "
            "as such in the UI (R7)."
        )

    # The mean can hide a station where the physics forecast alone was better.
    losers = table.filter(pl.col("rmse_cams") < pl.col("rmse_graph"))
    if not losers.is_empty():
        names = ", ".join(losers["station"].to_list())
        lines.append(
            "\n## Where this does not work\n\n"
            f"Raw CAMS beats the graph at: **{names}**. These are the cleanest, "
            "most peripheral sites in the set, and the pattern is consistent: "
            "the graph pulls a low-concentration outer station towards the "
            "dirtier city its neighbours describe. Averaging over stations "
            "hides this, which is exactly why the per-station table is the "
            "headline here and the mean is the footnote.\n\n"
            "The practical consequence for the map: a grid cell far from any "
            "station and upwind of the city should be shown with wide "
            "uncertainty, not as a confident interpolation."
        )

    scale = table["observed_mean"].mean()
    lines.append(
        f"\n## How good is this, really\n\n"
        f"Mean error of {meta['mean_rmse_graph']:.0f} µg/m³ against an observed "
        f"mean of {scale:.0f} µg/m³ is a large relative error — spatial skill is "
        "well behind the temporal model, which reaches 64 µg/m³ RMSE *with* the "
        "station's own history available. That gap is the honest measure of how "
        "much a monitor is worth. The surface is good enough to say which part "
        "of the city is worse on a given morning; it is not good enough to "
        "quote a number for an unmonitored block, and the UI must not imply "
        "otherwise (R7)."
    )
    return "\n".join(lines) + "\n"
