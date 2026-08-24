# Leave-one-station-out — Phase 4

Each station is deleted from the network, then predicted from the others. The held-out station contributes nothing: not to training, not to its own neighbourhood, and not through its own observation history (R7).

- data range `2025-08-01` to `2026-02-28`
- 6 stations held out in turn


## Held-out station error (µg/m³)

| station | nearest neighbour | hours | observed mean | **graph** | IDW | raw CAMS |
|---|---|---|---|---|---|---|
| Anand Vihar | 2.8 km | 2,638 | 232 | **94.4** | 124.6 | 143.2 |
| Rohini | 2.1 km | 2,594 | 213 | **84.3** | 111.6 | 125.4 |
| IGI Airport T3 | 4.6 km | 2,611 | 118 | **81.3** | 111.6 | 53.7 |
| Aya Nagar | 5.4 km | 2,425 | 117 | **76.0** | 110.3 | 56.6 |
| Knowledge Park V | 12.0 km | 1,997 | 178 | **79.4** | 105.8 | 96.2 |
| Vikas Sadan | 5.0 km | 1,921 | 144 | **80.2** | 93.4 | 78.6 |
| **mean** | | | | **82.6** | 109.6 | 92.3 |

## Gate

> What is the held-out station error in µg/m³, across at least four stations?

**82.6 µg/m³ RMSE**, averaged over 6 held-out stations.

Against distance-weighted interpolation: +24.6%. Against raw CAMS at the same point: +10.5%.

## Where this does not work

Raw CAMS beats the graph at: **IGI Airport T3, Aya Nagar, Vikas Sadan**. These are the cleanest, most peripheral sites in the set, and the pattern is consistent: the graph pulls a low-concentration outer station towards the dirtier city its neighbours describe. Averaging over stations hides this, which is exactly why the per-station table is the headline here and the mean is the footnote.

The practical consequence for the map: a grid cell far from any station and upwind of the city should be shown with wide uncertainty, not as a confident interpolation.

## How good is this, really

Mean error of 83 µg/m³ against an observed mean of 167 µg/m³ is a large relative error — spatial skill is well behind the temporal model, which reaches 64 µg/m³ RMSE *with* the station's own history available. That gap is the honest measure of how much a monitor is worth. The surface is good enough to say which part of the city is worse on a given morning; it is not good enough to quote a number for an unmonitored block, and the UI must not imply otherwise (R7).
