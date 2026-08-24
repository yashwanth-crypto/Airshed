"""Split correctness (R3). A leaky split is the failure that flatters a model most."""

from __future__ import annotations

import datetime as dt

import polars as pl

from airshed.config import load_config
from airshed.features import splits as sp

UTC = dt.timezone.utc


def _supervised(pad_days: int = 200, horizon: int = 72) -> pl.DataFrame:
    """A synthetic issue-time series spanning every configured block, plus padding.

    Derived from config rather than hardcoded, so moving the split blocks does
    not quietly turn these tests into no-ops against an empty holdout.
    """
    blocks = sp.blocks_from_config()
    first = min(b.start for b in blocks) - dt.timedelta(days=pad_days)
    last = max(b.end for b in blocks) + dt.timedelta(days=pad_days)
    issue = pl.datetime_range(first, last, interval="6h", time_zone="UTC", eager=True)
    return pl.DataFrame({"issue_time": issue}).with_columns(
        (pl.col("issue_time") + pl.duration(hours=horizon)).alias("target_time"),
        pl.lit("A").alias("station_id"),
        pl.lit(horizon, dtype=pl.Int32).alias("horizon_h"),
    )


def test_splits_do_not_overlap_in_time():
    parts = sp.split_frame(_supervised())
    train, val, test = parts["train"], parts["val"], parts["test"]
    assert train.height and val.height and test.height
    for other in (val, test):
        overlap = train.join(other, on="issue_time", how="semi")
        assert overlap.height == 0


def test_a_forecast_landing_inside_a_test_block_is_not_training_data():
    """Issued before the block, valid inside it — the classic 72 h straddle."""
    cfg = load_config()
    block_start = dt.datetime.fromisoformat(cfg.raw["split"]["test_blocks"][0]["start"]).replace(
        tzinfo=UTC
    )
    df = pl.DataFrame(
        {
            "issue_time": [block_start - dt.timedelta(hours=48)],
            "target_time": [block_start + dt.timedelta(hours=24)],
        }
    )
    labelled = sp.assign_split(df)
    assert labelled["split"].item() == "test"


def test_embargo_purges_rows_hugging_a_block_boundary():
    cfg = load_config()
    embargo_h = int(cfg.raw["split"]["embargo_h"])
    block_start = dt.datetime.fromisoformat(cfg.raw["split"]["test_blocks"][0]["start"]).replace(
        tzinfo=UTC
    )
    just_outside = block_start - dt.timedelta(hours=embargo_h - 1)
    df = pl.DataFrame(
        {"issue_time": [just_outside], "target_time": [just_outside - dt.timedelta(hours=1)]}
    )
    assert sp.assign_split(df)["split"].item() == "embargo"


def test_embargoed_rows_are_dropped_from_every_split():
    parts = sp.split_frame(_supervised())
    total = sum(p.height for p in parts.values())
    assert total < _supervised().height, "embargo must remove rows, not relabel them"


def test_holdout_is_whole_episodes_not_scattered_hours():
    """A test block must be one contiguous run, never interleaved with train."""
    labelled = sp.assign_split(_supervised()).sort("issue_time")
    flips = (
        labelled.select(
            (pl.col("split") != pl.col("split").shift(1)).fill_null(False).sum()
        ).item()
    )
    n_blocks = len(sp.blocks_from_config())
    # Each block contributes at most: enter embargo, enter block, leave block,
    # leave embargo. Scattered hours would produce far more transitions.
    assert flips <= 4 * n_blocks


def test_every_configured_block_is_represented():
    labelled = sp.assign_split(_supervised())
    labels = set(labelled["block_label"].drop_nulls().unique().to_list())
    assert labels == {b.label for b in sp.blocks_from_config()}
