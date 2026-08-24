# Build plan

Five phases. Each ends at a **gate** — a yes/no question with a number attached. Do not start a phase
before the previous gate is answered, because each phase de-risks the next.

Read `CLAUDE.md` first, especially the HARD RULES. Several tasks below exist only because of them.

---

## Phase 1 — Data spine

Unglamorous, and it decides whether the rest of the project happens. Everything downstream is blocked on
having clean, aligned, locally-stored history.

### Tasks

1. **Repo scaffold** — `uv init`, package layout per `CLAUDE.md`, `config.toml` with NCR bounding box,
   CPCB station list with coordinates, and GRAP thresholds transcribed from the official CAQM schedule.
2. **`ingest/cams.py`** — pull CAMS PM2.5 forecasts for NCR grid points from the Open-Meteo Air Quality
   API. Both live (`forecast_days`) and historical backfill. Partitioned Parquet by date.
3. **`ingest/meteo.py`** — pull forecast meteorology. **Two code paths, and keep them separate:**
   - *serving*: `api.open-meteo.com/v1/forecast`
   - *training*: `historical-forecast-api.open-meteo.com/v1/forecast` (archived past forecasts — this is
     R1's solution). Verify `boundary_layer_height` is available on the chosen model before building
     features that depend on it; if not, derive a proxy and document the substitution.
4. **`ingest/cpcb.py`** — ground truth. Live from the CCR portal or a mirror (AQICN / OpenAQ), plus bulk
   historical from data.gov.in. Per R6, mark gaps as missing with an explicit quality flag column; do not
   fill them.
5. **`ingest/metar.py`** — hourly visibility for VIDP from the Iowa State IEM ASOS archive.
6. **`ingest/fires.py`** — VIIRS/MODIS active fire detections over Punjab/Haryana from NASA FIRMS.
   Seasonal (Oct–Nov dominant) — expect empty returns out of season and handle it without erroring.
7. **`features/build.py`** — align every source onto one tz-aware hourly UTC index per station. This is
   where misalignment bugs hide, so unit-test it: assert no duplicated timestamps, no silent
   forward-fills across gap boundaries, and correct handling of the IST↔UTC offset.
8. **Time-block splitter** — implement train/val/test splitting by time block with whole-episode holdout
   (R3). Everything downstream imports this; nobody hand-rolls a split.

### Gate

> Can you reconstruct every input feature for any past week on demand, from local storage, with no
> network call?

Answer it by actually doing it for one week in November and one week in June.

---

## Phase 2 — Baseline and ablation

Roughly a day's work once Phase 1 exists, and it is the cheapest credibility available to this project.
**Build this before any dashboard.**

### Tasks

1. **`models/baselines.py`**
   - `PersistenceModel` — tomorrow = today (R2).
   - `RawCAMSModel` — CAMS forecast passed through unchanged.
   Both implement the same interface as the real model so the ablation harness treats them identically.
2. **`models/corrector.py`** — LightGBM learning the residual between CAMS and CPCB observations.
   Features: CAMS forecast at horizon, recent observed PM2.5, forecast meteorology (wind speed/direction,
   temperature, humidity, boundary-layer height), hour-of-day, day-of-week, season.
3. **`eval/metrics.py`** — RMSE, MAE, bias, and per-horizon breakdown at 24/48/72 h.
4. **`eval/ablation.py`** — one command, one table. Four rows: persistence, raw CAMS, CAMS + observation
   history, full feature set. Columns: RMSE and MAE at each horizon. Write the table to
   `docs/results/ablation.md` so it is version-controlled and diffable as the model improves.

### Gate

> Does the corrector beat raw CAMS, and does everything beat persistence, at all three horizons?

Record the actual numbers. If the corrector does not beat raw CAMS, stop and diagnose before proceeding —
that is the project's central claim and it must hold before anything is built on it.

---

## Phase 3 — Coupling and uncertainty

Now make it actually coupled, and make it say how confident it is.

### Tasks

1. **Multi-output core** — one model predicting PM2.5, boundary-layer height and visibility jointly at
   each horizon, with each series' recent history available to all three. This is the concrete answer to
   "where exactly is the coupling?" — the architecture, not a feature column.
2. **Direct multi-horizon heads** — one output head per horizon (R4). No recursive rollout.
3. **Quantile heads** — 10th/50th/90th percentile output instead of a point estimate. With LightGBM this
   is quantile objective and three models; in PyTorch it is a pinball loss.
4. **`grap.py`** — map the predicted distribution onto GRAP stage probabilities using the statutory
   thresholds from config. Output: probability per stage per horizon, plus lead time.
5. **Imbalance handling and reporting** — class weights or focal loss; report per-class recall and severe-
   class lead time (R5). Overall accuracy must not appear in any results table.
6. **Extend the ablation** — add rows for single-output vs coupled multi-output, so the coupling claim is
   measured on the same footing as everything else.

### Gate

> Does the coupled multi-output model beat the single-output one, measurably, on the same splits?

And: what is per-class recall on Stage III and Stage IV, and what lead time do we achieve on them?

---

## Phase 4 — Space

Only worth starting once the temporal model is genuinely good. Spatial detail on a weak forecast is
decoration.

### Tasks

1. **`models/graph.py`** — stations as nodes; edge weights a function of distance *and* how well the wind
   vector aligns with the inter-station bearing. Pollution transports along wind, so the graph must too —
   this is what a distance-weighted interpolation structurally cannot express. Precedent: AirPhyNet,
   TransNet, the ST-GNN literature.
2. **Downscaled surface** — predict onto a regular grid over NCR, using satellite aerosol data and road
   density as auxiliary predictors between stations.
3. **Leave-one-station-out validation** — hold out a station, predict it from the rest, report error.
   Repeat across several stations. Per R7, this is the *only* honest evidence of spatial skill; there is
   no block-level ground truth for anyone, including the operational government systems.

### Gate

> What is the held-out station error in µg/m³, across at least four stations?

State it as a number in `docs/results/`. If it is poor, the map is a visualisation, not a prediction —
and must be labelled as such.

---

## Phase 5 — Decision layer and replay

Turn the forecast into something a person acts on, and make the demo drivable.

### Tasks

1. **Attribution** — SHAP or attention weights, surfaced per forecast: is Thursday bad because of a
   shallow inversion, calm winds, or upwind fires?
2. **Stubble-smoke split** — HYSPLIT back-trajectories against FIRMS detections to estimate transported
   vs local contribution during burning season.
3. **Population-weighted exposure** — WorldPop overlay giving person-hours above threshold, not just a
   concentration map.
4. **Clean-route navigation** *(optional)* — OSMnx road graph with the pollution surface as edge cost.
5. **Historical replay mode** — the demo centrepiece. A judge picks a past date; the system reconstructs
   that day's inputs, shows what it would have forecast, attributes the episode, and compares against what
   CPCB actually recorded. This doubles as genuine forecast validation and it works year-round, which live
   mode does not during a clean-air month.
6. **UI** — MapLibre surface, GRAP probability panel with lead time, driver attribution, prediction
   intervals visible everywhere a number is shown, and a "last synced" indicator per R8.

### Gate

> Can a stranger drive the demo, including replay, without you narrating it?

---

## Suggested first session with Claude Code

Do these in order. They are deliberately small so the spine is real before any modelling starts.

1. Scaffold the repo and `config.toml` (Phase 1, task 1).
2. Build `ingest/cams.py` and pull one month of CAMS forecasts for a single NCR point. Inspect the output
   by hand before writing anything else.
3. Build `ingest/cpcb.py` for the same month and same location.
4. Align the two in `features/build.py` and plot CAMS against observed PM2.5.

That last plot is the whole project in one picture: the gap between the two lines is exactly what the
model is being asked to learn. If that gap looks structured, the approach is sound. If it looks like
noise, find out now.
