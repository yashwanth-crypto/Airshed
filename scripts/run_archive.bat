@echo off
REM Launcher for the daily archive. Put a shortcut to this in the Startup
REM folder (Win+R -> shell:startup) and set it to run minimised.
REM
REM This starts the archive in LOOP mode: it stays resident and re-checks every
REM 30 minutes whether today's forecast run is in the store yet. That matters
REM because the Startup folder fires once per logon, so the previous
REM run-once version archived nothing at all on a laptop left on for days.
REM
REM The loop asks a question about state ("is today's run missing?"), not about
REM the clock, so it is unaffected by sleep, hibernation or a closed lid. Task
REM Scheduler's fixed trigger was tried first and refused this account entirely
REM  - see docs/STATUS.md.
REM
REM Safe to launch repeatedly: a lock file keeps a second copy from starting,
REM every backfill skips what is already cached, and re-fetching a forecast run
REM replaces that run's partition rather than appending to it.
REM
REM Check it by the log, never by the launcher's own report:
REM     Get-Content C:\SIH\data\archive.log -Tail 5
REM Or ask for a yes-or-no answer:
REM     .venv\Scripts\python.exe scripts\daily_archive.py --health
cd /d C:\SIH
"C:\SIH\.venv\Scripts\python.exe" "C:\SIH\scripts\daily_archive.py" --loop
