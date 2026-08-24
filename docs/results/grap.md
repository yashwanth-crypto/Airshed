# GRAP decision layer — Phase 3

Probability that each GRAP stage is reached, from the predicted distribution of Delhi's city-wide 24-hour PM2.5.

- trained on 25,826 city-hours, tested on 4,557 (2025-12-12 to 2026-06-30)
- city 24 h RMSE 26.3 µg/m³, interval coverage 72.3%


GRAP is invoked on the city-wide average AQI, so the city series is modelled directly rather than aggregated from 51 correlated station forecasts after the fact.


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
| 24 h | 1 Poor | 814 | 100.0% | 95.3% | 6.1% |
| 24 h | 2 Very Poor | 670 | 96.9% | 84.1% | 15.3% |
| 24 h | 3 Severe | 133 | 72.9% | 54.5% | 6.0% |
| 24 h | 4 Severe Plus | 18 | 16.7% | 60.0% | 0.1% |
| 48 h | 1 Poor | 838 | 100.0% | 95.9% | 5.3% |
| 48 h | 2 Very Poor | 679 | 100.0% | 81.3% | 18.5% |
| 48 h | 3 Severe | 133 | 65.4% | 46.8% | 7.1% |
| 48 h | 4 Severe Plus | 18 | 61.1% | 68.8% | 0.3% |
| 72 h | 1 Poor | 856 | 100.0% | 96.6% | 4.2% |
| 72 h | 2 Very Poor | 697 | 98.3% | 82.1% | 17.2% |
| 72 h | 3 Severe | 133 | 82.0% | 40.8% | 11.0% |
| 72 h | 4 Severe Plus | 18 | 38.9% | 31.8% | 1.0% |

Recall is not monotone in horizon, and that is a property of the decision rule rather than of skill. The threshold is a fixed probability, and intervals widen with lead time, so a 72 h forecast crosses `p ≥ 0.25` more readily than a 24 h one — Stage III recall rises to 82% at 72 h while precision falls to 41%. Read the two columns together: the longer horizon is not seeing further, it is guessing more freely.


## Lead time on severe stages

The longest unbroken horizon at which the stage was already being called. A stage caught only at 24 h gives a day of warning; caught at 72 h it gives three.

| stage | events | caught at all | median lead | caught at 72 h |
|---|---|---|---|---|
| 1 Poor | 856 | 100.0% | 72 h | 100.0% |
| 2 Very Poor | 697 | 100.0% | 72 h | 98.3% |
| 3 Severe | 133 | 82.0% | 72 h | 82.0% |
| 4 Severe Plus | 18 | 61.1% | 48 h | 38.9% |
