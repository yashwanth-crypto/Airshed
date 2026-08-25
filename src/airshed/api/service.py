"""Service layer behind the API.

Everything here reads the local Parquet store. No endpoint touches a remote
source, so a CPCB or Open-Meteo outage degrades the demo to "stale cache with a
visible timestamp" rather than to an error page (R8).

The fitted model is cached on disk so the first request after a restart does not
wait for training. It is refit only when the cached copy is missing or the
requested training window has moved.
"""

from __future__ import annotations

import datetime as dt
import logging
import pickle
from pathlib import Path

import numpy as np
import polars as pl

from .. import attribution, grap
from ..config import load_config
from ..eval import ablation
from ..eval import grap_eval
from ..features import build as feat
from ..models import surface as surface_mod
from ..models.calibrate import CalibratedModel
from ..models.corrector import CorrectorModel
from ..store import available_dates, coverage

log = logging.getLogger(__name__)

MODEL_FILE = "forecast_model.pkl"
TRAIN_START = "2025-02-18"


class Service:
    """Loads data and models once, then answers questions from memory."""

    def __init__(self, train_start: str = TRAIN_START) -> None:
        self.cfg = load_config()
        self.train_start = train_start
        self._model: CalibratedModel | None = None
        self._trained_through: str | None = None

    # -- model -------------------------------------------------------------
    @property
    def model_path(self) -> Path:
        return self.cfg.processed_dir / MODEL_FILE

    def data_end(self) -> str:
        days = available_dates("cpcb")
        if not days:
            raise RuntimeError("no cached CPCB data — run `airshed ingest cpcb` first")
        return days[-1].isoformat()

    def model(self) -> CalibratedModel:
        if self._model is not None:
            return self._model
        if self.model_path.is_file():
            with open(self.model_path, "rb") as fh:
                payload = pickle.load(fh)
            self._model = payload["model"]
            self._trained_through = payload["trained_through"]
            log.info("loaded cached model trained through %s", self._trained_through)
            return self._model
        return self.fit()

    def fit(self) -> CalibratedModel:
        end = self.data_end()
        log.info("fitting forecast model on %s..%s", self.train_start, end)
        sup = ablation.load_supervised(self.train_start, end, cfg=self.cfg)
        train = sup.filter(pl.col("split") == "train").drop_nulls(["y", "cams_pm2_5_tgt"])
        val = sup.filter(pl.col("split") == "val").drop_nulls(["y", "cams_pm2_5_tgt"])

        model = CalibratedModel(
            CorrectorModel(use_obs_history=True, use_meteo=True, name="full")
        )
        model.fit(train, train["y"].to_numpy().astype(float))
        if not val.is_empty():
            model.calibrate(val, val["y"].to_numpy().astype(float))

        self.model_path.parent.mkdir(parents=True, exist_ok=True)
        with open(self.model_path, "wb") as fh:
            pickle.dump({"model": model, "trained_through": end}, fh)
        self._model = model
        self._trained_through = end
        return model

    # -- status ------------------------------------------------------------
    def status(self) -> dict:
        """What is cached and how stale it is — the "last synced" indicator (R8)."""
        datasets = {}
        for name in ("cpcb", "cams_archive", "meteo_archive", "metar", "cams_runs", "meteo_runs"):
            info = coverage(name)
            last = info["last"]
            age_h = None
            if last:
                # Measured from the END of the newest partition's day, not its
                # midnight. Partitions are dated, not stamped, and each holds up
                # to 24 hours of data -- so measuring from midnight reported a
                # feed that was 8 h behind as 29 h behind, which is the
                # difference between "normal lag" and "something is broken".
                end_of_day = dt.datetime.combine(
                    dt.date.fromisoformat(str(last)),
                    dt.time(23, 59),
                    tzinfo=dt.timezone.utc,
                )
                age_h = round(
                    max(0.0, (dt.datetime.now(dt.timezone.utc) - end_of_day).total_seconds() / 3600),
                    1,
                )
            datasets[name] = {**info, "age_hours": age_h}
        return {
            "datasets": datasets,
            "stations": len(self.cfg.stations),
            "trained_through": self._trained_through,
            "generated_at": dt.datetime.now(dt.timezone.utc).isoformat(),
        }

    # -- replay ------------------------------------------------------------
    def replay(self, target_date: str, horizon: int = 24) -> dict:
        """Reconstruct what the system would have forecast for a past day.

        This is the demo centrepiece and it doubles as genuine validation: the
        inputs are rebuilt from cache exactly as they stood at issue time, and
        the answer is compared with what CPCB actually recorded.
        """
        day = dt.date.fromisoformat(target_date)
        # Reach back far enough to build lags and the issuing window.
        start = day - dt.timedelta(days=6)
        end = day + dt.timedelta(days=1)

        base = feat.build_base(start, end, cfg=self.cfg)
        sup = feat.build_supervised(base, cfg=self.cfg)
        if sup.is_empty():
            return {"error": f"no data for {target_date}"}

        # Replay is the demo centrepiece and it doubles as validation, so it has
        # to reconstruct what was *available*, not what is convenient. The
        # archives return the best forecast for each past hour, which is a
        # short-lead one, so replaying from them shows the system performing
        # better than it can live. Substituting the forecast genuinely in hand
        # `horizon_h` earlier closes that gap. Falls back silently per row where
        # no lead-matched value exists, and `met_lead_matched` records which.
        sup = feat.apply_lead_matched_meteo(sup, cfg=self.cfg)

        window = sup.filter(
            (pl.col("target_time").dt.date() == day) & (pl.col("horizon_h") == horizon)
        )
        before_drop = window.height
        window = window.drop_nulls(["cams_pm2_5_tgt"])
        if window.is_empty():
            # Name the source that is actually short. "No forecasts landing on
            # that day" is true but useless: the usual cause is one dataset
            # lagging the others, and the fix is a top-up, not a code change.
            return {"error": self._why_no_replay(day, horizon, before_drop)}

        pred = self.model().predict(window)
        scored = window.select(
            ["station_id", "issue_time", "target_time", "horizon_h", "y", "cams_pm2_5_tgt"]
        ).with_columns(
            pl.Series("forecast", pred.q50),
            pl.Series("lower", pred.q10),
            pl.Series("upper", pred.q90),
        )

        city = (
            scored.group_by("target_time")
            .agg(
                pl.col("forecast").mean().alias("forecast"),
                pl.col("lower").mean().alias("lower"),
                pl.col("upper").mean().alias("upper"),
                pl.col("y").mean().alias("observed"),
                pl.col("cams_pm2_5_tgt").mean().alias("cams"),
                pl.len().alias("n_stations"),
            )
            .sort("target_time")
        )

        observed = scored["y"].to_numpy().astype(float)
        forecast = scored["forecast"].to_numpy().astype(float)
        ok = np.isfinite(observed) & np.isfinite(forecast)
        cams_v = scored["cams_pm2_5_tgt"].to_numpy().astype(float)

        return {
            "date": target_date,
            "horizon_h": horizon,
            "city_series": [
                {
                    "time": row["target_time"].isoformat(),
                    "forecast": _round(row["forecast"]),
                    "lower": _round(row["lower"]),
                    "upper": _round(row["upper"]),
                    "observed": _round(row["observed"]),
                    "cams": _round(row["cams"]),
                    "n_stations": row["n_stations"],
                }
                for row in city.iter_rows(named=True)
            ],
            "skill": {
                "n": int(ok.sum()),
                "rmse_model": _rmse(forecast, observed, ok),
                "rmse_cams": _rmse(cams_v, observed, ok),
                "observed_mean": _round(float(np.nanmean(observed[ok]))) if ok.any() else None,
                # What share of these rows were reconstructed with the forecast
                # genuinely available at issue time. A replay is only validation
                # if it uses what was in hand, so the number travels with the
                # score rather than being assumed.
                "lead_matched_share": (
                    _round(float(window["met_lead_matched"].mean()), 3)
                    if "met_lead_matched" in window.columns
                    else None
                ),
            },
            "grap": self._grap_for_day(scored),
            "drivers": self._drivers_for(window, horizon),
        }

    def _why_no_replay(self, day: dt.date, horizon: int, rows_before_drop: int) -> str:
        """Diagnose an empty replay window against what is actually cached."""
        short = []
        for name in self.REPLAY_SOURCES:
            days = available_dates(name)
            if not days:
                short.append(f"{name} is empty")
            elif days[-1] < day:
                short.append(f"{name} only reaches {days[-1]}")
        if short:
            return (
                f"{target_or(day)} is not replayable yet: " + "; ".join(short)
                + ". Run `airshed ingest cams|meteo --start ... --end ...` "
                "or the daily archive job to top these up."
            )
        if rows_before_drop == 0:
            return (
                f"no observations for {day}, so there is nothing to score a "
                f"{horizon} h forecast against"
            )
        return (
            f"{day} has observations but no CAMS forecast at the target hours; "
            "the CAMS archive is incomplete for that day"
        )

    def _grap_for_day(self, scored: pl.DataFrame) -> dict:
        """City AQI and stage probabilities for the replayed day."""
        city = (
            scored.group_by("target_time")
            .agg(
                pl.col("forecast").mean().alias("f"),
                pl.col("lower").mean().alias("lo"),
                pl.col("upper").mean().alias("hi"),
                pl.col("y").mean().alias("obs"),
            )
            .sort("target_time")
        )
        if city.is_empty():
            return {}
        # The daily figure GRAP is written against is a 24 h mean.
        mean_f = float(np.nanmean(city["f"].to_numpy()))
        mean_lo = float(np.nanmean(city["lo"].to_numpy()))
        mean_hi = float(np.nanmean(city["hi"].to_numpy()))
        observed_mean = float(np.nanmean(city["obs"].to_numpy()))

        from ..models.base import Quantiles

        q = Quantiles(np.array([mean_lo]), np.array([mean_f]), np.array([mean_hi]))
        probs = grap.stage_probabilities(q, self.cfg)
        stage_observed = int(grap.observed_stage(np.array([observed_mean]), self.cfg)[0])
        return {
            "forecast_24h_mean": _round(mean_f),
            "observed_24h_mean": _round(observed_mean),
            "observed_stage": stage_observed,
            "probabilities": {
                f"at_least_{s}": _round(float(probs[f"p_at_least_{s}"][0]), 3)
                for s in (1, 2, 3, 4)
            },
        }

    def _drivers_for(self, window: pl.DataFrame, horizon: int) -> list[dict]:
        try:
            table = attribution.drivers(self.model(), window, horizon=horizon, top=5)
        except KeyError:
            return []
        return [
            {
                "driver": row["driver"],
                "mean_contribution": _round(row["mean_contribution"]),
                "magnitude": _round(row["mean_magnitude"]),
            }
            for row in table.iter_rows(named=True)
        ]

    # -- live forecast -----------------------------------------------------
    def forecast(self) -> dict:
        """The live 72-hour forecast from the latest archived run.

        Everything is read from cache (R8): the run was stored by the daily
        archive job, the observations by the live sync. If either is stale the
        forecast still returns, with the staleness stated rather than hidden —
        a forecast built on three-day-old observations is worth having and
        worth labelling.
        """
        from ..features import live

        sup, issue = live.build_live_supervised(self.cfg)
        pred = self.model().predict(sup)
        scored = sup.select(["station_id", "issue_time", "target_time", "horizon_h"]).with_columns(
            pl.Series("forecast", pred.q50),
            pl.Series("lower", pred.q10),
            pl.Series("upper", pred.q90),
        )

        by_horizon = []
        for horizon in sorted(scored["horizon_h"].unique().to_list()):
            rows = scored.filter(pl.col("horizon_h") == horizon)
            city_mid = float(np.nanmean(rows["forecast"].to_numpy()))
            city_lo = float(np.nanmean(rows["lower"].to_numpy()))
            city_hi = float(np.nanmean(rows["upper"].to_numpy()))
            from ..models.base import Quantiles

            q = Quantiles(np.array([city_lo]), np.array([city_mid]), np.array([city_hi]))
            probs = grap.stage_probabilities(q, self.cfg)
            by_horizon.append(
                {
                    "horizon_h": int(horizon),
                    "target_time": rows["target_time"][0].isoformat(),
                    "city_pm25": _round(city_mid),
                    "lower": _round(city_lo),
                    "upper": _round(city_hi),
                    "grap": {
                        f"at_least_{s}": _round(float(probs[f"p_at_least_{s}"][0]), 3)
                        for s in (1, 2, 3, 4)
                    },
                    "stations": [
                        {
                            "id": r["station_id"],
                            "lat": self.cfg.station(r["station_id"]).lat,
                            "lon": self.cfg.station(r["station_id"]).lon,
                            "name": self.cfg.station(r["station_id"]).name,
                            "pm25": _round(r["forecast"]),
                            "lower": _round(r["lower"]),
                            "upper": _round(r["upper"]),
                        }
                        for r in rows.iter_rows(named=True)
                    ],
                }
            )

        age_h = round(
            (dt.datetime.now(dt.timezone.utc) - issue).total_seconds() / 3600, 1
        )
        return {
            "issued": issue.isoformat(),
            "issued_age_hours": age_h,
            "stale": age_h > 12,
            "horizons": by_horizon,
            "drivers": self._live_drivers(sup),
            "input_gap": self._input_gap(),
            # Sent so the interface declares stages by the same rule the
            # evaluation does. It previously hardcoded a flat 0.25 and had
            # drifted from the configured thresholds, which are deliberately
            # asymmetric -- a low bar for Severe because missing an episode
            # costs more than a false alarm, a high one for Poor because
            # declaring Poor on a one-in-four chance is crying wolf.
            "stage_thresholds": {
                str(k): v for k, v in grap_eval.DEFAULT_THRESHOLDS.items()
            },
        }

    def _input_gap(self) -> dict:
        """State the CAMS train/serve gap on the forecast itself.

        The model is trained on `cams_archive` and served `cams_runs`, and the
        two are not the same field. That is a real caveat on every number above,
        so it travels with them rather than living only in a results document.
        Measured from cache; never blocks the forecast.

        Cached for the day. The measurement scans the whole run/archive overlap,
        which is trivial at two run days and is not at four hundred — and it can
        only change when the daily job adds a run, so recomputing it per request
        would buy nothing.
        """
        from ..eval import camsoffset

        today = dt.date.today()
        if getattr(self, "_gap_day", None) == today:
            return self._gap_cache

        try:
            table, ok = camsoffset.run(self.cfg)
        except Exception as exc:  # a caveat must never take the forecast down
            log.warning("input gap unavailable: %s", str(exc)[:200])
            # Not cached: a transient failure should be retried, not pinned for
            # the rest of the day.
            return {"measured": False, "reason": "measurement failed"}

        if table.is_empty():
            gap = {
                "measured": False,
                "reason": "no run/archive overlap cached yet",
                "corrected": False,
            }
        else:
            worst = table.sort("bias").row(0, named=True)
            gap = {
                "measured": True,
                "corrected": False,
                "run_days": ok["run_days"],
                "settled_days": ok["settled_days"],
                "run_days_needed": ok["needed"],
                "worst_lead_day": int(worst["lead_day"]),
                "worst_bias_ugm3": _round(float(worst["bias"])),
                "note": (
                    "Trained on archived CAMS, served live CAMS. The served input "
                    f"runs {abs(float(worst['bias'])):.0f} ug/m3 "
                    f"{'below' if worst['bias'] < 0 else 'above'} the trained input "
                    f"at lead day {int(worst['lead_day'])}, on "
                    f"{ok['run_days']} run day(s) — too few to correct for "
                    f"({ok['needed']} needed), so the forecast is uncorrected. "
                    "See docs/results/camsoffset.md."
                ),
            }
        self._gap_day = today
        self._gap_cache = gap
        return gap

    def _live_drivers(self, sup: pl.DataFrame) -> list[dict]:
        try:
            table = attribution.drivers(self.model(), sup, horizon=72, top=5)
        except (KeyError, IndexError):
            return []
        return [
            {
                "driver": row["driver"],
                "mean_contribution": _round(row["mean_contribution"]),
                "magnitude": _round(row["mean_magnitude"]),
            }
            for row in table.iter_rows(named=True)
        ]

    # -- surface -----------------------------------------------------------
    def surface(self, stamp: str) -> dict:
        when = dt.datetime.fromisoformat(stamp)
        if when.tzinfo is None:
            when = when.replace(tzinfo=dt.timezone.utc)
        day = when.date()
        base = feat.build_base(day, day, cfg=self.cfg)
        grid = surface_mod.surface_for_hour(base, when, cfg=self.cfg)
        hour = base.filter(pl.col("time") == when)
        return {
            "time": when.isoformat(),
            "cells": [
                {
                    "lat": row["lat"],
                    "lon": row["lon"],
                    "pm25": _round(row["pm25"]),
                    "distance_km": row["distance_to_station_km"],
                }
                for row in grid.iter_rows(named=True)
            ],
            "stations": [
                {
                    "id": row["station_id"],
                    "lat": self.cfg.station(row["station_id"]).lat,
                    "lon": self.cfg.station(row["station_id"]).lon,
                    "name": self.cfg.station(row["station_id"]).name,
                    "pm25": _round(row["pm25_clean"]),
                }
                for row in hour.iter_rows(named=True)
            ],
        }

    #: Replay needs all of these present for the target date, not just ground
    #: truth. Offering a date backed by observations alone produces a confusing
    #: "no forecasts landing on that day" after the user has already clicked.
    REPLAY_SOURCES = ("cpcb", "cams_archive", "meteo_archive")

    def available_days(self) -> dict:
        """The range replay can actually serve — the intersection, not the union.

        Observations refresh from a live API while CAMS and meteorology are
        topped up in batch, so the datasets run to different dates. The date
        picker must reflect the shortest of them, or it will offer days that
        fail.
        """
        per_source = {}
        for name in self.REPLAY_SOURCES:
            days = available_dates(name)
            per_source[name] = (days[0], days[-1]) if days else None
        if any(v is None for v in per_source.values()):
            missing = [k for k, v in per_source.items() if v is None]
            return {"first": None, "last": None, "count": 0, "missing": missing}

        first = max(v[0] for v in per_source.values())
        last = min(v[1] for v in per_source.values())
        cpcb_days = available_dates("cpcb")
        return {
            "first": first.isoformat(),
            "last": last.isoformat(),
            "count": max((last - first).days + 1, 0),
            "limited_by": min(per_source, key=lambda k: per_source[k][1]),
            "observations_to": cpcb_days[-1].isoformat() if cpcb_days else None,
        }


def target_or(day) -> str:
    return day.isoformat() if hasattr(day, "isoformat") else str(day)


def _round(value, digits: int = 1):
    if value is None:
        return None
    try:
        if not np.isfinite(value):
            return None
    except TypeError:
        return None
    return round(float(value), digits)


def _rmse(pred: np.ndarray, obs: np.ndarray, ok: np.ndarray):
    if not ok.any():
        return None
    return round(float(np.sqrt(np.mean((pred[ok] - obs[ok]) ** 2))), 1)
