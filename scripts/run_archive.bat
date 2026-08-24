@echo off
REM Fallback launcher for the daily archive, for when Task Scheduler is
REM uncooperative. Put a shortcut to this in the Startup folder
REM (Win+R -> shell:startup) and it runs once at every logon.
REM
REM Safe to run repeatedly: every backfill skips what is already cached, and
REM re-fetching a forecast run replaces that run's partition rather than
REM appending to it.
cd /d C:\SIH
"C:\SIH\.venv\Scripts\python.exe" "C:\SIH\scripts\daily_archive.py"
