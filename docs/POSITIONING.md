# Where this stands against the problem statement

SIH26082 — "Air Pollution–Weather Coupled Forecasting System, Delhi NCR",
Ministry of Earth Sciences, Disaster Management.

Written 2026-08-24 alongside `STATUS.md`. Every number here is from held-out
data and traceable to a file in `docs/results/`.

---

## 1. Does it answer the statement?

| the statement asks for | status | evidence |
|---|---|---|
| a forecasting system for Delhi NCR | yes | +29% RMSE vs raw CAMS, +19.7% vs persistence, 5/5 folds |
| **coupled** with weather | yes, both directions | weather->pollution in the features; pollution->weather **measured**: +9.5% on visibility |
| 72-hour horizon | yes, and strongest there | skill *grows* with lead: +0.253 / +0.330 / +0.332 |
| disaster-management use | yes | GRAP stage probability with 72 h lead; fog alarms at 91% recall |
| operational realism | yes | live forecast, cache-only serving, staleness shown |

The word the statement turns on is **coupled**, and that is the one we can now
put a number against rather than assert from architecture.

---

## 2. The structural argument, in one table

This is the sharpest thing in the project, and it is visible in the skill
column of `ablation.md`:

| model | 24 h | 72 h |
|---|---|---|
| raw CAMS vs persistence | **-12.3%** | **+3.0%** |
| our correction vs persistence | +25.3% | +33.2% |

Read the first row twice. At 24 hours the physics forecast is *worse* than
assuming tomorrow looks like today. At 72 hours it is better. That crossover is
the whole thesis: **persistence knows the present, physics knows the future**,
and neither is enough alone. The correction layer takes the physics forecast —
which carries transport and regional build-up we cannot infer from station
history — and fixes its Delhi bias using local observations. It beats both at
every horizon, and its margin *widens* with lead time, which is exactly where a
72-hour product has to work and exactly where a purely autoregressive model
collapses.

Most competing approaches train on station history alone. Those models look
excellent at 24 h and decay badly by 72 h. Ours does the opposite.

---

## 3. Against the incumbents (SAFAR, Delhi DSS)

A judge from MoES will point out that the ministry already funds operational
systems. The answer is not that ours is better.

**We do not replace the physics model. We correct it.** The method is a bias
correction on top of *any* chemical transport forecast. We demonstrate it on
CAMS because CAMS is free and public; the identical layer bolts onto SAFAR's
WRF-Chem output with no change of design. What we bring is:

- a measured statement of the operational forecast's bias (-49 ug/m3 on raw
  CAMS over our test block, reduced to +6),
- a correction that recovers most of it,
- and the evaluation discipline to prove it did.

That is a collaboration proposition, not a competing product. It is also the
honest one: we have no chemical transport model and say so (`CLAUDE.md`,
explicitly out of scope).

---

## 4. What genuinely distinguishes this work

Ranked by how hard they are to replicate in a hackathon.

**1. Pollution improves the weather forecast — proven, not claimed.**
Raw GFS visibility over Delhi scores **21.3 km RMSE**: the operational weather
model is effectively blind to haze-driven visibility collapse. Correcting it
with weather alone reaches 1.19 km, level with persistence. Adding pollution
information reaches **1.08 km (+9.5%)** — and the gain scales with aerosol
exactly as the physics demands: +0.3% in clean air, +7.2% moderate, **+8.9%
poor**, +8.3% severe. A larger feature set would not care how dirty the air is.
This is the two-way coupling the statement asks for, and it is the single
result least likely to appear in any competing submission.

**2. A second decision product nobody else publishes.**
Fired as a probabilistic alarm rather than a thresholded median, the
pollution-informed model catches **91% of hours below 1 km visibility** against
84% for weather-only. That is flight diversions at IGI and pile-ups on NH-44
and NH-48 — squarely Disaster Management, and a use case distinct from AQI.

**3. Decision-grade output, not concentration-grade.**
GRAP stage *probabilities* with lead time: Stage III caught for 82% of event
hours at 72 h median lead, Stage IV 61% at 48 h. Thresholds are deliberately
asymmetric — Stage I needs p >= 0.50, Stage III only p >= 0.25 — because
missing a severe episode is a health outcome and a false alarm is an
inconvenience. Per-class recall only; overall accuracy appears nowhere (R5).

**4. Calibrated uncertainty.**
10-90% intervals holding the truth 77.8% of the time, conformally calibrated on
a split the model never trained on. A point forecast cannot support a threshold
decision; an uncalibrated interval is worse than none.

**5. The evaluation itself.**
Rolling-origin folds with error bars, a regime check printed on every run,
persistence in every table, and **negative results reported as negative**. Two
of our own additions are recorded as having no measurable effect. One earlier
verdict was overturned by better evaluation and the reversal is written down.
In a field where published air-quality models routinely fail to beat
persistence without noticing, this is the part a scientist will trust.

---

## 5. Where a sharp judge will push, and the honest answer

**"Your 72 h skill is measured with a CAMS that isn't a real 72 h forecast."**
Correct, and it is the strongest attack available. Training reads Open-Meteo's
air-quality archive, which is built from short-lead CAMS output. The
model-vs-CAMS comparison stays fair — both use the same input — but skill
against *persistence* at 72 h is likely optimistic, because persistence is
unaffected by the substitution while our input quietly improves. `cams_runs` is
now accumulating genuine archived runs; the gap can be quantified as it grows.

**"One winter of training data."**
True and stated on every ablation run by the regime check. It is why the upwind
and coupling verdicts are "cannot tell" rather than yes or no.

**"Your map says 82.6 ug/m3 error."**
Also true, reported per station rather than hidden in a mean, and the UI is
written to say the surface indicates which part of the city is worse — not what
an unmonitored block will read (R7). Raw CAMS beats our graph at the two
cleanest peripheral sites, and `loso.md` names them.

**"Your coupled multi-output model didn't work."**
Correct: +0.8% +/- 1.2%, no measurable effect. Reported as a null result. The
coupling that *does* work is the visibility direction above, which is the
harder and more interesting one.

**"You don't beat persistence on episode recall."**
Fair. Episode RMSE improves substantially (153.5 -> 129.1 ug/m3) but recall at
the 250 ug/m3 threshold is 47.1% against persistence's 48.0%. The model is more
accurate on episode hours while being marginally less trigger-happy at the
threshold. Class weighting is the untried fix.

**"Where are the fires?"**
Loaded, as of 2026-08-24: 274 days and 23,544 VIIRS/MODIS detections, November
2025 peaking at 8,470. They are the strongest of the three optional additions
(+1.3% across folds, **+4.3% on the stubble-season fold**) but still not
separable from noise on one burning season. The honest line is that the
mechanism shows up in the right season and the sample is too small to confirm
it.

---

## 6. What would most improve standing, ranked

1. **FIRMS key** — 2 minutes. Adds the dominant episode driver. Nothing else
   on this list is as cheap or as physically important.
2. **Lead with the visibility coupling in the pitch.** It is the unique claim,
   it answers the statement's central word, and it comes with a physical
   signature that survives scrutiny.
3. **Say the SAFAR line early**: we correct physics, we do not replace it, and
   the same layer applies to the ministry's own model. It converts the obvious
   objection into the value proposition.
4. **Let the archiver run.** Every day of true archived forecasts narrows the
   one methodological gap that is genuinely hard to defend.
5. **More winters** if any route opens. Everything currently marked "cannot
   tell" becomes decidable.

---

## 7. Summary judgement

The project answers the problem statement, and the part it answers best is the
part the statement is named after: coupling, with a measured number and a
physical signature, in the direction that is hard.

Its distinguishing quality is not any single metric — it is that every number
here can be defended, the negative results are on the page next to the positive
ones, and the limits are stated before anyone has to ask. Against operational
systems it is a correction layer, not a competitor. Against typical competing
submissions it has an external published baseline, error bars, and a second
decision product in fog.

Its real weakness is data, not method: one winter, no fires, and a physics
input that is better than the one production would see. All three are known,
written down, and cheap to improve in the order given above.
