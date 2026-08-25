@echo off
REM Stop the archive loop deliberately.
REM
REM The loop can run with no console window (see run_archive_hidden.vbs), so
REM "close the window" is not the off switch, and Task Manager is the wrong one:
REM killing the process leaves an orphaned lock that blocks the next launch.
REM Instead this drops a file the loop watches for. It notices within about five
REM seconds, exits cleanly, releases its lock, and the supervisor sees a clean
REM exit and stays stopped.
REM
REM Start it again with run_archive_hidden.vbs (hidden, the normal way) or
REM run_archive.bat (visible window, when you want to watch it).
setlocal
set "LOCK=C:\SIH\data\archive.lock"
set "STOP=C:\SIH\data\archive.stop"
REM ping, not timeout: timeout.exe refuses outright when stdin is redirected
REM ("Input redirection is not supported"), which is what happens whenever this
REM is called from a script or a shell pipeline rather than double-clicked.
set "WAIT=ping -n 6 127.0.0.1"

if not exist "%LOCK%" (
  echo No archive loop is holding the lock - nothing to stop.
  goto :eof
)

echo Asking the archive loop to stop...
type nul > "%STOP%"

REM Poll rather than sleep once: a loop mid-fetch finishes the pass first, and
REM the answer we want is "did the lock go away", not "has enough time passed".
for /L %%i in (1,1,12) do (
  %WAIT% >nul
  if not exist "%LOCK%" (
    echo Archive loop stopped and released its lock.
    goto :eof
  )
)

echo Still running after a minute - it is probably mid-fetch.
echo The stop request is left in place; it will be picked up at the next check.
echo Watch it finish with:  Get-Content C:\SIH\data\archive.log -Tail 5
