# Rolling-origin evaluation

Every headline number elsewhere comes from a single split. This repeats the comparison over expanding-window folds — training always before evaluation, each fold holding out the next contiguous season — so the spread across folds says how much a result would move on different data. A difference smaller than that spread is not a finding.

- folds: autumn-2025, episode-2025, late-winter-2026, spring-2026, summer-2026


## RMSE across folds (µg/m³)

| model | mean | sd | best fold | worst fold |
|---|---|---|---|---|
| full+upwind | 64.3 | 24.3 | 37.2 | 96.0 |
| full | 64.3 | 24.3 | 37.2 | 95.7 |
| coupled | 64.4 | 24.4 | 37.0 | 96.1 |
| full (lead-matched) | 65.0 | 25.1 | 37.1 | 98.1 |
| full (no fires) | 65.5 | 25.7 | 37.0 | 99.9 |
| persistence | 80.5 | 27.5 | 44.9 | 108.9 |
| raw-cams | 92.3 | 32.3 | 58.5 | 146.2 |

The spread across folds is large because the seasons differ, not because the models are unstable. That is exactly why the comparisons below are **paired**: both models see the same fold, so seasonal variation cancels.


## Are the contested claims real?

| comparison | folds better | mean gain | sd | verdict |
|---|---|---|---|---|
| correction vs persistence (`full+upwind` vs `persistence`) | 5/5 | +20.5% | +7.1% | **holds up** |
| correction vs raw CAMS (`full+upwind` vs `raw-cams`) | 5/5 | +29.6% | +17.3% | **holds up** |
| upwind fires (`full` vs `full (no fires)`) | 3/5 | +1.3% | +1.9% | inconsistent across folds |
| upwind corridor (`full+upwind` vs `full`) | 2/5 | +0.0% | +0.5% | inconsistent across folds |
| coupling (`coupled` vs `full`) | 2/5 | -0.0% | +0.4% | **does not hold** |
| lead-matched meteorology (`full (lead-matched)` vs `full`) | 1/5 | -0.9% | +1.0% | cost in the expected direction, within noise |

`full+upwind` vs `persistence`, per fold: +17.6%, +11.8%, +28.5%, +27.1%, +17.2%

`full+upwind` vs `raw-cams`, per fold: +12.9%, +34.3%, +16.9%, +27.2%, +56.5%

`full` vs `full (no fires)`, per fold: +0.0%, +4.3%, +1.6%, +1.2%, -0.5%

`full+upwind` vs `full`, per fold: +0.9%, -0.4%, -0.3%, -0.3%, +0.1%

`coupled` vs `full`, per fold: -0.3%, -0.4%, +0.1%, -0.2%, +0.6%

`full (lead-matched)` vs `full`, per fold: -0.7%, -2.5%, -0.7%, -0.7%, +0.3%

## What this changes

The correction itself is not in doubt. It beats both baselines on every fold, in every season, by a margin several times the scatter of that margin — and the raw-CAMS gain is largest in summer, when CAMS is furthest off.

Not distinguishable from no effect: **upwind fires** (+1.3%, 3/5 folds), **upwind corridor** (+0.0%, 2/5 folds), **coupling** (-0.0%, 2/5 folds). Each gain is smaller than its own spread, so on this evidence none of them can be claimed — nor ruled out. Worth noting: **upwind fires** does its best work on the `episode-2025` fold (+4.3%), well above its average. If that is the season the mechanism predicts, the mean across all folds is diluting a real seasonal effect rather than measuring its absence — and the way to tell is more seasons, not more features.

**lead-matched meteorology.** Scoring the same rows with the input the model will actually be given costs +0.9%, worse on 4/5 folds (scatter 1.0%). The cost is consistent in direction but no larger than the fold-to-fold spread, so the honest statement is that the optimism is real in sign and about a percent in size, not that it has been pinned to a number. It does not move the headline claim: the correction still beats both baselines on every fold with either input.

The single-split ablation is not a safe guide for effects this small. It once reported coupling as a clear negative (-0.9%, +0.3%, -1.3% by horizon) on the strength of one split that happened to fall unfavourably. Where the table above says a gain is smaller than its own scatter, the honest statement is "no measurable effect either way on one year of data" — neither "it helps" nor "it hurts". Deciding would need more winters, which is the constraint behind almost every limitation in this project.
