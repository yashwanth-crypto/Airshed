"""The CAMS train/serve gap: how far the served input is from the trained one.

The corrector is fitted on `cams_archive` and served `cams_runs`. Those are not
the same field. The archive is Open-Meteo's short-lead reconstruction — roughly
analysis quality — while a run is a genuine forecast at 0-120 h lead. A model
that learned "archive-CAMS -> observed PM2.5" and is then handed a live run is
reading an input drawn from a different distribution, and if the run sits
systematically below the archive the forecast will run low by roughly that much.

Unlike the meteorology, **this cannot be fixed retrospectively.** Checked
against both hosts on 2026-08-24: `previous-runs-api.open-meteo.com/v1/air-quality`
returns HTTP 404, and `pm2_5_previous_dayN` on the air-quality endpoint returns
0/48 non-null. There is no archived-forecast air-quality product. The only
source of truth is the overlap between runs we archive ourselves and the archive
values for the same hours — which accumulates one day at a time, and only for as
long as the daily job keeps running.

## Why the sample size here is not the row count

A day's overlap is ~2,400 rows: 51 stations across 21 CAMS cells, 24 hours, and
every one of them shares a single weather situation. They are nowhere near
independent. The honest unit is the **run day**, so every interval below comes
from a bootstrap that resamples whole issue dates, not rows — and is withheld
entirely below `MIN_CLUSTERS_FOR_CI` days, because resampling two days with
replacement has three possible outcomes and yields something that looks like a
tight interval while carrying no information. Reporting a bias with no interval
is the correct answer here, and the reason this module refuses to fit a
correction yet.

## Why the archive has to be allowed to settle

Open-Meteo's archive for a recent hour can still be revised. Comparing a run
against an archive value fetched the next morning measures partly that revision
rather than the lead gap, so a comparison only counts as settled once the
archive value is `SETTLE_DAYS` old. Provisional rows are still reported — with
the label attached — because hiding them would be worse than labelling them.
"""

from __future__ import annotations

import datetime as dt
import logging
from pathlib import Path

import numpy as np
import polars as pl

from ..config import Config, load_config
from ..store import read_range, available_dates

log = logging.getLogger(__name__)

RESULTS = Path("docs/results/camsoffset.md")

# Run days needed before a correction may be fitted at all. Not a statistical
# derivation — a floor. Below this the clustered bootstrap cannot separate a
# systematic offset from one unusual week, and a correction fitted on a monsoon
# fortnight would be applied to a November episode.
MIN_RUN_DAYS = 20

# How old an archive value must be before its comparison counts as settled.
SETTLE_DAYS = 4

BOOTSTRAP_DRAWS = 2000

# Below this many clusters a bootstrap is theatre, not inference. Resampling two
# run days with replacement has three distinct outcomes, so the percentile
# interval collapses onto the gap between two numbers and *looks* precise — the
# first draft of this module printed "-16.9 to -15.4" from two days, which reads
# as a tight confidence interval and is nothing of the sort.
MIN_CLUSTERS_FOR_CI = 5


def overlap(cfg: Config | None = None) -> pl.DataFrame:
    """Every (station, hour) where we hold both a live run and an archive value.

    Joined on valid time, so each row is one hour described twice: once by the
    forecast that was actually issued, once by the archive the model trained on.
    """
    cfg = cfg or load_config()
    days = available_dates("cams_runs")
    if not days:
        return pl.DataFrame()

    runs = read_range("cams_runs", days[0], days[-1])
    if runs.is_empty():
        return pl.DataFrame()

    lo = runs["time"].min().date()
    hi = runs["time"].max().date()
    archive = read_range("cams_archive", lo, hi)
    if archive.is_empty():
        log.warning("no cached CAMS archive over %s..%s", lo, hi)
        return pl.DataFrame()

    arc = (
        archive.select(["station_id", "time", "pm2_5"])
        .unique(subset=["station_id", "time"], keep="last")
        .rename({"pm2_5": "archive"})
    )
    joined = (
        runs.select(["station_id", "time", "issue_time", "lead_h", "pm2_5"])
        .rename({"pm2_5": "run"})
        .join(arc, on=["station_id", "time"], how="inner")
        .drop_nulls(["run", "archive"])
    )
    if joined.is_empty():
        return joined

    today = dt.date.today()
    return joined.with_columns(
        (pl.col("lead_h") // 24).cast(pl.Int32).alias("lead_day"),
        pl.col("issue_time").dt.date().alias("issue_date"),
        (pl.col("run") - pl.col("archive")).alias("delta"),
    ).with_columns(
        # Settled means the archive value has had time to stop being revised,
        # so the difference measures lead and not revision.
        (
            pl.col("time").dt.date()
            <= pl.lit(today - dt.timedelta(days=SETTLE_DAYS))
        ).alias("settled")
    )


def _clustered_ci(
    frame: pl.DataFrame, value: str = "delta", cluster: str = "issue_date"
) -> tuple[float, float]:
    """Bootstrap a mean by resampling whole clusters, not rows.

    Resampling rows would treat 2,400 correlated hours as 2,400 independent
    observations and return an interval roughly fifty times too narrow — which
    is exactly the mistake that would make a two-day offset look conclusive.
    """
    groups = [
        g[value].to_numpy().astype(float)
        for _, g in frame.group_by(cluster, maintain_order=True)
    ]
    groups = [g[np.isfinite(g)] for g in groups]
    groups = [g for g in groups if g.size]
    if len(groups) < MIN_CLUSTERS_FOR_CI:
        return float("nan"), float("nan")

    rng = np.random.default_rng(0)
    n = len(groups)
    means = np.empty(BOOTSTRAP_DRAWS)
    for i in range(BOOTSTRAP_DRAWS):
        pick = rng.integers(0, n, n)
        means[i] = np.concatenate([groups[j] for j in pick]).mean()
    return float(np.percentile(means, 2.5)), float(np.percentile(means, 97.5))


def measure(frame: pl.DataFrame | None = None, cfg: Config | None = None) -> pl.DataFrame:
    """Bias and RMSE of the served input against the trained one, per lead day."""
    frame = overlap(cfg) if frame is None else frame
    if frame.is_empty():
        return pl.DataFrame()

    rows = []
    for (lead_day,), part in frame.group_by(["lead_day"], maintain_order=True):
        d = part["delta"].to_numpy().astype(float)
        d = d[np.isfinite(d)]
        if not d.size:
            continue
        lo, hi = _clustered_ci(part)
        rows.append(
            {
                "lead_day": int(lead_day),
                "rows": part.height,
                "run_days": part["issue_date"].n_unique(),
                "settled_days": part.filter(pl.col("settled"))["issue_date"].n_unique(),
                "archive_mean": float(part["archive"].mean()),
                "run_mean": float(part["run"].mean()),
                "bias": float(d.mean()),
                "bias_lo": lo,
                "bias_hi": hi,
                "rmse": float(np.sqrt(np.mean(d**2))),
            }
        )
    return pl.DataFrame(rows).sort("lead_day") if rows else pl.DataFrame()


def sufficiency(frame: pl.DataFrame) -> dict:
    """Is there enough independent history to fit a correction? Usually not yet."""
    if frame.is_empty():
        return {"run_days": 0, "settled_days": 0, "ready": False, "needed": MIN_RUN_DAYS}
    settled = frame.filter(pl.col("settled"))
    return {
        "run_days": frame["issue_date"].n_unique(),
        "settled_days": settled["issue_date"].n_unique() if not settled.is_empty() else 0,
        "first_run": str(frame["issue_date"].min()),
        "last_run": str(frame["issue_date"].max()),
        "ready": (settled["issue_date"].n_unique() if not settled.is_empty() else 0)
        >= MIN_RUN_DAYS,
        "needed": MIN_RUN_DAYS,
    }


def fit_offset(cfg: Config | None = None) -> dict[int, float] | None:
    """Per-lead-day additive offset to bring a live run onto the archive scale.

    Returns `None` — deliberately, not an empty dict — until enough settled run
    days exist. A caller that cannot tell "no correction needed" from "not
    enough evidence to correct" would apply zero and call it validated.
    """
    frame = overlap(cfg)
    ok = sufficiency(frame)
    if not ok["ready"]:
        log.warning(
            "CAMS offset not fitted: %d settled run day(s), need %d. "
            "The gap is real but unquantified; serving is left uncorrected.",
            ok["settled_days"], ok["needed"],
        )
        return None

    settled = frame.filter(pl.col("settled"))
    table = measure(settled, cfg=cfg)
    # Subtract the bias: the correction moves the run onto the archive scale the
    # model was fitted against, so `corrected = run - bias`.
    return {int(r["lead_day"]): -float(r["bias"]) for r in table.iter_rows(named=True)}


# ---------------------------------------------------------------------------
# reporting
# ---------------------------------------------------------------------------
def to_markdown(table: pl.DataFrame, ok: dict) -> str:
    lines = [
        "# CAMS train/serve gap",
        "",
        f"Generated {dt.datetime.now(dt.timezone.utc):%Y-%m-%d %H:%M UTC}. "
        "Regenerate with `airshed camsoffset`.",
        "",
        "The corrector is trained on `cams_archive` and served `cams_runs`. This "
        "measures how far apart those two are for the same station and hour — "
        "the distribution gap the live forecast actually runs on.",
        "",
        "**It cannot be closed retrospectively.** There is no archived-forecast "
        "air-quality product: `previous-runs-api.open-meteo.com/v1/air-quality` "
        "returns 404 and `pm2_5_previous_dayN` returns 0/48 non-null on the "
        "air-quality endpoint. The only evidence is the overlap below, which "
        "grows by one day each time the daily archive job runs, and by nothing "
        "at all on the days it does not.",
        "",
    ]

    if table.is_empty():
        lines += [
            "> **No overlap yet.** Archive at least one forecast run "
            "(`airshed archive`) and let the archive catch up.",
            "",
        ]
        return "\n".join(lines)

    lines += [
        f"- runs held: **{ok['run_days']}** ({ok['first_run']} to {ok['last_run']}), "
        f"of which **{ok['settled_days']}** are settled",
        f"- a comparison counts as settled once the archive value is {SETTLE_DAYS} "
        "days old, because a recent archive hour can still be revised and that "
        "revision is not what we are trying to measure",
        "",
        "## Live run minus archive, by lead day",
        "",
        "Negative bias means the served input sits **below** the input the model "
        "was fitted on, which pushes the forecast low. The interval is a "
        "bootstrap over whole run days — resampling rows instead would treat "
        "~2,400 correlated hours as independent and report an interval about "
        "fifty times too narrow. It is left blank below "
        f"{MIN_CLUSTERS_FOR_CI} run days, because resampling a handful of days "
        "with replacement produces an interval that looks tight and means "
        "nothing.",
        "",
        "| lead day | true lead | rows | run days | archive mean | run mean | bias | 95% CI (clustered) | RMSE |",
        "|---|---|---|---|---|---|---|---|---|",
    ]
    for r in table.iter_rows(named=True):
        n = r["lead_day"]
        ci = (
            f"— *(needs ≥{MIN_CLUSTERS_FOR_CI} run days)*"
            if r["bias_lo"] != r["bias_lo"]
            else f"{r['bias_lo']:+.1f} to {r['bias_hi']:+.1f}"
        )
        lines.append(
            f"| {n} | {24*n}–{24*n+23} h | {r['rows']:,} | {r['run_days']} | "
            f"{r['archive_mean']:.1f} | {r['run_mean']:.1f} | **{r['bias']:+.1f}** | "
            f"{ci} | {r['rmse']:.1f} |"
        )
    lines += ["", _verdict(table, ok), ""]
    return "\n".join(lines)


def _verdict(table: pl.DataFrame, ok: dict) -> str:
    lines = ["## Verdict", ""]
    worst = table.sort("bias").row(0, named=True)

    if not ok["ready"]:
        lines += [
            f"> **Not enough evidence to correct — {ok['settled_days']} settled "
            f"run day(s), {ok['needed']} needed.**",
            "",
            "The gap is visible and it is in the direction that matters: the "
            f"served input runs {abs(worst['bias']):.1f} µg/m³ "
            f"{'below' if worst['bias'] < 0 else 'above'} the trained input at "
            f"lead day {worst['lead_day']}. But a handful of run days from one "
            "season cannot tell a systematic offset from one unusual week — "
            "which is why the interval column is mostly blank rather than "
            "reassuringly narrow.",
            "",
            "**Serving is therefore left uncorrected, on purpose.** Applying a "
            "bias fitted on this much data would replace a known, measured, "
            "reported gap with an unknown one — and it would be fitted on "
            "monsoon air and applied to a November episode, which is precisely "
            "the regime where it matters most and generalises least.",
            "",
            "What to do instead: keep the daily job alive. Every run it archives "
            "adds one independent observation to the table above, and nothing "
            "else does. `airshed camsoffset` re-reads the whole overlap each "
            "time, so no separate bookkeeping has to be maintained.",
        ]
        return "\n".join(lines)

    lines += [
        f"**{ok['settled_days']} settled run days — enough to fit.** "
        "`fit_offset()` returns a per-lead-day additive correction bringing the "
        "live run onto the scale the model was trained on. Before enabling it "
        "in the serving path, check that the interval excludes zero at the leads "
        "that matter and that the sample spans more than one season.",
    ]
    return "\n".join(lines)


def write(table: pl.DataFrame, ok: dict, path: Path | None = None) -> Path:
    out = path or RESULTS
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(to_markdown(table, ok), encoding="utf-8")
    return out


def run(cfg: Config | None = None) -> tuple[pl.DataFrame, dict]:
    frame = overlap(cfg)
    return measure(frame, cfg=cfg), sufficiency(frame)
