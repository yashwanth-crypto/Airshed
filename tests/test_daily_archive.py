"""Tests for the archive job's safety machinery.

The job's fetching is exercised every day by running. What is *not* exercised by
running is everything that only matters when something goes wrong: the staleness
alarm that has never fired, the lock that has never contended, the backup that
has never been needed. Those are the parts worth pinning, because the first time
they run for real is the day the archive has already stopped.
"""

from __future__ import annotations

import datetime as dt
import importlib.util
import os
import subprocess
import sys
from pathlib import Path

import pytest

from airshed.procs import lock_state, process_alive, read_lock

REPO = Path(__file__).resolve().parents[1]


def _load():
    """Import `scripts/daily_archive.py`, which is not on the package path."""
    spec = importlib.util.spec_from_file_location(
        "daily_archive", REPO / "scripts" / "daily_archive.py"
    )
    module = importlib.util.module_from_spec(spec)
    sys.modules["daily_archive"] = module
    spec.loader.exec_module(module)
    return module


da = _load()


def _coverage(last: str | None, days: int = 5):
    return lambda name: {"dataset": name, "days": days, "last": last, "rows": 100}


# -- staleness --------------------------------------------------------------
def test_age_is_measured_from_the_end_of_the_partition_day(monkeypatch):
    # Partitions are dated, not stamped. Measuring from midnight would report a
    # run archived this morning as almost a day old and cry wolf every day.
    today = dt.date.today().isoformat()
    monkeypatch.setattr(da, "coverage", _coverage(today))
    assert da.run_age_hours("cams_runs") == 0.0


def test_age_grows_with_a_missed_day(monkeypatch):
    old = (dt.date.today() - dt.timedelta(days=3)).isoformat()
    monkeypatch.setattr(da, "coverage", _coverage(old))
    age = da.run_age_hours("cams_runs")
    assert 48 < age < 96


def test_empty_store_reports_no_age(monkeypatch):
    monkeypatch.setattr(da, "coverage", _coverage(None))
    assert da.run_age_hours("cams_runs") is None


# -- health -----------------------------------------------------------------
def test_health_is_clean_when_fresh_and_backed_up(monkeypatch, tmp_path):
    monkeypatch.setattr(da, "coverage", _coverage(dt.date.today().isoformat()))
    monkeypatch.setenv("AIRSHED_BACKUP_DIR", str(tmp_path))
    assert da.health() == 0


def test_health_warns_when_there_is_no_off_machine_copy(monkeypatch):
    monkeypatch.setattr(da, "coverage", _coverage(dt.date.today().isoformat()))
    monkeypatch.delenv("AIRSHED_BACKUP_DIR", raising=False)
    assert da.health() == 1


def test_health_fails_loudly_when_the_archive_has_stalled(monkeypatch, tmp_path):
    stale = (dt.date.today() - dt.timedelta(days=4)).isoformat()
    monkeypatch.setattr(da, "coverage", _coverage(stale))
    monkeypatch.setenv("AIRSHED_BACKUP_DIR", str(tmp_path))
    assert da.health() == 2


def test_health_fails_when_no_runs_exist_at_all(monkeypatch, tmp_path):
    monkeypatch.setattr(da, "coverage", _coverage(None))
    monkeypatch.setenv("AIRSHED_BACKUP_DIR", str(tmp_path))
    assert da.health() == 2


def test_a_late_pass_is_not_reported_as_an_outage(monkeypatch, tmp_path):
    # Yesterday's run is ~24 h old at worst, inside STALE_AFTER_H. Reporting it
    # as an outage would train the operator to ignore the alarm.
    yesterday = (dt.date.today() - dt.timedelta(days=1)).isoformat()
    monkeypatch.setattr(da, "coverage", _coverage(yesterday))
    monkeypatch.setenv("AIRSHED_BACKUP_DIR", str(tmp_path))
    assert da.health() == 0


# -- due check --------------------------------------------------------------
def test_due_is_about_state_not_the_clock(monkeypatch):
    monkeypatch.setattr(da, "coverage", _coverage(dt.date.today().isoformat()))
    assert da.is_due() is False
    monkeypatch.setattr(
        da, "coverage", _coverage((dt.date.today() - dt.timedelta(days=1)).isoformat())
    )
    assert da.is_due() is True


def test_due_when_a_store_is_empty(monkeypatch):
    monkeypatch.setattr(da, "coverage", _coverage(None))
    assert da.is_due() is True


# -- backup -----------------------------------------------------------------
def test_backup_declines_rather_than_guessing_a_destination(monkeypatch):
    # Never default to a cloud folder: copying data into a synced directory
    # sends it off the machine, and that is the operator's call.
    monkeypatch.delenv("AIRSHED_BACKUP_DIR", raising=False)
    assert da.backup() is False


def test_backup_mirrors_only_the_irreplaceable_stores(monkeypatch, tmp_path):
    src = tmp_path / "repo" / "data" / "raw"
    for name in ("cams_runs", "meteo_runs", "cams_archive"):
        (src / name / "date=2026-08-24").mkdir(parents=True)
        (src / name / "date=2026-08-24" / "part.parquet").write_bytes(b"x")
    monkeypatch.setattr(da, "repo_root", lambda: tmp_path / "repo")
    dest = tmp_path / "backup"
    monkeypatch.setenv("AIRSHED_BACKUP_DIR", str(dest))

    assert da.backup() is True
    assert (dest / "cams_runs" / "date=2026-08-24" / "part.parquet").is_file()
    assert (dest / "meteo_runs" / "date=2026-08-24" / "part.parquet").is_file()
    # The archive is re-fetchable from Open-Meteo; the runs are not. Copying it
    # would multiply the backup size for nothing.
    assert not (dest / "cams_archive").exists()


def test_backup_is_incremental_and_keeps_earlier_days(monkeypatch, tmp_path):
    """A later pass must not delete days the earlier one saved."""
    src = tmp_path / "repo" / "data" / "raw" / "cams_runs"
    (src / "date=2026-08-23").mkdir(parents=True)
    (src / "date=2026-08-23" / "part.parquet").write_bytes(b"a")
    monkeypatch.setattr(da, "repo_root", lambda: tmp_path / "repo")
    dest = tmp_path / "backup"
    monkeypatch.setenv("AIRSHED_BACKUP_DIR", str(dest))
    da.backup()

    (src / "date=2026-08-24").mkdir(parents=True)
    (src / "date=2026-08-24" / "part.parquet").write_bytes(b"b")
    da.backup()

    assert (dest / "cams_runs" / "date=2026-08-23" / "part.parquet").is_file()
    assert (dest / "cams_runs" / "date=2026-08-24" / "part.parquet").is_file()


# -- lock -------------------------------------------------------------------
@pytest.fixture
def lock_path(monkeypatch, tmp_path):
    path = tmp_path / "archive.lock"
    monkeypatch.setattr(da, "LOCK", path)
    return path


def test_second_instance_refuses_to_start(lock_path):
    assert da.acquire_lock() is True
    assert da.acquire_lock() is False
    da.release_lock()
    assert not lock_path.exists()


def test_a_stale_lock_is_taken_over_not_obeyed(lock_path):
    """A lock outliving its process must not stop the archive forever.

    On a laptop, "killed without cleanup" means "the lid closed", which is
    routine. If that could block the job permanently it would cause the exact
    outage the lock exists to prevent.
    """
    old = dt.datetime.now(dt.timezone.utc) - dt.timedelta(
        minutes=da.CHECK_EVERY_MIN * 10
    )
    lock_path.write_text(old.isoformat(), encoding="utf-8")
    assert da.acquire_lock() is True


def test_an_unreadable_lock_is_taken_over(lock_path):
    lock_path.write_text("not a timestamp", encoding="utf-8")
    assert da.acquire_lock() is True


def test_the_lock_records_the_pid_that_holds_it(lock_path):
    """A timestamp alone cannot tell a running loop from a dead one."""
    assert da.acquire_lock() is True
    age_min, pid = read_lock(lock_path)
    assert pid == os.getpid()
    assert age_min < 1


def test_a_fresh_lock_from_a_dead_process_is_taken_over_at_once(lock_path):
    """The failure this exists to stop: a killed loop blocking every relaunch.

    It happened twice in two days. The age rule alone waits 90 minutes before
    it will call a lock stale, so a loop killed at noon kept every later launch
    backing off politely while nothing archived. Archived runs cannot be
    backfilled, so those minutes are the whole point.
    """
    dead = _a_pid_that_has_exited()
    fresh = dt.datetime.now(dt.timezone.utc)
    lock_path.write_text(f"{fresh.isoformat()}\n{dead}\n", encoding="utf-8")
    assert da.acquire_lock() is True
    _, pid = read_lock(lock_path)
    assert pid == os.getpid()


def test_a_live_process_is_obeyed_however_the_clock_reads(lock_path):
    fresh = dt.datetime.now(dt.timezone.utc)
    lock_path.write_text(f"{fresh.isoformat()}\n{os.getpid()}\n", encoding="utf-8")
    assert da.acquire_lock() is False


def test_a_live_pid_silent_for_hours_is_treated_as_recycled(lock_path):
    """A pid the OS has handed to something else must not block forever.

    Our own pid stands in for the recycled one: alive, but with a heartbeat far
    older than any running loop would leave.
    """
    old = dt.datetime.now(dt.timezone.utc) - dt.timedelta(
        minutes=da.CHECK_EVERY_MIN * 8
    )
    lock_path.write_text(f"{old.isoformat()}\n{os.getpid()}\n", encoding="utf-8")
    assert da.acquire_lock() is True


def test_release_does_not_drop_a_lock_someone_else_took_over(lock_path):
    """After a takeover the previous owner may still be shutting down.

    If its `finally: release_lock()` deleted the new owner's lock, the takeover
    would leave the live loop unprotected -- two loops, double every request.
    """
    fresh = dt.datetime.now(dt.timezone.utc)
    other = _a_pid_that_has_exited()
    lock_path.write_text(f"{fresh.isoformat()}\n{other}\n", encoding="utf-8")
    da.release_lock()
    assert lock_path.exists()


def test_liveness_probe_answers_for_this_process():
    assert process_alive(os.getpid()) is True
    assert process_alive(_a_pid_that_has_exited()) is False
    assert process_alive(0) is None


def test_a_stop_request_is_consumed_once(monkeypatch, tmp_path):
    """The off switch has to be one-shot.

    A stop file left behind would stop the next loop too -- and the next launch
    after that is often the one that would have caught the day's run.
    """
    stop = tmp_path / "archive.stop"
    monkeypatch.setattr(da, "STOP", stop)
    assert da.stop_requested() is False
    stop.write_text("", encoding="utf-8")
    assert da.stop_requested() is True
    assert not stop.exists()
    assert da.stop_requested() is False


def _a_pid_that_has_exited() -> int:
    """A pid that is certainly not running: start a process, wait for its end."""
    proc = subprocess.Popen([sys.executable, "-c", "pass"])
    proc.wait()
    return proc.pid
