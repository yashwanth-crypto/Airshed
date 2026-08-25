"""Who holds the archive lock, and is that process still alive?

Two callers need the same answer and must not disagree about it. The archive
loop asks before starting, so a second copy cannot double every request. The
health check asks so it can tell "the runs are fresh because the loop is
working" from "the runs are fresh because it ran this morning and then died" --
which look identical for up to 36 hours, and did.
"""

from __future__ import annotations

import datetime as dt
import os
import sys
from pathlib import Path


def process_alive(pid: int) -> bool | None:
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


def read_lock(path: Path) -> tuple[float, int | None]:
    """Age of the lock in minutes, and the pid that wrote it if it recorded one.

    Locks written before this file recorded pids hold a bare timestamp, so the
    pid is optional and its absence leaves the caller with only the age rule.
    """
    try:
        lines = path.read_text(encoding="utf-8").strip().splitlines()
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


def lock_state(path: Path) -> dict:
    """What the lock file says about the loop, in one dict.

    `running` is deliberately three-valued. A lock naming a live pid is running;
    one naming a dead pid is not; one with no pid at all -- written by a version
    before pids were recorded -- cannot be resolved either way, and saying so is
    better than guessing.
    """
    if not path.is_file():
        return {"held": False, "pid": None, "age_min": None, "running": False}
    age_min, pid = read_lock(path)
    alive = process_alive(pid) if pid is not None else None
    return {
        "held": True,
        "pid": pid,
        "age_min": None if age_min == float("inf") else age_min,
        "running": alive,
    }
