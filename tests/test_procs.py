"""Tests for the liveness probe behind the archive lock and the health check.

Both callers make a decision that costs something when it is wrong. The loop
refuses to start when it thinks another copy is alive; the health check reports
green when it thinks the loop is running. Getting either backwards is how the
archive stops without anyone noticing, which it did three times in two days.
"""

from __future__ import annotations

import datetime as dt
import os
import subprocess
import sys

from airshed.procs import lock_state, process_alive, read_lock


def _a_pid_that_has_exited() -> int:
    proc = subprocess.Popen([sys.executable, "-c", "pass"])
    proc.wait()
    return proc.pid


def _write_lock(path, pid=None, age_min=0.0):
    stamp = dt.datetime.now(dt.timezone.utc) - dt.timedelta(minutes=age_min)
    body = stamp.isoformat() if pid is None else f"{stamp.isoformat()}\n{pid}\n"
    path.write_text(body, encoding="utf-8")


def test_this_process_is_alive():
    assert process_alive(os.getpid()) is True


def test_an_exited_process_is_not_alive():
    assert process_alive(_a_pid_that_has_exited()) is False


def test_an_impossible_pid_is_unknown_rather_than_dead():
    """Zero and negatives are not questions the OS can answer.

    `None` matters: callers treat "cannot tell" differently from "dead", because
    taking over a lock we cannot prove is free risks two loops.
    """
    assert process_alive(0) is None
    assert process_alive(-1) is None


def test_no_lock_file_means_nothing_is_running():
    from pathlib import Path

    state = lock_state(Path("no-such-lock-file"))
    assert state == {"held": False, "pid": None, "age_min": None, "running": False}


def test_a_live_pid_reads_as_running(tmp_path):
    path = tmp_path / "archive.lock"
    _write_lock(path, pid=os.getpid(), age_min=3)
    state = lock_state(path)
    assert state["held"] is True
    assert state["running"] is True
    assert state["pid"] == os.getpid()
    assert 2 < state["age_min"] < 4


def test_a_dead_pid_reads_as_not_running(tmp_path):
    """The case the old health check could not see.

    A loop that archived this morning and died at noon leaves runs that look
    fresh for another 36 hours, so freshness alone reported green.
    """
    path = tmp_path / "archive.lock"
    _write_lock(path, pid=_a_pid_that_has_exited())
    assert lock_state(path)["running"] is False


def test_a_lock_without_a_pid_is_unresolved(tmp_path):
    """Written by a build from before pids were recorded."""
    path = tmp_path / "archive.lock"
    _write_lock(path, pid=None)
    state = lock_state(path)
    assert state["held"] is True
    assert state["running"] is None


def test_an_unreadable_lock_reports_no_age(tmp_path):
    path = tmp_path / "archive.lock"
    path.write_text("not a timestamp", encoding="utf-8")
    age, pid = read_lock(path)
    assert age == float("inf")
    assert pid is None
    assert lock_state(path)["age_min"] is None
