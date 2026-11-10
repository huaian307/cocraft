@echo off
rem ---------------------------------------------------------------------------
rem Restart ONLY the panel HTTP service (port 8787).
rem Closing/reopening the panel window does NOT restart the service:
rem the daemon (watch.py, 8788) keeps the old process alive, so new endpoints
rem (/audit/brief, /cu/config...) are missing and the UI shows 400/404.
rem This kills the listener by PORT, waits for the daemon to respawn it,
rem then prints the new pid / startedAt / ui version.
rem ASCII only (cmd reads this file in the console codepage).
rem ---------------------------------------------------------------------------
setlocal
set PORT=8787
set PY=

if exist "%~dp0..\python\python.exe" set "PY=%~dp0..\python\python.exe"
if not defined PY if exist "%~dp0..\python\pythonw.exe" set "PY=%~dp0..\python\pythonw.exe"
if not defined PY (
  for /f "delims=" %%p in ('where python 2^>nul') do if not defined PY set "PY=%%p"
)
if not defined PY (
  if exist "D:\Python312\python.exe" set "PY=D:\Python312\python.exe"
)
if not defined PY (
  echo [BAD] Cannot find python.exe. Run:
  echo       pythonw backend\server.py --port %PORT%
  pause
  exit /b 1
)

"%PY%" "%~dp0..\tools\restart_server.py" --port %PORT% %*
set RC=%ERRORLEVEL%
echo.
if not "%RC%"=="0" echo [BAD] Restart failed (exit %RC%)
pause
exit /b %RC%
