"""Long-running in-process scheduler, for when a system cron is not available.

Keep it boring (CLAUDE.md): one job, one interval, logs to the same file the
cron job uses.

    .venv/Scripts/python.exe scripts/scheduler.py
"""

from __future__ import annotations

import logging

from apscheduler.schedulers.blocking import BlockingScheduler

from daily_archive import main as archive

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)-7s %(message)s")
log = logging.getLogger("scheduler")

# 01:00 UTC is 06:30 IST, shortly after the CAMS run that a morning bulletin
# would be based on.
HOUR_UTC = 1

if __name__ == "__main__":
    scheduler = BlockingScheduler(timezone="UTC")
    scheduler.add_job(archive, "cron", hour=HOUR_UTC, minute=0, id="daily-archive")
    log.info("scheduled daily archive at %02d:00 UTC — ctrl-c to stop", HOUR_UTC)
    archive()  # run once on start so a restart never skips a day
    scheduler.start()
