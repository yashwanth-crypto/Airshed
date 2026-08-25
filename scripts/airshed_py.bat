@echo off
REM Run python with the project's packages, whichever interpreter this machine
REM currently permits.
REM
REM   scripts\airshed_py.bat -m airshed.cli health
REM   scripts\airshed_py.bat scripts\daily_archive.py --health
REM
REM Exists because Device Guard began refusing .venv\Scripts\python.exe on
REM 2026-08-25 (exit 1073751882, STATUS_INVALID_IMAGE_HASH) while allowing the
REM uv-managed interpreter the venv was built from. Rather than edit every
REM command in the runbook, this picks whichever one runs and puts the venv's
REM site-packages on PYTHONPATH when it has to fall back.
REM
REM Delete it once the venv interpreter is permitted again; nothing depends on
REM it except convenience.
setlocal
set "VENV_PY=C:\SIH\.venv\Scripts\python.exe"
set "BASE_PY=C:\Users\yashw\AppData\Roaming\uv\python\cpython-3.11.15-windows-x86_64-none\python.exe"

"%VENV_PY%" -c "pass" >nul 2>&1
if errorlevel 1 (
  set "PYTHONPATH=C:\SIH\.venv\Lib\site-packages;C:\SIH\src"
  "%BASE_PY%" %*
) else (
  "%VENV_PY%" %*
)
REM Propagate python's exit code: `airshed health` answers with it, and a
REM wrapper that always reports success would make the health check useless.
set "RC=%ERRORLEVEL%"
endlocal & exit /b %RC%
