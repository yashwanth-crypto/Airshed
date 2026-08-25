# Lead-matched meteorology — is the 72 h number a 72 h number?

Generated 2026-08-25 02:14 UTC. Regenerate with `airshed leadmatch`.

`meteo_archive` returns the best available forecast for each past hour, which is a short-lead one. Training on that and serving a genuine 72 h forecast is the distribution mismatch R1 exists to prevent, one level down: not reanalysis-vs-forecast, but short-lead-vs-long-lead. This table rebuilds the identical rows with the forecast that was really available `horizon_h` hours earlier, from the Open-Meteo Previous Runs API, and re-scores.

- data range `2025-02-18` to `2026-08-20`, evaluated on the **test** split
- 1,071,904 training rows, 194,012 evaluation rows
- lead-matched meteorology reached 100.0% of rows; the rest are excluded so both columns describe the same rows

Lead day `N` means the value came from the run initialised `N` days before the valid day, so the true lead is `24N + hour_of_day`. The mapping is never optimistic: a 72 h horizon is scored against a forecast at least 72 hours old, sometimes 95.

## RMSE by horizon (µg/m³, lower is better)

| model | 24 h | 48 h | 72 h | overall |
|---|---|---|---|---|
| persistence — archive (short-lead) | 79.6 | 87.7 | 89.7 | 85.8 |
| persistence — lead-matched | 79.6 | 87.7 | 89.7 | 85.8 |
| raw-cams — archive (short-lead) | 94.3 | 92.8 | 92.2 | 93.1 |
| raw-cams — lead-matched | 94.3 | 92.8 | 92.2 | 93.1 |
| full — archive (short-lead) | 59.6 | 60.9 | 61.0 | 60.5 |
| full — lead-matched | 59.7 | 61.1 | 61.6 | 60.8 |

## The inputs themselves, before any model

RMSE between the short-lead archive value and the forecast at real lead, for the same station and hour. This is forecast error growth, measured directly and independent of any model — the size of any effect below should be read against it.

| lead day | true lead | rows | temperature_2m | wind_speed_10m | wind_direction_10m | relative_humidity_2m | precipitation |
|---|---|---|---|---|---|---|---|
| 1 | 24–47 h | 1,011,480 | 1.61 | 4.51 | 86.22 | 6.60 | 0.75 |
| 2 | 48–71 h | 1,011,480 | 2.08 | 5.26 | 93.99 | 8.38 | 0.83 |
| 3 | 72–95 h | 1,011,480 | 2.34 | 5.60 | 96.59 | 9.72 | 1.18 |

## Verdict

Persistence and raw CAMS use no meteorology and are unchanged between the two columns, which confirms the frames are row-for-row comparable.

| horizon | short-lead met | real-lead met | optimism |
|---|---|---|---|
| 24 h | 59.6 | 59.7 | +0.0% |
| 48 h | 60.9 | 61.1 | +0.4% |
| 72 h | 61.0 | 61.6 | +1.1% |
| overall | 60.5 | 60.8 | +0.5% |

**The 72 h number was optimistic by +1.1%** on this split. That much of the reported skill came from meteorology fresher than anything production will ever see.

**One split does not settle an effect this size.** It is the same magnitude as the fires and upwind effects this project declines to claim from a single split, so it gets the same treatment: see the `lead-matched meteorology` row in `rolling.md`, where the cost is measured across five folds. The direction is consistent; the magnitude is not pinned.

### What is still short-lead

These variables have no Previous Runs form and could not be swapped, so they keep their short-lead values here: `boundary_layer_height`, `visibility`, `temperature_925hPa`, `temperature_850hPa`, `wind_speed_925hPa`, `wind_direction_925hPa`, `geopotential_height_925hPa`. Boundary-layer height is among them, and it is the most important variable in the set — the derived `inversion`, `lapse_*` and `ventilation_index` features inherit the problem. CAMS PM2.5 has no archived-forecast endpoint at all.

So this table removes one source of optimism and measures it; it does not remove them all. The remaining gap closes only forward, as `meteo_runs` and `cams_runs` accumulate real archived runs — which is why every day the daily archive job fails to run is a day that cannot be recovered.
