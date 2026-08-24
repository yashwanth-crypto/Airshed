"""Ingest modules.

Every module here exposes the same shape (CLAUDE.md, Conventions):

    fetch(start, end, **kwargs) -> pl.DataFrame     # tz-aware UTC `time`
    backfill(start, end, **kwargs) -> list[Path]    # writes Parquet, idempotent

`ingest/` is the only package allowed to touch the network.
"""
