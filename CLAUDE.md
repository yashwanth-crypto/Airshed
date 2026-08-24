# Airshed — Air Pollution–Weather Coupled Forecasting System (Delhi NCR)

Smart India Hackathon 2026 · **SIH26082** · Ministry of Earth Sciences · Software · Disaster Management

## What we are building

A 72-hour forecasting system for Delhi NCR that treats air pollution and meteorology as one coupled
system, and outputs **GRAP stage probabilities** (the policy decision) rather than only a
concentration number.

Two outputs matter:

1. **Forecast** — PM2.5, boundary-layer height and visibility, jointly, at 24/48/72 h, with prediction
   intervals, downscaled to a spatial surface finer than the station network.
2. **Decision** — probability that each GRAP stage (I–IV) will be met in the next 72 h, with lead time
   and driver attribution ("upwind fires + shallow inversion").

---

## THE CORE ARCHITECTURAL DECISION — do not refactor this away

**We do not train a forecaster from scratch. We train a correction layer on top of a free physics forecast.**

CAMS (Copernicus Atmosphere Monitoring Service) publishes global PM2.5 forecasts several days ahead,
free and keyless via Open-Meteo. It is coarse (~40 km cells — only a handful cover all of NCR) and
biased over India (global emission inventories represent India poorly).

That combination is exactly what we want:

- **CAMS supplies the future** — predicted winds, transport, regional build-up.
- **Our model supplies the local** — bias correction and sub-grid detail.

Why this is non-negotiable: a model trained only on station history is autoregressive. It knows how
pollution *has* behaved, not what the atmosphere is going to *do*, so skill collapses past ~24 h —
precisely where a 72 h forecast has to work. Feeding it a physics forecast of the future fixes this
structurally.

It also gives us our baseline for free. Raw CAMS is an external, published, internationally-used
forecast. Beating it by a measured RMSE margin is a result, not a claim.

**If you are ever tempted to "simplify" by dropping CAMS and training directly on station history —
don't. That is the thing that makes this project ordinary.**

---

## HARD RULES

These are not style preferences. Each one has sunk air-quality projects that were otherwise fine.

### R1 — Never use ERA5 at inference time
ERA5 reanalysis lags real time by ~5 days. It is the obvious training source and it is **unusable for
live prediction**. Train on archived *forecast* meteorology (Open-Meteo Historical Forecast API /
Previous Runs API), not reanalysis, so training and serving see the same input distribution. If ERA5 is
used for any exploratory work, it must never reach a model that will be served.

### R2 — Persistence goes in every results table
"Tomorrow = today" is a brutally strong AQI baseline that published models routinely fail to beat.
Every evaluation must report persistence alongside the model. A result that does not clear persistence
at a given horizon is a negative result and must be reported as one.

### R3 — Never random-split the time series
Split by time block. Hold out whole episodes (an entire November), not scattered hours. Adjacent hourly
samples leak the answer.

### R4 — No naive recursive rollout
Do not feed predicted PM2.5 back in as the next step's input across 72 h; error compounds. Use direct
multi-horizon output heads (one head per horizon) or scheduled sampling.

### R5 — Report per-class recall for GRAP, never overall accuracy
Stage IV is rare. Overall accuracy will look excellent and mean nothing. Report per-class recall and
lead time on severe classes. Missing a severe episode costs far more than a false alarm — reflect that
in the loss (class weights or focal loss) and in how results are presented.

### R6 — Treat station gaps as missing, not interpolable
CPCB stations go offline, relocate and swap instruments. Do not interpolate across long gaps. Check for
step changes in a station's distribution before trusting its history.

### R7 — Never claim spatial detail that came from CAMS
CAMS is ~40 km. It gives the regional signal and the future transport. Any neighbourhood-level detail
comes from our downscaling layer and must be validated by leave-one-station-out, not asserted.

### R8 — Cache everything; never live-query in the demo path
All ingestion writes to our own store on a schedule. The UI reads the cache and shows a "last synced"
timestamp. A CPCB outage must degrade the demo gracefully, not kill it.

---

## Data sources

| Source | What for | Endpoint / access | Notes |
|---|---|---|---|
| Open-Meteo Air Quality | CAMS PM2.5 forecast (the spine) | `https://air-quality-api.open-meteo.com/v1/air-quality` | Free, **no API key**. `forecast_days` up to 7, `past_days` for recent history. |
| Open-Meteo Forecast | Forecast meteorology (serving) | `https://api.open-meteo.com/v1/forecast` | Free. Check `boundary_layer_height` availability per model (GFS / ECMWF / ICON) — verify before depending on it. |
| Open-Meteo Historical Forecast | Archived past forecasts (**training**) | `https://historical-forecast-api.open-meteo.com/v1/forecast` | **This is how we satisfy R1.** Past initialised forecast runs, not reanalysis. Also see the Previous Runs API. |
| CPCB CAAQMS | Ground truth PM2.5 / AQI | `https://app.cpcbccr.com/ccr` ; mirrors: AQICN, OpenAQ | Historical bulk archives on data.gov.in. Reliability is patchy — see R8. |
| METAR (VIDP) | Hourly visibility, independent validation | Iowa State IEM ASOS archive | Free bulk historical. Direct measurement of the pollution–fog coupling. |
| NASA FIRMS | Active fire detections (stubble) | VIIRS / MODIS active fire products | Free, requires a free registration key. |
| NOAA HYSPLIT | Back-trajectory / smoke attribution | HYSPLIT model | Free. Used to split a spike into transported vs local. |
| CAQM | GRAP stage thresholds | Published GRAP schedule PDF | Statutory thresholds — hardcode from the official schedule, cite it. |
| WorldPop | Gridded population (exposure) | WorldPop rasters | Free. Only needed at Phase 5. |
| ERA5 | Exploration / backfill **only** | Copernicus CDS | **Subject to R1.** Never in the serving path. |

---

## Stack

- **Python 3.11+**, `uv` for dependency management.
- **Storage:** Parquet on disk + **DuckDB** for queries. No database server — it is fast, portable, and
  ideal for hourly time-series analytics. Do not reach for Postgres unless something concretely needs it.
- **Data:** `polars` (preferred) or `pandas`; `xarray` for gridded fields.
- **Models:** start with `LightGBM` for the correction baseline — it is fast, strong on tabular
  time-series features, and gets us to a measured result quickly. Move to `PyTorch` only when the
  ablation shows we need sequence structure. `torch-geometric` for the graph layer at Phase 4.
- **API:** `FastAPI`.
- **Frontend:** React + **MapLibre GL** (open source, no access token — do *not* use Mapbox) with
  `deck.gl` for the pollution surface.
- **Scheduling:** `APScheduler` or plain cron. Keep it boring.

---

## Repo layout

```
src/airshed/
  ingest/       one module per source: cpcb.py cams.py meteo.py metar.py fires.py
  features/     feature construction, aligned to a common hourly index
  models/       baselines.py (persistence, raw CAMS) corrector.py graph.py
  eval/         metrics.py ablation.py
  grap.py       threshold mapping + stage probability
  api/          FastAPI app
data/           gitignored — raw/ and processed/
docs/           BUILD_PLAN.md and design notes
notebooks/      exploration only; nothing here is imported by src/
tests/
```

`baselines.py` is written **before** `corrector.py`. Always.

---

## Conventions

- All timestamps **UTC** internally, converted to IST only at the presentation layer. Every dataframe
  with a time column carries a tz-aware index.
- Every ingest module exposes the same shape: `fetch(start, end, **kwargs) -> pl.DataFrame` and writes
  Parquet partitioned by date. Idempotent — re-running a fetch overwrites cleanly.
- Every model exposes `fit(X, y)` / `predict(X) -> quantiles` returning 10th/50th/90th percentile, not a
  point estimate.
- Config in one `config.toml`; no magic numbers scattered in modules. Station coordinates, GRAP
  thresholds and grid bounds all live there.
- Tests for the feature-building and evaluation code specifically — those are where silent correctness
  bugs (leakage, misalignment) hide. Model quality is checked by the ablation table, not unit tests.

---

## Git and GitHub

**Every commit and every push is authored by `yashwanth-crypto`. Nothing else.**

- **No co-author trailers.** Do not append `Co-Authored-By:` lines to commit messages — not for Claude,
  not for any tool, not for anyone.
- **No tool attribution anywhere.** Commit messages, PR titles, PR bodies and issue descriptions must not
  mention Claude, Claude Code, or any AI assistant, and must not carry generated-with footers, session
  links or badges.
- **No `Assisted-by:`, `Generated-with:` or equivalent trailers**, in any wording.
- Before the first commit, confirm the repo identity is set:

  ```bash
  git config user.name  "yashwanth-crypto"
  git config user.email "<the email attached to the yashwanth-crypto GitHub account>"
  ```

  Use the address GitHub associates with that account so commits attribute correctly on the contribution
  graph. If you prefer to keep the address private, use the GitHub-provided `users.noreply.github.com`
  address for the account.

- Commit messages: short imperative subject (max ~72 chars), a blank line, then body only when the change
  needs explanation. Describe the change, not the process that produced it.
- Never commit anything under `data/` — it is gitignored, and pushing station archives or model
  checkpoints will bloat the repo permanently.
- Never commit API keys or credentials. FIRMS and any other keyed source read from environment variables
  or an untracked `.env`.

---

## Definition of done, per phase

See `docs/BUILD_PLAN.md` for the full breakdown. Each phase ends at a gate that is a yes/no question
with a number attached. Do not start a phase before its predecessor's gate is answered.

---

## Explicitly out of scope

Do not build these, and do not let scope drift toward them:

- **A chemical transport model.** Running WRF-Chem is a multi-month HPC undertaking and the ministry
  already has one. We consume physics; we do not reimplement it.
- **Hospital admission correlation.** Not public at usable granularity. Model exposure instead.
- **CCTV visibility nowcasting.** Access and privacy complications. METAR gives the same signal cleanly.
- **Low-cost sensor fusion**, unless we actually obtain network access or deploy our own units. Roadmap
  item, not a promise.

---

## The question we will be asked

A judge will know MoES already funds SAFAR and the Delhi Decision Support System. The answer:

> "We don't replace the physics model — we correct it. Here is the bias in the operational forecast,
> here is our correction, here is the RMSE reduction, and the same method applies to your model as
> easily as to CAMS."

That answer only exists if Phase 2 was built. Which is why Phase 2 comes before the dashboard.
