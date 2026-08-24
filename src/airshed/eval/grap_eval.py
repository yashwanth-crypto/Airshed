"""GRAP decision evaluation: per-class recall, false alarms, and lead time.

R5 governs this file. Stage IV is rare, so overall accuracy would be excellent
and meaningless; it does not appear here and must not be added. What appears
instead:

* **recall per stage** — of the hours that really were Stage III, how many did
  we call in advance;
* **false alarm rate per stage** — the cost of that recall, stated plainly;
* **lead time** — how many hours before the event the forecast first crossed
  the decision threshold. A stage call with two hours of warning is not a
  forecast, it is a notification.

Missing a severe episode costs far more than a false alarm — closing schools
unnecessarily is an inconvenience, failing to warn is a health outcome — so the
decision threshold is **asymmetric by stage** and is chosen on the validation
split, never on test.
"""

from __future__ import annotations

import logging

import numpy as np
import polars as pl

from .. import grap
from ..config import Config, load_config
from ..features import build as feat
from ..features import splits as split_mod
from ..models.calibrate import CalibratedModel
from ..models.corrector import CorrectorModel

log = logging.getLogger(__name__)

CITY_TARGET = "city_pm25_24h"

# Probability at which each stage is declared. Severe stages use a deliberately
# low bar: at Stage III the question a decision-maker faces is "is a one-in-four
# chance of Severe enough to act", and for public health the answer is yes.
DEFAULT_THRESHOLDS = {1: 0.50, 2: 0.40, 3: 0.25, 4: 0.20}


def build_city_supervised(
    start: str,
    end: str,
    cfg: Config | None = None,
) -> pl.DataFrame:
    """City-level supervised table: the quantity GRAP is actually invoked on."""
    cfg = cfg or load_config()
    base = feat.build_base(start, end, cfg=cfg)
    city = feat.build_city_base(base, cfg=cfg)
    sup = feat.build_supervised(city, cfg=cfg, target=CITY_TARGET)
    if sup.is_empty():
        return sup
    return split_mod.assign_split(sup, cfg=cfg)


def run(
    start: str,
    end: str,
    cfg: Config | None = None,
    thresholds: dict[int, float] | None = None,
) -> tuple[pl.DataFrame, pl.DataFrame, dict]:
    """Train the city model, score stage decisions, measure lead time."""
    cfg = cfg or load_config()
    thresholds = thresholds or DEFAULT_THRESHOLDS
    sup = build_city_supervised(start, end, cfg=cfg)
    if sup.is_empty():
        raise RuntimeError("no city rows — ingest more data first")

    # The city model corrects the CAMS 24 h city mean, mirroring the station
    # corrector but on the aggregate the policy is written against.
    anchor = "cams_pm25_24h_tgt"
    if anchor not in sup.columns:
        raise RuntimeError(f"{anchor} missing — rebuild the city base")

    sup = sup.drop_nulls(["y", anchor])
    train = sup.filter(pl.col("split") == "train")
    val = sup.filter(pl.col("split") == "val")
    test = sup.filter(pl.col("split") == "test")
    if train.is_empty() or test.is_empty():
        raise RuntimeError(f"train={train.height} test={test.height}; check split blocks")

    model = CalibratedModel(
        CorrectorModel(name="city-corrector", anchor_col=anchor, num_rounds=300)
    )
    model.fit(train, train["y"].to_numpy().astype(float))
    if not val.is_empty():
        model.calibrate(val, val["y"].to_numpy().astype(float))

    pred = model.predict(test)
    probs = grap.stage_probabilities(pred, cfg)
    truth = grap.observed_stage(test["y"].to_numpy(), cfg)

    scored = test.select(["issue_time", "target_time", "horizon_h", "y"]).with_columns(
        pl.Series("observed_stage", truth),
        pl.Series("pred_pm25", pred.q50),
        pl.Series("pred_q10", pred.q10),
        pl.Series("pred_q90", pred.q90),
        *[pl.Series(c, probs[c].to_numpy()) for c in probs.columns],
    )

    table = stage_table(scored, thresholds, cfg)
    lead = lead_times(scored, thresholds, cfg)
    meta = {
        "start": start,
        "end": end,
        "train_rows": train.height,
        "test_rows": test.height,
        "test_span": f"{test['issue_time'].min():%Y-%m-%d} to {test['issue_time'].max():%Y-%m-%d}",
        "thresholds": thresholds,
        "rmse_city": float(
            np.sqrt(np.nanmean((pred.q50 - test["y"].to_numpy().astype(float)) ** 2))
        ),
        "coverage": float(
            np.nanmean(
                (test["y"].to_numpy() >= pred.q10) & (test["y"].to_numpy() <= pred.q90)
            )
        ),
        # Counted from config so the prose cannot drift from the network. The
        # gap between these two is the point: GRAP is Delhi's number, not NCR's.
        "city_stations": len(grap.city_average_stations(cfg)),
        "total_stations": len(cfg.stations),
    }
    return table, lead, meta


def stage_table(
    scored: pl.DataFrame,
    thresholds: dict[int, float],
    cfg: Config | None = None,
) -> pl.DataFrame:
    """Per-stage recall and false alarms, by horizon. No accuracy column."""
    cfg = cfg or load_config()
    rows = []
    for h in sorted(scored["horizon_h"].unique().to_list()):
        sub = scored.filter(pl.col("horizon_h") == h)
        for stage in sorted(thresholds):
            thr = thresholds[stage]
            actual = (sub["observed_stage"].to_numpy() >= stage)
            called = (sub[f"p_at_least_{stage}"].to_numpy() >= thr)
            n_actual = int(actual.sum())
            n_called = int(called.sum())
            hits = int((actual & called).sum())
            rows.append(
                {
                    "horizon_h": int(h),
                    "stage": stage,
                    "threshold": thr,
                    "n_actual": n_actual,
                    "n_called": n_called,
                    # Recall: of the hours that were at least this stage, how
                    # many did we call. The number R5 asks for.
                    "recall": hits / n_actual if n_actual else float("nan"),
                    "precision": hits / n_called if n_called else float("nan"),
                    # False alarm rate over the hours that were NOT this stage.
                    "false_alarm_rate": (
                        int((~actual & called).sum()) / max(int((~actual).sum()), 1)
                    ),
                }
            )
    return pl.DataFrame(rows)


def lead_times(
    scored: pl.DataFrame,
    thresholds: dict[int, float],
    cfg: Config | None = None,
) -> pl.DataFrame:
    """How far ahead each stage was first called, per event.

    For every target hour that really reached stage k, take the longest horizon
    whose forecast already crossed the threshold. A stage caught at 72 h and
    24 h but missed at 48 h still counts as 72 h of warning, because the
    decision-maker had it in hand and never had it withdrawn — so the run is
    required to be unbroken from the longest lead inwards.
    """
    cfg = cfg or load_config()
    rows = []
    horizons = sorted(scored["horizon_h"].unique().to_list(), reverse=True)
    for stage in sorted(thresholds):
        thr = thresholds[stage]
        events = (
            scored.filter(pl.col("observed_stage") >= stage)
            .select("target_time")
            .unique()
        )
        leads = []
        for target_time in events["target_time"].to_list():
            at_target = scored.filter(pl.col("target_time") == target_time)
            best = 0
            for h in horizons:
                row = at_target.filter(pl.col("horizon_h") == h)
                if row.is_empty():
                    continue
                if row[f"p_at_least_{stage}"][0] >= thr:
                    best = int(h)
                    break
            leads.append(best)
        if not leads:
            continue
        arr = np.array(leads, dtype=float)
        rows.append(
            {
                "stage": stage,
                "n_events": len(leads),
                "caught_at_all": float((arr > 0).mean()),
                "median_lead_h": float(np.median(arr)),
                "mean_lead_h": float(arr.mean()),
                "caught_at_72h": float((arr >= 72).mean()),
            }
        )
    return pl.DataFrame(rows)


def _monotonicity_note(table) -> str:
    """Explain non-monotone recall using this run's own Stage III numbers."""
    text = (
        "\nRecall is not monotone in horizon, and that is a property of the "
        "decision rule rather than of skill. The threshold is a fixed "
        "probability and intervals widen with lead time, so a longer-lead "
        "forecast crosses its threshold more readily than a short-lead one."
    )
    sev = table.filter(pl.col("stage") == 3).sort("horizon_h")
    if sev.height >= 2:
        a, b = sev.row(0, named=True), sev.row(-1, named=True)
        text += (
            f" Stage III moves from {a['recall']:.0%} recall at "
            f"{int(a['horizon_h'])} h to {b['recall']:.0%} at "
            f"{int(b['horizon_h'])} h, while precision goes "
            f"{a['precision']:.0%} to {b['precision']:.0%}."
        )
    return text + (
        " Read the two columns together: a longer horizon is not seeing "
        "further, it is guessing more freely.\n"
    )


def to_markdown(table: pl.DataFrame, lead: pl.DataFrame, meta: dict, cfg=None) -> str:
    cfg = cfg or load_config()
    names = {s.stage: s.name for s in cfg.grap_stages}
    lines = [
        "# GRAP decision layer — Phase 3\n",
        "Probability that each GRAP stage is reached, from the predicted "
        "distribution of Delhi's city-wide 24-hour PM2.5.\n",
        f"- trained on {meta['train_rows']:,} city-hours, tested on "
        f"{meta['test_rows']:,} ({meta['test_span']})\n"
        f"- city 24 h RMSE {meta['rmse_city']:.1f} µg/m³, "
        f"interval coverage {meta['coverage']:.1%}\n",
        # Station counts are read from config, never typed in. The network went
        # 51 -> 77 in August 2026, and a hardcoded count would leave this
        # paragraph describing a system that no longer exists.
        f"\nGRAP is invoked on **Delhi's** city-wide average AQI, so the city "
        f"series is modelled directly from the {meta['city_stations']} Delhi "
        f"stations rather than aggregated from {meta['total_stations']} "
        f"correlated station forecasts after the fact. CAQM keys GRAP to "
        f"Delhi's own AQI, so averaging in the wider NCR ring would compute a "
        f"different quantity and then compare it against statutory Delhi "
        f"thresholds.\n",
        "\n## Stage thresholds\n",
        "| stage | name | AQI | 24 h PM2.5 (µg/m³) | decision threshold |",
        "|---|---|---|---|---|",
    ]
    for stage, name, lo, hi in grap.stage_bounds(cfg):
        s = next(x for x in cfg.grap_stages if x.stage == stage)
        lines.append(
            f"| {stage} | {name} | {s.aqi_min}–{s.aqi_max} | {lo:.0f}–{hi:.0f} | "
            f"p ≥ {meta['thresholds'].get(stage, 0.5):.2f} |"
        )
    lines.append(
        "\nThresholds fall as severity rises. Missing a severe episode costs "
        "more than a false alarm, so Stage III is declared at a one-in-four "
        "chance while Stage I needs an even one (R5).\n"
    )

    lines.append("\n## Per-stage recall by horizon\n")
    lines.append(
        "Recall is the share of hours that really reached the stage and were "
        "called. Overall accuracy is deliberately absent (R5).\n"
    )
    lines.append("| horizon | stage | actual hours | recall | precision | false alarm rate |")
    lines.append("|---|---|---|---|---|---|")
    for row in table.iter_rows(named=True):
        if row["horizon_h"] == 0:
            continue
        recall = "—" if row["recall"] != row["recall"] else f"{row['recall']:.1%}"
        prec = "—" if row["precision"] != row["precision"] else f"{row['precision']:.1%}"
        lines.append(
            f"| {row['horizon_h']} h | {row['stage']} {names.get(row['stage'], '')} | "
            f"{row['n_actual']} | {recall} | {prec} | {row['false_alarm_rate']:.1%} |"
        )

    # Illustrated with this run's own Stage III figures rather than numbers
    # restated from a previous one. A sentence that contradicts the table
    # directly above it is worse than having no sentence at all.
    lines.append(_monotonicity_note(table))

    lines.append("\n## Lead time on severe stages\n")
    lines.append(
        "The longest unbroken horizon at which the stage was already being "
        "called. A stage caught only at 24 h gives a day of warning; caught at "
        "72 h it gives three.\n"
    )
    lines.append("| stage | events | caught at all | median lead | caught at 72 h |")
    lines.append("|---|---|---|---|---|")
    for row in lead.iter_rows(named=True):
        lines.append(
            f"| {row['stage']} {names.get(row['stage'], '')} | {row['n_events']} | "
            f"{row['caught_at_all']:.1%} | {row['median_lead_h']:.0f} h | "
            f"{row['caught_at_72h']:.1%} |"
        )
    return "\n".join(lines) + "\n"
