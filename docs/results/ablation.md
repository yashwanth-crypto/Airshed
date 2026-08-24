# Ablation — Phase 2

Generated 2026-08-24 12:17 UTC. Regenerate with `airshed ablation`.

- data range: `2025-02-18` to `2026-08-19`
- trained on 806,438 rows (2025-02-19 to 2026-08-18)
- evaluated on 133,729 **test** rows (2025-12-12 to 2026-06-30), blocks: summer-2026, winter-late-2025
- 51 stations, observed mean 127.4 µg/m³
- CAMS source class: archive_short_lead


Splits are time blocks with whole-episode holdout and a 96 h embargo (R3). Persistence appears in every table (R2). `skill` is the RMSE reduction against persistence: 0 means no better, negative means worse.


> **Read the horizon columns with `leadmatch.md` open.** The forecast inputs here come from the archives, which return the *best available* forecast for each past hour — a short-lead one. Re-scoring the same rows with meteorology at real forecast lead costs about 1% (worse on 4/5 rolling folds), and CAMS cannot be lead-matched at all. The comparisons in this table are sound because every model reads the same inputs; the absolute 48 and 72 h numbers are optimistic by roughly that much.


## RMSE by horizon (µg/m³, lower is better)

| model | 24 h | 48 h | 72 h | overall |
|---|---|---|---|---|
| persistence | 84.7 | 93.9 | 96.1 | 91.8 |
| persistence-daily | 93.7 | 95.8 | 99.4 | 96.3 |
| raw-cams | 95.2 | 93.4 | 93.1 | 93.9 |
| scaled-cams | 95.3 | 93.5 | 93.3 | 94.1 |
| cams+obs | 63.7 | 65.2 | 64.6 | 64.5 |
| full (no fires) | 63.3 | 63.2 | 64.7 | 63.7 |
| full | 62.6 | 62.9 | 63.8 | 63.1 |
| full+upwind | 62.5 | 63.2 | 64.2 | 63.3 |
| coupled | 63.0 | 63.0 | 64.1 | 63.3 |
| full+upwind+cal | 62.5 | 63.2 | 64.2 | 63.3 |
| coupled+cal | 63.0 | 63.0 | 64.1 | 63.3 |

## Skill against persistence (higher is better, 0 = no better)

| model | 24 h | 48 h | 72 h | overall |
|---|---|---|---|---|
| persistence | +0.000 | +0.000 | +0.000 | +0.000 |
| persistence-daily | -0.106 | -0.020 | -0.035 | -0.050 |
| raw-cams | -0.123 | +0.005 | +0.030 | -0.024 |
| scaled-cams | -0.125 | +0.005 | +0.029 | -0.025 |
| cams+obs | +0.249 | +0.306 | +0.328 | +0.297 |
| full (no fires) | +0.253 | +0.327 | +0.327 | +0.305 |
| full | +0.261 | +0.331 | +0.336 | +0.312 |
| full+upwind | +0.262 | +0.327 | +0.331 | +0.310 |
| coupled | +0.257 | +0.330 | +0.333 | +0.310 |
| full+upwind+cal | +0.262 | +0.327 | +0.331 | +0.310 |
| coupled+cal | +0.257 | +0.330 | +0.333 | +0.310 |

## Bias (µg/m³, negative = under-forecast)

| model | 24 h | 48 h | 72 h | overall |
|---|---|---|---|---|
| persistence | +2.0 | +7.5 | +8.7 | +6.1 |
| persistence-daily | +1.8 | +4.2 | +6.2 | +4.1 |
| raw-cams | -8.4 | -6.4 | -8.9 | -7.9 |
| scaled-cams | -12.8 | -10.8 | -13.2 | -12.3 |
| cams+obs | -3.4 | +0.3 | -4.1 | -2.4 |
| full (no fires) | -1.5 | +0.8 | -3.1 | -1.3 |
| full | +0.3 | +0.5 | -3.7 | -1.0 |
| full+upwind | -1.9 | +0.6 | -3.4 | -1.6 |
| coupled | -2.6 | -0.2 | -3.7 | -2.2 |
| full+upwind+cal | -1.9 | +0.6 | -3.4 | -1.6 |
| coupled+cal | -2.6 | -0.2 | -3.7 | -2.2 |

## Episode hours (observed PM2.5 >= 250 µg/m³)

Overall error is dominated by ordinary hours. These are the hours the system exists for (R5).

| model | episode RMSE | episode recall |
|---|---|---|
| persistence | 153.5 | 48.0% |
| persistence-daily | 163.5 | 44.6% |
| raw-cams | 184.9 | 10.6% |
| scaled-cams | 189.8 | 8.8% |
| cams+obs | 130.9 | 45.2% |
| full (no fires) | 127.9 | 48.1% |
| full | 127.3 | 47.6% |
| full+upwind | 128.0 | 47.5% |
| coupled | 128.6 | 46.4% |
| full+upwind+cal | 128.0 | 47.5% |
| coupled+cal | 128.6 | 46.4% |

## Interval calibration

The 10-90 interval should contain the truth about 80% of the time. Far below means overconfident; far above means uselessly wide.

| model | coverage | mean width (µg/m³) |
|---|---|---|
| persistence | 57.8% | 89 |
| persistence-daily | 57.6% | 92 |
| raw-cams | 61.9% | 135 |
| scaled-cams | 61.7% | 135 |
| cams+obs | 55.5% | 80 |
| full (no fires) | 58.4% | 84 |
| full | 58.9% | 85 |
| full+upwind | 59.4% | 86 |
| coupled | 58.6% | 83 |
| full+upwind+cal | 80.0% | 134 |
| coupled+cal | 79.4% | 129 |

## Upwind fires (FIRMS)

Stubble burning is the forcing that turns a bad Delhi November into a severe one, so this is the physically most important feature family in the set. `full` carries VIIRS and MODIS detections over Punjab and Haryana — counts and radiative power over the last 24 and 72 hours; `full (no fires)` withholds exactly those columns and is otherwise identical.

| horizon | no fires | with fires | difference |
|---|---|---|---|
| 24 h | 63.3 | 62.6 | +1.0% |
| 48 h | 63.2 | 62.9 | +0.5% |
| 72 h | 64.7 | 63.8 | +1.4% |

Episode recall: without fires 48.1%, with fires 47.6%.

**A gain of +1.0% on average**, smaller than the physical importance of stubble would suggest. The likely reason is that only one burning season is in the data, so the model has seen the pattern once. Check the rolling-origin table before claiming it.

## Upwind corridor (airshed)

Delhi's severe episodes are substantially imported. `full+upwind` adds 24 monitors 65-340 km up the Punjab-Haryana corridor, as transport features: wind-aligned corridor concentration, estimated travel time, and the advected value that is currently arriving. `full` is identical except that those columns are withheld, so the difference is the value of seeing upwind rather than the value of having more columns.

| horizon | Delhi only | + upwind corridor | difference |
|---|---|---|---|
| 24 h | 62.6 | 62.5 | +0.2% |
| 48 h | 62.9 | 63.2 | -0.6% |
| 72 h | 63.8 | 64.2 | -0.7% |

Episode recall: Delhi only 47.6%, with upwind 47.5%.

### Does the gain appear when the wind is actually from the corridor?

Transport information can only help when something is being transported. If the gain does not concentrate on aligned hours, it is noise rather than physics — the same test the visibility coupling had to pass.

| wind alignment | hours | Delhi only | + upwind | gain |
|---|---|---|---|---|
| wind not from corridor | 33,399 | 56.4 | 56.4 | +0.0% |
| partly aligned | 6,373 | 44.0 | 44.1 | -0.2% |
| corridor straight upwind | 82,207 | 67.7 | 68.1 | -0.5% |

**The upwind corridor does not yet pay for itself.** Report it as a negative result. The likeliest causes are that the corridor signal is already implicit in the forecast wind field, or that transport timing needs a trajectory model rather than a distance-over-wind-speed estimate.

## Coupling (Phase 3 gate)

> Does the coupled multi-output model beat the single-output one, measurably, on the same splits?

| horizon | single-output RMSE | coupled RMSE | difference |
|---|---|---|---|
| 24 h | 62.6 | 63.0 | -0.5% |
| 48 h | 62.9 | 63.0 | -0.1% |
| 72 h | 63.8 | 64.1 | -0.4% |

Episode recall: single-output 47.6%, coupled 46.4%.

**Coupling does not pay for itself yet.** The chained visibility head does not measurably improve PM2.5 accuracy. The most likely reason is in the data, not the architecture: observed visibility comes from a single airport (VIDP) and is broadcast to all 51 stations, so it carries one city-wide signal that the meteorology already supplies. Report it as a negative result and do not claim coupling as a benefit until a second visibility source or a genuinely per-station second series exists.

## Regime check

A model cannot forecast conditions it has never trained on. If these two rows disagree sharply, a poor score is a data-coverage result, not a modelling result, and has to be read as one.

| split | mean PM2.5 | p95 | episode hours |
|---|---|---|---|
| train | 74.4 | 216.0 | 3.5% |
| holdout | 127.4 | 324.0 | 12.1% |

> **Regime mismatch.** The holdout has 3.4x the episode frequency of the training data, so every learned model here is extrapolating and an under-forecast bias is the expected consequence. Fix the coverage before reading the gate as a verdict on the method.

## Gate

> Does the corrector beat raw CAMS, and does everything beat persistence, at all three horizons?

| horizon | full vs raw-cams | full vs persistence | verdict |
|---|---|---|---|
| 24 h | +34.2% | +26.1% | PASS |
| 48 h | +32.7% | +33.1% | PASS |
| 72 h | +31.5% | +33.6% | PASS |

**Gate met.**
