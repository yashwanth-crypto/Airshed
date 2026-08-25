# Rolling-origin evaluation

Every headline number elsewhere comes from a single split. This repeats the comparison over expanding-window folds — training always before evaluation, each fold holding out the next contiguous season — so the spread across folds says how much a result would move on different data. A difference smaller than that spread is not a finding.

- folds: autumn-2025, episode-2025, late-winter-2026, spring-2026, summer-2026


## RMSE across folds (µg/m³)

| model | mean | sd | best fold | worst fold |
|---|---|---|---|---|
| full+upwind | 61.9 | 21.8 | 39.6 | 90.6 |
| full | 62.0 | 21.7 | 39.7 | 90.3 |
| coupled | 62.1 | 22.2 | 39.6 | 91.8 |
| full (lead-matched) | 62.6 | 22.3 | 40.1 | 92.2 |
| full (no fires) | 62.8 | 23.2 | 39.5 | 94.6 |
| persistence | 77.8 | 24.6 | 47.8 | 103.6 |
| raw-cams | 91.4 | 27.6 | 62.7 | 136.6 |

The spread across folds is large because the seasons differ, not because the models are unstable. That is exactly why the comparisons below are **paired**: both models see the same fold, so seasonal variation cancels.


## Are the contested claims real?

| comparison | folds better | mean gain | sd | verdict |
|---|---|---|---|---|
| correction vs persistence (`full+upwind` vs `persistence`) | 5/5 | +20.6% | +7.2% | **holds up** |
| correction vs raw CAMS (`full+upwind` vs `raw-cams`) | 5/5 | +31.5% | +17.5% | **holds up** |
| upwind fires (`full` vs `full (no fires)`) | 3/5 | +0.9% | +2.1% | inconsistent across folds |
| upwind corridor (`full+upwind` vs `full`) | 3/5 | +0.0% | +0.4% | inconsistent across folds |
| coupling (`coupled` vs `full`) | 4/5 | -0.1% | +0.9% | **does not hold** |
| lead-matched meteorology (`full (lead-matched)` vs `full`) | 0/5 | -0.9% | +0.7% | **real cost, 0.9%** |

`full+upwind` vs `persistence`, per fold: +17.2%, +12.5%, +29.3%, +27.1%, +17.0%

`full+upwind` vs `raw-cams`, per fold: +12.4%, +33.7%, +18.7%, +35.2%, +57.5%

`full` vs `full (no fires)`, per fold: +0.0%, +4.6%, +0.0%, +0.2%, -0.4%

`full+upwind` vs `full`, per fold: +0.4%, -0.4%, +0.4%, -0.2%, +0.0%

`coupled` vs `full`, per fold: +0.4%, -1.6%, +0.6%, +0.1%, +0.0%

`full (lead-matched)` vs `full`, per fold: -0.8%, -2.1%, -0.3%, -0.3%, -1.0%

## What this changes

The correction itself is not in doubt. It beats both baselines on every fold, in every season, by a margin several times the scatter of that margin — and the raw-CAMS gain is largest in summer, when CAMS is furthest off.

Not distinguishable from no effect: **upwind fires** (+0.9%, 3/5 folds), **upwind corridor** (+0.0%, 3/5 folds), **coupling** (-0.1%, 4/5 folds). Each gain is smaller than its own spread, so on this evidence none of them can be claimed — nor ruled out. Worth noting: **upwind fires** does its best work on the `episode-2025` fold (+4.6%), well above its average. If that is the season the mechanism predicts, the mean across all folds is diluting a real seasonal effect rather than measuring its absence — and the way to tell is more seasons, not more features.

**lead-matched meteorology.** Scoring the same rows with the input the model will actually be given costs +0.9%, worse on 5/5 folds (scatter 0.7%). That is larger than the fold-to-fold spread, so it is a real cost and the lead-matched column is the one to quote.

The single-split ablation is not a safe guide for effects this small. It once reported coupling as a clear negative (-0.9%, +0.3%, -1.3% by horizon) on the strength of one split that happened to fall unfavourably. Where the table above says a gain is smaller than its own scatter, the honest statement is "no measurable effect either way on one year of data" — neither "it helps" nor "it hurts". Deciding would need more winters, which is the constraint behind almost every limitation in this project.
