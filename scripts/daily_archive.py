"""Daily archive job. Point cron or Windows Task Scheduler at this.

Stores today's CAMS and meteorology forecast runs, then trims nothing: the
archive only becomes valuable by accumulating. Exit code is non-zero if either
source failed, so a scheduler can surface the failure.

    .venv/Scripts/python.exe scripts/daily_archive.py
"""

from __future__ import annotations

import datetime as dt
import logging
import sys
from pathlib import Path

from airshed.ingest import cams, cpcb, metar, meteo
from airshed.store import coverage

LOG = Path("data/archive.log")

# How far back to re-check the archive datasets each day. Comfortably more than
# any source's publication lag, so a few days of downtime heal by themselves.
RECENT_DAYS = 10


def main() -> int:
    LOG.parent.mkdir(parents=True, exist_ok=True)
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)-7s %(message)s",
        handlers=[logging.FileHandler(LOG, encoding="utf-8"), logging.StreamHandler()],
    )
    logging.getLogger("httpx").setLevel(logging.WARNING)  # keeps keys out of the log file
    log = logging.getLogger("archive")
    log.info("daily archive starting (%s UTC)", dt.datetime.now(dt.timezone.utc))

    failures = 0
    for name, fn in (("cams", cams.archive_run), ("meteo", meteo.archive_run)):
        try:
            paths = fn()
            log.info("%s_runs: wrote %d partition(s)", name, len(paths))
        except Exception as exc:
            log.error("%s_runs failed: %s", name, str(exc)[:300])
            failures += 1

    # Recent observations too: a forecast run without fresh history is a
    # forecast with no idea where it is starting from.
    try:
        paths = cpcb.sync_recent(hours=96)
        log.info("cpcb live sync: wrote %d partition(s)", len(paths))
    except Exception as exc:
        log.error("cpcb live sync failed: %s", str(exc)[:300])
        failures += 1

    # Top up the *archive* datasets as well. Replay reads these, not the run
    # store, so without this the observations march ahead while CAMS and
    # meteorology stall — and the demo offers a date it cannot actually serve.
    # These backfills skip whatever is already cached, so the cost is only the
    # genuinely new days.
    today = dt.date.today()
    start = today - dt.timedelta(days=RECENT_DAYS)
    for name, fn in (("cams_archive", cams.backfill),
                     ("meteo_archive", meteo.backfill),
                     ("metar", metar.backfill)):
        try:
            paths = fn(start, today)
            log.info("%s: topped up %d partition(s)", name, len(paths))
        except Exception as exc:
            log.error("%s top-up failed: %s", name, str(exc)[:300])
            failures += 1

    for name in ("cams_runs", "meteo_runs"):
        c = coverage(name)
        log.info("%s now holds %s run(s), latest %s", name, c["days"], c["last"])
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
