"""Does the reported 72 h skill survive being given a real 72 h forecast?

Every number in `ablation.md` is built from `meteo_archive`, which returns the
*best available* forecast for each past hour — in practice a short-lead one.
So the column called "the 72 h meteorology" was, in training, a forecast only a
few hours old. The model was graded on an input it will never be given in
production, and the fingerprint of that is visible in the ablation table itself:
`full` scores 62.6 / 62.9 / 63.8 across 24 / 48 / 72 h, essentially flat, and
`raw-cams` actually *improves* with lead. No forecast improves with lead.

This module rebuilds the same rows with the meteorology that was genuinely
available `horizon_h` hours earlier and re-runs the comparison. The gap between
the two is the optimism, measured rather than argued.

**What this does not fix.** Only 13 of 20 meteorological variables have a
Previous Runs form. Boundary-layer height — the single most important one — has
none, nor do visibility or any pressure-level variable, so `inversion`,
`lapse_*` and `ventilation_index` stay short-lead. CAMS PM2.5 itself has no
archived-forecast endpoint at all. This measurement is therefore an *upper
bound on remaining honesty*, not a clean bill: it removes one source of
optimism and quantifies it, and leaves the others in place and named.
"""

from __future__ import annotations

import datetime as dt
import logging
from pathlib import Path

import numpy as np
import polars as pl

from ..config import Config, load_config
from ..models.base import Model
from ..models.baselines import PersistenceModel, RawCAMSModel
from ..models.corrector import CorrectorModel
from ..store import read_range
from . import metrics
from .ablation import load_supervised

log = logging.getLogger(__name__)

RESULTS = Path("docs/results/leadmatch.md")


def models() -> list[Model]:
    """Persistence and raw CAMS are unaffected by the swap and anchor the table.

    Persistence uses no meteorology at all, and raw CAMS uses no meteorology
    either, so their rows must come out identical in both columns. If they do
    not, the two frames are not row-for-row comparable and the whole comparison
    is void — which is exactly why they are here.
    """
    return [
        PersistenceModel(),
        RawCAMSModel(),
        CorrectorModel(
            use_obs_history=True, use_meteo=True, name="full",
            drop_prefixes=("upwind_",),
        ),
    ]


def _fit_score(sup: pl.DataFrame, evaluate_on: str) -> tuple[pl.DataFrame, dict]:
    train = sup.filter(pl.col("split") == "train")
    held = sup.filter(pl.col("split") == evaluate_on)
    if train.is_empty() or held.is_empty():
        raise RuntimeError(
            f"train has {train.height} rows and {evaluate_on} has {held.height}"
        )

    needed = ["y", "cams_pm2_5_tgt", "obs_lag_1h", "obs_lag_24h"]
    train = train.drop_nulls([c for c in needed if c in train.columns])
    held = held.drop_nulls([c for c in needed if c in held.columns])

    y_train = train["y"].to_numpy().astype(float)
    rows = []
    base_pred = None
    for model in models():
        model.fit(train, y_train)
        pred = model.predict(held)
        if base_pred is None:
            base_pred = pred
        table = metrics.horizon_table(held, pred, baseline=base_pred)
        rows.append(table.with_columns(pl.lit(model.name).alias("model")))
    return pl.concat(rows, how="vertical_relaxed"), {
        "train_rows": train.height,
        "test_rows": held.height,
    }


def input_degradation(start: str, end: str, cfg: Config | None = None) -> pl.DataFrame:
    """How far the real forecast is from the short-lead one, per lead day.

    This is the mechanism, measured directly on the inputs and independent of
    any model: if these numbers were near zero there would be nothing for the
    swap to change, and the size of the model effect should be read against
    them.
    """
    cfg = cfg or load_config()
    archive = read_range("meteo_archive", start, end)
    lead = read_range("meteo_leadmatched", start, end)
    if archive.is_empty() or lead.is_empty():
        return pl.DataFrame()

    variables = [
        v for v in cfg.source("meteo")["lead_matched_hourly"]
        if v in archive.columns and v in lead.columns
    ]
    arc = archive.select(["station_id", "time"] + variables).unique(
        subset=["station_id", "time"], keep="last"
    )
    joined = lead.select(["station_id", "time", "lead_day"] + variables).join(
        arc, on=["station_id", "time"], how="inner", suffix="_arc"
    )
    if joined.is_empty():
        return pl.DataFrame()

    rows = []
    for lead_day, part in joined.group_by("lead_day", maintain_order=True):
        row = {"lead_day": int(lead_day[0]), "n": part.height}
        for v in variables:
            d = (part[v] - part[f"{v}_arc"]).drop_nulls().to_numpy()
            row[v] = float(np.sqrt(np.mean(d**2))) if d.size else float("nan")
        rows.append(row)
    return pl.DataFrame(rows).sort("lead_day")


def run(
    start: str,
    end: str,
    cfg: Config | None = None,
    evaluate_on: str = "test",
) -> tuple[pl.DataFrame, dict]:
    """Score the same models twice: short-lead meteorology, then real lead."""
    cfg = cfg or load_config()

    sup_arc = load_supervised(start, end, cfg=cfg, lead_matched=False)
    sup_lm = load_supervised(start, end, cfg=cfg, lead_matched=True)
    if sup_arc.is_empty() or sup_lm.is_empty():
        raise RuntimeError("no supervised rows — ingest more data first")

    # Restrict both frames to the rows the swap actually reached. A row whose
    # lead-matched meteorology is missing would otherwise sit in one column as
    # a corrected row and in the other as an uncorrected one, and the
    # difference between the columns would stop meaning what it says.
    matched = sup_lm.filter(pl.col("met_lead_matched"))
    keys = ["station_id", "issue_time", "horizon_h"]
    sup_arc = sup_arc.join(matched.select(keys), on=keys, how="semi")
    coverage = matched.height / sup_lm.height if sup_lm.height else 0.0
    log.info(
        "lead-matched rows %d/%d (%.1f%%)", matched.height, sup_lm.height, 100 * coverage
    )

    arc_table, arc_meta = _fit_score(sup_arc, evaluate_on)
    lm_table, lm_meta = _fit_score(matched, evaluate_on)

    table = arc_table.with_columns(pl.lit("archive (short-lead)").alias("meteorology")).vstack(
        lm_table.with_columns(pl.lit("lead-matched").alias("meteorology"))
    )

    meta = {
        "generated": dt.datetime.now(dt.timezone.utc).strftime("%Y-%m-%d %H:%M UTC"),
        "start": start,
        "end": end,
        "row_coverage": coverage,
        "evaluate_on": evaluate_on,
        "horizons": cfg.horizons,
        "unavailable": list(cfg.source("meteo").get("lead_matched_unavailable", [])),
        "replaced": list(cfg.source("meteo").get("lead_matched_hourly", [])),
        **arc_meta,
    }
    meta["degradation"] = input_degradation(start, end, cfg=cfg)
    return table, meta


# ---------------------------------------------------------------------------
# reporting
# ---------------------------------------------------------------------------
def _cell(table: pl.DataFrame, model: str, meteorology: str, horizon: int) -> float | None:
    hit = table.filter(
        (pl.col("model") == model)
        & (pl.col("meteorology") == meteorology)
        & (pl.col("horizon_h") == horizon)
    )
    return None if hit.is_empty() else float(hit["rmse"][0])


def to_markdown(table: pl.DataFrame, meta: dict) -> str:
    horizons = list(meta["horizons"]) + [0]
    names = table["model"].unique(maintain_order=True).to_list()

    def head(label: str) -> list[str]:
        cols = " | ".join(f"{h} h" if h else "overall" for h in horizons)
        return [f"| {label} | {cols} |", "|---" * (len(horizons) + 1) + "|"]

    out: list[str] = [
        "# Lead-matched meteorology — is the 72 h number a 72 h number?",
        "",
        f"Generated {meta['generated']}. Regenerate with `airshed leadmatch`.",
        "",
        "`meteo_archive` returns the best available forecast for each past hour, "
        "which is a short-lead one. Training on that and serving a genuine 72 h "
        "forecast is the distribution mismatch R1 exists to prevent, one level "
        "down: not reanalysis-vs-forecast, but short-lead-vs-long-lead. This "
        "table rebuilds the identical rows with the forecast that was really "
        "available `horizon_h` hours earlier, from the Open-Meteo Previous Runs "
        "API, and re-scores.",
        "",
        f"- data range `{meta['start']}` to `{meta['end']}`, evaluated on the "
        f"**{meta['evaluate_on']}** split",
        f"- {meta['train_rows']:,} training rows, {meta['test_rows']:,} evaluation rows",
        f"- lead-matched meteorology reached {meta['row_coverage']:.1%} of rows; "
        "the rest are excluded so both columns describe the same rows",
        "",
        "Lead day `N` means the value came from the run initialised `N` days "
        "before the valid day, so the true lead is `24N + hour_of_day`. The "
        "mapping is never optimistic: a 72 h horizon is scored against a "
        "forecast at least 72 hours old, sometimes 95.",
        "",
        "## RMSE by horizon (µg/m³, lower is better)",
        "",
    ]
    out += head("model")
    for name in names:
        for met in ("archive (short-lead)", "lead-matched"):
            cells = []
            for h in horizons:
                v = _cell(table, name, met, h)
                cells.append("—" if v is None else f"{v:.1f}")
            out.append(f"| {name} — {met} | " + " | ".join(cells) + " |")
    out += ["", _degradation_section(meta), "", _verdict(table, meta), ""]
    return "\n".join(out)


def _degradation_section(meta: dict) -> str:
    deg = meta.get("degradation")
    if deg is None or deg.is_empty():
        return ""
    show = [c for c in ("temperature_2m", "wind_speed_10m", "wind_direction_10m",
                        "relative_humidity_2m", "precipitation") if c in deg.columns]
    lines = [
        "## The inputs themselves, before any model",
        "",
        "RMSE between the short-lead archive value and the forecast at real lead, "
        "for the same station and hour. This is forecast error growth, measured "
        "directly and independent of any model — the size of any effect below "
        "should be read against it.",
        "",
        "| lead day | true lead | rows | " + " | ".join(show) + " |",
        "|---" * (len(show) + 3) + "|",
    ]
    for row in deg.iter_rows(named=True):
        n = row["lead_day"]
        vals = " | ".join(f"{row[c]:.2f}" for c in show)
        lines.append(f"| {n} | {24*n}–{24*n+23} h | {row['n']:,} | {vals} |")
    return "\n".join(lines)


def _verdict(table: pl.DataFrame, meta: dict) -> str:
    lines = ["## Verdict", ""]

    # The two baselines use no meteorology, so any movement in their rows means
    # the frames are not comparable and every other number here is void.
    for name in ("persistence", "raw-cams"):
        a = _cell(table, name, "archive (short-lead)", 0)
        b = _cell(table, name, "lead-matched", 0)
        if a is None or b is None:
            continue
        if abs(a - b) > 0.05:
            lines += [
                f"> **Invalid comparison.** `{name}` uses no meteorology, so its "
                f"RMSE must be identical in both columns. It moved {a:.2f} -> "
                f"{b:.2f}. The two frames are not row-for-row comparable and "
                "nothing in this table can be read until that is fixed.",
                "",
            ]
            return "\n".join(lines)
    lines += [
        "Persistence and raw CAMS use no meteorology and are unchanged between "
        "the two columns, which confirms the frames are row-for-row comparable.",
        "",
    ]

    rows = []
    for h in list(meta["horizons"]) + [0]:
        a = _cell(table, "full", "archive (short-lead)", h)
        b = _cell(table, "full", "lead-matched", h)
        if a is None or b is None:
            continue
        rows.append((h, a, b, 100.0 * (b - a) / a))

    lines += [
        "| horizon | short-lead met | real-lead met | optimism |",
        "|---|---|---|---|",
    ]
    for h, a, b, pct in rows:
        label = "overall" if h == 0 else f"{h} h"
        lines.append(f"| {label} | {a:.1f} | {b:.1f} | {pct:+.1f}% |")
    lines.append("")

    long_h = [r for r in rows if r[0] == max(meta["horizons"])]
    if long_h:
        _, a, b, pct = long_h[0]
        if pct < 1.0:
            lines += [
                f"**The 72 h number survives.** Giving the model a genuine 72 h "
                f"forecast moves its error by {pct:+.1f}%, which means the "
                "reported skill was not resting on short-lead meteorology.",
            ]
        else:
            lines += [
                f"**The 72 h number was optimistic by {pct:+.1f}%** on this split. "
                "That much of the reported skill came from meteorology fresher "
                "than anything production will ever see.",
            ]
        # This project has been wrong before by reading an effect of about a
        # percent off one split — `rolling.md` records coupling being called a
        # clear negative and then overturned. The same caution has to apply to a
        # result that flatters nobody, or the standard is being applied
        # selectively.
        lines += [
            "",
            "**One split does not settle an effect this size.** It is the same "
            "magnitude as the fires and upwind effects this project declines to "
            "claim from a single split, so it gets the same treatment: see the "
            "`lead-matched meteorology` row in `rolling.md`, where the cost is "
            "measured across five folds. The direction is consistent; the "
            "magnitude is not pinned.",
        ]
    lines += [
        "",
        "### What is still short-lead",
        "",
        "These variables have no Previous Runs form and could not be swapped, so "
        "they keep their short-lead values here: "
        + ", ".join(f"`{v}`" for v in meta["unavailable"])
        + ". Boundary-layer height is among them, and it is the most important "
        "variable in the set — the derived `inversion`, `lapse_*` and "
        "`ventilation_index` features inherit the problem. CAMS PM2.5 has no "
        "archived-forecast endpoint at all.",
        "",
        "So this table removes one source of optimism and measures it; it does "
        "not remove them all. The remaining gap closes only forward, as "
        "`meteo_runs` and `cams_runs` accumulate real archived runs — which is "
        "why every day the daily archive job fails to run is a day that cannot "
        "be recovered.",
    ]
    return "\n".join(lines)


def write(table: pl.DataFrame, meta: dict, path: Path | None = None) -> Path:
    out = path or RESULTS
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(to_markdown(table, meta), encoding="utf-8")
    return out
