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
