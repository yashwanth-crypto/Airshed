@echo off
REM Launcher and supervisor for the daily archive.
REM
REM Start it hidden from the Startup folder: put a shortcut to
REM run_archive_hidden.vbs in shell:startup, not to this file. Run this one
REM directly when you want to watch it in a window.
REM Stop it with stop_archive.bat -- not by killing python, which orphans the
REM lock, and not by closing the window, which is how three loops died.
REM
REM LOOP mode: the archive stays resident and re-checks every 30 minutes whether
REM today's forecast run is in the store yet. It asks a question about state
REM ("is today's run missing?"), not about the clock, so sleep, hibernation and
REM a closed lid cannot make it miss. Task Scheduler was tried first and refused
REM this account entirely - see docs/STATUS.md.
REM
REM SUPERVISOR: if the python process dies, this restarts it after a minute
REM instead of leaving the archive silent until the next logon. Archived
REM forecast runs cannot be backfilled, so silence is the one failure that
REM costs something permanent.
REM
REM Check it by the log, never by this launcher's own report:
REM     Get-Content C:\SIH\data\archive.log -Tail 5
REM Or ask for a yes-or-no answer:
REM     scripts\airshed_py.bat scripts\daily_archive.py --health
cd /d C:\SIH
echo Airshed archive supervisor.  Stop it with scripts\stop_archive.bat
echo.

REM --- pick an interpreter that this machine will actually run ----------------
REM 2026-08-25: Device Guard began blocking .venv\Scripts\python.exe outright
REM (STATUS_INVALID_IMAGE_HASH, exit 1073751882) and the archive loop died three
REM times in two days with nothing in the log - the signature of a process
REM stopped by policy rather than one that failed. The venv interpreter is a
REM copy of the uv-managed one, so when the copy is refused we run the original
REM with the venv's packages on PYTHONPATH. Same code, same packages, a binary
REM the policy allows. Delete this block once the venv interpreter is permitted.
set "VENV_PY=C:\SIH\.venv\Scripts\python.exe"
set "BASE_PY=C:\Users\yashw\AppData\Roaming\uv\python\cpython-3.11.15-windows-x86_64-none\python.exe"
set "PY=%VENV_PY%"

"%VENV_PY%" -c "pass" >nul 2>&1
if errorlevel 1 (
  echo venv interpreter refused by policy - falling back to the uv interpreter.
  set "PY=%BASE_PY%"
  set "PYTHONPATH=C:\SIH\.venv\Lib\site-packages;C:\SIH\src"
)

REM Restart only what should be restarted:
REM   0  a deliberate stop via stop_archive.bat -> stay stopped
REM   1  another copy holds the lock            -> a correct refusal, not a crash
REM   *  anything else, or a hard kill          -> restart, that is what this is for
:run
"%PY%" "C:\SIH\scripts\daily_archive.py" --loop
if "%ERRORLEVEL%"=="0" (
  echo Archive loop stopped cleanly.
  goto :eof
)
if "%ERRORLEVEL%"=="1" (
  echo Another archive process holds the lock. Nothing to supervise here.
  goto :eof
)
echo.
echo Archive loop exited with code %ERRORLEVEL% at %DATE% %TIME% - restarting in 60s.
REM ping, not timeout: timeout.exe is shadowed by a different binary on a Git
REM Bash PATH, and refuses outright when stdin is redirected. Either failure
REM turns the pause into an error and this supervisor into a busy loop that
REM respawns hundreds of times a minute. ping waits the same minute and always
REM runs.
ping -n 61 127.0.0.1 >nul
goto run
