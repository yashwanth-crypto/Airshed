# Lead-matched meteorology — is the 72 h number a 72 h number?

Generated 2026-08-24 12:04 UTC. Regenerate with `airshed leadmatch`.

`meteo_archive` returns the best available forecast for each past hour, which is a short-lead one. Training on that and serving a genuine 72 h forecast is the distribution mismatch R1 exists to prevent, one level down: not reanalysis-vs-forecast, but short-lead-vs-long-lead. This table rebuilds the identical rows with the forecast that was really available `horizon_h` hours earlier, from the Open-Meteo Previous Runs API, and re-scores.

- data range `2025-02-18` to `2026-08-19`, evaluated on the **test** split
- 806,438 training rows, 133,729 evaluation rows
- lead-matched meteorology reached 100.0% of rows; the rest are excluded so both columns describe the same rows

Lead day `N` means the value came from the run initialised `N` days before the valid day, so the true lead is `24N + hour_of_day`. The mapping is never optimistic: a 72 h horizon is scored against a forecast at least 72 hours old, sometimes 95.

## RMSE by horizon (µg/m³, lower is better)

| model | 24 h | 48 h | 72 h | overall |
|---|---|---|---|---|
| persistence — archive (short-lead) | 84.7 | 93.9 | 96.1 | 91.8 |
| persistence — lead-matched | 84.7 | 93.9 | 96.1 | 91.8 |
| raw-cams — archive (short-lead) | 95.2 | 93.4 | 93.1 | 93.9 |
| raw-cams — lead-matched | 95.2 | 93.4 | 93.1 | 93.9 |
| full — archive (short-lead) | 62.6 | 62.9 | 63.8 | 63.1 |
| full — lead-matched | 62.5 | 64.1 | 64.5 | 63.7 |

## The inputs themselves, before any model

RMSE between the short-lead archive value and the forecast at real lead, for the same station and hour. This is forecast error growth, measured directly and independent of any model — the size of any effect below should be read against it.

| lead day | true lead | rows | temperature_2m | wind_speed_10m | wind_direction_10m | relative_humidity_2m | precipitation |
|---|---|---|---|---|---|---|---|
| 1 | 24–47 h | 667,680 | 1.60 | 4.44 | 86.32 | 6.49 | 0.75 |
| 2 | 48–71 h | 667,680 | 2.07 | 5.18 | 93.88 | 8.25 | 0.85 |
| 3 | 72–95 h | 667,680 | 2.32 | 5.52 | 96.66 | 9.57 | 1.18 |

## Verdict

Persistence and raw CAMS use no meteorology and are unchanged between the two columns, which confirms the frames are row-for-row comparable.

| horizon | short-lead met | real-lead met | optimism |
|---|---|---|---|
| 24 h | 62.6 | 62.5 | -0.3% |
| 48 h | 62.9 | 64.1 | +2.0% |
| 72 h | 63.8 | 64.5 | +1.1% |
| overall | 63.1 | 63.7 | +1.0% |

**The 72 h number was optimistic by +1.1%** on this split. That much of the reported skill came from meteorology fresher than anything production will ever see.

**One split does not settle an effect this size.** It is the same magnitude as the fires and upwind effects this project declines to claim from a single split, so it gets the same treatment: see the `lead-matched meteorology` row in `rolling.md`, where the cost is measured across five folds. The direction is consistent; the magnitude is not pinned.

### What is still short-lead

These variables have no Previous Runs form and could not be swapped, so they keep their short-lead values here: `boundary_layer_height`, `visibility`, `temperature_925hPa`, `temperature_850hPa`, `wind_speed_925hPa`, `wind_direction_925hPa`, `geopotential_height_925hPa`. Boundary-layer height is among them, and it is the most important variable in the set — the derived `inversion`, `lapse_*` and `ventilation_index` features inherit the problem. CAMS PM2.5 has no archived-forecast endpoint at all.

So this table removes one source of optimism and measures it; it does not remove them all. The remaining gap closes only forward, as `meteo_runs` and `cams_runs` accumulate real archived runs — which is why every day the daily archive job fails to run is a day that cannot be recovered.
