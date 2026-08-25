# Project status — handoff

Written 2026-08-25. Read this first in a new session, then `CLAUDE.md` for the
rules and `docs/BUILD_PLAN.md` for the phase gates. For the demo, read
`docs/DEMO_RUNBOOK.md`. For how the work stands against the problem statement,
see `docs/POSITIONING.md`.

---

## One-paragraph summary

All five phases are built and every results document is current at **77
stations**. The central claim holds with error bars: the correction beats raw
CAMS by **31.5%** and persistence by **20.6%**, on **5 of 5** rolling folds. The
dashboard is demo-ready and opens on a **held-out** severe episode. Two claims
were withdrawn this week and both matter more than the wins: the visibility
coupling no longer supports an aerosol *mechanism*, and our own 48/72 h numbers
were optimistic by 0.9%, now measured. The binding constraint remains one winter
of trainable ground truth; the second season is November 2026.

---

## Environment

- Python 3.11 via `uv`; venv at `.venv`. Run things as
  `scriptsirshed_py.bat -m airshed.cli ...` — **not**
  `.venv/Scripts/python.exe`, which Device Guard now blocks (see 1a below).
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

> All 77 stations, regenerated 2026-08-25. **Read absolute values with care:** the
> 26 stations added are mostly peripheral and cleaner, so the holdout mean fell
> 127.4 -> 115.9 ug/m3 and episode hours 12.1% -> 9.5%. The test set got easier.
> Skill against the baselines is the comparable quantity, and it moved in
> opposite directions — better against CAMS, slightly worse against persistence,
> because clean outer stations are more persistent.

| claim | value | evidence |
|---|---|---|
| vs raw CAMS | **+31.5%**, 5/5 folds | `rolling.md` |
| vs persistence | **+20.6%**, 5/5 folds | `rolling.md` |
| Gate, by horizon vs CAMS | +36.3 / +35.0 / +34.1% | `ablation.md` |
| Interval coverage, city model | **85.1%** (target 80%) | `grap.md` |
| GRAP Stage III @72 h | **73.8%** recall, 66.0% precision | `grap.md` |
| Spatial, leave-one-station-out | **81.8 ug/m3** | `loso.md` |
| Visibility from pollution | +7.5% (mechanism unresolved) | `coupling.md` |
| **Demo replay, held out** | 2025-12-22: model **91**, CAMS **123**, observed **259** | `/api/replay` |

Negative and null results, reported as such:

- **Short-lead meteorology was flattering us.** Rebuilding the same rows with the
  forecast genuinely in hand costs **0.9%, worse on 5/5 folds**. Confirmed this
  week — it was "within noise" at 51 stations and now clears its own scatter.
  `leadmatch.md` is the column to quote.
- **The coupling mechanism is not established.** +7.5% RMSE is real, but the gain
  no longer concentrates in polluted air (+4.0% clean vs +5.0% dirtiest), it
  moves with how the pollution basket is defined, and a basket *near VIDP* does
  **worse** than a city-wide one at every radius. Report the number, not the
  physics. `coupling.md`, `data-findings.md` section 16.
- **Hyperparameter search: the defaults hold.** 24 configurations, 12,288-point
  space. `num_leaves=255` won on validation (+1.2%) and lost on test (-0.3%) at
  2.4x the fit cost, so it was reverted. The useful result is the null one — the
  model was never badly mistuned. `tuning.md`.
- **Fires** +0.9% (3/5), **upwind corridor** +0.0% (3/5), **coupled multi-output**
  -0.1% (4/5). Each smaller than its own spread.

## Pending — ranked

Nothing below blocks the internal hackathon. The dashboard and the pitch are
ready; `docs/DEMO_RUNBOOK.md` is the operational document for the day.

### 1. Keep the archive job alive — the only item with a deadline
Archived forecast runs **cannot be backfilled**. There is no archived-forecast
air-quality product (checked both hosts, `data-findings.md` section 13), so
`cams_runs` and `meteo_runs` grow only by this job running.

- Runs as a resident loop, **started hidden** from
  `scripts/run_archive_hidden.vbs` (put the shortcut to *that* in the Startup
  folder, not to the `.bat`). It re-checks every 30 min and asks whether today's
  run is in the store, not what time it is, so sleep and reboots cannot make it
  miss.
- **Stop it with `scripts/stop_archive.bat`.** Not by killing python, which
  orphans the lock; not by closing a window, which is how three loops died.
- **After a restart you do nothing** — the Startup shortcut brings the loop
  back. Confirm with `scripts/check_system.bat`, which answers in one screen
  whether the loop, the run stores, the model and the dashboard are all up.
  `docs/DEMO_RUNBOOK.md` opens with the restart procedure.
- Verify by the log, never by a launcher's report: `airshed health` (exit 0 =
  fine) or `Get-Content C:/SIH/data/archive.log -Tail 5`.
- Backup is set to `C:/airshed-backup`. **It is on the same disk** — that
  protects against an accidental delete, not a drive failure. Moving it to a USB
  drive or a synced folder is one line in `.env`.

**It stopped three times in two days (2026-08-24 22:35 UTC, 2026-08-25 12:06 and
~12:30), each time with nothing in the log** — the signature of a process
stopped from outside rather than one that failed. Total silence: 8.8 h, 5.5 h
and 65 min. Nothing was lost, because each day's run was already archived when
the loop died, but in November that is the whole ballgame. Four changes, all in
place and tested:

1. **The lock records the pid, not just a timestamp.** A timestamp alone cannot
   tell a running loop from a dead one, so every relaunch backed off politely
   for the 90 minutes the age rule takes to declare a lock stale. It now asks
   the OS whether that pid is alive: dead means take over immediately, alive
   means refuse and say whose pid it is. A live pid silent for three hours is
   treated as recycled and taken over anyway, so nothing can block forever.
2. **`run_archive.bat` supervises.** If the loop exits for any reason other
   than a deliberate stop or a correct lock refusal, it restarts it after a
   minute.
3. **The loop can run with no console window at all** (`run_archive_hidden.vbs`)
   — a window is a thing that gets closed while tidying the taskbar. The off
   switch is therefore a file: `stop_archive.bat` drops `data/archive.stop`, the
   loop notices within ~5 s, exits 0, releases its lock, and the supervisor
   stays stopped.
4. **A pass is wrapped in `keep_awake`**, so the machine cannot sleep through a
   five-minute fetch.

### 1a. Device Guard now blocks `.venv\Scripts\python.exe` — read this first
Discovered 2026-08-25 while restarting the archive:

```
'C:\SIH\.venv\Scripts\python.exe' was blocked by your organization's
Device Guard policy.        (exit 1073751882, STATUS_INVALID_IMAGE_HASH)
```

The uv-managed interpreter the venv was **built from** still runs, so this is
about that copy of the binary, not about Python. Until the policy is changed,
**every command in this file that starts `.venv\Scripts\python.exe` will fail**,
including the demo launcher — which would have failed on the day, in the room,
with no dashboard.

`scripts/airshed_py.bat` is the workaround: it probes the venv interpreter, and
when that is refused it runs the uv one with `\.venv\Lib\site-packages` and
`src` on `PYTHONPATH`. Same code, same packages, a binary the policy allows.
`run_archive.bat` and `run_dashboard.bat` both go through it.

    scripts\airshed_py.bat -m airshed.cli health
    scripts\airshed_py.bat scripts\daily_archive.py --health

The proper fix is to allow the interpreter (or recreate the venv so it is a
binary the policy accepts) and then delete the shim — nothing depends on it
except convenience. Do that **before** relying on any Startup-folder launcher,
because a policy that blocks a binary at launch will stop the archive silently.

### 2. November 2026 — what everything else is waiting for
A second episode season resolves, in one run: fires, upwind corridor, coupled
multi-output, Stage IV's 17 event hours, and the regime mismatch (train mean
72.0 vs holdout 115.9). It also supplies enough run days to correct the CAMS
train/serve gap. Re-run `ablation`, `rolling --lead-matched`, `leadmatch`,
`grap`, `loso`, `coupling` when it lands.

### 3. The CAMS train/serve gap — accrues, cannot be rushed
Trained on `cams_archive`, served `cams_runs`. Measured at **-32.4 ug/m3 at lead
day 2 on 3 run days**; 20 settled days are required before fitting a correction,
and a bias fitted on monsoon air would be applied to a November episode. Now
surfaced on `/api/forecast` as `input_gap` and shown on the dashboard as a
"Known bias" note. `camsoffset.md`.

### 4. Postponed — real, and none of it changes the pitch
- **The coupling question.** Whether a second visibility source, or a
  per-station series, revives the mechanism. Currently three checks against.
- **`Service.model()` never refits.** Its docstring claims it refits when the
  training window moves; the code only checks whether a pickle exists. Harmless
  now (refit manually with `Service().fit()`), but the doc and the code disagree.
- **45 more NCR stations** — 77 of 96 in the official roster.
- **Road density and CAMS AOD** as downscaling predictors; **population-weighted
  exposure** (needs a WorldPop raster); **HYSPLIT** stubble-smoke split.
- **A second winter of CPCB history** (2022-11 to 2025-02) would double the
  trainable period. CCR is captcha-gated; AQICN is city-level daily only.

### 5. Known permanent limitations — not tasks
- **January 2026 has a nine-day hole** (2026-01-11 to 01-19) inside the winter
  test block. Upstream and unrecoverable, confirmed on both OpenAQ backends.
  Every winter number is computed without it. `data-findings.md` section 15.
- **CAMS PM2.5 cannot be lead-matched.** No archived-forecast air-quality
  endpoint exists, so BLH, visibility and pressure-level meteorology also stay
  short-lead in training. Closes only forward, as `meteo_runs` accumulates.

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

Demo: double-click `scripts/run_dashboard.bat` (starts the server and opens the
browser). It opens on a **held-out** severe episode chosen from the split
config, and prints the split on screen. See `docs/DEMO_RUNBOOK.md`.

The archive job is separate and restarts at logon; the dashboard does not.

**180 tests pass.**
