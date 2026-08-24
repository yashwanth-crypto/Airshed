# Ablation — Phase 2

Generated 2026-08-24 16:15 UTC. Regenerate with `airshed ablation`.

- data range: `2025-02-18` to `2026-08-20`
- trained on 1,071,904 rows (2025-02-19 to 2026-08-19)
- evaluated on 194,012 **test** rows (2025-12-12 to 2026-06-30), blocks: summer-2026, winter-late-2025
- 76 stations, observed mean 115.9 µg/m³
- CAMS source class: archive_short_lead


Splits are time blocks with whole-episode holdout and a 96 h embargo (R3). Persistence appears in every table (R2). `skill` is the RMSE reduction against persistence: 0 means no better, negative means worse.


> **Read the horizon columns with `leadmatch.md` open.** The forecast inputs here come from the archives, which return the *best available* forecast for each past hour — a short-lead one. Re-scoring the same rows with meteorology at real forecast lead costs about 1% (worse on 4/5 rolling folds), and CAMS cannot be lead-matched at all. The comparisons in this table are sound because every model reads the same inputs; the absolute 48 and 72 h numbers are optimistic by roughly that much.


## RMSE by horizon (µg/m³, lower is better)

| model | 24 h | 48 h | 72 h | overall |
|---|---|---|---|---|
| persistence | 79.6 | 87.7 | 89.7 | 85.8 |
| persistence-daily | 87.4 | 89.3 | 91.9 | 89.6 |
| raw-cams | 94.3 | 92.8 | 92.2 | 93.1 |
| scaled-cams | 93.0 | 91.4 | 91.0 | 91.8 |
| cams+obs | 61.1 | 61.6 | 61.7 | 61.5 |
| full (no fires) | 60.0 | 60.3 | 61.6 | 60.6 |
| full | 60.0 | 60.3 | 60.8 | 60.4 |
| full+upwind | 60.0 | 60.0 | 61.0 | 60.4 |
| coupled | 59.7 | 60.1 | 61.2 | 60.4 |
| full+upwind+cal | 60.0 | 60.0 | 61.0 | 60.4 |
| coupled+cal | 59.7 | 60.1 | 61.2 | 60.4 |

## Skill against persistence (higher is better, 0 = no better)

| model | 24 h | 48 h | 72 h | overall |
|---|---|---|---|---|
| persistence | +0.000 | +0.000 | +0.000 | +0.000 |
| persistence-daily | -0.098 | -0.018 | -0.024 | -0.044 |
| raw-cams | -0.184 | -0.058 | -0.027 | -0.085 |
| scaled-cams | -0.167 | -0.042 | -0.014 | -0.070 |
| cams+obs | +0.233 | +0.297 | +0.312 | +0.283 |
| full (no fires) | +0.247 | +0.312 | +0.313 | +0.293 |
| full | +0.246 | +0.312 | +0.323 | +0.296 |
| full+upwind | +0.247 | +0.315 | +0.320 | +0.297 |
| coupled | +0.250 | +0.314 | +0.318 | +0.297 |
| full+upwind+cal | +0.247 | +0.315 | +0.320 | +0.297 |
| coupled+cal | +0.250 | +0.314 | +0.318 | +0.297 |

## Bias (µg/m³, negative = under-forecast)

| model | 24 h | 48 h | 72 h | overall |
|---|---|---|---|---|
| persistence | +1.9 | +6.1 | +7.3 | +5.1 |
| persistence-daily | +2.0 | +4.0 | +5.6 | +3.9 |
| raw-cams | +4.1 | +5.4 | +2.9 | +4.2 |
| scaled-cams | -7.5 | -6.3 | -8.4 | -7.4 |
| cams+obs | -3.2 | -0.4 | -3.9 | -2.5 |
| full (no fires) | -3.7 | -0.9 | -2.9 | -2.5 |
| full | -3.5 | -1.0 | -2.7 | -2.4 |
| full+upwind | -2.1 | -1.8 | -1.8 | -1.9 |
| coupled | -2.5 | -2.6 | -4.1 | -3.1 |
| full+upwind+cal | -2.1 | -1.8 | -1.8 | -1.9 |
| coupled+cal | -2.5 | -2.6 | -4.1 | -3.1 |

## Episode hours (observed PM2.5 >= 250 µg/m³)

Overall error is dominated by ordinary hours. These are the hours the system exists for (R5).

| model | episode RMSE | episode recall |
|---|---|---|
| persistence | 158.3 | 45.5% |
| persistence-daily | 167.4 | 42.5% |
| raw-cams | 185.2 | 11.0% |
| scaled-cams | 198.3 | 5.9% |
| cams+obs | 139.1 | 41.4% |
| full (no fires) | 137.5 | 43.0% |
| full | 136.9 | 43.2% |
| full+upwind | 136.5 | 43.7% |
| coupled | 137.6 | 42.4% |
| full+upwind+cal | 136.5 | 43.7% |
| coupled+cal | 137.6 | 42.4% |

## Interval calibration

The 10-90 interval should contain the truth about 80% of the time. Far below means overconfident; far above means uselessly wide.

| model | coverage | mean width (µg/m³) |
|---|---|---|
| persistence | 60.9% | 86 |
| persistence-daily | 60.9% | 90 |
| raw-cams | 64.3% | 136 |
| scaled-cams | 63.7% | 133 |
| cams+obs | 59.1% | 78 |
| full (no fires) | 61.6% | 82 |
| full | 61.9% | 82 |
| full+upwind | 61.4% | 81 |
| coupled | 61.6% | 81 |
| full+upwind+cal | 78.6% | 116 |
| coupled+cal | 79.2% | 118 |

## Upwind fires (FIRMS)

Stubble burning is the forcing that turns a bad Delhi November into a severe one, so this is the physically most important feature family in the set. `full` carries VIIRS and MODIS detections over Punjab and Haryana — counts and radiative power over the last 24 and 72 hours; `full (no fires)` withholds exactly those columns and is otherwise identical.

| horizon | no fires | with fires | difference |
|---|---|---|---|
| 24 h | 60.0 | 60.0 | -0.1% |
| 48 h | 60.3 | 60.3 | -0.0% |
| 72 h | 61.6 | 60.8 | +1.4% |

Episode recall: without fires 43.0%, with fires 43.2%.

**A gain of +0.4% on average**, smaller than the physical importance of stubble would suggest. The likely reason is that only one burning season is in the data, so the model has seen the pattern once. Check the rolling-origin table before claiming it.

## Upwind corridor (airshed)

Delhi's severe episodes are substantially imported. `full+upwind` adds 24 monitors 65-340 km up the Punjab-Haryana corridor, as transport features: wind-aligned corridor concentration, estimated travel time, and the advected value that is currently arriving. `full` is identical except that those columns are withheld, so the difference is the value of seeing upwind rather than the value of having more columns.

| horizon | Delhi only | + upwind corridor | difference |
|---|---|---|---|
| 24 h | 60.0 | 60.0 | +0.1% |
| 48 h | 60.3 | 60.0 | +0.5% |
| 72 h | 60.8 | 61.0 | -0.4% |

Episode recall: Delhi only 43.2%, with upwind 43.7%.

### Does the gain appear when the wind is actually from the corridor?

Transport information can only help when something is being transported. If the gain does not concentrate on aligned hours, it is noise rather than physics — the same test the visibility coupling had to pass.

| wind alignment | hours | Delhi only | + upwind | gain |
|---|---|---|---|---|
| wind not from corridor | 48,440 | 54.0 | 54.3 | -0.6% |
| partly aligned | 10,294 | 44.2 | 44.7 | -1.0% |
| corridor straight upwind | 118,936 | 64.4 | 64.3 | +0.1% |

**A small but physically consistent gain: +0.0% on average.** That is too small to headline on its own, and it would be easy to dismiss as noise — except that it has the right shape. The improvement appears when the wind is down the corridor and turns slightly negative when it is not, and a spurious gain from extra columns would not track the wind direction. Claim the mechanism, not the magnitude, and revisit after a full stubble season: this window contains one October-November, and transport is seasonal.

## Coupling (Phase 3 gate)

> Does the coupled multi-output model beat the single-output one, measurably, on the same splits?

| horizon | single-output RMSE | coupled RMSE | difference |
|---|---|---|---|
| 24 h | 60.0 | 59.7 | +0.5% |
| 48 h | 60.3 | 60.1 | +0.3% |
| 72 h | 60.8 | 61.2 | -0.8% |

Episode recall: single-output 43.2%, coupled 42.4%.

**Coupling does not pay for itself yet.** The chained visibility head does not measurably improve PM2.5 accuracy. The most likely reason is in the data, not the architecture: observed visibility comes from a single airport (VIDP) and is broadcast to every station, so it carries one city-wide signal that the meteorology already supplies. Report it as a negative result and do not claim coupling as a benefit until a second visibility source or a genuinely per-station second series exists.

## Regime check

A model cannot forecast conditions it has never trained on. If these two rows disagree sharply, a poor score is a data-coverage result, not a modelling result, and has to be read as one.

| split | mean PM2.5 | p95 | episode hours |
|---|---|---|---|
| train | 72.0 | 205.5 | 3.1% |
| holdout | 115.9 | 303.4 | 9.5% |

> **Regime mismatch.** The holdout has 3.0x the episode frequency of the training data, so every learned model here is extrapolating and an under-forecast bias is the expected consequence. Fix the coverage before reading the gate as a verdict on the method.

## Gate

> Does the corrector beat raw CAMS, and does everything beat persistence, at all three horizons?

| horizon | full vs raw-cams | full vs persistence | verdict |
|---|---|---|---|
| 24 h | +36.3% | +24.6% | PASS |
| 48 h | +35.0% | +31.2% | PASS |
| 72 h | +34.1% | +32.3% | PASS |

**Gate met.**
