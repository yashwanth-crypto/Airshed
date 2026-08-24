"""Patient backfill: retry a date range until the local store is complete.

Open-Meteo's free tier rate-limits, and a 23-month backfill will hit it. Since
`backfill` is resumable and skips cached days, retrying costs only what is
still missing. Run this in the background and forget about it.
"""

from __future__ import annotations

import logging
import sys
import time

from airshed.ingest import cams, meteo
from airshed.store import missing_dates

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)-7s %(message)s")
log = logging.getLogger("backfill")

SOURCES = {"meteo": (meteo.backfill, meteo.ARCHIVE_DATASET),
           "cams": (cams.backfill, cams.ARCHIVE_DATASET)}


def main(source: str, start: str, end: str, attempts: int = 24, wait_s: int = 900) -> int:
    fn, dataset = SOURCES[source]
    for attempt in range(1, attempts + 1):
        gaps = missing_dates(dataset, start, end)
        if not gaps:
            log.info("%s complete for %s..%s", dataset, start, end)
            return 0
        log.info("attempt %d/%d — %d days still missing", attempt, attempts, len(gaps))
        try:
            fn(start, end)
        except Exception as exc:
            log.warning("attempt %d failed: %s", attempt, str(exc)[:200])
        if missing_dates(dataset, start, end):
            log.info("sleeping %ds for the rate limit to clear", wait_s)
            time.sleep(wait_s)
    remaining = missing_dates(dataset, start, end)
    log.error("gave up with %d days missing", len(remaining))
    return 1


if __name__ == "__main__":
    sys.exit(main(sys.argv[1], sys.argv[2], sys.argv[3]))
