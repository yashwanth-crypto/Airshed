"""CAMS against observed PM2.5 — the picture the whole project rests on.

The gap between the two lines is exactly what the correction layer is asked to
learn. If it looks structured, the approach is sound; if it looks like noise,
we need to know now (docs/BUILD_PLAN.md, "Suggested first session").

    python scripts/plot_cams_vs_obs.py 2025-11-01 2025-11-14
"""

from __future__ import annotations

import sys
from pathlib import Path

import matplotlib

matplotlib.use("Agg")
import matplotlib.dates as mdates  # noqa: E402
import matplotlib.pyplot as plt  # noqa: E402
import polars as pl  # noqa: E402

from airshed.features import build as feat  # noqa: E402

OUT = Path("docs/results")


def main(start: str, end: str) -> int:
    base = feat.build_base(start, end)
    paired = base.filter(
        pl.col("pm25_clean").is_not_null() & pl.col("cams_pm2_5").is_not_null()
    )
    if paired.is_empty():
        print("no paired hours — ingest CPCB and CAMS for this range first")
        return 1

    city = (
        paired.group_by("time")
        .agg(
            pl.col("pm25_clean").mean().alias("observed"),
            pl.col("cams_pm2_5").mean().alias("cams"),
            pl.len().alias("n"),
        )
        .sort("time")
    )
    # IST for display only; everything stored stays UTC.
    city = city.with_columns(pl.col("time").dt.convert_time_zone("Asia/Kolkata"))

    obs, cams = paired["pm25_clean"], paired["cams_pm2_5"]
    bias = (cams - obs).mean()
    rmse = ((cams - obs) ** 2).mean() ** 0.5
    corr = paired.select(pl.corr("pm25_clean", "cams_pm2_5")).item()

    fig, (ax1, ax2) = plt.subplots(
        1, 2, figsize=(15, 5.5), gridspec_kw={"width_ratios": [2, 1]}
    )

    t = city["time"].to_list()
    ax1.fill_between(t, city["cams"], city["observed"], color="#d62728", alpha=0.13,
                     label="gap the corrector must learn")
    ax1.plot(t, city["observed"], color="#1a1a1a", lw=1.8, label="CPCB observed (city mean)")
    ax1.plot(t, city["cams"], color="#1f77b4", lw=1.8, label="CAMS forecast")
    ax1.set_ylabel("PM2.5  (µg/m³)")
    ax1.set_title(f"Delhi NCR, {start} to {end}", loc="left", fontsize=11)
    ax1.legend(frameon=False, fontsize=9)
    ax1.grid(alpha=0.25)
    ax1.xaxis.set_major_formatter(mdates.DateFormatter("%d %b"))
    ax1.spines[["top", "right"]].set_visible(False)

    ax2.scatter(obs, cams, s=4, alpha=0.15, color="#1f77b4", edgecolors="none")
    hi = float(max(obs.max(), cams.max()))
    ax2.plot([0, hi], [0, hi], color="#555", lw=1, ls="--", label="1:1")
    ax2.set_xlabel("observed PM2.5 (µg/m³)")
    ax2.set_ylabel("CAMS PM2.5 (µg/m³)")
    ax2.set_title(
        f"bias {bias:+.0f}   RMSE {rmse:.0f}   r {corr:.2f}", loc="left", fontsize=11
    )
    ax2.legend(frameon=False, fontsize=9)
    ax2.grid(alpha=0.25)
    ax2.spines[["top", "right"]].set_visible(False)

    fig.suptitle(
        "CAMS systematically under-forecasts Delhi PM2.5 — the correction target",
        fontsize=13, y=0.99, x=0.01, ha="left",
    )
    fig.tight_layout()
    OUT.mkdir(parents=True, exist_ok=True)
    out = OUT / f"cams_vs_observed_{start}_{end}.png"
    fig.savefig(out, dpi=140)
    print(f"paired hours {paired.height:,}")
    print(f"observed mean {obs.mean():.1f}  CAMS mean {cams.mean():.1f}  ratio {cams.mean()/obs.mean():.2f}")
    print(f"bias {bias:+.1f}  RMSE {rmse:.1f}  corr {corr:.3f}")
    print(f"wrote {out}")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1], sys.argv[2]))
