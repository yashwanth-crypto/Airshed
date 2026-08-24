"""The coupling proof: does knowing the pollution improve the weather forecast?

The problem statement asks for a *coupled* system. Weather-to-pollution is easy
and already in the model — mixing height, ventilation, inversion strength all
drive the PM2.5 correction. The direction that is actually hard, and that a
chemical transport model exists to capture, is the reverse: **aerosol changes
the weather**. Heavy haze cuts the sunlight reaching the ground, which cools the
surface, which shallows the mixing layer, which traps more aerosol; and dense
aerosol supplies the condensation nuclei that turn a humid night into fog.

Visibility is where that loop becomes measurable, because visibility is
observed by an instrument (METAR at VIDP) and forecast by a physics model that
knows nothing about Delhi's aerosol load. On 2024-11-01 the model's visibility
diagnostic read a flat 24.1 km — its maximum — while METAR read 1.5 km.

So the experiment is a clean one, and it isolates exactly one thing:

| model | anchor | may use |
|---|---|---|
| `model-visibility` | GFS visibility | nothing — the raw physics forecast |
| `persistence` | last observation | nothing |
| `weather-only` | GFS visibility | meteorology, calendar, visibility history |
| `pollution-informed` | GFS visibility | the above **plus** PM2.5 and CAMS |

`pollution-informed` minus `weather-only` is the value of knowing the pollution
when forecasting the weather. Both correct the same anchor, on the same rows,
with the same splits; only the pollution features differ.

One extra check matters more than the headline number. If this is real coupling
and not just "more columns helped", the gain must **concentrate in polluted
conditions**. A model that improves visibility equally on clean days is telling
you something about feature count, not about aerosol physics. So the result is
reported stratified by observed PM2.5.
"""

from __future__ import annotations

import logging

import numpy as np
import polars as pl

from ..config import Config, load_config
from ..features import build as feat
from ..features import splits as split_mod
from ..models.calibrate import CalibratedModel
from ..models.corrector import CorrectorModel

log = logging.getLogger(__name__)

TARGET = "metar_visibility_km"
ANCHOR = "vis_anchor_km"

# Feature families that carry pollution information. Withheld from the
# weather-only model so the comparison isolates the coupling.
POLLUTION_PREFIXES = ("cams_", "obs_", "nbr_", "fire_", "pm25")

# Aviation and road-safety thresholds. Below 1 km, IGI operations degrade and
# highway pile-ups start; below 0.5 km, low-visibility procedures apply.
LOW_VIS_KM = 1.0
VERY_LOW_VIS_KM = 0.5


def build_supervised(start: str, end: str, cfg: Config | None = None) -> pl.DataFrame:
    """City-level table with observed visibility as the target."""
    cfg = cfg or load_config()
    base = feat.build_base(start, end, cfg=cfg)
    city = feat.build_city_base(base, cfg=cfg)
    if city.is_empty():
        return city

    # The physics anchor: Open-Meteo reports visibility in metres.
    city = city.with_columns((pl.col("met_visibility") / 1000.0).alias(ANCHOR))
    sup = feat.build_supervised(city, cfg=cfg, target=TARGET)
    if sup.is_empty():
        return sup
    # Anchor at *target* time — what the physics model forecast for that hour.
    sup = sup.with_columns((pl.col("met_visibility_tgt") / 1000.0).alias(ANCHOR))
    return split_mod.assign_split(sup, cfg=cfg)


def run(
    start: str,
    end: str,
    cfg: Config | None = None,
    num_rounds: int = 300,
) -> tuple[pl.DataFrame, pl.DataFrame, dict]:
    cfg = cfg or load_config()
    sup = build_supervised(start, end, cfg=cfg)
    if sup.is_empty():
        raise RuntimeError("no visibility rows — ingest METAR and CPCB first")

    sup = sup.drop_nulls(["y", ANCHOR, "vis_lag_1h"])
    train = sup.filter(pl.col("split") == "train")
    val = sup.filter(pl.col("split") == "val")
    test = sup.filter(pl.col("split") == "test")
    if train.height < 500 or test.is_empty():
        raise RuntimeError(f"train={train.height} test={test.height}; check split blocks")

    y_train = train["y"].to_numpy().astype(float)
    y_test = test["y"].to_numpy().astype(float)

    # Calibrated on the validation split, as with PM2.5. Without it the
    # quantile heads are overconfident and P(visibility < 1 km) almost never
    # clears any useful alarm threshold, which is a calibration failure
    # masquerading as a forecasting one.
    weather_only = CalibratedModel(CorrectorModel(
        anchor_col=ANCHOR, num_rounds=num_rounds, name="weather-only",
        drop_prefixes=POLLUTION_PREFIXES,
    ))
    pollution_informed = CalibratedModel(CorrectorModel(
        anchor_col=ANCHOR, num_rounds=num_rounds, name="pollution-informed",
    ))
    for model in (weather_only, pollution_informed):
        model.fit(train, y_train)
        if not val.is_empty():
            model.calibrate(val, val["y"].to_numpy().astype(float))

    q_weather = weather_only.predict(test)
    q_pollution = pollution_informed.predict(test)
    predictions = {
        "model-visibility": test[ANCHOR].to_numpy().astype(float),
        "persistence": test["vis_lag_1h"].to_numpy().astype(float),
        "weather-only": q_weather.q50,
        "pollution-informed": q_pollution.q50,
    }
    # A median forecast regresses to the mean and so almost never dips below
    # 1 km, which makes it useless as a fog alarm however good its RMSE. The
    # distribution knows better than the median does: the same quantile
    # interpolation the GRAP layer uses turns the forecast into
    # P(visibility < 1 km), and an alarm fires on probability, not on a point
    # estimate. Missing a fog event costs a diverted flight; a false alarm
    # costs a cautious one.
    fog = _fog_alarms(q_weather, q_pollution, y_test)

    rows = []
    for name, pred in predictions.items():
        for horizon in sorted(test["horizon_h"].unique().to_list()):
            sel = (test["horizon_h"].to_numpy() == horizon)
            rows.append({"model": name, "horizon_h": int(horizon),
                         **_score(pred[sel], y_test[sel])})
        rows.append({"model": name, "horizon_h": 0, **_score(pred, y_test)})
    table = pl.DataFrame(rows)

    strata = _by_pollution(test, predictions, y_test)
    meta_fog = fog
    meta = {
        "start": start, "end": end,
        "train_rows": train.height, "test_rows": test.height,
        "test_span": f"{test['issue_time'].min():%Y-%m-%d} to {test['issue_time'].max():%Y-%m-%d}",
        "n_weather_features": len(weather_only.inner._features),
        "n_pollution_features": len(pollution_informed.inner._features),
        "observed_median_km": float(np.nanmedian(y_test)),
        "low_vis_hours": int((y_test < LOW_VIS_KM).sum()),
        "fog": meta_fog,
    }
    return table, strata, meta


FOG_THRESHOLDS = (0.05, 0.10, 0.20, 0.30, 0.50)


def _fog_alarms(q_weather, q_pollution, y: np.ndarray) -> list[dict]:
    """Probabilistic low-visibility alarms, swept across alarm thresholds.

    A single threshold hides the trade-off. Sweeping it shows the operating
    curve, so the choice becomes a policy decision about the relative cost of a
    missed fog event versus a cautious one, rather than a hidden default.
    """
    from .. import grap

    out = []
    actual = np.isfinite(y) & (y < LOW_VIS_KM)
    for name, q in (("weather-only", q_weather), ("pollution-informed", q_pollution)):
        p_low = grap._cdf(q.q10, q.q50, q.q90, LOW_VIS_KM)
        for threshold in FOG_THRESHOLDS:
            called = p_low >= threshold
            hits = int((actual & called).sum())
            out.append(
                {
                    "model": name,
                    "threshold": threshold,
                    "recall": hits / max(int(actual.sum()), 1),
                    "precision": hits / max(int(called.sum()), 1),
                    "false_alarm_rate": int((~actual & called).sum()) / max(int((~actual).sum()), 1),
                }
            )
    return out


def _score(pred: np.ndarray, y: np.ndarray) -> dict:
    ok = np.isfinite(pred) & np.isfinite(y)
    if not ok.any():
        return {"n": 0}
    err = pred[ok] - y[ok]
    low = ok & (y < LOW_VIS_KM)
    called = low & (pred < LOW_VIS_KM)
    return {
        "n": int(ok.sum()),
        "rmse_km": float(np.sqrt(np.mean(err**2))),
        "mae_km": float(np.mean(np.abs(err))),
        "bias_km": float(np.mean(err)),
        "low_vis_n": int(low.sum()),
        # Of the hours visibility really fell below 1 km, how many were called.
        "low_vis_recall": float(called.sum() / low.sum()) if low.sum() else float("nan"),
    }


def _by_pollution(
    test: pl.DataFrame, predictions: dict[str, np.ndarray], y: np.ndarray
) -> pl.DataFrame:
    """RMSE split by how polluted the air actually was.

    This is the physical test. Coupling predicts the gain should grow with
    aerosol load; a generic feature-count effect would not care.
    """
    pm = test["y_pm25_actual"].to_numpy() if "y_pm25_actual" in test.columns else None
    if pm is None:
        pm = test["obs_lag_1h"].to_numpy().astype(float)
    bands = [(0, 60, "clean (<60)"), (60, 120, "moderate (60-120)"),
             (120, 250, "poor (120-250)"), (250, 1e9, "severe (>250)")]
    rows = []
    for lo, hi, label in bands:
        sel = np.isfinite(pm) & (pm >= lo) & (pm < hi)
        if sel.sum() < 30:
            continue
        entry = {"pm25_band": label, "n": int(sel.sum()),
                 "median_visibility_km": float(np.nanmedian(y[sel]))}
        for name, pred in predictions.items():
            entry[name] = _score(pred[sel], y[sel]).get("rmse_km")
        rows.append(entry)
    return pl.DataFrame(rows)


def to_markdown(table: pl.DataFrame, strata: pl.DataFrame, meta: dict) -> str:
    def cell(model: str, horizon: int, column: str):
        row = table.filter((pl.col("model") == model) & (pl.col("horizon_h") == horizon))
        if row.is_empty() or row[column][0] is None:
            return None
        value = row[column][0]
        return None if value != value else float(value)

    models = table["model"].unique(maintain_order=True).to_list()
    horizons = [h for h in sorted(table["horizon_h"].unique().to_list()) if h > 0]

    lines = [
        "# Coupling proof — does pollution improve the weather forecast?\n",
        "The problem statement asks for a coupled system. Weather-to-pollution "
        "is already in the model. This measures the hard direction: whether "
        "knowing Delhi's aerosol load improves a *weather* forecast — "
        "visibility — that the physics model produces without it.\n",
        f"- {meta['train_rows']:,} training hours, {meta['test_rows']:,} held-out "
        f"hours ({meta['test_span']})\n"
        f"- median observed visibility {meta['observed_median_km']:.1f} km; "
        f"{meta['low_vis_hours']} hours below {LOW_VIS_KM:.0f} km\n"
        f"- weather-only model sees {meta['n_weather_features']} features, "
        f"pollution-informed sees {meta['n_pollution_features']}\n",
        "\n## Visibility RMSE by horizon (km, lower is better)\n",
        "| model | " + " | ".join(f"{h} h" for h in horizons) + " | overall |",
        "|---" * (len(horizons) + 2) + "|",
    ]
    for model in models:
        cells = [f"| {model} "]
        for h in horizons + [0]:
            v = cell(model, h, "rmse_km")
            cells.append(f"| {v:.2f} " if v is not None else "| — ")
        lines.append("".join(cells) + "|")

    lines.append("\n## Low-visibility recall (observed below 1 km)\n")
    lines.append(
        "The hours that matter for disaster management: flight diversions at "
        "IGI, highway pile-ups on the NH-44 and NH-48 corridors.\n"
    )
    lines.append("| model | hours below 1 km | recall |")
    lines.append("|---|---|---|")
    for model in models:
        n = cell(model, 0, "low_vis_n")
        recall = cell(model, 0, "low_vis_recall")
        lines.append(
            f"| {model} | {int(n) if n else 0} | "
            + (f"{recall:.1%} |" if recall is not None else "— |")
        )

    fog = meta.get("fog") or []
    if fog:
        lines.append("\n## Fog alarms from the distribution, not the median\n")
        lines.append(
            "Thresholding a median forecast is a poor alarm: it regresses to the "
            "mean and so rarely dips below 1 km, whatever its RMSE — which is why "
            "the recall column above reads near zero for both correctors. Firing "
            "instead on P(visibility < 1 km) uses the "
            "whole predicted distribution, the same treatment the GRAP layer gives "
            "stage thresholds. Missing a fog event costs a diverted flight; a false "
            "alarm costs a cautious one.\n"
        )
        lines.append("| model | alarm at P >= | recall | precision | false alarm rate |")
        lines.append("|---|---|---|---|---|")
        for row in fog:
            lines.append(
                f"| {row['model']} | {row['threshold']:.2f} | {row['recall']:.1%} | "
                f"{row['precision']:.1%} | {row['false_alarm_rate']:.1%} |"
            )

    lines.append("\n## The physical test: does the gain grow with pollution?\n")
    lines.append(
        "If this is aerosol-driven coupling, the improvement must concentrate "
        "where the aerosol is. If it were merely a larger feature set, it would "
        "not care how dirty the air was.\n"
    )
    lines.append(
        "| observed PM2.5 | hours | median visibility | model | weather-only | "
        "**pollution-informed** | gain |"
    )
    lines.append("|---|---|---|---|---|---|---|")
    for row in strata.iter_rows(named=True):
        wo, pi = row.get("weather-only"), row.get("pollution-informed")
        gain = (wo - pi) / wo if wo and pi else None
        lines.append(
            f"| {row['pm25_band']} | {row['n']:,} | "
            f"{row['median_visibility_km']:.1f} km | "
            f"{row.get('model-visibility', float('nan')):.2f} | {wo:.2f} | "
            f"**{pi:.2f}** | " + (f"{gain:+.1%} |" if gain is not None else "— |")
        )

    lines.append("\n## Verdict\n")
    wo = cell("weather-only", 0, "rmse_km")
    pi = cell("pollution-informed", 0, "rmse_km")
    phys = cell("model-visibility", 0, "rmse_km")
    if wo and pi and phys:
        lines.append(
            f"Raw physics visibility: **{phys:.2f} km** RMSE. Correcting it with "
            f"weather alone: **{wo:.2f} km**. Adding pollution: **{pi:.2f} km** "
            f"({(wo - pi) / wo:+.1%} against weather-only)."
        )
        if pi < wo:
            lines.append(
                "\n**Pollution information improves the weather forecast.** That "
                "is the coupled direction the problem statement asks for, stated "
                "as a number on held-out data rather than asserted from "
                "architecture."
            )
        else:
            lines.append(
                "\n**Negative result: pollution information does not improve the "
                "visibility forecast here.** Report it as one and do not claim "
                "two-way coupling until it does."
            )
    return "\n".join(lines) + "\n"
