"""CPCB CAAQMS ground truth — the thing we are trying to predict.

Two backends, chosen by what you have:

`archive`  OpenAQ's public S3 bulk archive. **No API key.** One gzipped CSV per
           location per day, back to whenever the station was commissioned.
           This is the training source. It needs `openaq_id` filled in
           `config.toml`, which `resolve_ids()` does once.

`api`      OpenAQ v3 REST, needs `OPENAQ_API_KEY` (free, instant registration).
           Used for station discovery and for recent/live observations, since
           the S3 archive lags by roughly a day.

R6 is enforced here, not downstream. CPCB stations go offline, get relocated
and have instruments swapped, so:

  * hours with no report are simply absent — never forward-filled;
  * every hour carries `n_obs` and a `quality_flag`;
  * `flag_step_changes()` reports distribution breaks so a station whose
    instrument changed mid-series can be excluded rather than silently trusted.
"""

from __future__ import annotations

import datetime as dt
import gzip
import io
import json
import logging
import math
import os
import re
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import httpx
import polars as pl

from ..config import Config, Station, load_config
from ..net import USER_AGENT, get_json
from ..store import missing_dates, write_partitioned

log = logging.getLogger(__name__)

DATASET = "cpcb"
UPWIND_DATASET = "cpcb_upwind"
PARAMETER = "pm25"

# Physically implausible values seen in CAAQMS feeds. Flagged, never deleted
# silently, and never repaired by interpolation.
MIN_PLAUSIBLE = 1.0
MAX_PLAUSIBLE = 1200.0
STUCK_HOURS = 6  # identical value for this many consecutive hours = instrument fault

ARCHIVE_WORKERS = 8


# ---------------------------------------------------------------------------
# Backend A — public S3 archive (no key)
# ---------------------------------------------------------------------------
def fetch_archive(
    start: dt.date | str,
    end: dt.date | str,
    cfg: Config | None = None,
    stations: list[Station] | None = None,
) -> pl.DataFrame:
    """Hourly PM2.5 per station from the OpenAQ bulk archive."""
    cfg = cfg or load_config()
    base = cfg.source("cpcb")["archive_bucket_url"]
    start_d, end_d = _as_date(start), _as_date(end)
    targets = [s for s in (stations or cfg.stations) if s.resolved]

    unresolved = [s.id for s in (stations or cfg.stations) if not s.resolved]
    if unresolved:
        log.warning(
            "%d stations have no openaq_id and were skipped: %s ... "
            "run `airshed cpcb resolve-ids` once (needs OPENAQ_API_KEY)",
            len(unresolved),
            ", ".join(unresolved[:5]),
        )
    if not targets:
        return pl.DataFrame()

    jobs = [
        (s, day)
        for s in targets
        for day in _days(start_d, end_d)
    ]
    frames: list[pl.DataFrame] = []
    with httpx.Client(
        timeout=httpx.Timeout(60.0, connect=20.0), headers={"User-Agent": USER_AGENT}
    ) as client:
        with ThreadPoolExecutor(max_workers=ARCHIVE_WORKERS) as pool:
            for df in pool.map(lambda job: _fetch_day(client, base, *job), jobs):
                if df is not None and not df.is_empty():
                    frames.append(df)

    if not frames:
        log.warning("archive returned nothing for %s..%s", start_d, end_d)
        return pl.DataFrame()

    raw = pl.concat(frames, how="vertical_relaxed")
    return _to_hourly(raw)


def _fetch_day(
    client: httpx.Client, base: str, station: Station, day: dt.date
) -> pl.DataFrame | None:
    key = (
        f"records/csv.gz/locationid={station.openaq_id}"
        f"/year={day.year}/month={day.month:02d}"
        f"/location-{station.openaq_id}-{day:%Y%m%d}.csv.gz"
    )
    try:
        resp = client.get(f"{base}/{key}")
    except httpx.HTTPError as exc:  # network blip on one day is not fatal
        log.warning("archive fetch failed %s %s: %s", station.id, day, exc)
        return None
    if resp.status_code == 404:
        return None  # station offline that day — a real gap, left as a gap (R6)
    if resp.status_code != 200:
        log.warning("archive %s %s -> HTTP %s", station.id, day, resp.status_code)
        return None

    text = gzip.decompress(resp.content).decode("utf-8", errors="replace")
    df = pl.read_csv(io.StringIO(text), infer_schema_length=0)
    if df.is_empty() or "parameter" not in df.columns:
        return None
    df = df.filter(pl.col("parameter") == PARAMETER)
    if df.is_empty():
        return None
    return df.select(
        pl.col("datetime"),
        pl.col("value").cast(pl.Float64, strict=False).alias("value"),
        pl.lit(station.id).alias("station_id"),
    )


# ---------------------------------------------------------------------------
# Backend B — OpenAQ v3 API (needs key)
# ---------------------------------------------------------------------------
def _api_key() -> str:
    from ..env import load_dotenv

    load_dotenv()  # also works when called from a notebook or a test
    key = os.environ.get("OPENAQ_API_KEY", "").strip()
    if not key:
        raise RuntimeError(
            "OPENAQ_API_KEY is not set. Register free at "
            "https://explore.openaq.org/register and put the key in .env, or use "
            "the keyless S3 archive backend once station ids are resolved."
        )
    return key


def resolve_stations(
    cfg: Config | None = None,
    max_km: float = 3.0,
    window_start: dt.datetime | None = None,
    window_end: dt.datetime | None = None,
) -> list[dict]:
    """Map configured stations to OpenAQ location ids by proximity.

    One-off, needs a key. Match radius is deliberately tight: our coordinates
    are approximate, but two CAAQMS stations are rarely within 3 km, and a
    wrong match silently trains the model on the wrong neighbourhood.
    """
    cfg = cfg or load_config()
    window_start = window_start or dt.datetime.fromisoformat(
        cfg.source("meteo")["blh_available_from"]
    ).replace(tzinfo=dt.timezone.utc)
    window_end = window_end or dt.datetime.now(dt.timezone.utc)
    src = cfg.source("cpcb")
    lat_min, lon_min, lat_max, lon_max = cfg.bbox()
    headers = {"X-API-Key": _api_key()}

    locations: list[dict] = []
    page = 1
    while True:
        payload = get_json(
            f"{src['api_url']}/locations",
            params={
                "bbox": f"{lon_min},{lat_min},{lon_max},{lat_max}",
                "limit": 1000,
                "page": page,
            },
            headers=headers,
        )
        results = payload.get("results", [])
        locations.extend(results)
        if len(results) < 1000:
            break
        page += 1
    log.info("openaq returned %d locations in the NCR bbox", len(locations))

    candidates = [
        loc
        for loc in locations
        if any(s.get("parameter", {}).get("name") == PARAMETER for s in loc.get("sensors", []))
    ]

    # Score every (station, location) pair, then assign globally best-first.
    #
    # Assigning station by station is what produced the Faridabad tangle:
    # "Sector 16A" is dead in OpenAQ (both registrations ended, 2018 and 2022),
    # so processing it first let it claim "New Industrial Town", which then had
    # to settle for "Sector 11" — two wrong matches from one missing station.
    # Ranking all pairs together means the confident matches are made first and
    # a station with no live counterpart simply goes unmatched.
    pairs = []
    for station in cfg.stations:
        for loc in candidates:
            coords = loc.get("coordinates") or {}
            if coords.get("latitude") is None:
                continue
            km = _haversine_km(station.lat, station.lon, coords["latitude"], coords["longitude"])
            if km > max_km:
                continue
            first, last = _coverage(loc)
            pairs.append(
                {
                    "covers": _covers_window(first, last, window_start, window_end),
                    "name_score": _name_score(station, loc),
                    "span": (last - first).days if first and last else 0,
                    "km": km,
                    "station": station,
                    "loc": loc,
                }
            )
    pairs.sort(key=lambda p: (p["covers"], p["name_score"], p["span"], -p["km"]), reverse=True)

    records: list[dict] = []
    used: set[int] = set()
    matched: set[str] = set()
    for pair in pairs:
        station, loc = pair["station"], pair["loc"]
        if station.id in matched or loc["id"] in used:
            continue
        # A match must both cover our study window and agree on the name.
        # Distance alone is not evidence when the coordinate is approximate,
        # and a dead station is worse than no station: it yields empty ground
        # truth that reads as a routine outage (R6) instead of a wrong id.
        if not pair["covers"] or pair["name_score"] <= 0.0:
            continue
        matched.add(station.id)
        used.add(int(loc["id"]))
        coords = loc["coordinates"]
        records.append(
            {
                "station_id": station.id,
                "openaq_id": int(loc["id"]),
                "openaq_name": loc.get("name", "?"),
                # OpenAQ carries the operator-published position, which is
                # better than our assembled guess — see coord_quality.
                "lat": round(float(coords["latitude"]), 6),
                "lon": round(float(coords["longitude"]), 6),
                "distance_km": round(pair["km"], 3),
                "first": str((loc.get("datetimeFirst") or {}).get("utc", ""))[:10],
                "last": str((loc.get("datetimeLast") or {}).get("utc", ""))[:10],
            }
        )
        log.info(
            "%-6s %-30s -> openaq %-7d %-40s (%.2f km)",
            station.id, station.name, loc["id"], loc.get("name", "?"), pair["km"],
        )

    for station in cfg.stations:
        if station.id in matched:
            continue
        near = [p for p in pairs if p["station"].id == station.id]
        if not near:
            log.warning(
                "%-6s %-30s -> no OpenAQ location within %.1f km",
                station.id, station.name, max_km,
            )
            continue
        live = [p for p in near if p["covers"]]
        if not live:
            best = min(near, key=lambda p: p["km"])
            log.warning(
                "%-6s %-30s -> nothing live nearby; closest is %r, last reported %s. "
                "Left unresolved rather than matched to a retired station.",
                station.id, station.name, best["loc"].get("name", "?"),
                str((best["loc"].get("datetimeLast") or {}).get("utc", "?"))[:10],
            )
        else:
            best = max(live, key=lambda p: (p["name_score"], -p["km"]))
            log.warning(
                "%-6s %-30s -> live candidates nearby but none share a name "
                "(closest %r, %.2f km). Left unresolved; check by hand.",
                station.id, station.name, best["loc"].get("name", "?"), best["km"],
            )
    return records


def resolve_ids(cfg: Config | None = None, max_km: float = 3.0) -> dict[str, int]:
    """station_id -> OpenAQ location id."""
    return {r["station_id"]: r["openaq_id"] for r in resolve_stations(cfg, max_km)}


def emit_station_toml(records: list[dict], cfg: Config | None = None) -> str:
    """Render a replacement `stations` block using OpenAQ's published coordinates.

    Paste over the block in `config.toml` and set coord_quality = "openaq".
    Stations that did not match keep their existing approximate position and an
    openaq_id of 0, so the file stays a complete roster rather than silently
    shrinking to whatever OpenAQ happened to carry.
    """
    cfg = cfg or load_config()
    by_id = {r["station_id"]: r for r in records}
    lines = ["stations = ["]
    for s in cfg.stations:
        r = by_id.get(s.id)
        lat = r["lat"] if r else s.lat
        lon = r["lon"] if r else s.lon
        oid = r["openaq_id"] if r else 0
        lines.append(
            f'  {{ id = "{s.id}", name = "{s.name}", city = "{s.city}", '
            f'agency = "{s.agency}", lat = {lat}, lon = {lon}, openaq_id = {oid} }},'
        )
    lines.append("]")
    return "\n".join(lines)


def apply_ids_to_config(mapping: dict[str, int], cfg: Config | None = None) -> int:
    """Write resolved ids back into config.toml, one station line at a time."""
    cfg = cfg or load_config()
    path = cfg.root / "config.toml"
    text = path.read_text(encoding="utf-8")
    changed = 0
    for station_id, openaq_id in mapping.items():
        pattern = re.compile(
            rf'(\{{\s*id = "{re.escape(station_id)}".*?openaq_id = )(\d+)', re.DOTALL
        )
        text, n = pattern.subn(rf"\g<1>{openaq_id}", text, count=1)
        changed += n
    path.write_text(text, encoding="utf-8")
    load_config.cache_clear()
    return changed


SENSOR_CACHE = "openaq_sensors.json"


def sensor_ids(cfg: Config | None = None, refresh: bool = False) -> dict[str, list[int]]:
    """station_id -> PM2.5 sensor ids, cached on disk.

    The mapping changes only when CPCB re-commissions an instrument, but
    looking it up costs one API call per station against a 60-per-minute
    limit. Fetching it on every live sync is what turned a routine refresh
    into a wall of 429s.
    """
    cfg = cfg or load_config()
    path = cfg.processed_dir / SENSOR_CACHE
    if path.is_file() and not refresh:
        cached = json.loads(path.read_text(encoding="utf-8"))
        if cached:
            return {k: list(v) for k, v in cached.items()}

    src = cfg.source("cpcb")
    headers = {"X-API-Key": _api_key()}
    mapping: dict[str, list[int]] = {}
    for station in cfg.stations:
        if not station.resolved:
            continue
        try:
            payload = get_json(
                f"{src['api_url']}/locations/{station.openaq_id}/sensors", headers=headers
            )
        except Exception as exc:
            log.warning("sensor lookup failed for %s: %s", station.id, str(exc)[:120])
            continue
        ids = [
            s["id"]
            for s in payload.get("results", [])
            if s.get("parameter", {}).get("name") == PARAMETER
        ]
        if ids:
            mapping[station.id] = ids
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(mapping, indent=1), encoding="utf-8")
    log.info("cached PM2.5 sensor ids for %d stations", len(mapping))
    return mapping


def fetch_recent(
    hours: int = 72,
    cfg: Config | None = None,
) -> pl.DataFrame:
    """Recent observations from the v3 API — the live path the archive cannot serve.

    The S3 bulk archive lags by several days, which is fine for training and
    useless for a forecast that needs this morning's readings. This is the only
    place the live system talks to a keyed API.
    """
    cfg = cfg or load_config()
    src = cfg.source("cpcb")
    headers = {"X-API-Key": _api_key()}
    end = dt.datetime.now(dt.timezone.utc)
    start = end - dt.timedelta(hours=hours)
    sensors = sensor_ids(cfg)

    frames = []
    for station_id, ids in sensors.items():
        for sensor_id in ids:
            try:
                payload = get_json(
                    f"{src['api_url']}/sensors/{sensor_id}/hours",
                    params={
                        "datetime_from": start.strftime("%Y-%m-%dT%H:%M:%SZ"),
                        "datetime_to": end.strftime("%Y-%m-%dT%H:%M:%SZ"),
                        "limit": 1000,
                    },
                    headers=headers,
                )
            except Exception as exc:
                log.warning("live fetch failed for %s: %s", station_id, str(exc)[:120])
                continue
            rows = payload.get("results", [])
            if not rows:
                continue
            frames.append(
                pl.DataFrame(
                    {
                        "datetime": [r["period"]["datetimeFrom"]["utc"] for r in rows],
                        "value": [r.get("value") for r in rows],
                        "station_id": [station_id] * len(rows),
                    }
                ).with_columns(pl.col("value").cast(pl.Float64, strict=False))
            )
    if not frames:
        log.warning("live observation fetch returned nothing")
        return pl.DataFrame()
    return _to_hourly(pl.concat(frames, how="vertical_relaxed"))


def sync_recent(hours: int = 72, cfg: Config | None = None) -> list[Path]:
    """Fetch and cache recent observations so the live forecast has history."""
    df = fetch_recent(hours=hours, cfg=cfg)
    if df.is_empty():
        return []
    return write_partitioned(df, DATASET)


# ---------------------------------------------------------------------------
# Uniform entry points
# ---------------------------------------------------------------------------
def fetch(start: dt.date | str, end: dt.date | str, **kwargs) -> pl.DataFrame:
    return fetch_archive(start, end, **kwargs)


BACKFILL_CHUNK_DAYS = 15


def backfill_upwind(
    start: dt.date | str,
    end: dt.date | str,
    cfg: Config | None = None,
    skip_existing: bool = True,
) -> list[Path]:
    """Cache the upwind corridor into its own dataset.

    Kept apart from `cpcb` so an upwind station can never leak into the city
    average, into a forecast target, or into leave-one-station-out.
    """
    cfg = cfg or load_config()
    return backfill(
        start, end, cfg=cfg, skip_existing=skip_existing,
        stations=list(cfg.upwind_stations), dataset=UPWIND_DATASET,
    )


def backfill(
    start: dt.date | str,
    end: dt.date | str,
    cfg: Config | None = None,
    skip_existing: bool = True,
    stations: list[Station] | None = None,
    dataset: str = DATASET,
) -> list[Path]:
    """Cache ground truth in chunks, resumable.

    Two years across fifty stations is tens of thousands of small archive
    files; fetching it all before writing anything would risk the whole run on
    the last request. Chunked writes make a re-run cost only what is missing.
    """
    cfg = cfg or load_config()
    start_d, end_d = _as_date(start), _as_date(end)
    written: list[Path] = []
    cur = start_d
    while cur <= end_d:
        stop = min(cur + dt.timedelta(days=BACKFILL_CHUNK_DAYS - 1), end_d)
        if skip_existing and not missing_dates(dataset, cur, stop):
            log.info("%s %s..%s already cached", dataset, cur, stop)
            cur = stop + dt.timedelta(days=1)
            continue
        log.info("%s %s..%s", dataset, cur, stop)
        df = fetch_archive(cur, stop, cfg=cfg, stations=stations)
        if not df.is_empty():
            written += write_partitioned(df, dataset)
        cur = stop + dt.timedelta(days=1)
    return written


# ---------------------------------------------------------------------------
# Quality
# ---------------------------------------------------------------------------
def _to_hourly(raw: pl.DataFrame) -> pl.DataFrame:
    """Aggregate raw observations to the hourly UTC index and flag quality.

    No resampling onto a complete index: an hour with no observation must not
    exist as a row, so that a later join makes the gap visible instead of
    inventing a value (R6).
    """
    # The archive stamps CPCB readings in local time with an explicit offset
    # ("2025-11-01T00:45:00+05:30"), and at 15-minute resolution. Parse the
    # offset rather than inferring, convert to UTC, and only then truncate to
    # the hour — truncating first would bin readings by IST half-hours and
    # smear every hourly average across two real hours.
    obs = (
        raw.with_columns(
            # The two OpenAQ paths disagree on how they write an offset: the S3
            # archive emits "+05:30", the v3 API emits "Z". One format string
            # cannot read both, and the mismatch fails *silently* — strict=False
            # turns every unparsed stamp into a null and the drop_nulls below
            # then discards the entire live feed. Normalise Z to +00:00 first.
            pl.col("datetime")
            .str.replace(r"Z$", "+00:00")
            .str.to_datetime(format="%Y-%m-%dT%H:%M:%S%z", time_unit="us", strict=False)
            .dt.convert_time_zone("UTC")
            .dt.truncate("1h")
            .alias("time")
        )
        .drop_nulls(["time", "value"])
        .filter(pl.col("value").is_not_nan())
    )
    if obs.is_empty():
        return pl.DataFrame()

    hourly = (
        obs.group_by(["station_id", "time"])
        .agg(
            pl.col("value").mean().alias("pm25"),
            pl.col("value").min().alias("pm25_min"),
            pl.col("value").max().alias("pm25_max"),
            pl.len().alias("n_obs"),
        )
        .sort(["station_id", "time"])
    )
    return flag_quality(hourly)


def flag_quality(hourly: pl.DataFrame) -> pl.DataFrame:
    """Apply the quality rules to an already-hourly frame.

    Shared with the Kaggle historical loader so that every observation in the
    store — whichever decade and whichever source it came from — is judged by
    identical rules. Different cleaning per era would put a step change into
    the training data that looks exactly like a change in the air.
    """
    # Stuck instrument: the same value repeating hour after hour.
    hourly = hourly.with_columns(
        (pl.col("pm25").round(2) == pl.col("pm25").round(2).shift(1))
        .over("station_id")
        .fill_null(False)
        .alias("_same_as_prev")
    )
    hourly = hourly.with_columns(
        pl.col("_same_as_prev")
        .cum_sum()
        .over("station_id")
        .alias("_run")
    )
    stuck = (
        hourly.group_by(["station_id", "_run"])
        .agg(pl.len().alias("_run_len"))
    )
    hourly = hourly.join(stuck, on=["station_id", "_run"], how="left")

    flagged = hourly.with_columns(
        pl.when(pl.col("pm25") < MIN_PLAUSIBLE)
        .then(pl.lit("suspect_low"))
        .when(pl.col("pm25") > MAX_PLAUSIBLE)
        .then(pl.lit("suspect_high"))
        .when((pl.col("_run_len") >= STUCK_HOURS) & pl.col("_same_as_prev"))
        .then(pl.lit("stuck"))
        .otherwise(pl.lit("ok"))
        .alias("quality_flag")
    ).drop(["_same_as_prev", "_run", "_run_len"])

    return flagged.with_columns(
        pl.when(pl.col("quality_flag") == "ok")
        .then(pl.col("pm25"))
        .otherwise(None)
        .alias("pm25_clean")
    )


def flag_step_changes(
    df: pl.DataFrame, window_days: int = 30, ratio: float = 2.0
) -> pl.DataFrame:
    """Find stations whose level jumps between adjacent months (R6).

    A relocation or instrument swap shows up as a step in the monthly median.
    This does not fix anything — it produces the list you check by eye before
    trusting a station's history.
    """
    if df.is_empty():
        return pl.DataFrame()
    monthly = (
        df.filter(pl.col("quality_flag") == "ok")
        .with_columns(pl.col("time").dt.truncate(f"{window_days}d").alias("block"))
        .group_by(["station_id", "block"])
        .agg(pl.col("pm25").median().alias("median"), pl.len().alias("n"))
        .sort(["station_id", "block"])
    )
    return (
        monthly.with_columns(
            (pl.col("median") / pl.col("median").shift(1).over("station_id")).alias("step_ratio")
        )
        .filter(
            (pl.col("step_ratio") > ratio) | (pl.col("step_ratio") < 1 / ratio)
        )
    )


# ---------------------------------------------------------------------------
def _coverage(loc: dict) -> tuple[dt.datetime | None, dt.datetime | None]:
    """(first, last) measurement times an OpenAQ location reports having."""
    out = []
    for key in ("datetimeFirst", "datetimeLast"):
        raw = (loc.get(key) or {}).get("utc")
        try:
            out.append(dt.datetime.fromisoformat(raw.replace("Z", "+00:00")) if raw else None)
        except (AttributeError, ValueError):
            out.append(None)
    return out[0], out[1]


def _covers_window(
    first: dt.datetime | None,
    last: dt.datetime | None,
    start: dt.datetime,
    end: dt.datetime,
) -> bool:
    """True when the station record overlaps the period we train on."""
    if first is None or last is None:
        return False
    return first <= end and last >= start


# Words that appear in almost every station name and so carry no identifying
# information — matching on them would make everything look like everything.
_STOPWORDS = {
    "delhi", "new", "ncr", "india", "up", "uttar", "pradesh", "haryana",
    "rajasthan", "cpcb", "dpcc", "uppcb", "hspcb", "rspcb", "imd", "iitm",
    "sector", "phase", "nagar", "colony", "road", "marg", "crossing",
}


def _tokens(name: str) -> set[str]:
    cleaned = re.sub(r"[^a-z0-9 ]+", " ", name.lower())
    return {t for t in cleaned.split() if len(t) > 1}


def _name_score(station: Station, loc: dict) -> float:
    """Jaccard overlap between our station name and OpenAQ's, ignoring boilerplate.

    Numbers are kept and weighted, because "Sector 11" and "Sector 51" differ
    only there and are 5 km apart. A tie on identifying words is broken by the
    operating agency, which distinguishes co-located instruments such as the
    two Pusa stations that share a rooftop.
    """
    ours = _tokens(station.name)
    theirs = _tokens(loc.get("name", ""))
    ours_key = ours - _STOPWORDS
    theirs_key = theirs - _STOPWORDS
    if not ours_key or not theirs_key:
        return 0.0

    overlap = ours_key & theirs_key
    if not overlap:
        return 0.0
    score = len(overlap) / len(ours_key | theirs_key)

    # A digit present in one name and contradicted in the other is a different
    # station, not a near miss.
    our_nums = {t for t in ours if t.isdigit()}
    their_nums = {t for t in theirs if t.isdigit()}
    if our_nums and their_nums and not (our_nums & their_nums):
        return 0.0

    if station.agency.lower() in _tokens(loc.get("name", "")):
        score += 0.05
    return score


def _haversine_km(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    r = 6371.0088
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dp = p2 - p1
    dl = math.radians(lon2 - lon1)
    a = math.sin(dp / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
    return 2 * r * math.asin(math.sqrt(a))


def _as_date(value: dt.date | str) -> dt.date:
    return dt.date.fromisoformat(value) if isinstance(value, str) else value


def _days(start: dt.date, end: dt.date) -> list[dt.date]:
    return [start + dt.timedelta(days=i) for i in range((end - start).days + 1)]
