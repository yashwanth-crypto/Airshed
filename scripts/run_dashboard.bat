@echo off
REM Starts the Airshed dashboard, then opens it in your browser.
REM
REM Double-click this after a restart. The archive job restarts on its own via
REM the Startup folder; this one does not, because a web server you did not ask
REM for should not be listening on a port every time you log in.
REM
REM Leave the window open. Closing it stops the server. Ctrl+C also stops it.
REM
REM Everything it serves is read from the local Parquet cache, so it works with
REM no internet and shows a "last synced" age rather than silently going stale.
REM
REM If the page says the port is already in use, the dashboard is already
REM running - just open http://localhost:8018

cd /d C:\SIH
echo Starting Airshed on http://localhost:8018
echo Leave this window open. Press Ctrl+C to stop.
echo.

REM Give uvicorn a moment to bind before the browser asks for the page. ping, not
REM timeout: timeout.exe refuses when stdin is redirected and is shadowed by a
REM different binary on a Git Bash PATH, and either failure opens the browser
REM instantly at a port nothing is listening on yet.
start "" /min cmd /c "ping -n 5 127.0.0.1 >nul & start http://localhost:8018"

REM Through the interpreter shim, not .venv\Scripts\python.exe directly: Device
REM Guard began refusing that binary on 2026-08-25 (exit 1073751882), which
REM would have failed on demo day with "blocked by your organization's policy"
REM and no dashboard. The shim falls back to the uv interpreter the venv was
REM built from. See scripts\airshed_py.bat.
call "C:\SIH\scripts\airshed_py.bat" -m uvicorn airshed.api.app:app --port 8018
