"""Load `.env` into the process environment.

Secrets live in an untracked `.env` at the repo root and reach the code only
through `os.environ`, so nothing has to be passed around as an argument and
nothing can be accidentally committed or logged as part of a config dump.

Real environment variables always win over the file, so CI and a scheduled
cron job can override without editing anything on disk.
"""

from __future__ import annotations

import logging
import os
from pathlib import Path

log = logging.getLogger(__name__)


def load_dotenv(path: Path | None = None, override: bool = False) -> int:
    """Read KEY=VALUE lines from `.env`. Returns how many variables were set."""
    if path is None:
        from .config import repo_root

        try:
            path = repo_root() / ".env"
        except FileNotFoundError:
            return 0
    if not path.is_file():
        return 0

    count = 0
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        key = key.strip()
        value = value.strip().strip('"').strip("'")
        if not key or not value:
            continue
        if key in os.environ and not override:
            continue
        os.environ[key] = value
        count += 1
    return count
