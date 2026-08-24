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
- Git repo initialised; first commit `f5890f2` holds all 83 source, test and
  doc files. Identity is set at repo level to `yashwanth-crypto
  <nadhahari44@gmail.com>` per `CLAUDE.md`. **`data/` is gitignored** and
  verified excluded, as are `.env` and both API keys.
- Secrets in `.env` (gitignored): `OPENAQ_API_KEY` and `FIRMS_MAP_KEY` are
  both set and working.
- Set `PYTHONIOENCODING=utf-8` before CLI calls or the Windows console mangles
  the output tables.

---

## Data on disk

| dataset | days | range | rows |
|---|---|---|---|
| `cpcb` | 2551 | 2014-12-31 -> 2026-08-24 | 1,537,930 |
| `cpcb_upwind` | 537 | 2025-02-18 -> 2026-08-19 | 195,188 |
| `cams_archive` | 739 | 2024-06-01 -> 2026-08-24 | 1,365,672 |
| `meteo_archive` | 709 | 2024-09-15 -> 2026-08-24 | 1,209,816 |
| `meteo_leadmatched` | 553 | 2025-02-18 -> 2026-08-24 | 2,704,536 |
| `metar` | 739 | 2024-06-01 -> 2026-08-24 | 18,904 |
| `fires` | 274 | 2025-09-15 -> 2026-08-23 | 23,544 |
| `cams_runs` | **2** | 2026-08-23 -> 2026-08-24 | 12,240 |
| `meteo_runs` | **2** | 2026-08-23 -> 2026-08-24 | 12,240 |

**Station count is 77 as of 2026-08-24** (was 51). Every dataset above carries
all 77 except `meteo_leadmatched`, which has them to 2026-02-12 and the original
51 thereafter — the backfill hit Open-Meteo's daily quota. Finish it with
`scripts/finish_station_expansion.py`.

**The `cpcb` row is misleading on its own.** It spans 2015-2026 only because
the Kaggle archive (2015-2020) was loaded. The **trainable** period is the
intersection of ground truth, CAMS and meteorology, which is **2025-02-18
onward** — see `docs/notes/data-findings.md` section 11 for the era table.

---

## Completed

**Phase 1 — data spine.** Gate met. `airshed gate` rebuilds a week of every
feature with sockets physically blocked, so "reads from cache" is verified, not
claimed. **77** NCR stations resolved to OpenAQ ids with authoritative coordinates
(51 until the 2026-08-24 expansion), plus 24 upwind corridor stations held
separately. See `data-findings.md` section 14.

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
- **Lead-matched meteorology** (`airshed ingest meteo-leadmatched`,
  `airshed leadmatch`) — training meteorology at the lead it is actually used
  at, from the Open-Meteo Previous Runs API, instead of the short-lead archive
  value. See `docs/results/leadmatch.md` and `data-findings.md` section 12.

---

## Headline numbers (all from held-out data)

> **STALE as of 2026-08-24.** Every number below and every file in
> `docs/results/` was computed on **51** stations. The network is now **77**, so
> the evaluation row set has changed and these are not comparable to a rerun —
> the GRAP city average in particular is now scoped to Delhi's 44 stations
> rather than all of them, which is a correction, not a tweak. Finish
> `scripts/finish_station_expansion.py`, then regenerate `ablation`, `rolling
> --lead-matched`, `leadmatch`, `grap` and `loso` before quoting anything.


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
- **Lead-matched meteorology costs about 1%** (-0.9% +/- 1.0%, worse on 4/5
  folds). Training meteorology was short-lead, so the horizon columns above were
  mildly optimistic. The direction is consistent, the magnitude is inside the
  fold scatter, and the headline claim is unaffected: the correction still beats
  both baselines on every fold with either input. `leadmatch.md`, `rolling.md`.
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

### 2. Keep the archive running — hardened 2026-08-24
This is the highest-priority standing item, because everything below is blocked
on it and it is the only item with a deadline. Stubble season starts within
weeks; archived forecast runs cannot be recovered afterwards.

**It now runs as a resident loop, not once per logon.** `scripts/run_archive.bat`
(shortcut in `%APPDATA%\Microsoft\Windows\Start Menu\Programs\Startup`,
minimised) starts `daily_archive.py --loop`, which re-checks every 30 minutes.
The check is a question about *state* — "is today's run in the store yet?" — not
about the clock, so sleep, hibernation and a closed lid cannot make it miss.
The previous run-once launcher archived nothing at all on a laptop left on for
four days.

- A lock file (`data/archive.lock`) stops a second copy; a lock older than
  90 minutes is taken over, so a process killed by a closed lid cannot block the
  archive permanently.
- The loop swallows and logs a failing pass rather than dying on it.
- Windows Task Scheduler was tried first and refused this account outright
  (`LogonType=InteractiveToken` rejecting a Microsoft-account session). `S4U`
  would fix it; the loop is simpler and needs no elevation.

**Off-machine backup — set this.** `AIRSHED_BACKUP_DIR` mirrors `cams_runs` and
`meteo_runs` after every successful pass. They are the only datasets here that
cannot be re-fetched, and they are **~250 KB/day, about 90 MB/year**. Currently
**unset**, which means one disk failure in December costs the whole winter. It
is opt-in by design — copying into a synced folder sends the data off the
machine, which is the operator's call — but it should be made.

**Checking it.** Never trust a launcher's own report; `schtasks /run` printed
"SUCCESS: Attempted" three times while nothing ran.

    airshed health                          # yes/no, exits non-zero
    Get-Content C:\SIH\datarchive.log -Tail 5

`airshed health` fails with exit 2 when a run store is stale (>36 h) or empty,
and exit 1 when there is no off-machine backup. The dashboard shows a red
"Archive stalled" banner on the same condition, and the daily pass logs the
CAMS-offset run count, so a stalled job also shows up as a number that stops
moving.

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

### 4. Close the CAMS train/serve gap — now the larger of the two
The lead-matched work settled the meteorology half. What it also showed is that
the corrector barely leans on the lead-sensitive meteorology at all: the inputs
moved a great deal between lead day 1 and 3 (wind direction RMSE 86 -> 97
degrees, temperature 1.60 -> 2.32 K) while the model moved about a percent. Its
skill comes from CAMS and observation history, so **the remaining optimism sits
almost entirely in short-lead CAMS**.

**It cannot be backfilled.** Confirmed against both hosts: the previous-runs API
has no air-quality path (404) and `pm2_5_previous_dayN` is empty on the
air-quality endpoint. There is no archived-forecast air-quality product.

**Measurement is built and running.** `airshed camsoffset` ->
`docs/results/camsoffset.md`, re-measured by the daily job, surfaced on
`/api/forecast` as `input_gap` and printed beside the live forecast in the UI.
Current state, on the first two archived runs:

| lead day | archive mean | run mean | bias | RMSE |
|---|---|---|---|---|
| 0 (0-23 h) | 70.0 | 53.9 | **-16.2** | 25.4 |
| 1 (24-47 h) | 79.9 | 59.3 | **-20.6** | 22.9 |

Same 21 cells on both sides, so it is lead and not geometry, and the direction
is the one that costs — the live forecast is being handed an input below what it
was fitted against and will run low.

**No correction is applied, deliberately.** Gated at 20 settled run days. Two
days is two observations, not 2,448 — the rows within a day share one weather
situation, so intervals come from a bootstrap over whole run days and are
withheld below five. More importantly a bias fitted on monsoon air at 70 ug/m3
would be applied to a November episode at 400. See `data-findings.md` section 13.

**What actually moves this forward:** the daily job surviving, and a winter in
the run store. Nothing else does.

### 5. Finish the station expansion, then regenerate everything
`meteo_leadmatched` is short from 2026-02-13 onward for the 26 new stations;
`scripts/finish_station_expansion.py` completes it in one pass once Open-Meteo's
daily allowance resets. Then rerun the five results commands — until that
happens `docs/results/` describes a 51-station network that no longer exists.

Expect the numbers to move for reasons that are nothing to do with modelling:
26 more stations, many of them peripheral and several with only months of
history, plus a GRAP city average that is now Delhi-only. Report the station-set
change alongside the new numbers, not just the new numbers.

### 6. Smaller, all unblocked
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
      eval/       metrics ablation rolling leadmatch camsoffset loso visibility grap_eval
      api/        app service static/index.html
      grap.py attribution.py store.py config.py net.py env.py verify.py keepawake.py
    docs/results/   ablation rolling leadmatch camsoffset loso grap coupling (+ csv, png)
    docs/notes/     data-findings.md   <- every measured constraint, with evidence

Key commands: `airshed status | health | gate | ablation | rolling | leadmatch |
camsoffset | grap | loso | coupling | features | episodes | archive`, and `airshed ingest
cams|meteo|meteo-leadmatched|cpcb|upwind|live|history|metar|fires|expand-stations`.

`airshed rolling --lead-matched` adds the paired fold comparison against
short-lead meteorology.

Demo: `.venv/Scripts/python.exe -m uvicorn airshed.api.app:app --port 8018`

**167 tests pass.**
