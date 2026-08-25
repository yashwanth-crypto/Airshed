@echo off
REM Is everything the model needs alive and current? Double-click after a
REM restart, or any time you want a straight answer.
REM
REM Reports four things, in the order they can go wrong:
REM   1. the archive loop  - the only process that must always be running
REM   2. the run stores    - fresh, and how old
REM   3. the model         - fitted and cached, or first request will refit
REM   4. the dashboard     - listening, or not started (which is normal)
setlocal
cd /d C:\SIH
echo ============================================================
echo  AIRSHED - system check   %DATE% %TIME%
echo ============================================================
echo.

call "C:\SIH\scripts\airshed_py.bat" -m airshed.cli health
set "HEALTH=%ERRORLEVEL%"
echo.

if exist "C:\SIH\data\processed\forecast_model.pkl" (
  echo model: cached at data\processed\forecast_model.pkl
) else (
  echo model: NOT cached - the first forecast request will fit one, which takes
  echo        a minute. Harmless, but do not meet it for the first time on the day.
)

REM Two filters, not one regex: netstat prints the port before the state, so a
REM pattern that assumes the other order silently reports "not running" while
REM the dashboard is serving happily.
netstat -ano | findstr ":8018" | findstr "LISTENING" >nul
if errorlevel 1 (
  echo dashboard: not running - start it with scripts\run_dashboard.bat when needed
) else (
  echo dashboard: listening on http://localhost:8018
)

echo.
echo --- last 5 lines of the archive log ------------------------
powershell -NoProfile -Command "Get-Content C:\SIH\data\archive.log -Tail 5"
echo ------------------------------------------------------------
echo.

if "%HEALTH%"=="0" (
  echo VERDICT: everything the model needs is alive and current.
) else (
  if "%HEALTH%"=="1" echo VERDICT: working, with a warning above worth reading.
  if "%HEALTH%"=="2" echo VERDICT: SOMETHING IS DOWN - read the red line above.
  echo.
  echo If the archive loop is not running:  scripts\run_archive_hidden.vbs
)
echo.
pause
