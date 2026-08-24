"""NASA FIRMS active fire detections over Punjab and Haryana.

Stubble burning is the seasonal forcing that turns a bad Delhi November into a
severe one. Detections are strongly seasonal (Oct-Nov dominant, a smaller
April-May wheat peak), so an empty return is the normal case for most of the
year and must never raise — a feature that errors out in June is a feature that
never gets used.

Needs a free FIRMS map key (`FIRMS_MAP_KEY`). Without one this module logs and
returns empty rather than failing, and the fire features fall back to zero with
an explicit `fires_available` flag so the model can tell "no fires" apart from
"no data".
"""

from __future__ import annotations

import datetime as dt
import io
import logging
import os
from pathlib import Path

import polars as pl

from ..config import Config, load_config
from ..net import get_text
from ..store import write_partitioned

log = logging.getLogger(__name__)

DATASET = "fires"
MAX_DAY_RANGE = 5  # FIRMS area API caps a request at 5 days ("Expects [1..5]")
NRT_CUTOFF_DAYS = 60  # older than this, NRT products are gone; use the SP archive


def fetch(
    start: dt.date | str,
    end: dt.date | str,
    cfg: Config | None = None,
) -> pl.DataFrame:
    """Detections in the upwind fire region over [start, end]."""
    cfg = cfg or load_config()
    src = cfg.source("firms")
    from ..env import load_dotenv

    load_dotenv()
    key = os.environ.get("FIRMS_MAP_KEY", "").strip()
    if not key:
        log.warning(
            "FIRMS_MAP_KEY not set — skipping fire ingest. Fire features will be "
            "flagged unavailable, not zero. Free key: "
            "https://firms.modaps.eosdis.nasa.gov/api/area/"
        )
        return pl.DataFrame()

    start_d, end_d = _as_date(start), _as_date(end)
    region = cfg.domain["fire_region"]
    area = f"{region['lon_min']},{region['lat_min']},{region['lon_max']},{region['lat_max']}"
    frames = []
    requests = failures = 0
    # Product family is decided **per chunk**, from that chunk's own age.
    # Choosing it once from the range end is wrong the moment a backfill spans
    # the NRT cutoff: a request covering 2025-09 to 2026-08 would ask the
    # near-real-time products for data a year old, get nothing, and report a
    # quiet season. That is how the November stubble peak went missing.
    today = dt.date.today()
    for cur, span in _chunks(start_d, end_d):
        chunk_age = (today - cur).days
        products = (
            src["archive_products"] if chunk_age > NRT_CUTOFF_DAYS else src["products"]
        )
        for product in products:
            url = f"{src['url']}/{key}/{product}/{area}/{span}/{cur.isoformat()}"
            requests += 1
            try:
                text = get_text(url)
            except Exception as exc:  # one product missing must not kill the rest
                failures += 1
                log.warning("FIRMS %s %s: %s", product, cur, _redact(str(exc), key))
                continue
            df = _parse(text, product)
            if not df.is_empty():
                frames.append(df)

    if not frames:
        # An empty result and a wholly failed fetch are different situations
        # and must not print the same reassuring sentence. Out of season the
        # API genuinely returns nothing; a bad key or a rejected day range
        # returns nothing too, and calling that "expected" hides a real fault.
        if failures == requests and requests:
            raise RuntimeError(
                f"every FIRMS request failed ({failures}/{requests}) — check "
                "FIRMS_MAP_KEY and the day range; no data was written"
            )
        log.info(
            "no fire detections %s..%s across %d request(s) (expected outside Oct-Nov)",
            start_d, end_d, requests,
        )
        return pl.DataFrame()
    if failures:
        log.warning("%d of %d FIRMS requests failed; result is partial", failures, requests)
    return pl.concat(frames, how="vertical_relaxed").sort("time")


def _chunks(start: dt.date, end: dt.date):
    """(start, span) windows no longer than the API's day-range cap."""
    cur = start
    while cur <= end:
        span = min(MAX_DAY_RANGE, (end - cur).days + 1)
        yield cur, span
        cur += dt.timedelta(days=span)


def _redact(text: str, key: str) -> str:
    """Keep the map key out of logs. FIRMS puts it in the URL path."""
    return text.replace(key, "<FIRMS_MAP_KEY>") if key else text


def backfill(
    start: dt.date | str,
    end: dt.date | str,
    cfg: Config | None = None,
) -> list[Path]:
    df = fetch(start, end, cfg=cfg)
    if df.is_empty():
        return []
    return write_partitioned(df, DATASET)


# ---------------------------------------------------------------------------
def _parse(text: str, product: str) -> pl.DataFrame:
    if not text or "latitude" not in text.splitlines()[0]:
        return pl.DataFrame()
    df = pl.read_csv(io.StringIO(text), infer_schema_length=0)
    if df.is_empty():
        return pl.DataFrame()

    # acq_time is HHMM, zero-padded inconsistently; FIRMS timestamps are UTC.
    return (
        df.with_columns(
            pl.col("acq_time").cast(pl.Int32, strict=False).alias("_hhmm"),
            pl.col("latitude").cast(pl.Float64, strict=False),
            pl.col("longitude").cast(pl.Float64, strict=False),
            pl.col("frp").cast(pl.Float64, strict=False),
        )
        .with_columns(
            (
                pl.col("acq_date").str.to_datetime(format="%Y-%m-%d", time_unit="us")
                + pl.duration(hours=pl.col("_hhmm") // 100, minutes=pl.col("_hhmm") % 100)
            )
            .dt.replace_time_zone("UTC")
            .alias("time")
        )
        .select(
            "time",
            "latitude",
            "longitude",
            "frp",
            pl.col("confidence").cast(pl.Utf8, strict=False),
            pl.col("daynight").cast(pl.Utf8, strict=False).alias("daynight"),
            pl.lit(product).alias("product"),
        )
    )


def _as_date(value: dt.date | str) -> dt.date:
    return dt.date.fromisoformat(value) if isinstance(value, str) else value
