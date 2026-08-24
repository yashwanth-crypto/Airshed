# Airshed

Air pollution–weather coupled 72-hour forecasting for Delhi NCR.
Smart India Hackathon 2026 · SIH26082 · Ministry of Earth Sciences.

We do not train a forecaster from scratch. We train a **correction layer on top of
CAMS**, a free global physics forecast — CAMS supplies the future (transport,
regional build-up), our model supplies the local (bias correction, sub-grid detail).
Read `CLAUDE.md` before touching anything; the hard rules there are load-bearing.

**Resuming work? Start with `docs/STATUS.md`** — what is done, what is
pending, and the silent failure modes already found and fixed.

## Setup

```bash
uv sync
cp .env.example .env      # optional: only needed for OpenAQ live + FIRMS
```

## Daily archive (start this once and leave it)

```bash
.venv/Scripts/python.exe scripts/daily_archive.py
```

It stores the CAMS and meteorology runs **and** refreshes recent observations
from the OpenAQ API. Both halves are needed: the S3 bulk archive lags several
days, so a forecast built only on it would start from stale history.

Stores today's CAMS and meteorology runs with real issue times and lead hours.
Open-Meteo's air-quality *archive* is built from short-lead CAMS output, so it
is not a 72 h forecast; `cams_runs` accumulates genuine forecast runs from the
day this starts, and every day it does not run is a day of archive lost. Under
Windows Task Scheduler, daily at 06:30 IST:

```bash
schtasks /create /tn Airshed-Archive /tr "C:\SIH\.venv\Scripts\python.exe C:\SIH\scripts\daily_archive.py" /sc daily /st 06:30
```

## The airshed: upwind network

24 CAAQMS monitors 65-340 km up the Punjab-Haryana corridor, bearing 280-360
degrees from Delhi, feed transport features: wind-aligned corridor
concentration, estimated travel time, and the advected value currently
arriving. Smoke lifted off a Punjab field takes 12-36 hours to reach the city,
so an upwind monitor is information about Delhi's future that Delhi's own
history cannot contain.

```bash
airshed ingest upwind --start 2025-02-18 --end 2026-08-19
```

They are held in a separate `cpcb_upwind` dataset and never enter the NCR set:
never forecast targets, never part of the city average GRAP is keyed to, never
held out in leave-one-station-out. Mixing them in would silently redefine what
"Delhi AQI" means.

## Historical archive (observations only)

```bash
airshed ingest history --directory data/manual
```

Loads the Kaggle CPCB archive (2015-2020, 854k hourly rows, 50 of 51 stations
matched by name). **It cannot train the model** -- CAMS and forecast
meteorology do not reach back that far, so the input side is missing and the
supervised builder drops those rows. It is used for episode climatology and
station-quality history, where observations alone are enough: it carries 698
Stage IV hours against 54 in the modern period.

See `docs/notes/data-findings.md` section 11 for the era table and for the one
window that would actually extend training: **2022-11 to 2025-02**.

## Ingest

Every source caches to Parquet under `data/raw/<source>/date=YYYY-MM-DD/`.
Nothing in the demo path ever queries a live API (R8).

```bash
airshed ingest cams    --start 2024-10-15 --end 2024-11-30
airshed ingest meteo   --start 2024-10-15 --end 2024-11-30 --path training
airshed ingest metar   --start 2024-10-15 --end 2024-11-30
airshed ingest cpcb    --start 2024-10-15 --end 2024-11-30
airshed ingest fires   --start 2024-10-15 --end 2024-11-30
```

## Phase 1 gate

```bash
airshed gate      # rebuilds a winter and a summer week with the network blocked
```

The gate physically blocks socket connections while it runs, so "reads from
local storage" is verified rather than asserted.

## Rolling-origin evaluation (error bars)

```bash
airshed rolling      # five expanding-window folds, checkpointed per fold
```

Single-split numbers cannot tell a 1% effect from noise. This repeats the
comparison over five seasonal folds -- training always before evaluation -- and
reports the spread. **Checkpointed per fold**: a sleeping laptop, a Windows
update or a cancelled run costs at most the fold in flight, and re-running the
same command resumes. It also holds the system awake while it works, in-process,
without touching power settings.

| comparison | folds better | mean gain | sd | verdict |
|---|---|---|---|---|
| correction vs persistence | 5/5 | **+19.7%** | 8.1% | holds up |
| correction vs raw CAMS | 5/5 | **+29.0%** | 17.0% | holds up |
| upwind corridor | 4/5 | +0.5% | 1.0% | within noise |
| coupled multi-output | 3/5 | +0.8% | 1.2% | within noise |

The core claim survives contact with every season. The two additions do not
produce a gain larger than their own fold-to-fold spread, and are reported as
having no measurable effect either way -- which corrects an earlier
single-split verdict that called coupling a clear negative. See
`docs/results/rolling.md`.

## Ablation (Phase 2)

```bash
airshed ablation      # fits every model, scores the holdout, writes the table
```

Writes `docs/results/ablation.md`, which is version-controlled so the numbers
are diffable as the model changes. The table reports persistence on every row
(R2), per-class episode recall rather than accuracy (R5), and a regime check
that says whether the training and holdout periods describe the same atmosphere
at all — a poor score against a mismatched holdout is a data result, not a
model result, and the table says so itself.

## Status

**Phase 2 gate met.** The corrector beats raw CAMS by 31-34% RMSE and
persistence by 25-33%, at all three horizons, on held-out winter and summer
blocks. Bias falls from -49 ug/m3 (raw CAMS) to +6. Full table:
`docs/results/ablation.md`.

**Phase 3 partly met.**

* *Uncertainty:* fixed. Conformal calibration on the validation split lifts
  10-90 interval coverage from 54% to 77.8% against a target of 80%, without
  touching the median (identical RMSE rows in the table prove it).
* *Decision layer:* built. `airshed grap` predicts Delhi city-wide 24 h PM2.5
  and converts the distribution to GRAP stage probabilities using the statutory
  CAQM thresholds. Stage III is caught for 82% of event hours with a median
  72 h lead; Stage IV for 61%, median 48 h. Per-class recall only -- no accuracy
  column anywhere (R5).
* *Coupling:* two directions, two different answers.
  - Chained multi-output (PM2.5 + visibility predicting PM2.5) is a **negative
    result** and is reported as one: -0.9%, +0.3%, -1.3% by horizon. See the
    coupling section of `docs/results/ablation.md`.
  - The **reverse direction works**, and it is the harder one the problem
    statement is really asking about. See `docs/results/coupling.md`:

    ```bash
    airshed coupling      # does knowing the pollution improve the weather forecast?
    ```

    Raw GFS visibility scores 21.3 km RMSE over Delhi -- it is effectively blind
    to haze. Correcting it with weather alone reaches 1.19 km, level with
    persistence. Adding pollution information reaches **1.08 km, +9.5%**, and
    the gain concentrates exactly where aerosol physics says it should: +0.3% in
    clean air, +7.2% moderate, +8.9% poor, +8.3% severe. A larger feature set
    would not care how dirty the air was.

    Fired as a probabilistic alarm rather than a thresholded median, the
    pollution-informed model catches **91% of hours below 1 km** against 84% for
    weather-only -- flight diversions at IGI and highway pile-ups on NH-44/NH-48.

**Phase 4 gate answered: 82.6 ug/m3 RMSE**, averaged over six held-out
stations, against 109.6 for distance-weighted interpolation (+24.6%) and 92.3
for raw CAMS (+10.5%). The wind-aware graph beats both on average, but raw CAMS
is better at the two cleanest peripheral sites -- reported per station in
`docs/results/loso.md` rather than hidden in the mean.

```bash
airshed loso                              # leave-one-station-out validation
python scripts/plot_surface.py 2025-11-12T02:00
```

## Demo (Phase 5)

```bash
.venv/Scripts/python.exe -m uvicorn airshed.api.app:app --port 8018
```

Open `http://localhost:8018`. The top panel is the **live 72-hour forecast**
from the latest archived run, with 10-90% intervals and GRAP stage
probabilities, labelled with how old the observations behind it are. Below it,
pick any date the cache covers and press Replay:
the system rebuilds that day's inputs as they stood at issue time, shows what it
would have forecast against what CPCB actually recorded, states the GRAP stage
probabilities, and names the drivers. Replay works year-round, which live mode
does not during a clean-air month.

Every endpoint reads the local Parquet store; none touches the network, and the
header carries a "last synced" age that turns red when the cache goes stale (R8).

Spatial skill is well behind temporal skill (82.6 vs 64 ug/m3 RMSE, and the
temporal model has the station's own history). The surface is good enough to
say which part of the city is worse this morning; it is not good enough to
quote a number for an unmonitored block, and the UI must not imply otherwise
(R7). Every grid cell carries its distance to the nearest monitor for exactly
this reason.

**Phase 5 partly built.** Attribution (exact tree SHAP, grouped into causes a
person recognises), historical replay, the GRAP probability panel and the
MapLibre UI all work end to end. Replaying 12 Nov 2025 at 24 h lead gives a city
24 h mean of 265 ug/m3 against an observed 272, with 56% probability on Stage
III -- which is the stage that occurred.

Not built, and deliberately not faked:

* **Stubble-smoke split** (HYSPLIT back-trajectories against FIRMS detections).
  Blocked on a free NASA FIRMS key; `ingest/fires.py` is written and flags fire
  features unavailable rather than zero, so nothing silently pretends there were
  no fires.
* **Population-weighted exposure.** Needs a WorldPop raster we have not
  downloaded. The grid it would multiply against already exists.
* **Clean-route navigation.** Optional in the build plan; not started.

Known and unfixed: only one winter of ground truth exists (OpenAQ has a gap
from 2018 to Feb 2025), so the training and holdout regimes still differ.
Road density and satellite AOD are not yet auxiliary predictors in the surface.
