"""Train / validation / test splitting by time block (R3).

Nobody hand-rolls a split. A random split of hourly air-quality data leaks the
answer outright: 14:00 and 15:00 on the same November afternoon are nearly the
same row, so a shuffled holdout measures interpolation, not forecasting, and
reports an RMSE that will not survive contact with a real November.

Two protections here:

* whole **episodes** are held out — an entire winter block, not scattered hours;
* an **embargo** purges rows either side of every block boundary, because a row
  issued at t carries a target at t+72 and would otherwise straddle the seam.

Blocks come from `config.toml`. Anything outside val and test blocks (and
outside their embargo zones) is training data.
"""

from __future__ import annotations

import datetime as dt
from dataclasses import dataclass
from enum import Enum

import polars as pl

from ..config import Config, load_config


class Split(str, Enum):
    TRAIN = "train"
    VAL = "val"
    TEST = "test"
    EMBARGO = "embargo"


@dataclass(frozen=True, slots=True)
class Block:
    start: dt.datetime
    end: dt.datetime
    label: str
    split: Split

    def contains(self, ts: dt.datetime) -> bool:
        return self.start <= ts <= self.end


def blocks_from_config(cfg: Config | None = None) -> list[Block]:
    cfg = cfg or load_config()
    spec = cfg.raw["split"]
    out: list[Block] = []
    for split, key in ((Split.VAL, "val_blocks"), (Split.TEST, "test_blocks")):
        for b in spec.get(key, []):
            out.append(
                Block(
                    start=_start_of(b["start"]),
                    end=_end_of(b["end"]),
                    label=b.get("label", f"{split.value}-{b['start']}"),
                    split=split,
                )
            )
    return sorted(out, key=lambda b: b.start)


def assign_split(
    df: pl.DataFrame,
    cfg: Config | None = None,
    time_col: str = "issue_time",
    target_col: str = "target_time",
) -> pl.DataFrame:
    """Label every row train / val / test / embargo.

    A row belongs to a block if **either** its issue time or its target time
    falls inside it — a forecast issued before a test episode that lands inside
    it is a test row, not a training row. Rows near a boundary but not inside
    any block become `embargo` and are dropped by `split_frame`.
    """
    cfg = cfg or load_config()
    blocks = blocks_from_config(cfg)
    embargo = dt.timedelta(hours=int(cfg.raw["split"]["embargo_h"]))

    times = [pl.col(time_col)]
    if target_col in df.columns:
        times.append(pl.col(target_col))

    expr = pl.lit(Split.TRAIN.value)
    # Embargo first, so an explicit block assignment below overrides it.
    for b in blocks:
        touches_embargo = _any_between(times, b.start - embargo, b.end + embargo)
        expr = pl.when(touches_embargo).then(pl.lit(Split.EMBARGO.value)).otherwise(expr)
    for b in blocks:
        touches_block = _any_between(times, b.start, b.end)
        expr = pl.when(touches_block).then(pl.lit(b.split.value)).otherwise(expr)

    label_expr = pl.lit(None, dtype=pl.Utf8)
    for b in blocks:
        label_expr = (
            pl.when(_any_between(times, b.start, b.end))
            .then(pl.lit(b.label))
            .otherwise(label_expr)
        )

    return df.with_columns(expr.alias("split"), label_expr.alias("block_label"))


def split_frame(
    df: pl.DataFrame,
    cfg: Config | None = None,
    time_col: str = "issue_time",
    target_col: str = "target_time",
) -> dict[str, pl.DataFrame]:
    """Return {"train":…, "val":…, "test":…}. Embargoed rows are discarded."""
    labelled = assign_split(df, cfg, time_col, target_col)
    return {
        s.value: labelled.filter(pl.col("split") == s.value)
        for s in (Split.TRAIN, Split.VAL, Split.TEST)
    }


def summarise(df: pl.DataFrame) -> pl.DataFrame:
    """Row counts and date ranges per split — print this before trusting a result."""
    if "split" not in df.columns:
        raise ValueError("call assign_split first")
    time_col = "issue_time" if "issue_time" in df.columns else "time"
    return (
        df.group_by(["split", "block_label"])
        .agg(
            pl.len().alias("rows"),
            pl.col(time_col).min().alias("from"),
            pl.col(time_col).max().alias("to"),
        )
        .sort(["split", "from"])
    )


# ---------------------------------------------------------------------------
def _any_between(exprs: list[pl.Expr], lo: dt.datetime, hi: dt.datetime) -> pl.Expr:
    cond = exprs[0].is_between(lo, hi)
    for e in exprs[1:]:
        cond = cond | e.is_between(lo, hi)
    return cond


def _start_of(value: str) -> dt.datetime:
    return dt.datetime.combine(
        dt.date.fromisoformat(value), dt.time.min, tzinfo=dt.timezone.utc
    )


def _end_of(value: str) -> dt.datetime:
    return dt.datetime.combine(
        dt.date.fromisoformat(value), dt.time(23, 59, 59), tzinfo=dt.timezone.utc
    )
