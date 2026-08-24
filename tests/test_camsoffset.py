"""Tests for the CAMS train/serve gap measurement.

The failure mode this guards against is not a crash. It is a module that
cheerfully reports a confident-looking offset from three days of monsoon data,
which someone then wires into the serving path. Every test here is about
refusing to do that.
"""

from __future__ import annotations

import datetime as dt

import numpy as np
import polars as pl

from airshed.eval import camsoffset


def _overlap(n_days: int, bias: float = -16.0, spread: float = 1.0) -> pl.DataFrame:
    """Synthetic overlap: `n_days` run days, 24 hours, 5 stations."""
    rng = np.random.default_rng(0)
    rows = []
    for d in range(n_days):
        issue = dt.datetime(2026, 1, 1, tzinfo=dt.timezone.utc) + dt.timedelta(days=d)
        # One shared per-day shift, which is the whole point: rows inside a day
        # are correlated, so the day is the unit of independence.
        day_shift = rng.normal(0, spread)
        for h in range(24):
            for s in range(5):
                archive = 100.0
                rows.append(
                    {
                        "station_id": f"S{s}",
                        "time": issue + dt.timedelta(hours=h),
                        "issue_time": issue,
                        "lead_h": h,
                        "run": archive + bias + day_shift,
                        "archive": archive,
                        "lead_day": 0,
                        "issue_date": issue.date(),
                        "delta": bias + day_shift,
                        "settled": True,
                    }
                )
    return pl.DataFrame(rows)


def test_bias_is_recovered():
    table = camsoffset.measure(_overlap(10, bias=-16.0, spread=0.0))
    assert abs(table["bias"][0] - (-16.0)) < 0.01


def test_no_interval_below_the_cluster_floor():
    # Two run days: a bootstrap here has three distinct outcomes and would print
    # a narrow, meaningless interval. It must print nothing instead.
    table = camsoffset.measure(_overlap(2))
    assert np.isnan(table["bias_lo"][0])
    assert np.isnan(table["bias_hi"][0])


def test_interval_appears_once_enough_run_days_exist():
    table = camsoffset.measure(_overlap(camsoffset.MIN_CLUSTERS_FOR_CI))
    assert np.isfinite(table["bias_lo"][0])
    assert table["bias_lo"][0] <= table["bias"][0] <= table["bias_hi"][0]


def test_interval_widens_when_days_disagree():
    """Day-to-day disagreement must show up as a wider interval, not a tighter one."""
    tight = camsoffset.measure(_overlap(10, spread=0.5))
    loose = camsoffset.measure(_overlap(10, spread=8.0))
    tight_w = tight["bias_hi"][0] - tight["bias_lo"][0]
    loose_w = loose["bias_hi"][0] - loose["bias_lo"][0]
    assert loose_w > tight_w * 3


def test_clustered_interval_is_wider_than_a_naive_row_bootstrap():
    """The clustering is the point: row resampling understates the spread badly.

    Rows within a day share one shift, so treating them as independent shrinks
    the interval by roughly sqrt(rows per day) — the error that would make a
    two-day offset look conclusive.
    """
    frame = _overlap(10, spread=5.0)
    lo, hi = camsoffset._clustered_ci(frame)
    d = frame["delta"].to_numpy()
    naive = 1.96 * d.std(ddof=1) / np.sqrt(d.size)
    assert (hi - lo) > 4 * (2 * naive)


def test_fit_offset_refuses_without_enough_settled_days(monkeypatch):
    monkeypatch.setattr(camsoffset, "overlap", lambda cfg=None: _overlap(3))
    assert camsoffset.fit_offset() is None


def test_fit_offset_returns_the_negated_bias_when_ready(monkeypatch):
    monkeypatch.setattr(
        camsoffset, "overlap", lambda cfg=None: _overlap(camsoffset.MIN_RUN_DAYS, bias=-16.0, spread=0.0)
    )
    offset = camsoffset.fit_offset()
    assert offset is not None
    # `corrected = run - bias`, so a run sitting 16 low is moved 16 up.
    assert abs(offset[0] - 16.0) < 0.01


def test_unsettled_rows_do_not_count_towards_readiness():
    frame = _overlap(camsoffset.MIN_RUN_DAYS).with_columns(pl.lit(False).alias("settled"))
    ok = camsoffset.sufficiency(frame)
    assert ok["run_days"] == camsoffset.MIN_RUN_DAYS
    assert ok["settled_days"] == 0
    assert not ok["ready"]


def test_empty_overlap_reports_not_ready_rather_than_failing():
    ok = camsoffset.sufficiency(pl.DataFrame())
    assert not ok["ready"]
    assert ok["run_days"] == 0
    assert camsoffset.to_markdown(pl.DataFrame(), ok).startswith("# CAMS train/serve gap")
