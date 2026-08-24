"""Ask the OS not to sleep while a long job is running.

This is the in-process route: the running program declares that it is busy, and
the request dies with the process. Nothing in the machine's power settings is
changed, nothing needs administrator rights, and a crash cannot leave the
laptop permanently unable to sleep.

The display is deliberately allowed to switch off — only the *system* is held
awake, so a long evaluation does not sit there burning the screen.

On anything other than Windows this is a no-op rather than an error: the jobs
it wraps must still run in CI and on a server.
"""

from __future__ import annotations

import contextlib
import logging
import sys

log = logging.getLogger(__name__)

ES_CONTINUOUS = 0x80000000
ES_SYSTEM_REQUIRED = 0x00000001


@contextlib.contextmanager
def keep_awake(reason: str = "airshed job"):
    """Hold off system sleep for the duration of the block."""
    if not sys.platform.startswith("win"):
        yield
        return

    try:
        import ctypes

        kernel32 = ctypes.windll.kernel32
        previous = kernel32.SetThreadExecutionState(ES_CONTINUOUS | ES_SYSTEM_REQUIRED)
        if previous == 0:
            log.warning("could not request wakefulness; the machine may sleep mid-run")
        else:
            log.info("holding the system awake for %s (display may still sleep)", reason)
    except Exception as exc:  # never let a power hint break the actual work
        log.warning("keep-awake unavailable: %s", exc)
        yield
        return

    try:
        yield
    finally:
        with contextlib.suppress(Exception):
            kernel32.SetThreadExecutionState(ES_CONTINUOUS)
            log.info("released the wake lock")
