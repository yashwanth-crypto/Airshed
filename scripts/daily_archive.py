"""Daily archive job — the one process that cannot be allowed to quietly stop.

Archived forecast runs cannot be recovered retrospectively. There is no
archived-forecast air-quality product (checked: the previous-runs host has no
air-quality path, and `pm2_5_previous_dayN` is empty), so `cams_runs` and
`meteo_runs` only ever grow by this job having run. A day missed in November is
a day of episode-season evidence gone permanently, and November is exactly where
every open question in this project is supposed to resolve.

Three modes:

    python scripts/daily_archive.py              one pass, exit code says how it went
    python scripts/daily_archive.py --loop       stay resident, re-check periodically
    python scripts/daily_archive.py --health     report staleness only, fetch nothing

`--loop` exists because the Startup-folder launcher fires **once per logon**. A
laptop left on for four days archived once. Rather than trusting a fixed daily
trigger — Task Scheduler already refused this account, see docs/STATUS.md — the
loop wakes every `CHECK_EVERY_MIN` and asks a question that survives sleep,
hibernation and a closed lid: *is today's run in the store yet?* If yes it goes
back to sleep, if no it fetches. Nothing depends on the machine being awake at a
particular instant.

Set `AIRSHED_BACKUP_DIR` to a directory on another disk or a synced folder and
the run stores are mirrored there after every successful pass. They are ~250 KB
a day, about 90 MB a year, and they are the only irreplaceable thing here. It is
opt-in and off by default on purpose: copying data into a cloud-synced folder
sends it somewhere, and that is the operator's call to make, not this script's.
"""

from __future__ import annotations

import argparse
import datetime as dt
import logging
import os
import shutil
import sys
import time
from pathlib import Path

from airshed.config import repo_root
from airshed.env import load_dotenv
from airshed.keepawake import keep_awake
from airshed.net import DailyQuotaExceeded
from airshed.ingest import cams, cpcb, metar, meteo
from airshed.store import coverage

# Absolute, not relative. Task Scheduler runs with the working directory set to
# C:\Windows\System32, so a relative path would silently put the log
# somewhere unwritable — or worse, somewhere unexpected but writable.
LOG = repo_root() / "data" / "archive.log"
LOCK = repo_root() / "data" / "archive.lock"

# Deliberate off switch. The loop can run with no console attached -- that is
# the point of the hidden launcher, since a console window is a thing that gets
# closed by accident, and three loops died that way in two days. A window you
# cannot close needs a stop that is not "close the window", so stopping is a
# file: `stop_archive.bat` creates it, the loop notices within seconds, exits 0,
# and the supervisor treats 0 as "stay stopped".
STOP = repo_root() / "data" / "archive.stop"

# How often the loop wakes to notice a stop request while waiting out its tick.
STOP_POLL_S = 5

# How far back to re-check the archive datasets each day. Comfortably more than
# any source's publication lag, so a few days of downtime heal by themselves.
RECENT_DAYS = 10

# A `meteo_leadmatched` partition below this many stations predates a station
# addition and needs filling. Not the full roster: stations legitimately drop
# out for a day, and chasing that would refetch forever.
LEADMATCH_MIN_STATIONS = 70

# Loop cadence. Frequent enough that a laptop opened briefly still catches the
# day, cheap enough to be irrelevant: a pass with nothing to do is a few reads
# of the local store.
CHECK_EVERY_MIN = 30

# A run store older than this is a failure worth shouting about, not a nuisance.
# Slightly over a day, so a late pass is not reported as an outage.
STALE_AFTER_H = 36

# The datasets that cannot be re-fetched later, and are therefore the ones worth
# mirroring off the machine.
IRREPLACEABLE = ("cams_runs", "meteo_runs")


def one_pass() -> int:
    """Fetch everything due. Idempotent — safe to call as often as you like.

    Held awake for the duration. A pass takes about five minutes and the machine
    sleeping through it costs a tick, not data — but in November a tick is worth
    holding the lid open for. The request dies with the process and changes no
    power setting, so a crash cannot leave the laptop permanently awake.
    """
    log = logging.getLogger("archive")
    log.info("archive pass starting (%s UTC)", dt.datetime.now(dt.timezone.utc))
    with keep_awake("airshed archive pass"):
        return _one_pass()


def _one_pass() -> int:
    """The pass itself, so `one_pass` is only the wakefulness wrapper."""
    log = logging.getLogger("archive")
    failures = 0
    quota_hit = False
    for name, fn in (("cams", cams.archive_run), ("meteo", meteo.archive_run)):
        try:
            paths = fn()
            log.info("%s_runs: wrote %d partition(s)", name, len(paths))
        except DailyQuotaExceeded as exc:
            # Distinct from a failure. The allowance resets, `is_due()` will
            # still be true, and the loop retries without anyone intervening.
            # Counting it as a failure would also suppress the backup below,
            # which protects data we already hold and has nothing to do with
            # today's quota.
            log.warning("%s_runs deferred — %s", name, str(exc)[:200])
            quota_hit = True
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

    # Lead-matched meteorology. No lag is needed and that was checked, not
    # assumed: `previous_day3` returns 24/24 non-null for *today's* valid hours,
    # because the run from three days ago already forecast this far ahead. Only
    # future valid days would be short, and this job never asks for those.
    try:
        paths = meteo.backfill_previous_runs(start, today)
        log.info("meteo_leadmatched: topped up %d partition(s)", len(paths))
    except DailyQuotaExceeded as exc:
        log.warning("meteo_leadmatched top-up deferred — %s", str(exc)[:160])
        quota_hit = True
    except Exception as exc:
        log.error("meteo_leadmatched top-up failed: %s", str(exc)[:300])
        failures += 1

    # Heal a ragged dataset. Adding stations mid-life leaves `meteo_leadmatched`
    # complete up to wherever the backfill reached and short after it, and the
    # top-up above cannot see that: `skip_existing` only asks whether a
    # partition exists, not whether it holds every station. The 2026-08-24
    # expansion ran out of daily quota partway and left exactly this shape, so
    # the job repairs it itself rather than waiting to be remembered.
    try:
        from airshed.ingest.repair import short_partitions, stations_missing_from

        # Driven by which partitions are short, not by whether the newest one
        # is. The top-up above repairs the newest days first, so a probe of the
        # latest partition comes back clean while the backlog is untouched --
        # and this repair, gated on that probe, silently did nothing.
        short = short_partitions(meteo.LEADMATCHED_DATASET, LEADMATCH_MIN_STATIONS)
        if short:
            wanted = stations_missing_from(meteo.LEADMATCHED_DATASET, short)
            if wanted:
                log.info(
                    "meteo_leadmatched: %d partition(s) short (%s..%s), filling "
                    "%d station(s)",
                    len(short), short[0], short[-1], len(wanted),
                )
                filled = meteo.backfill_previous_runs(
                    short[0], short[-1], station_ids=wanted, chunk_days=10
                )
                log.info("meteo_leadmatched: filled %d partition(s)", len(filled))
    except DailyQuotaExceeded as exc:
        log.warning("meteo_leadmatched gap fill deferred — %s", str(exc)[:160])
        quota_hit = True
    except Exception as exc:
        log.error("meteo_leadmatched gap fill failed: %s", str(exc)[:300])
        failures += 1

    for name in ("cams_runs", "meteo_runs"):
        c = coverage(name)
        log.info("%s now holds %s run(s), latest %s", name, c["days"], c["last"])

    # Re-measure the CAMS train/serve gap. It reads only the local stores, so it
    # costs nothing, and it is the one number that improves purely by this job
    # having run — there is no archived-forecast air-quality product to backfill
    # it from. Logging the count each day makes a stalled job visible as a
    # number that stops moving.
    try:
        from airshed.eval import camsoffset

        table, ok = camsoffset.run()
        camsoffset.write(table, ok)
        if table.is_empty():
            log.info("cams offset: no run/archive overlap yet")
        else:
            worst = table.sort("bias").row(0, named=True)
            log.info(
                "cams offset: %d run day(s), %d settled of %d needed; "
                "worst bias %+.1f ug/m3 at lead day %d",
                ok["run_days"], ok["settled_days"], ok["needed"],
                worst["bias"], worst["lead_day"],
            )
    except Exception as exc:
        log.error("cams offset measurement failed: %s", str(exc)[:300])
        failures += 1

    if quota_hit:
        log.warning(
            "Open-Meteo daily quota exhausted. Runs still missing will be "
            "fetched on the next pass after the allowance resets; the loop "
            "keeps asking because `is_due()` reads the store, not the clock."
        )
    if failures == 0:
        backup()
    else:
        # Mirroring a half-written pass would propagate the failure to the copy
        # that exists precisely to survive one.
        log.warning("skipping backup: %d step(s) failed this pass", failures)

    return 1 if failures else 0


# ---------------------------------------------------------------------------
# staleness
# ---------------------------------------------------------------------------
def run_age_hours(dataset: str) -> float | None:
    """Hours since the newest partition of a run store. None if it is empty."""
    info = coverage(dataset)
    if not info.get("last"):
        return None
    last = dt.date.fromisoformat(str(info["last"]))
    # Partitions are dated, not stamped, so measure from the end of that day.
    end_of_day = dt.datetime.combine(last, dt.time(23, 59), tzinfo=dt.timezone.utc)
    delta = dt.datetime.now(dt.timezone.utc) - end_of_day
    return max(0.0, delta.total_seconds() / 3600)


def health() -> int:
    """Report run-store freshness. Non-zero exit means something is wrong.

    Separate from `airshed status`, which prints a table and always succeeds.
    This is the thing to point a monitor at, and the thing to run by hand when
    you want a yes-or-no answer rather than a wall of numbers.
    """
    log = logging.getLogger("archive")
    worst = 0
    for name in IRREPLACEABLE:
        age = run_age_hours(name)
        if age is None:
            log.error("%s: EMPTY - no forecast runs archived at all", name)
            worst = 2
            continue
        info = coverage(name)
        if age > STALE_AFTER_H:
            log.error(
                "%s: STALE - newest run %s, %.0f h old (limit %d h). "
                "Archived runs cannot be backfilled; every day missed is gone.",
                name, info["last"], age, STALE_AFTER_H,
            )
            worst = max(worst, 2)
        else:
            log.info(
                "%s: ok - %s run(s), newest %s, %.0f h old",
                name, info["days"], info["last"], age,
            )

    if not os.environ.get("AIRSHED_BACKUP_DIR"):
        log.warning(
            "AIRSHED_BACKUP_DIR is not set - the run stores exist on this "
            "machine only. They are ~90 MB a year and cannot be re-fetched."
        )
        worst = max(worst, 1)
    return worst


# ---------------------------------------------------------------------------
# backup
# ---------------------------------------------------------------------------
def backup() -> bool:
    """Mirror the irreplaceable run stores to `AIRSHED_BACKUP_DIR`, if set.

    Opt-in, and deliberately not defaulted to a cloud folder: copying project
    data into a synced directory sends it off the machine, which is the
    operator's decision to make rather than this script's. Unset means "no
    backup", logged as a warning rather than silently skipped.
    """
    log = logging.getLogger("archive")
    dest_root = os.environ.get("AIRSHED_BACKUP_DIR", "").strip()
    if not dest_root:
        log.warning(
            "no AIRSHED_BACKUP_DIR set - run stores are on this machine only. "
            "Set it to a second disk or a synced folder; they are ~250 KB/day."
        )
        return False

    dest = Path(dest_root)
    try:
        dest.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        log.error("backup directory unusable (%s): %s", dest, exc)
        return False

    copied = 0
    for name in IRREPLACEABLE:
        src = repo_root() / "data" / "raw" / name
        if not src.is_dir():
            continue
        try:
            # dirs_exist_ok makes this an incremental mirror rather than a
            # delete-and-rewrite: a failure mid-copy must not empty the backup.
            shutil.copytree(src, dest / name, dirs_exist_ok=True)
            copied += 1
        except OSError as exc:
            log.error("backup of %s failed: %s", name, str(exc)[:200])
            return False
    log.info("backup: mirrored %d run store(s) to %s", copied, dest)
    return True


# ---------------------------------------------------------------------------
# single instance
# ---------------------------------------------------------------------------
def _process_alive(pid: int) -> bool | None:
    """Is that process still running? `None` means we could not tell.

    Deliberately not `os.kill(pid, 0)`: on Windows that maps to TerminateProcess
    for anything other than CTRL_*_EVENT, so the liveness probe would kill the
    process it was asking about.
    """
    if pid <= 0:
        return None
    if sys.platform.startswith("win"):
        try:
            import ctypes

            SYNCHRONIZE, WAIT_TIMEOUT, ERROR_ACCESS_DENIED = 0x00100000, 0x102, 5
            kernel32 = ctypes.windll.kernel32
            handle = kernel32.OpenProcess(SYNCHRONIZE, False, pid)
            if not handle:
                # Access denied means it exists and belongs to someone else.
                return kernel32.GetLastError() == ERROR_ACCESS_DENIED
            try:
                return kernel32.WaitForSingleObject(handle, 0) == WAIT_TIMEOUT
            finally:
                kernel32.CloseHandle(handle)
        except Exception:
            return None
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    except OSError:
        return None
    return True


def _read_lock() -> tuple[float, int | None]:
    """Age of the lock in minutes, and the pid that wrote it if it recorded one.

    Locks written before this file recorded pids hold a bare timestamp, so the
    pid is optional and its absence falls back to the age rule.
    """
    try:
        lines = LOCK.read_text(encoding="utf-8").strip().splitlines()
        stamp = dt.datetime.fromisoformat(lines[0].strip())
        age_min = (dt.datetime.now(dt.timezone.utc) - stamp).total_seconds() / 60
    except (ValueError, OSError, IndexError):
        return float("inf"), None
    pid = None
    if len(lines) > 1:
        try:
            pid = int(lines[1].strip())
        except ValueError:
            pid = None
    return age_min, pid


def acquire_lock() -> bool:
    """Refuse to start a second copy. Two loops would double every request.

    A stale lock - left by a process killed without cleanup, which on a laptop
    means "the lid closed and it never came back" - is taken over rather than
    treated as fatal. A lock that outlives its process must not be able to stop
    the archive forever; that would be the outage it exists to prevent.

    The lock records the pid as well as the time, because a timestamp alone
    cannot tell a running loop from a dead one. It happened twice in two days:
    the loop was killed without cleanup, the lock outlived it, and every later
    launch backed off politely for the 90 minutes the age rule takes to declare
    a lock stale. Asking the operating system whether that pid is still alive
    turns a 90-minute blind spot into an immediate takeover, and makes the
    refusal correct rather than merely probable when it *is* alive.

    Three guards, in order of how much they know:

    * pid recorded and dead      -> take over now, whatever the clock says
    * pid recorded and alive     -> refuse, unless the stamp is so old that the
                                    pid has plainly been recycled by the OS
    * no pid, or liveness unknown-> fall back to the age rule
    """
    log = logging.getLogger("archive")
    now = dt.datetime.now(dt.timezone.utc)

    if LOCK.is_file():
        age_min, pid = _read_lock()
        alive = _process_alive(pid) if pid is not None else None

        if alive is False:
            log.warning(
                "lock left by dead process %d (%.0f min old) - taking over", pid, age_min
            )
        elif alive is True and age_min >= CHECK_EVERY_MIN * 6:
            # A live pid this far past its last heartbeat is not our loop; the
            # OS has recycled the number. Blocking forever on that would be the
            # outage the lock exists to prevent.
            log.warning(
                "lock names pid %d, alive but silent for %.0f min - assuming "
                "the pid was recycled and taking over",
                pid,
                age_min,
            )
        elif alive is True:
            log.warning("archive already running as pid %d; exiting", pid)
            return False
        elif age_min < CHECK_EVERY_MIN * 3:
            log.warning("another archive process appears to be running; exiting")
            return False
        else:
            log.warning("stale lock (%.0f min old) - taking over", age_min)

    LOCK.parent.mkdir(parents=True, exist_ok=True)
    _write_lock(now)
    return True


def _write_lock(when: dt.datetime) -> None:
    LOCK.write_text(f"{when.isoformat()}\n{os.getpid()}\n", encoding="utf-8")


def touch_lock() -> None:
    try:
        _write_lock(dt.datetime.now(dt.timezone.utc))
    except OSError:
        pass


def release_lock() -> None:
    """Drop the lock, but only if it is still ours.

    After a takeover the previous owner may still be shutting down. Deleting a
    lock it no longer holds would leave the live loop unprotected.
    """
    try:
        if LOCK.is_file():
            _, pid = _read_lock()
            if pid is not None and pid != os.getpid():
                return
        LOCK.unlink(missing_ok=True)
    except OSError:
        pass


# ---------------------------------------------------------------------------
# entry points
# ---------------------------------------------------------------------------
def is_due() -> bool:
    """Is today's run missing from either store?

    Deliberately a question about *state*, not about the clock. A trigger that
    asks "is it 06:30?" misses whenever the machine is asleep at 06:30; this one
    is answered correctly whenever the machine happens to be awake at all.
    """
    today = dt.date.today()
    for name in IRREPLACEABLE:
        info = coverage(name)
        if not info.get("last") or dt.date.fromisoformat(str(info["last"])) < today:
            return True
    return False


# Hours of observations to re-pull on each tick. Short: the point is to stay
# current, and the daily pass already reaches back far enough to heal gaps.
# Re-fetching four days every half hour would be 8x the requests for the same
# result.
SYNC_WINDOW_H = 12


def sync_observations() -> None:
    """Top up recent CPCB observations. Cheap, and runs on every loop tick.

    Separate from `one_pass` on purpose. The forecast run is archived once a
    day and cannot be backfilled; observations arrive continuously and the
    interface shows their age, so they are worth keeping current between runs.
    """
    log = logging.getLogger("archive")
    try:
        paths = cpcb.sync_recent(hours=SYNC_WINDOW_H)
        if paths:
            log.info("observations: refreshed %d partition(s)", len(paths))
    except DailyQuotaExceeded as exc:
        log.warning("observation sync deferred — %s", str(exc)[:160])
    except Exception as exc:
        log.error("observation sync failed: %s", str(exc)[:300])


def stop_requested() -> bool:
    """Has someone asked the loop to stop? Consumes the request if so."""
    try:
        if STOP.is_file():
            STOP.unlink(missing_ok=True)
            return True
    except OSError:
        pass
    return False


def _wait_for_next_tick() -> bool:
    """Sleep out the tick in slices. True if a stop was requested meanwhile.

    One long sleep would make the off switch take up to half an hour to answer,
    which is long enough that an operator reaches for Task Manager instead --
    and killing the process is exactly the ending that leaves an orphaned lock.
    """
    remaining = CHECK_EVERY_MIN * 60
    while remaining > 0:
        time.sleep(min(STOP_POLL_S, remaining))
        remaining -= STOP_POLL_S
        if stop_requested():
            return True
    return False


def loop() -> int:
    log = logging.getLogger("archive")
    if not acquire_lock():
        return 1
    log.info(
        "archive loop started as pid %d; checking every %d min, log at %s",
        os.getpid(),
        CHECK_EVERY_MIN,
        LOG,
    )
    try:
        while True:
            if stop_requested():
                log.info("stop requested; archive loop exiting")
                return 0
            touch_lock()
            try:
                if is_due():
                    one_pass()
                else:
                    # Observations refresh on every tick, not only when a
                    # forecast run is due. `is_due` asks about the run stores,
                    # so once the day's run was archived the loop slept and
                    # ground truth went stale for the rest of the day -- the
                    # dashboard sat ~9 h behind when upstream was only ~5.
                    sync_observations()
            except Exception as exc:
                # A loop that dies on one bad night is a loop that was not worth
                # writing. Log it and try again on the next tick.
                log.error("pass raised, continuing: %s", str(exc)[:300])
            if _wait_for_next_tick():
                log.info("stop requested; archive loop exiting")
                return 0
    except KeyboardInterrupt:
        log.info("archive loop stopped")
        return 0
    finally:
        release_lock()


def _setup_logging() -> None:
    LOG.parent.mkdir(parents=True, exist_ok=True)
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)-7s %(message)s",
        handlers=[logging.FileHandler(LOG, encoding="utf-8"), logging.StreamHandler()],
    )
    logging.getLogger("httpx").setLevel(logging.WARNING)  # keeps keys out of the log
    # AIRSHED_BACKUP_DIR is documented in .env.example, so .env has to actually
    # be read here — `backup()` looks at os.environ and nothing else would put
    # it there. A real environment variable still wins over the file.
    load_dotenv()


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="Airshed daily archive job.")
    ap.add_argument("--loop", action="store_true", help="Stay resident and re-check.")
    ap.add_argument(
        "--health", action="store_true", help="Report staleness, fetch nothing."
    )
    args = ap.parse_args(argv)

    _setup_logging()
    if args.health:
        return health()
    if args.loop:
        return loop()
    return one_pass()


if __name__ == "__main__":
    sys.exit(main())
