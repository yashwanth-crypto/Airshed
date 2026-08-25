# Hyperparameter search — correction layer

Generated 2026-08-25 03:55 UTC. Regenerate with `scripts/tune_corrector.py`.

- 24 configurations, seed 0, `2025-02-18` to `2026-08-20`
- scored on the **validation** split at **72 h**, median head only


Tuned on validation and never on test: the test blocks are held for the number that gets reported, and a search that sees them turns the ablation into a training curve. Scored at 72 h because overall RMSE is dominated by 24 h rows, where observation history does most of the work and the correction matters least.


## Result

| configuration | val RMSE @72 h |
|---|---|
| defaults (untuned) | 69.592 |
| best of 24 | **68.204** |
| difference | **+2.0%** |

## What was adopted, and what was not

`num_leaves = 255` only. That single change was applied to `DEFAULT_PARAMS`;
the other five parameters in the best configuration were left at their defaults.

| configuration | val RMSE @72 h | share of the gain |
|---|---|---|
| defaults (`num_leaves=31`) | 69.592 | — |
| **defaults + `num_leaves=255` only** | **68.753** | **60%** |
| full six-parameter best | 68.204 | 100% |

**Why only one.** `num_leaves` is the only parameter with a signal that survives
more than one draw — median RMSE falls monotonically with tree size across all
24 trials (70.31, 69.39, 69.19, 68.97 for 31, 63, 127, 255) and 8 of the top 10
trials use 255. Rounds, `min_data_in_leaf` and learning rate were flat or noisy
in the medians, so the remaining 0.8% comes from five parameters the data does
not individually support. Adopting them would be fitting a lucky combination on
the split we tuned on.

**It is not free.** A fit goes from 33 s to 78 s on 1.07M rows, so every
ablation and rolling regeneration now costs roughly 2.4x more.

**The number above is validation.** The honest figure is whatever the
regenerated ablation reports on the held-out test split, which was never seen by
this search.

**Worth adopting: +2.0% on validation at 72 h.** Apply it to `DEFAULT_PARAMS` as a deliberate edit, then regenerate the ablation so the reported numbers come from the tuned model. The gain above is a validation figure and will differ on test.


## Best configuration

```toml
learning_rate = 0.05
num_leaves = 255
min_data_in_leaf = 80
feature_fraction = 0.7
bagging_fraction = 0.9
lambda_l2 = 1.0
num_rounds = 600
```

## All trials, best first

| learning_rate | num_leaves | min_data_in_leaf | feature_fraction | bagging_fraction | lambda_l2 | num_rounds | val RMSE |
|---|---|---|---|---|---|---|---|
| 0.05 | 255 | 80 | 0.7 | 0.9 | 1.0 | 600 | 68.204 |
| 0.05 | 255 | 40 | 0.6 | 0.9 | 20.0 | 300 | 68.548 |
| 0.05 | 255 | 20 | 0.8 | 0.8 | 5.0 | 400 | 68.627 |
| 0.03 | 127 | 40 | 0.8 | 0.8 | 0.5 | 300 | 68.782 |
| 0.02 | 255 | 40 | 0.8 | 0.8 | 20.0 | 400 | 68.828 |
| 0.08 | 255 | 80 | 0.6 | 0.9 | 0.5 | 300 | 68.967 |
| 0.05 | 255 | 80 | 0.7 | 0.9 | 0.5 | 900 | 68.977 |
| 0.03 | 127 | 40 | 0.7 | 0.7 | 0.5 | 600 | 69.056 |
| 0.08 | 255 | 80 | 0.6 | 0.8 | 0.5 | 900 | 69.185 |
| 0.08 | 255 | 20 | 0.8 | 0.9 | 20.0 | 900 | 69.207 |
| 0.05 | 63 | 80 | 0.9 | 0.7 | 20.0 | 600 | 69.216 |
| 0.08 | 31 | 20 | 0.7 | 0.7 | 0.5 | 300 | 69.219 |
| 0.03 | 63 | 20 | 0.6 | 0.7 | 0.5 | 400 | 69.236 |
| 0.02 | 255 | 20 | 0.8 | 0.7 | 0.5 | 300 | 69.264 |
| 0.08 | 127 | 40 | 0.7 | 0.9 | 20.0 | 600 | 69.322 |
| 0.03 | 63 | 20 | 0.9 | 0.7 | 0.5 | 300 | 69.360 |
| 0.02 | 127 | 160 | 0.6 | 0.8 | 5.0 | 300 | 69.422 |
| 0.05 | 63 | 40 | 0.6 | 0.9 | 5.0 | 300 | 69.429 |
| 0.03 | 63 | 40 | 0.7 | 0.9 | 20.0 | 300 | 69.526 |
| 0.08 | 63 | 20 | 0.9 | 0.9 | 20.0 | 600 | 69.848 |
| 0.03 | 31 | 80 | 0.7 | 0.8 | 0.5 | 300 | 69.905 |
| 0.03 | 31 | 40 | 0.7 | 0.8 | 5.0 | 300 | 70.310 |
| 0.08 | 31 | 160 | 0.8 | 0.7 | 5.0 | 300 | 70.372 |
| 0.08 | 31 | 80 | 0.6 | 0.7 | 0.5 | 600 | 70.958 |
