"""Finish the 2026-08-24 station expansion once the API allowance resets.

The expansion from 51 to 77 stations left one dataset short. `cpcb`,
`cams_archive` and `meteo_archive` all carry every station; `meteo_leadmatched`
covers 2025-02-18..2026-02-12 and stops there, because the backfill exhausted
Open-Meteo's daily request quota.

The cause is fixed — `backfill_previous_runs` now resolves the cell map once for
the whole run instead of once per chunk, which is what spent the allowance — so
this should complete in a single pass.

    .venv/Scripts/python.exe scripts/finish_station_expansion.py

Idempotent: it re-checks which partitions are actually short and asks only for
those. Safe to run repeatedly, and a no-op once there is nothing left to do.

You may never need it. The daily archive job now performs the same repair, so
this completes on its own the first time the job runs after the allowance
resets. Keep the script for when you want it done now rather than tomorrow.
"""

from __future__ import annotations

import logging
import sys

from airshed.env import load_dotenv
from airshed.ingest import meteo
from airshed.ingest.repair import short_partitions, stations_missing_from
from airshed.net import DailyQuotaExceeded

DATASET = "meteo_leadmatched"
EXPECTED_MIN_STATIONS = 70


def main() -> int:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(message)s")
    logging.getLogger("httpx").setLevel(logging.WARNING)
    log = logging.getLogger("finish")
    load_dotenv()

    short = short_partitions(DATASET, EXPECTED_MIN_STATIONS)
    if not short:
        log.info("%s already carries every station — nothing to do", DATASET)
        return 0

    # Asked of the short partitions, not the newest one -- see
    # repair.stations_missing_from.
    ids = stations_missing_from(DATASET, short)
    log.info(
        "%d partition(s) short, %s..%s; fetching %d station(s)",
        len(short), short[0], short[-1], len(ids),
    )
    try:
        written = meteo.backfill_previous_runs(
            short[0], short[-1], station_ids=ids, chunk_days=10
        )
    except DailyQuotaExceeded as exc:
        log.error("%s — run this again tomorrow", str(exc)[:200])
        return 2

    still = short_partitions(DATASET, EXPECTED_MIN_STATIONS)
    log.info("wrote %d partition(s); %d still short", len(written), len(still))
    if still:
        log.warning("still short %s..%s — run again", still[0], still[-1])
        return 1

    log.info("station expansion complete across every dataset")
    log.warning(
        "docs/results/ is now stale: the evaluation row set changed from 51 to "
        "77 stations. Regenerate ablation, rolling, leadmatch, grap and loso "
        "before quoting any number."
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
