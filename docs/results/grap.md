# GRAP decision layer — Phase 3

Probability that each GRAP stage is reached, from the predicted distribution of Delhi's city-wide 24-hour PM2.5.

- trained on 26,548 city-hours, tested on 4,719 (2025-12-12 to 2026-06-30)
- city 24 h RMSE 26.5 µg/m³, interval coverage 85.1%


GRAP is invoked on **Delhi's** city-wide average AQI, so the city series is modelled directly from the 44 Delhi stations rather than aggregated from 77 correlated station forecasts after the fact. CAQM keys GRAP to Delhi's own AQI, so averaging in the wider NCR ring would compute a different quantity and then compare it against statutory Delhi thresholds.


## Stage thresholds

| stage | name | AQI | 24 h PM2.5 (µg/m³) | decision threshold |
|---|---|---|---|---|
| 1 | Poor | 201–300 | 90–120 | p ≥ 0.50 |
| 2 | Very Poor | 301–400 | 120–250 | p ≥ 0.40 |
| 3 | Severe | 401–450 | 250–314 | p ≥ 0.25 |
| 4 | Severe Plus | 451–1000 | 316–380 | p ≥ 0.20 |

Thresholds fall as severity rises. Missing a severe episode costs more than a false alarm, so Stage III is declared at a one-in-four chance while Stage I needs an even one (R5).


## Per-stage recall by horizon

Recall is the share of hours that really reached the stage and were called. Overall accuracy is deliberately absent (R5).

| horizon | stage | actual hours | recall | precision | false alarm rate |
|---|---|---|---|---|---|
| 24 h | 1 Poor | 814 | 99.6% | 98.5% | 1.7% |
| 24 h | 2 Very Poor | 668 | 96.9% | 85.1% | 13.2% |
| 24 h | 3 Severe | 145 | 81.4% | 52.2% | 7.8% |
| 24 h | 4 Severe Plus | 17 | 100.0% | 25.4% | 3.3% |
| 48 h | 1 Poor | 838 | 100.0% | 97.1% | 3.4% |
| 48 h | 2 Very Poor | 668 | 97.6% | 80.7% | 17.2% |
| 48 h | 3 Severe | 145 | 80.0% | 52.0% | 7.5% |
| 48 h | 4 Severe Plus | 17 | 88.2% | 46.9% | 1.1% |
| 72 h | 1 Poor | 856 | 100.0% | 97.5% | 2.9% |
| 72 h | 2 Very Poor | 684 | 97.5% | 83.3% | 14.4% |
| 72 h | 3 Severe | 145 | 73.8% | 66.0% | 3.7% |
| 72 h | 4 Severe Plus | 17 | 52.9% | 69.2% | 0.2% |

Recall is not monotone in horizon, and that is a property of the decision rule rather than of skill. The threshold is a fixed probability and intervals widen with lead time, so a longer-lead forecast crosses its threshold more readily than a short-lead one. Stage III moves from 81% recall at 24 h to 74% at 72 h, while precision goes 52% to 66%. Read the two columns together: a longer horizon is not seeing further, it is guessing more freely.


## Lead time on severe stages

The longest unbroken horizon at which the stage was already being called. A stage caught only at 24 h gives a day of warning; caught at 72 h it gives three.

| stage | events | caught at all | median lead | caught at 72 h |
|---|---|---|---|---|
| 1 Poor | 856 | 100.0% | 72 h | 100.0% |
| 2 Very Poor | 684 | 100.0% | 72 h | 97.5% |
| 3 Severe | 145 | 88.3% | 72 h | 73.8% |
| 4 Severe Plus | 17 | 100.0% | 72 h | 52.9% |
