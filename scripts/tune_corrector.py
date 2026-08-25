"""Hyperparameter search for the correction layer.

The model has run on `DEFAULT_PARAMS` since Phase 2 and has never been tuned, so
whatever it is worth is a floor rather than a result.

Two rules make the search honest, and both matter more here than the search
itself:

**Tuned on validation, never on test.** The test blocks are held for the number
that gets reported. A search that peeks at them turns the ablation into a
training curve, and the improvement it reports is the search overfitting.

**Scored at 72 h, not overall.** Overall RMSE is dominated by 24 h rows, where
observation history does most of the work and the correction matters least. The
horizon this project exists for is the one to tune against.

    .venv/Scripts/python.exe scripts/tune_corrector.py --trials 24

Writes `docs/results/tuning.md` and prints the winner. Changes no defaults --
applying the result is a separate, deliberate edit.
"""

from __future__ import annotations

import argparse
import datetime as dt
import itertools
import json
import logging
import random
import sys
from pathlib import Path

import numpy as np
import polars as pl

from airshed.config import load_config
from airshed.eval import ablation
from airshed.models.corrector import DEFAULT_PARAMS, DEFAULT_ROUNDS, CorrectorModel

log = logging.getLogger("tune")

RESULTS = Path("docs/results/tuning.md")
TUNE_HORIZON = 72

# Ranges around the untuned defaults rather than a sweep of everything: the
# question is whether the defaults are leaving anything on the table, not which
# of ten thousand configurations wins on one validation split.
SEARCH_SPACE = {
    "learning_rate": [0.02, 0.03, 0.05, 0.08],
    "num_leaves": [31, 63, 127, 255],
    "min_data_in_leaf": [20, 40, 80, 160],
    "feature_fraction": [0.6, 0.7, 0.8, 0.9],
    "bagging_fraction": [0.7, 0.8, 0.9],
    "lambda_l2": [0.5, 1.0, 5.0, 20.0],
}
ROUNDS_SPACE = [300, 400, 600, 900]


def sample(rng: random.Random) -> tuple[dict, int]:
    params = {k: rng.choice(v) for k, v in SEARCH_SPACE.items()}
    return params, rng.choice(ROUNDS_SPACE)


def score(train, val, params: dict, rounds: int) -> float:
    """Validation RMSE at the tuning horizon. Median head only.

    Fitting all three quantiles per trial would triple the cost to refine a
    number the search does not rank on.
    """
    model = CorrectorModel(
        use_obs_history=True, use_meteo=True, name="tune",
        params={**DEFAULT_PARAMS, **params}, num_rounds=rounds,
        drop_prefixes=("upwind_",), quantiles=(0.5,),
    )
    model.fit(train, train["y"].to_numpy().astype(float))
    sel = val.filter(pl.col("horizon_h") == TUNE_HORIZON)
    if sel.is_empty():
        return float("nan")
    pred = model.predict(sel).q50
    y = sel["y"].to_numpy().astype(float)
    ok = np.isfinite(pred) & np.isfinite(y)
    return float(np.sqrt(np.mean((pred[ok] - y[ok]) ** 2)))


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="Tune the corrector on validation.")
    ap.add_argument("--start", default="2025-02-18")
    ap.add_argument("--end", default="2026-08-20")
    ap.add_argument("--trials", type=int, default=24)
    ap.add_argument("--seed", type=int, default=0)
    args = ap.parse_args(argv)

    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(message)s")
    logging.getLogger("airshed").setLevel(logging.WARNING)
    cfg = load_config()

    log.info("building supervised table %s..%s", args.start, args.end)
    sup = ablation.load_supervised(args.start, args.end, cfg=cfg)
    needed = [c for c in ("y", "cams_pm2_5_tgt", "obs_lag_1h", "obs_lag_24h") if c in sup.columns]
    sup = sup.drop_nulls(needed)
    train = sup.filter(pl.col("split") == "train")
    val = sup.filter(pl.col("split") == "val")
    if train.is_empty() or val.is_empty():
        log.error("train=%d val=%d — check split blocks", train.height, val.height)
        return 1
    log.info("train %d rows, val %d rows", train.height, val.height)

    rng = random.Random(args.seed)
    baseline = score(train, val, {}, DEFAULT_ROUNDS)
    log.info("defaults: val RMSE @%d h = %.3f", TUNE_HORIZON, baseline)

    seen, rows = set(), []
    for i in range(args.trials):
        params, rounds = sample(rng)
        key = json.dumps({**params, "rounds": rounds}, sort_keys=True)
        if key in seen:
            continue
        seen.add(key)
        rmse = score(train, val, params, rounds)
        rows.append({**params, "num_rounds": rounds, "val_rmse": rmse})
        log.info(
            "trial %2d/%d  rmse=%.3f  %s",
            i + 1, args.trials, rmse,
            " ".join(f"{k}={v}" for k, v in params.items()),
        )

    table = pl.DataFrame(rows).sort("val_rmse")
    best = table.row(0, named=True)
    gain = (baseline - best["val_rmse"]) / baseline

    RESULTS.parent.mkdir(parents=True, exist_ok=True)
    RESULTS.write_text(_markdown(table, baseline, best, gain, args), encoding="utf-8")
    log.info("best val RMSE %.3f vs default %.3f (%+.1f%%)", best["val_rmse"], baseline, 100 * gain)
    log.info("wrote %s", RESULTS)
    if gain < 0.01:
        log.warning(
            "under 1%% -- the defaults were already reasonable. Report that "
            "rather than adopting a configuration for no measured benefit."
        )
    return 0


def _markdown(table, baseline, best, gain, args) -> str:
    cols = [c for c in table.columns if c != "val_rmse"]
    lines = [
        "# Hyperparameter search — correction layer\n",
        f"Generated {dt.datetime.now(dt.timezone.utc):%Y-%m-%d %H:%M} UTC. "
        "Regenerate with `scripts/tune_corrector.py`.\n",
        f"- {table.height} configurations, seed {args.seed}, "
        f"`{args.start}` to `{args.end}`\n"
        f"- scored on the **validation** split at **{TUNE_HORIZON} h**, median "
        "head only\n",
        "\nTuned on validation and never on test: the test blocks are held for "
        "the number that gets reported, and a search that sees them turns the "
        "ablation into a training curve. Scored at 72 h because overall RMSE is "
        "dominated by 24 h rows, where observation history does most of the work "
        "and the correction matters least.\n",
        "\n## Result\n",
        f"| configuration | val RMSE @{TUNE_HORIZON} h |",
        "|---|---|",
        f"| defaults (untuned) | {baseline:.3f} |",
        f"| best of {table.height} | **{best['val_rmse']:.3f}** |",
        f"| difference | **{gain:+.1%}** |",
        "",
    ]
    if gain < 0.01:
        lines.append(
            f"**The defaults were already close.** {gain:+.1%} on validation is "
            "not worth adopting a new configuration for; the search is worth "
            "having as evidence that the model was not left badly mistuned, "
            "which is the question it was run to answer.\n"
        )
    else:
        lines.append(
            f"**Worth adopting: {gain:+.1%} on validation at {TUNE_HORIZON} h.** "
            "Apply it to `DEFAULT_PARAMS` as a deliberate edit, then regenerate "
            "the ablation so the reported numbers come from the tuned model. The "
            "gain above is a validation figure and will differ on test.\n"
        )
    lines += ["\n## Best configuration\n", "```toml"]
    for k in cols:
        lines.append(f"{k} = {best[k]}")
    lines += ["```", "\n## All trials, best first\n",
              "| " + " | ".join(cols) + " | val RMSE |",
              "|" + "---|" * (len(cols) + 1)]
    for row in table.iter_rows(named=True):
        lines.append(
            "| " + " | ".join(str(row[c]) for c in cols) + f" | {row['val_rmse']:.3f} |"
        )
    return "\n".join(lines) + "\n"


if __name__ == "__main__":
    sys.exit(main())
