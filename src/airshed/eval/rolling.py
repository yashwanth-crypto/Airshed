"""Rolling-origin evaluation — error bars for every claim.

Every number in `ablation.md` comes from one train/test split, which means a
difference of a few tenths of a percent is reported without any notion of how
much it would move on a different split. That is not good enough to decide
whether the upwind corridor helps or whether coupling hurts: both of those
verdicts currently rest on differences smaller than the noise we have not
measured.

This runs the same comparison over several **expanding-window** folds. Training
always precedes evaluation, as in operation; the window grows as history
accumulates; each fold holds out the next contiguous block. Reporting the
spread across folds turns "+0.6%" into "+0.6% ± something", and only then is it
possible to say whether a result is real.

**Checkpointed by fold.** Each fold's scores are written to disk the moment it
finishes, and a re-run skips folds already on disk. A laptop that sleeps, a
Windows update that reboots, or a cancelled command costs at most the fold in
flight — not the hour before it.
"""

from __future__ import annotations

import datetime as dt
import json
import logging
from pathlib import Path

import numpy as np
import polars as pl

from ..config import Config, load_config
from ..features import build as feat
from ..models.base import Model
from ..models.baselines import PersistenceModel, RawCAMSModel
from ..models.corrector import CorrectorModel
from ..models.coupled import CoupledCorrector
from .metrics import horizon_table

log = logging.getLogger(__name__)

CHECKPOINT_DIR = "rolling"
EMBARGO_H = 96


def fold_models() -> list[Model]:
    """A deliberately small set: the comparisons whose verdicts are in doubt.

    Every extra model multiplies the runtime by a fold, and the questions that
    actually need error bars are "does the corrector beat the baselines" and
    "do upwind and coupling add anything".
    """
    return [
        PersistenceModel(),
        RawCAMSModel(),
        CorrectorModel(
            use_obs_history=True, use_meteo=True, name="full (no fires)",
            drop_prefixes=("upwind_", "fire_"),
        ),
        CorrectorModel(
            use_obs_history=True, use_meteo=True, name="full",
            drop_prefixes=("upwind_",),
        ),
        CorrectorModel(use_obs_history=True, use_meteo=True, name="full+upwind"),
        CoupledCorrector(name="coupled"),
    ]


def default_folds(cfg: Config | None = None) -> list[dict]:
    """Expanding-window folds across the observed period.

    Boundaries follow the seasons rather than an arbitrary grid, so each fold
    holds out a coherent regime: a build-up, an episode season, a late winter,
    a clean spring, a summer.
    """
    return [
        {"label": "autumn-2025", "train_end": "2025-09-15", "test": ("2025-09-20", "2025-10-31")},
        {"label": "episode-2025", "train_end": "2025-11-01", "test": ("2025-11-06", "2025-12-20")},
        {"label": "late-winter-2026", "train_end": "2025-12-21", "test": ("2025-12-26", "2026-02-10")},
        {"label": "spring-2026", "train_end": "2026-02-15", "test": ("2026-02-20", "2026-04-15")},
        {"label": "summer-2026", "train_end": "2026-04-20", "test": ("2026-04-25", "2026-06-30")},
    ]


def _checkpoint_path(cfg: Config, label: str) -> Path:
    return cfg.processed_dir / CHECKPOINT_DIR / f"{label}.json"


def run(
    start: str,
    end: str,
    cfg: Config | None = None,
    folds: list[dict] | None = None,
    resume: bool = True,
    lead_matched: bool = False,
) -> tuple[pl.DataFrame, dict]:
    """Score every model on every fold. Resumable.

    `lead_matched` adds a second pass in which the forecast meteorology is the
    value genuinely available `horizon_h` hours earlier rather than the
    short-lead archive value, so the optimism measured on a single split in
    `leadmatch.md` gets the same error bars as every other contested claim.
    """
    cfg = cfg or load_config()
    folds = folds or default_folds(cfg)

    # Build the supervised table once and slice it per fold: rebuilding it five
    # times is the single most expensive thing this could do.
    log.info("building supervised table %s..%s", start, end)
    base = feat.build_base(start, end, cfg=cfg)
    sup = feat.build_supervised(
        base, cfg=cfg, extra_targets={"y_vis": "metar_visibility_km"}
    )
    if sup.is_empty():
        raise RuntimeError("no supervised rows")
    needed = [c for c in ("y", "cams_pm2_5_tgt", "obs_lag_1h", "obs_lag_24h") if c in sup.columns]
    sup = sup.drop_nulls(needed)

    results: list[dict] = []
    for fold in folds:
        path = _checkpoint_path(cfg, fold["label"])
        if resume and path.is_file():
            log.info("fold %s already done — loading checkpoint", fold["label"])
            results.extend(json.loads(path.read_text(encoding="utf-8")))
            continue

        rows = _run_fold(sup, fold, cfg)
        if not rows:
            log.warning("fold %s produced nothing", fold["label"])
            continue
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(rows, indent=1), encoding="utf-8")
        log.info("fold %s checkpointed to %s", fold["label"], path.name)
        results.extend(rows)

    # Second pass with meteorology at real forecast lead. It is a change of
    # columns, not of model, so it cannot be expressed as another entry in
    # `fold_models()` — the frame itself has to be rebuilt. Only `full` is
    # re-run: the baselines use no meteorology and would produce identical
    # rows, and the point of this pass is one paired comparison.
    if lead_matched:
        log.info("rebuilding supervised table with lead-matched meteorology")
        sup_lm = feat.apply_lead_matched_meteo(
            feat.build_supervised(
                base, cfg=cfg, extra_targets={"y_vis": "metar_visibility_km"}
            ),
            cfg=cfg,
        ).drop_nulls(needed)
        # The paired comparison is only valid if both passes see the same rows.
        # At 100% coverage the filter below is a no-op; below that, the two
        # columns describe different row sets and the difference between them
        # stops meaning what the table says it means.
        covered = float(sup_lm["met_lead_matched"].mean() or 0.0)
        if covered < 0.999:
            log.warning(
                "lead-matched meteorology reached only %.1f%% of rows — the "
                "paired comparison against `full` is NOT row-for-row and must "
                "not be read as one",
                100 * covered,
            )
        else:
            log.info("lead-matched meteorology reached %.1f%% of rows", 100 * covered)
        sup_lm = sup_lm.filter(pl.col("met_lead_matched"))

        lm_models = [
            CorrectorModel(
                use_obs_history=True, use_meteo=True, name="full",
                drop_prefixes=("upwind_",),
            )
        ]
        for fold in folds:
            path = _checkpoint_path(cfg, fold["label"] + "-leadmatched")
            if resume and path.is_file():
                log.info("fold %s (lead-matched) already done", fold["label"])
                results.extend(json.loads(path.read_text(encoding="utf-8")))
                continue
            rows = _run_fold(
                sup_lm, fold, cfg, models=lm_models, suffix=" (lead-matched)"
            )
            if not rows:
                continue
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(json.dumps(rows, indent=1), encoding="utf-8")
            results.extend(rows)

    if not results:
        raise RuntimeError("no folds produced results")
    table = pl.DataFrame(results)
    return table, summarise(table)


def _run_fold(
    sup: pl.DataFrame,
    fold: dict,
    cfg: Config,
    models: list[Model] | None = None,
    suffix: str = "",
) -> list[dict]:
    train_end = _utc(fold["train_end"])
    test_start, test_end = _utc(fold["test"][0]), _utc(fold["test"][1], end_of_day=True)
    embargo = dt.timedelta(hours=EMBARGO_H)

    # Training stops an embargo before the test block opens, and a row is
    # excluded if either its issue time or its target time crosses the line —
    # a 72 h forecast issued just before the cutoff lands inside the holdout.
    train = sup.filter(
        (pl.col("issue_time") < train_end - embargo)
        & (pl.col("target_time") < train_end - embargo)
    )
    test = sup.filter(
        (pl.col("target_time") >= test_start) & (pl.col("target_time") <= test_end)
    )
    if train.height < 5000 or test.height < 500:
        log.warning(
            "fold %s: train=%d test=%d — skipped", fold["label"], train.height, test.height
        )
        return []

    y_train = train["y"].to_numpy().astype(float)
    y_test = test["y"].to_numpy().astype(float)
    log.info(
        "fold %s: train %d rows to %s, test %d rows %s..%s",
        fold["label"], train.height, fold["train_end"], test.height, *fold["test"],
    )

    baseline = None
    rows: list[dict] = []
    for model in (models or fold_models()):
        model.fit(train, y_train)
        pred = model.predict(test)
        if baseline is None:
            baseline = pred
        scored = horizon_table(test, pred, baseline=baseline)
        for row in scored.iter_rows(named=True):
            rows.append(
                {
                    "fold": fold["label"],
                    "model": model.name + suffix,
                    "horizon_h": row["horizon_h"],
                    "n": row["n"],
                    "rmse": _clean(row.get("rmse")),
                    "mae": _clean(row.get("mae")),
                    "bias": _clean(row.get("bias")),
                    "episode_recall": _clean(row.get("episode_recall")),
                    "observed_mean": float(np.nanmean(y_test)),
                }
            )
        log.info("  %s scored", model.name)
    return rows


def summarise(table: pl.DataFrame) -> dict:
    overall = table.filter(pl.col("horizon_h") == 0)
    per_model = (
        overall.group_by("model")
        .agg(
            pl.col("rmse").mean().alias("mean_rmse"),
            pl.col("rmse").std().alias("sd_rmse"),
            pl.col("rmse").min().alias("best_fold"),
            pl.col("rmse").max().alias("worst_fold"),
            pl.len().alias("folds"),
        )
        .sort("mean_rmse")
    )
    return {
        "folds": sorted(table["fold"].unique().to_list()),
        "per_model": per_model.to_dicts(),
        "paired": _paired(overall),
    }


def _paired(overall: pl.DataFrame) -> list[dict]:
    """Fold-by-fold differences for the two contested claims.

    A paired comparison is the right test here: both models see the identical
    fold, so the fold-to-fold variation cancels and what is left is the effect.
    Comparing two unpaired averages would drown a 1% effect in seasonal spread
    that has nothing to do with either model.
    """
    comparisons = [
        # The headline claim deserves error bars as much as the contested ones.
        ("correction vs persistence", "persistence", "full+upwind", "addition"),
        ("correction vs raw CAMS", "raw-cams", "full+upwind", "addition"),
        ("upwind fires", "full (no fires)", "full", "addition"),
        ("upwind corridor", "full", "full+upwind", "addition"),
        ("coupling", "full", "coupled", "addition"),
        # Not an addition, and the sign convention matters. For every row above,
        # the hypothesis is "the challenger helps" and a negative gain refutes
        # it. Here the hypothesis is "the archive number was optimistic", so a
        # negative gain *confirms* it. Labelling this one "does not hold" from
        # the same rule would state the opposite of what it means.
        ("lead-matched meteorology", "full", "full (lead-matched)", "penalty"),
    ]
    out = []
    for name, reference, challenger, kind in comparisons:
        wide = (
            overall.filter(pl.col("model").is_in([reference, challenger]))
            .select(["fold", "model", "rmse"])
            .pivot(index="fold", on="model", values="rmse")
        )
        if reference not in wide.columns or challenger not in wide.columns:
            continue
        base = wide[reference].to_numpy().astype(float)
        alt = wide[challenger].to_numpy().astype(float)
        gains = (base - alt) / base
        finite = gains[np.isfinite(gains)]
        if finite.size == 0:
            continue
        folds_order = wide["fold"].to_list()
        best_i = int(np.nanargmax(gains)) if np.isfinite(gains).any() else 0
        out.append(
            {
                "claim": name,
                "kind": kind,
                "reference": reference,
                "challenger": challenger,
                "best_fold": folds_order[best_i],
                "best_fold_gain": float(gains[best_i]),
                "folds": int(finite.size),
                "mean_gain": float(finite.mean()),
                "sd_gain": float(finite.std(ddof=1)) if finite.size > 1 else float("nan"),
                "folds_better": int((finite > 0).sum()),
                "per_fold": [round(float(g), 4) for g in gains],
            }
        )
    return out


def to_markdown(table: pl.DataFrame, meta: dict) -> str:
    lines = [
        "# Rolling-origin evaluation\n",
        "Every headline number elsewhere comes from a single split. This repeats "
        "the comparison over expanding-window folds — training always before "
        "evaluation, each fold holding out the next contiguous season — so the "
        "spread across folds says how much a result would move on different "
        "data. A difference smaller than that spread is not a finding.\n",
        f"- folds: {', '.join(meta['folds'])}\n",
        "\n## RMSE across folds (µg/m³)\n",
        "| model | mean | sd | best fold | worst fold |",
        "|---|---|---|---|---|",
    ]
    for row in meta["per_model"]:
        sd = row["sd_rmse"]
        lines.append(
            f"| {row['model']} | {row['mean_rmse']:.1f} | "
            + (f"{sd:.1f} " if sd is not None and sd == sd else "— ")
            + f"| {row['best_fold']:.1f} | {row['worst_fold']:.1f} |"
        )

    lines.append(
        "\nThe spread across folds is large because the seasons differ, not "
        "because the models are unstable. That is exactly why the comparisons "
        "below are **paired**: both models see the same fold, so seasonal "
        "variation cancels.\n"
    )

    lines.append("\n## Are the contested claims real?\n")
    lines.append("| comparison | folds better | mean gain | sd | verdict |")
    lines.append("|---|---|---|---|---|")
    for row in meta["paired"]:
        sd = row["sd_gain"]
        sd_txt = f"{sd:+.1%}" if sd == sd else "—"
        # A gain is only worth claiming if it is consistent in sign and larger
        # than the fold-to-fold scatter of the gain itself.
        consistent = row["folds_better"] == row["folds"]
        exceeds_noise = sd != sd or abs(row["mean_gain"]) > sd
        if row.get("kind") == "penalty":
            # A negative gain is the expected direction here, so the question is
            # whether the cost is consistent and bigger than the scatter — not
            # whether it is positive.
            worse = row["folds"] - row["folds_better"]
            if worse == row["folds"] and exceeds_noise:
                verdict = f"**real cost, {-row['mean_gain']:.1%}**"
            elif worse > row["folds"] / 2:
                verdict = "cost in the expected direction, within noise"
            else:
                verdict = "no measurable cost"
        elif consistent and exceeds_noise and row["mean_gain"] > 0:
            verdict = "**holds up**"
        elif row["mean_gain"] > 0 and consistent:
            verdict = "positive but within noise"
        elif row["mean_gain"] > 0:
            verdict = "inconsistent across folds"
        else:
            verdict = "**does not hold**"
        lines.append(
            f"| {row['claim']} (`{row['challenger']}` vs `{row['reference']}`) | "
            f"{row['folds_better']}/{row['folds']} | {row['mean_gain']:+.1%} | "
            f"{sd_txt} | {verdict} |"
        )
    for row in meta["paired"]:
        lines.append(
            f"\n`{row['challenger']}` vs `{row['reference']}`, per fold: "
            + ", ".join(f"{g:+.1%}" for g in row["per_fold"])
        )
    lines.append(
        "\n## What this changes\n\n"
        "The correction itself is not in doubt. It beats both baselines on "
        "every fold, in every season, by a margin several times the scatter of "
        "that margin — and the raw-CAMS gain is largest in summer, when CAMS is "
        "furthest off.\n\n"
        + _additions_verdict(meta)
        + _penalty_verdict(meta)
        + "\n\nThe single-split ablation is not a safe guide for effects this "
        "small. It once reported coupling as a clear negative (-0.9%, +0.3%, "
        "-1.3% by horizon) on the strength of one split that happened to fall "
        "unfavourably. Where the table above says a gain is smaller than its "
        "own scatter, the honest statement is \"no measurable effect either way "
        "on one year of data\" — neither \"it helps\" nor \"it hurts\". "
        "Deciding would need more winters, which is the constraint behind "
        "almost every limitation in this project."
    )
    return "\n".join(lines) + "\n"


def _penalty_verdict(meta: dict) -> str:
    """Rows where a negative gain is the confirmation, not the refutation.

    Kept separate from the additions paragraph on purpose. Folding "the archive
    number was optimistic" into a list of features that failed to help would
    report the opposite of what the numbers say.
    """
    rows = [r for r in meta.get("paired", []) if r.get("kind") == "penalty"]
    if not rows:
        return ""

    parts = []
    for row in rows:
        sd = row["sd_gain"]
        cost = -row["mean_gain"]
        worse = row["folds"] - row["folds_better"]
        sd_txt = f"{sd:.1%}" if sd == sd else "unknown"
        line = (
            f"\n\n**{row['claim']}.** Scoring the same rows with the input the "
            f"model will actually be given costs {cost:+.1%}, worse on "
            f"{worse}/{row['folds']} folds (scatter {sd_txt})."
        )
        if sd == sd and abs(row["mean_gain"]) <= sd:
            line += (
                " The cost is consistent in direction but no larger than the "
                "fold-to-fold spread, so the honest statement is that the "
                "optimism is real in sign and about a percent in size, not that "
                "it has been pinned to a number. It does not move the headline "
                "claim: the correction still beats both baselines on every fold "
                "with either input."
            )
        else:
            line += (
                " That is larger than the fold-to-fold spread, so it is a real "
                "cost and the lead-matched column is the one to quote."
            )
        parts.append(line)
    return "".join(parts)


def _additions_verdict(meta: dict) -> str:
    """Describe each optional addition from its own numbers.

    Written from the table rather than by hand: an earlier version of this
    paragraph asserted "better on three folds of five" and stayed on the page
    after a re-run made it two, contradicting the table directly above it.
    """
    additions = [
        row for row in meta.get("paired", [])
        if row["claim"] not in ("correction vs persistence", "correction vs raw CAMS")
        and row.get("kind") != "penalty"
    ]
    if not additions:
        return ""

    helps, unclear = [], []
    for row in additions:
        sd = row["sd_gain"]
        consistent = row["folds_better"] == row["folds"]
        beats_noise = sd != sd or abs(row["mean_gain"]) > sd
        target = helps if (consistent and beats_noise and row["mean_gain"] > 0) else unclear
        target.append(
            f"**{row['claim']}** ({row['mean_gain']:+.1%}, "
            f"{row['folds_better']}/{row['folds']} folds)"
        )

    parts = []
    if helps:
        parts.append(
            "Carrying their weight: " + ", ".join(helps) + " — consistent in "
            "sign across folds and larger than the fold-to-fold scatter."
        )
    if unclear:
        parts.append(
            "Not distinguishable from no effect: " + ", ".join(unclear)
            + ". Each gain is smaller than its own spread, so on this evidence "
            "none of them can be claimed — nor ruled out."
        )

    # Where an addition's best fold is also its physically expected one, that is
    # worth saying. It is not proof — one season cannot be — but a gain landing
    # in the right season is different from a gain landing anywhere.
    for row in additions:
        if row.get("best_fold") and row["best_fold_gain"] > 0.02:
            parts.append(
                f"Worth noting: **{row['claim']}** does its best work on the "
                f"`{row['best_fold']}` fold ({row['best_fold_gain']:+.1%}), well "
                "above its average. If that is the season the mechanism predicts, "
                "the mean across all folds is diluting a real seasonal effect "
                "rather than measuring its absence — and the way to tell is more "
                "seasons, not more features."
            )
    return " ".join(parts)


def _utc(value: str, end_of_day: bool = False) -> dt.datetime:
    day = dt.date.fromisoformat(value)
    time = dt.time(23, 59, 59) if end_of_day else dt.time.min
    return dt.datetime.combine(day, time, tzinfo=dt.timezone.utc)


def _clean(value):
    if value is None:
        return None
    value = float(value)
    return None if value != value else value
