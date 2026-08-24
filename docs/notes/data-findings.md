# Data findings — Phase 1

Everything here was measured against the live endpoints, not read from docs.
Dates are the dates the check was run (2026-08-23).

---

## 1. CAMS is coarse, and the numbers say how coarse

Sampling all 50 configured stations returns **21 distinct CAMS grid cells**.
Roughly two and a half stations share every cell, and central Delhi resolves to
a handful of points. This is the concrete form of R7: any neighbourhood-level
structure in our output comes from the downscaling layer, never from CAMS.

## 2. CAMS is biased low over Delhi — which is the whole project

For 1–3 November 2024, CAMS PM2.5 across the NCR stations ran:

| statistic | value (µg/m³) |
|---|---|
| mean | 78.7 |
| median | 78.5 |
| max | 160.7 |

Delhi observed several hundred µg/m³ over the same window. The gap is
structured, not noise, and it is exactly what the correction layer is being
asked to learn.

## 3. Archived CAMS is short-lead, not a 72 h forecast

Open-Meteo's air-quality archive reaches back to at least 2022 and is free, but
it is built from CAMS output at **short lead**. It is not a 72 h forecast.

Checked and ruled out: `pm2_5_previous_day1..7` (the previous-runs variables
that exist for weather) return **0/72 non-null** for `cams_global`, both on past
and future hours. There is no archived-forecast air-quality endpoint.

Consequences, all handled explicitly rather than silently:

* `cams_archive` rows carry `source_class = "archive_short_lead"` and a null
  `lead_h`;
* `cams_runs` — populated by the daily cron from today onward — carries
  `source_class = "live_forecast"` and a real `lead_h`, and is the honest
  training source as it accumulates;
* the ablation must report archive-trained and run-trained results separately
  once `cams_runs` has enough days.

This is the same class of hazard R1 names for ERA5. The rule is not "never use
it", it is "never use it without saying so".

## 4. `boundary_layer_height` exists on GFS only, and only since Sept 2024

Mixing height is the most important single variable for whether today's
emissions stay in the city, so this was checked model by model on the
historical-forecast endpoint at Delhi:

| model | June 2024 | Nov 2024 |
|---|---|---|
| `best_match` | 0/72 | 72/72 |
| `gfs_seamless` | 0/72 | 72/72 |
| `gfs_global` | 0/72 | 72/72 |
| `ecmwf_ifs04` | 0/72 | 0/72 |
| `ecmwf_ifs025` | 0/72 | 0/72 |
| `icon_seamless` | 0/72 | 0/72 |
| `icon_global` | 0/72 | 0/72 |
| `jma_seamless` | 0/72 | 0/72 |

Availability by date on `gfs_seamless`:

| date | non-null |
|---|---|
| 2022-11-01 | 0/24 |
| 2023-11-01 | 0/24 |
| 2024-08-01 | 0/24 |
| 2024-09-01 | 0/24 |
| **2024-09-15** | **24/24** |
| 2024-10-01 | 24/24 |
| 2025-11-15 | 24/24 |
| 2026-08-01 | 24/24 |

Two decisions follow, both recorded in `config.toml`:

1. **The model is pinned to `gfs_seamless`**, not `best_match`. A better model
   with the key variable missing is worse than a decent model that has it, and
   `best_match` silently changes which model answers, so the same query returns
   BLH in one month and nulls in another.
2. **Training history starts 2024-09-15** (`blh_available_from`). That still
   covers two full winter episode seasons (2024-25 and 2025-26), so the split
   blocks were moved into that window.

Earlier history remains usable for everything except BLH, via the documented
proxy below, and every row carries `blh_available` so a model is never quietly
fed a column that is null for half its training span.

### The proxy, if pre-Sept-2024 data is ever needed

`lapse_2m_925 = T(2 m) − T(925 hPa)` — positive when the surface is warmer than
the air ~750 m above it (mixing), negative under an inversion (trapping).
`lapse_925_850` and a boolean `inversion` come along with it. These are useful
predictors in their own right, since Delhi's winter episodes *are* inversions.

## 5. Model visibility is useless here; METAR is not

For 1 November 2024 at Delhi:

* Open-Meteo `visibility`: **24 140 m** (the model's maximum), flat.
* METAR at VIDP: **1.5–2.1 km**, varying hour to hour.

The model diagnostic misses the haze event completely. Since it derives from the
same physics we are correcting, it could not have validated our forecast
anyway. METAR is a real instrument reading and stays the independent check.

Coverage check: VIDP returned **120/120 hours** for a five-day window.
VIDD (Safdarjung) returns nothing from the IEM ASOS archive — it is configured
but empty, and the feature builder selects the station with the most rows.

## 6. CPCB ground truth needs one credential — verified, not assumed

**Resolved 2026-08-23.** The key was obtained; 51 of 51 stations now map to
OpenAQ location ids and ground truth flows from the keyless S3 archive.
What the matching exercise turned up is recorded below, because each item
would have produced a wrong model rather than an error.

Rechecked on 2026-08-23 with browser headers, following redirects:

| route | result |
|---|---|
| `api.openaq.org/v3` | **401** — `"A valid API key must be provided in the X-API-Key header."` |
| `openaq-data-archive` S3 bulk archive | **open, no key** — 55 313 location ids, but no geographic metadata |
| `openaq-fetches` legacy realtime bucket | **empty** — decommissioned |
| `airquality.cpcb.gov.in` | **301 → 200**, live single-page app |
| `app.cpcbccr.com/ccr` | **308 → 200**, live single-page app |
| CPCB data endpoints (`aqi_all_Parameters`, `all_india_stations`) | **404** |
| CPCB CCR download flow | gated behind `captcha/api/v1/captcha/generate` |

The sites are up; the network is fine. The CCR bulk-download path is
**captcha-gated**, so it is not something we automate.

The workable route: one free OpenAQ key resolves our 50 stations to location
ids *once* (`airshed cpcb resolve-ids --apply`), after which the **keyless** S3
bulk archive serves all historical training data. The key is then needed only
for live updates.

Two keyless workarounds were attempted and abandoned as too expensive for the
value: probing fixed dates across id space (1.75 % hit rate) and enumerating all
55 313 ids then sampling (0 Indian stations in a 1 203-id sample, because only
~7 % of ids have recent data). Either could be pushed to completion with ~110 k
requests against a free public bucket; a free key is the better trade.

## 7. Official station roster obtained

`docs/reference/cpcb-caaqm-stations-ncr.pdf`, pulled from CPCB's own reporting
API (no captcha on that path), lists the official Delhi-NCR CAAQM network:

| region | stations |
|---|---|
| Delhi | 46 |
| Haryana | 23 |
| Uttar Pradesh | 23 |
| Rajasthan | 4 |
| **total** | **96** |

`config.toml` currently carries a 50-station subset. Names and operating
agencies now check out against this roster. The PDF has **no coordinates**, so
`coord_quality` stays `"approximate"` until `resolve-ids` replaces them with
OpenAQ's published positions (`--emit-toml` writes the replacement block).

Delhi stations in the official roster and not yet in config: Cantonment Area,
Commonwealth Sports Complex, IGNOU Maidan Garhi, JNU, NSUT Jaffarpur, Talkatora
Garden, IIT Delhi, Lodhi Road (IITM, distinct from IMD Lodhi Road), Pusa (IITM,
distinct from Pusa IMD).

---

## 8. Matching stations to OpenAQ is not a proximity problem

Three separate failure modes showed up, each of which silently produces a
plausible-looking but wrong dataset. All three are now handled in
`ingest/cpcb.py::resolve_stations`.

### Retired duplicate registrations

OpenAQ carries several registrations per CAAQMS station, most of them dead:

| id | station | first | last |
|---|---|---|---|
| 235 | Anand Vihar | 2016-02-05 | 2026-08-23 (live) |
| 5509 | Anand Vihar | 2018-03-09 | 2022-10-31 |
| 10487 | Anand Vihar | 2018-08-13 | 2021-09-20 |
| 5613 | ITO | 2018-03-09 | 2026-08-23 (live) |
| 10489 | ITO | 2020-01-20 | 2022-10-31 |

Nearest-coordinate matching picked **10487** and **10489** — both retired years
ago. Ground truth would have come back empty for our 2024-26 window, and under
R6 an empty series is indistinguishable from a routine CAAQMS outage. Matching
now requires the candidate record to overlap the training window.

### Names disambiguate what coordinates cannot

Faridabad has "New Industrial Town" and "Sector 11" 1.6 km apart. With an
approximate coordinate, the wrong one won by 450 m and looked like a good
match. Matching now scores name overlap first, ignoring boilerplate words, and
treats contradicting digits ("Sector 11" vs "Sector 51") as disqualifying.

### Greedy assignment propagates one error into two

Sector 16A is dead in OpenAQ (both registrations ended, 2018 and 2022). Because
stations were matched one at a time in config order, Sector 16A claimed New
Industrial Town, which then had to settle for Sector 11 — one missing station,
two wrong matches. All (station, location) pairs are now ranked together and
assigned best-first, and a station with no live counterpart is left unresolved
with a warning rather than matched to something nearby.

Result: **HR004 was repointed** to Sector 11 (live since 2020, the longest
Faridabad record) and **Sector 30** added as HR007, giving 51 stations.

## 9. The raw CPCB feed needs the quality flags it is given

Two weeks of November 2025, 51 stations, 14,841 station-hours:

| flag | hours |
|---|---|
| ok | 14 606 |
| suspect_low | 143 |
| stuck | 71 |
| suspect_high | 21 |

The flagged values are not marginal. `-9999.0` sentinels appear at UP001, and
HR003 reports **1 630 733 µg/m³**. Left in, the raw mean is 463.6 µg/m³ against
a cleaned median of 184. Flagged rows are excluded from `pm25_clean` but kept in
`pm25`, so nothing is destroyed and the exclusion is auditable.

Also: the archive stamps readings in **local time with an offset**
(`2025-11-01T00:45:00+05:30`) at **15-minute resolution**. Parsing must read the
offset and convert to UTC *before* truncating to the hour; truncating first bins
by IST half-hours and smears every hourly mean across two real hours.

## 10. The correction target, measured

CAMS against CPCB observations, 1-14 November 2025, 14 553 paired
station-hours:

| | PM2.5 (µg/m³) |
|---|---|
| observed mean | 210.8 |
| CAMS mean | 80.8 |
| observed median | 184.8 |
| CAMS median | 81.5 |
| observed p95 | 448.0 |
| CAMS p95 | 123.9 |

**Bias −130.0 µg/m³. RMSE 175.6. Correlation 0.326. CAMS predicts 38% of
observed.**

This is the result the project needed to see before building anything on it.
The error is a large, consistent scale bias rather than noise, and the positive
correlation says CAMS does carry timing information — it knows *when*, not *how
much*. That is a learnable correction, and it is why the architecture puts a
correction layer on top of a physics forecast instead of training a forecaster
from scratch.

Figure: `docs/results/cams_vs_observed_2025-11-01_2025-11-14.png`.

---

## 11. The trainable era is an intersection, and it is narrow

A row can train the correction model only if **all three** of ground truth,
CAMS and forecast meteorology exist for that hour. Each begins at a different
date, measured against the live endpoints:

| source | available from | note |
|---|---|---|
| Forecast meteorology (GFS, historical-forecast) | ~2021-06 | 2021-03 returns 0/24, 2021-06 returns 24/24 |
| `boundary_layer_height` on that endpoint | 2024-09-15 | earlier years fall back to the lapse-rate proxy |
| CAMS air-quality archive | ~2022-11 | 2022-06 returns 0/24, 2022-11 returns 24/24 |
| CPCB observations (OpenAQ) | 2025-02-18 | the 2018-2025 gap |
| CPCB observations (Kaggle archive) | 2015-01 to 2020-07 | see below |

Intersecting them gives a trainable period of **2025-02 onward** — one winter.
CAMS is the binding constraint at the front, ground truth at the back.

### What that means for the Kaggle archive

The 2015-2020 CPCB history loads cleanly (854,640 hourly rows, 50 of 51
stations matched by name) but **cannot train or evaluate the corrector**: there
is no CAMS and no forecast meteorology for those years, so the entire input
side of the model is missing. Rows carry `source = "kaggle_cpcb_2015_2020"`,
and the supervised builder drops them for want of features — silently and
correctly, but worth knowing before wondering where they went.

It is still worth having, for the things that need observations alone:

| | 2015-2020 (Kaggle) | 2025-2026 (OpenAQ) |
|---|---|---|
| Severe (Stage III) hours | 1,860 | 233 |
| Severe+ (Stage IV) hours | **698** | 54 |

That is a **13x larger Stage IV sample** for describing how episodes behave —
their duration, seasonality and onset — even though it cannot be used to score
a forecast. It also gives five extra years of station-quality history for the
R6 step-change checks.

The two eras also differ in level: Stage III or worse covers 7.5% of hours in
2015-2020 against 2.5% in 2025-2026. Some of that is a genuine improvement in
Delhi's air and some is a different station mix; the comparison is suggestive,
not a trend estimate, and should not be presented as one.

### What would actually extend the trainable period

**CPCB observations for 2022-11 to 2025-02.** That window is the one where CAMS
and meteorology both already exist and only ground truth is missing. Filling it
would add roughly 2.3 years and **two more winter episode seasons**, which is
what the rolling-origin comparisons need to resolve effects of about 1%.
Earlier years than 2022-11 add nothing to model training, whatever their
quality, because CAMS does not go back that far.

---

## 12. The archives are short-lead, and only some of it can be undone

Section 3 recorded that archived CAMS is short-lead. The same is true of the
archived *meteorology*, and it was not flagged: `historical-forecast-api`
returns the best available forecast for each past hour, which in practice is a
forecast a few hours old. So the column the model was trained to read as "the
meteorology 72 hours ahead" was, in training, nothing of the sort.

The fingerprint is in `ablation.md` itself. `full` scores 62.6 / 62.9 / 63.8
across 24 / 48 / 72 h — essentially flat — and `raw-cams` *improves* with lead,
95.2 -> 93.1. No forecast improves with lead. Both rows are reading a valid-time
value, not a forecast at that lead.

### The Previous Runs API fixes part of it

`https://previous-runs-api.open-meteo.com/v1/forecast` serves `<var>_previous_dayN`:
for a valid hour on day D, the value from the run initialised on day D-N.

**Semantics, verified rather than read off the docs.** Queried for valid hours
on 2026-08-23 and 2026-08-24 and compared against the runs this repo had already
archived itself:

| our stored run | valid day | best-matching API series | max diff |
|---|---|---|---|
| issued 2026-08-24 | 2026-08-24 (lead 0-23) | `temperature_2m` | **0.000** |
| issued 2026-08-23 | 2026-08-23 (lead 0-23) | `temperature_2m` | 1.400 |
| issued 2026-08-23 | 2026-08-24 (lead 24-47) | `temperature_2m_previous_day1` | 1.400 |

The exact zero settles it: `previous_dayN` is indexed on the **valid day**, so
true lead is `24N + hour_of_day`. Horizon 24 h -> `previous_day1`, 48 h -> day 2,
72 h -> day 3, and the mapping is deliberately pessimistic — a 72 h horizon is
scored against a forecast at least 72 hours old, sometimes 95.

Coverage is complete over the whole trainable era: 48/48 non-null at 2025-02-18,
2025-06-01, 2025-11-05 and 2026-08-20. Backfilled to 553 days, 2,030,616 rows,
51 stations, zero nulls.

There is **no lag**: `previous_day3` returns 24/24 for *today's* valid hours,
because the run from three days ago already forecast this far ahead. Only future
valid days would be short. Checked, because the daily job was briefly written to
skip four days on the assumption that a lag existed.

### What cannot be lead-matched, and it includes the important one

Not every variable has a `_previous_dayN` form, and the gaps are not random:

| variable | `_previous_dayN` | how it fails |
|---|---|---|
| 13 surface variables (temperature, humidity, dew point, pressure, wind 10 m and 100 m, gusts, precipitation, cloud, radiation, CAPE) | yes | — |
| `boundary_layer_height` | **no** | 0/72 non-null, on `gfs_seamless`, `gfs_global`, `gfs025`, `ecmwf_ifs025`, `icon_seamless` and `best_match` alike |
| `visibility` | **no** | 0/72 non-null |
| `temperature_925hPa`, `temperature_850hPa`, `wind_speed_925hPa`, `wind_direction_925hPa`, `geopotential_height_925hPa` | **no** | HTTP 400 — the API rejects `_previous_dayN` on pressure levels outright |

BLH is the single most important variable in the set and the one the whole
coupling argument rests on, and the derived `inversion`, `lapse_*` and
`ventilation_index` features inherit the problem. Those columns stay short-lead
in training. At *serving* time they are genuine long-lead values from the live
run, so this is a residual train/serve gap that closes only forward, as
`meteo_runs` accumulates.

CAMS PM2.5 has no archived-forecast endpoint at all (section 3), so the same is
true of it and more so.

### How much it was worth

`leadmatch.md` has the table. On the single test split the swap costs +1.1% at
72 h and +2.0% at 48 h, and nothing at 24 h — the right shape, since at 24 h
lead the two sources are closest. But the effect is the same size as the fires
and upwind effects this project already refuses to claim from one split, so it
gets the same treatment: see the `lead-matched meteorology` row in
`rolling.md` before quoting a number.

Meanwhile the inputs themselves moved a great deal — wind direction RMSE
86 -> 97 degrees and temperature 1.60 -> 2.32 K between lead day 1 and 3. Large
input change, small output change, which says the corrector leans on CAMS and
observation history far more than on the lead-sensitive meteorology. That makes
short-lead **CAMS** the larger remaining problem, not the meteorology.

---

## 13. The CAMS train/serve gap, measured

Section 3 established that archived CAMS is short-lead. Section 12 fixed the
equivalent problem for meteorology using the Previous Runs API. **There is no
such fix for CAMS**, and that was confirmed against both hosts on 2026-08-24:

| probe | result |
|---|---|
| `previous-runs-api.open-meteo.com/v1/air-quality` | **HTTP 404** — the host serves weather only |
| `pm2_5_previous_day1`, `pm2_5_previous_day3` on the air-quality endpoint | **0/48 non-null** |

So the archive cannot be lead-matched, and the only evidence about the gap is
the overlap between forecast runs we archive ourselves and the archive values
for the same hours. That accrues one day per successful run of the daily job and
by no other means.

### What the overlap says so far

`airshed camsoffset` -> `docs/results/camsoffset.md`. On the first two archived
runs:

| lead day | true lead | rows | run days | archive mean | run mean | bias | RMSE |
|---|---|---|---|---|---|---|---|
| 0 | 0-23 h | 2,448 | 2 | 70.0 | 53.9 | **-16.2** | 25.4 |
| 1 | 24-47 h | 1,224 | 1 | 79.9 | 59.3 | **-20.6** | 22.9 |

Both sides resolve to the same 21 CAMS cells, so this is lead, not geometry.
The direction is the one that costs: the served input sits *below* the input the
corrector was fitted against, which pushes the live forecast low.

### Why no correction has been applied

Two run days is two observations, not 2,448. A day's overlap is 51 stations
across 21 cells under one weather situation; the rows are nowhere near
independent, and the unit of independence is the run day. Every interval in
`camsoffset.md` therefore comes from a bootstrap over whole issue dates, and is
**withheld entirely below five run days** — resampling two days with replacement
has three possible outcomes and yields something that looks like a tight
interval while carrying no information. The first draft of that module printed
"-16.9 to -15.4" from two days, which is exactly the number that would have got
a bogus correction wired into serving.

A correction is gated at **20 settled run days** (`MIN_RUN_DAYS`), where settled
means the archive value is at least four days old — a recent archive hour can
still be revised, and that revision is not what we are trying to measure.

The regime argument matters more than the sample-size one. A bias fitted on
monsoon air, when Delhi runs at 70 µg/m³, would be applied to a November episode
at 400. That is the regime where the correction matters most and generalises
least, so the honest position is to measure the gap, report it on the forecast
itself, and leave the numbers uncorrected until there is winter data.

### Where it is stated

Not only in this file. `/api/forecast` returns an `input_gap` object with the
measured bias and the run-day count, the dashboard prints it beside the live
forecast table, and the daily job logs the count each morning — so a job that
stops running shows up as a number that stops moving.

---

## 14. The station expansion, 51 -> 77

`airshed cpcb discover` inverts `resolve-ids`: instead of starting from a
hand-written roster and hunting each entry's OpenAQ id, it reads what OpenAQ
actually serves in the NCR bbox. Coordinates are operator-published and liveness
is a fact rather than a hope.

Of **125** PM2.5 locations in the bbox, 51 were already configured and **26** were
added. The exclusions are the interesting part:

| excluded | count | why |
|---|---|---|
| `caaqm`-provider registrations | 27 | **every one is dead** — all end 2018 or 2022. Provider turns out to be a clean signal for the duplicate-registration trap in section 8 |
| AirGradient, Clarity | 8 | low-cost sensors; CLAUDE.md puts sensor fusion out of scope |
| AirNow, StateAir | 2 | US diplomatic posts — a different network with its own calibration, and GRAP is defined on the CAAQMS average |
| dead CPCB registrations | ~10 | ended 2018-2025, or lived only weeks |
| `Pusa, Delhi - DPCC` | 1 | co-located, see below |

Result: 77 stations — DL 44, UP 17, HR 14, RJ 2 (Rajasthan is new).

### GRAP's city average had no city filter

`build_city_base` averaged **every** configured station. With 51 stations that
was already 27% non-Delhi (Gurugram, Noida, Ghaziabad, Faridabad); the expansion
reaches Meerut at 60 km, Bhiwadi at 57 and Dharuhera at 59, which would have put
Delhi at 53% of its own average.

CAQM invokes GRAP on **Delhi's** AQI, computed by CPCB from Delhi's own
stations. Averaging in the NCR ring produces a different quantity that looks
similar, and forecasting it against statutory Delhi thresholds means forecasting
the wrong number accurately. `grap.city_average_cities` now scopes the
aggregate; the station model still trains on all 77.

### Co-located stations break leave-one-station-out

`Pusa, Delhi - IMD` (id 5404, configured as DL024) and `Pusa, Delhi - DPCC`
(id 6356) are both live and sit at 28.639645, 77.146262 — the same point to the
metre. Two agencies on one campus, most likely.

Including both would double-weight that site in the city average and, worse,
hand leave-one-station-out a perfect twin: a held-out station predicted from a
copy of itself is not measuring spatial skill (R7). `COLOCATED_KM = 0.25`
excludes it. The threshold is tight on purpose — NISE Gwal Pahari and TERI Gram
are 590 m apart and are two genuinely different stations, so anything under 2 km
is flagged for a human rather than dropped.

### `expand-stations` is right for CAMS and wrong for meteorology

`repair.expand` copies a cell-mate's rows and needs no network. Whether that is
correct depends entirely on the grid:

| dataset | cell size | furthest new station from a served cell | verdict |
|---|---|---|---|
| `cams_archive` | ~0.4 deg | 0.364 deg (MIET Meerut) | inside one cell — copying is what a fresh request would return |
| `meteo_archive` | ~0.11 deg | **0.444 deg** (MIET Meerut) | **four cells out**; copying would have assigned weather from ~49 km away |

Meteorology was refetched properly for the new stations. Nothing would have
complained if it had not been.

### Three caches that did not notice config had grown

Each one succeeded while quietly doing less than it claimed:

1. **`sensor_ids()`** returned its cached map wholesale. All 26 stations
   backfilled correctly and then appeared *absent* from the last four days,
   because those come from the live API path — and under R6 an absent station is
   indistinguishable from a CAAQMS outage. `resolve_cells` had guarded against
   this since Phase 1; `sensor_ids` had not.
2. **The fix then caused a refresh storm.** OpenAQ answers HTTP 500 for Wave
   City's sensors endpoint, so it stayed permanently "missing" and triggered a
   full 76-station relookup — 120 seconds — on every live sync. A failed lookup
   now records an empty entry, so it counts as attempted.
3. **`backfill_previous_runs`** skipped every chunk whose partitions existed, so
   it would have fetched nothing at all for a station added later.

### What exhausted the API quota

`fetch_previous_runs` resolves the cell map itself, and `backfill_previous_runs`
calls it once per chunk. With 10-day chunks over 553 days that is **56 redundant
cell-resolution requests** on top of the real ones, which spent Open-Meteo's
daily allowance and stalled `meteo_leadmatched` at 2026-02-12.

The map is now resolved once per backfill. A daily-quota 429 also raises
`net.DailyQuotaExceeded` and is no longer retried with exponential backoff:
"come back tomorrow" and "slow down" are the same status code and want opposite
handling, and backing off five times against a spent allowance only makes the
failure slower.

**Outstanding:** `meteo_leadmatched` covers 2025-02-18..2026-02-12 for all 77
stations and 2026-02-13 onward for the original 51. Run
`scripts/finish_station_expansion.py` after the allowance resets.

---

## 15. There is a nine-day hole in the winter ground truth, and it is upstream

`cpcb` is missing 11 partitions across the trainable era:

| range | days |
|---|---|
| 2025-04-10 | 1 |
| **2026-01-11 .. 2026-01-19** | **9** |
| 2026-06-24 | 1 |

The nine-day run sits **inside the winter test block**, so every winter number
this project reports is computed on a January with nine days absent.

### It is not our failure, and that was checked both ways

The first suspicion was our own fetcher, because a probe returned an SSL
handshake timeout. That turned out to be an unrelated intermittent fault against
the S3 host. With retries in place the backfill reported a **0% fetch-failure
rate** and still returned nothing, and a direct probe is unambiguous:

| date | DL001 | DL002 | DL006 |
|---|---|---|---|
| 2026-01-09 | 200 | 200 | 200 |
| 2026-01-10 | 200 | 200 | 200 |
| **2026-01-11** | **404** | **404** | **404** |
| **2026-01-15** | **404** | **404** | **404** |
| **2026-01-19** | **404** | **404** | **404** |
| 2026-01-20 | 200 | 200 | 200 |

The v3 API agrees, and it is a separate backend: 48 rows for 2026-01-08..10,
**0 rows** for 2026-01-11..14, 28 rows for 2026-01-20..22. Both of OpenAQ's
paths have nothing, so the data does not exist upstream and **cannot be
recovered from any source this project has**. Treat it as a permanent gap, not a
to-do.

### What the investigation did fix

Two real defects in the fetch path, neither of which caused this gap but either
of which could have manufactured one:

1. **A transport failure was indistinguishable from a 404.** Both returned
   `None`, so "we could not ask" and "we asked and the station was offline" were
   the same value — precisely the confusion R6 exists to prevent. There is now a
   `FETCH_FAILED` sentinel, and transport failures are retried three times
   before being reported.
2. **A partially-failed day was written anyway.** Because a partition that
   exists is treated as cached and complete by every later backfill, persisting
   a crippled day converted a transient network problem into permanent invisible
   data loss. `backfill` now refuses to write a range where more than
   `MAX_FETCH_FAILURE_RATE` (25%) of station-day fetches failed, and says so
   loudly, so the range is retried later instead of being silently sealed.

Covered by `tests/test_archive_gaps.py`.
