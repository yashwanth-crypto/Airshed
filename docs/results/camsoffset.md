# CAMS train/serve gap

Generated 2026-08-29 05:34 UTC. Regenerate with `airshed camsoffset`.

The corrector is trained on `cams_archive` and served `cams_runs`. This measures how far apart those two are for the same station and hour — the distribution gap the live forecast actually runs on.

**It cannot be closed retrospectively.** There is no archived-forecast air-quality product: `previous-runs-api.open-meteo.com/v1/air-quality` returns 404 and `pm2_5_previous_dayN` returns 0/48 non-null on the air-quality endpoint. The only evidence is the overlap below, which grows by one day each time the daily archive job runs, and by nothing at all on the days it does not.

- runs held: **7** (2026-08-23 to 2026-08-29), of which **3** are settled
- a comparison counts as settled once the archive value is 4 days old, because a recent archive hour can still be revised and that revision is not what we are trying to measure

## Live run minus archive, by lead day

Negative bias means the served input sits **below** the input the model was fitted on, which pushes the forecast low. The interval is a bootstrap over whole run days — resampling rows instead would treat ~2,400 correlated hours as independent and report an interval about fifty times too narrow. It is left blank below 5 run days, because resampling a handful of days with replacement produces an interval that looks tight and means nothing.

| lead day | true lead | rows | run days | archive mean | run mean | bias | 95% CI (clustered) | RMSE |
|---|---|---|---|---|---|---|---|---|
| 0 | 0–23 h | 11,688 | 7 | 87.2 | 93.9 | **+6.6** | -1.9 to +14.1 | 30.2 |
| 1 | 24–47 h | 9,840 | 6 | 91.7 | 102.6 | **+10.9** | +2.2 to +18.6 | 33.6 |
| 2 | 48–71 h | 7,992 | 5 | 96.7 | 94.4 | **-2.2** | -6.7 to +1.8 | 32.1 |
| 3 | 72–95 h | 6,144 | 4 | 103.0 | 89.7 | **-13.4** | — *(needs ≥5 run days)* | 41.8 |
| 4 | 96–119 h | 3,449 | 3 | 103.4 | 81.0 | **-22.5** | — *(needs ≥5 run days)* | 44.4 |

## Verdict

> **Not enough evidence to correct — 3 settled run day(s), 20 needed.**

The gap is visible and it is in the direction that matters: the served input runs 22.5 µg/m³ below the trained input at lead day 4. But a handful of run days from one season cannot tell a systematic offset from one unusual week — which is why the interval column is mostly blank rather than reassuringly narrow.

**Serving is therefore left uncorrected, on purpose.** Applying a bias fitted on this much data would replace a known, measured, reported gap with an unknown one — and it would be fitted on monsoon air and applied to a November episode, which is precisely the regime where it matters most and generalises least.

What to do instead: keep the daily job alive. Every run it archives adds one independent observation to the table above, and nothing else does. `airshed camsoffset` re-reads the whole overlap each time, so no separate bookkeeping has to be maintained.
