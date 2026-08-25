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

REM Give uvicorn a moment to bind before the browser asks for the page.
start "" /min cmd /c "timeout /t 4 >nul & start http://localhost:8018"

"C:\SIH\.venv\Scripts\python.exe" -m uvicorn airshed.api.app:app --port 8018
