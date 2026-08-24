# Project status — handoff

Written 2026-08-24. Read this first in a new session, then `CLAUDE.md` for the
rules and `docs/BUILD_PLAN.md` for the phase gates. For how the work stands
against the problem statement and against the incumbents, see
`docs/POSITIONING.md`.

---

## One-paragraph summary

All five phases of `BUILD_PLAN.md` have been built. The central claim holds
with error bars: the correction layer beats raw CAMS by **29%** and persistence
by **19.7%** RMSE, on **5 of 5** rolling-origin folds. Uncertainty is
calibrated, the GRAP decision layer works with 72 h lead on Stage III, and the
coupled direction the problem statement asks about is **demonstrated with a
number** (visibility, +9.5%, gain concentrated in polluted air). The binding
constraint on everything else is that only **one winter** of trainable ground
truth exists.

---

## Environment

- Python 3.11 via `uv`; venv at `.venv`. Run things as
  `.venv/Scripts/python.exe -m airshed.cli ...`.
- **`data/` is not under version control and the repo is not a git repo yet.**
  `git init` has not been run. Nothing has ever been committed.
- Secrets in `.env` (gitignored): `OPENAQ_API_KEY` is set and working.
  `FIRMS_MAP_KEY` is **not** set.
- Set `PYTHONIOENCODING=utf-8` before CLI calls or the Windows console mangles
  the output tables.

---

## Data on disk

| dataset | days | range | rows |
|---|---|---|---|
| `cpcb` | 2551 | 2014-12-31 -> 2026-08-24 | 1,375,507 |
| `cpcb_upwind` | 537 | 2025-02-18 -> 2026-08-19 | 195,188 |
| `cams_archive` | 739 | 2024-06-01 -> 2026-08-24 | 904,536 |
| `meteo_archive` | 709 | 2024-09-15 -> 2026-08-24 | 864,744 |
| `metar` | 739 | 2024-06-01 -> 2026-08-24 | 18,894 |
| `cams_runs` | **1** | 2026-08-23 | 6,120 |
| `meteo_runs` | **1** | 2026-08-23 | 6,120 |
| `fires` | 274 | 2025-09-15 -> 2026-08-23 | 23,544 |

**The `cpcb` row is misleading on its own.** It spans 2015-2026 only because
the Kaggle archive (2015-2020) was loaded. The **trainable** period is the
intersection of ground truth, CAMS and meteorology, which is **2025-02-18
onward** — see `docs/notes/data-findings.md` section 11 for the era table.

---

## Completed

**Phase 1 — data spine.** Gate met. `airshed gate` rebuilds a week of every
feature with sockets physically blocked, so "reads from cache" is verified, not
claimed. 51 NCR stations resolved to OpenAQ ids with authoritative coordinates,
plus 24 upwind corridor stations held separately.

**Phase 2 — baseline and ablation.** Gate met. `airshed ablation` writes
`docs/results/ablation.md`: persistence, persistence-daily, raw CAMS, scaled
CAMS, cams+obs, full, full+upwind, coupled, and calibrated variants. Reports
per-horizon RMSE, skill, bias, episode recall, interval coverage, and a
**regime check** stating whether train and holdout describe the same atmosphere.

**Phase 3 — coupling and uncertainty.** Partly met.
- Calibration: conformal, normalised by interval width. Coverage **54% to 77.8%**
  against an 80% target, median untouched.
- GRAP: `airshed grap` -> `docs/results/grap.md`. Stage III caught for **82%**
  of event hours at **72 h median lead**; Stage IV **61%** at 48 h. Per-class
  recall only — accuracy appears nowhere (R5).
- Coupled multi-output: **no measurable effect** (see below).

**Phase 4 — space.** Gate answered: **82.6 ug/m3** held-out error across 6
stations, beating IDW by 24.6% and raw CAMS by 10.5%. Wind-aware graph, verified
by tests that a northerly reads the northern station. Downscaled surface on a
0.05 degree grid, every cell carrying its distance to the nearest monitor.

**Phase 5 — decision layer and demo.** Attribution via exact tree SHAP grouped
into human causes; historical replay; **live 72 h forecast**; MapLibre UI with
GRAP probabilities, intervals everywhere, and a "last synced" age that reddens
when stale.

**Beyond the build plan.**
- **Rolling-origin evaluation** (`airshed rolling`), checkpointed per fold so a
  sleeping laptop costs at most one fold.
- **Visibility coupling proof** (`airshed coupling`) — the direction the
  problem statement actually asks about.
- **Upwind airshed network** — 24 monitors 65-340 km up the Punjab corridor.
- **Daily archive job** (`scripts/daily_archive.py`) — archives runs, syncs
  live observations, tops up the archives.

---

## Headline numbers (all from held-out data)

| claim | value | evidence |
|---|---|---|
| vs raw CAMS | **+29.0%** RMSE, 5/5 folds | `rolling.md` |
| vs persistence | **+19.7%** RMSE, 5/5 folds | `rolling.md` |
| Interval coverage | 77.8% (target 80%) | `ablation.md` |
| GRAP Stage III | 82% caught, 72 h median lead | `grap.md` |
| Spatial (leave-one-station-out) | 82.6 ug/m3 | `loso.md` |
| Visibility from pollution | **+9.5%**, +8.9% in polluted air | `coupling.md` |
| Raw GFS visibility | 21.3 km RMSE — blind to haze | `coupling.md` |
| Fog alarm recall | 91% (vs 84% weather-only) | `coupling.md` |

Negative and null results, reported as such:
- **Coupled multi-output: no measurable effect** (+0.8% +/- 1.2%, 3/5 folds).
- **Upwind corridor: no measurable effect** (+0.5% +/- 1.0%, 4/5 folds), though
  the gain does concentrate on wind-aligned hours (+1.0% vs -0.4%).

---

## Pending — ranked

### 1. More seasons — the constraint behind every open question
**Fires are now loaded and evaluated.** 274 days, 23,544 detections, November
2025 peaking at 8,470. All results in `docs/results/` were regenerated with
them. Outcome:

- Fires are the **strongest of the three optional additions**: +1.0% in the
  single-split ablation, +1.3% across rolling folds, and **+4.3% on the
  `episode-2025` fold** — the stubble season, exactly where the mechanism
  predicts.
- It is still **not statistically separable** (sd 1.9%, 3/5 folds). One burning
  season cannot settle it.

That is the pattern for all three additions — fires, upwind corridor, coupling.
Each is small, each is smaller than its own fold-to-fold spread, and the fix is
**more winters, not more features**. See `POSITIONING.md` section 5.

### 2. Schedule the daily archive (one command)
`cams_runs` holds **1 day**. Every day it does not run is a day of true
archived-forecast history lost, and that archive is what will eventually let us
report an honest 72 h number instead of one measured with short-lead CAMS.

    schtasks /create /tn Airshed-Archive /tr "C:\SIH\.venv\Scripts\python.exe C:\SIH\scripts\daily_archive.py" /sc daily /st 06:30

### 3. More winters of ground truth
The single binding constraint. One winter cannot resolve a 1% effect, which is
why both the coupling and upwind verdicts are "cannot tell".
- CPCB CCR Advanced Search: **captcha-gated, and reportedly no data after
  Nov 2023**. The useful window would have been 2022-11 to 2025-02.
- AQICN data platform (https://aqicn.org/data-platform/register/): covers
  2015 to 2026Q3, one registration, **city-level daily** and possibly in AQI
  units rather than ug/m3. Would extend the *decision* layer to ~4 winters,
  not the hourly station model.
- Kaggle archive (2015-2020) is **already loaded** but cannot train: no CAMS or
  meteorology reaches back that far.

### 4. git init and first commit
Nothing is under version control. Per `CLAUDE.md`: author `yashwanth-crypto`,
no co-author trailers, no tool attribution, never commit `data/`.

### 5. Smaller, all unblocked
- Hyperparameter search (never tuned; `DEFAULT_PARAMS` throughout).
- 45 more NCR stations (51 of 96 in the official roster) — improves the weakest
  layer, needs only the OpenAQ key we already have.
- Road density and CAMS AOD as downscaling predictors (in BUILD_PLAN, not built).
- Population-weighted exposure (needs a WorldPop raster).
- Stubble-smoke split via HYSPLIT (needs FIRMS first).

---

## Traps already hit — do not re-learn these

Each was a silent failure: no exception, plausible-looking output.

1. **Two OpenAQ paths, two timestamp formats.** The S3 archive writes
   `+05:30`, the v3 API writes `Z`. One format string read only the first and
   `strict=False` turned the rest into nulls, so the entire live feed vanished
   into a `drop_nulls`. Covered by `tests/test_ingest_formats.py`.
2. **Dead station registrations.** OpenAQ carries retired duplicates; Anand
   Vihar has three, two long dead. Matching on distance alone selected a station
   that stopped reporting in 2021, and empty ground truth looks exactly like a
   routine CAAQMS outage.
3. **Roman numerals and site numbers.** `Knowledge Park V` matched
   `Knowledge Park III`; `Sector 11` could match `Sector 51`. Contradicting
   numerals now disqualify a pairing.
4. **Quantile sorting moved the baseline's median**, turning "raw CAMS" into a
   bias-corrected CAMS and flattering the thing we must beat.
5. **IST is +5:30.** Hourly local stamps land at :30 past the UTC hour and never
   join an on-the-hour index.
6. **BLH exists only on GFS, only since 2024-09-15.** `best_match` silently
   changes which model answers.
7. **Single-split differences are noise.** A confident negative verdict on
   coupling was overturned by rolling-origin.
8. **Freshness asymmetry.** Live observations ran ahead of the CAMS archive, so
   replay offered dates it could not serve. The daily job now tops up archives
   and `/api/days` reports the intersection, not the union.

---

## Where things live

    src/airshed/
      ingest/     cams meteo cpcb metar fires kaggle_history repair openmeteo _stationmatch
      features/   build splits upwind live
      models/     base baselines corrector coupled calibrate graph surface
      eval/       metrics ablation rolling loso visibility grap_eval
      api/        app service static/index.html
      grap.py attribution.py store.py config.py net.py env.py verify.py keepawake.py
    docs/results/   ablation rolling loso grap coupling (+ csv, png)
    docs/notes/     data-findings.md   <- every measured constraint, with evidence

Key commands: `airshed status | gate | ablation | rolling | grap | loso |
coupling | features | episodes | archive`, and
`airshed ingest cams|meteo|cpcb|upwind|live|history|metar|fires|expand-stations`.

Demo: `.venv/Scripts/python.exe -m uvicorn airshed.api.app:app --port 8018`

**109 tests pass.**
