@echo off
setlocal

set "SCRIPT_DIR=%~dp0"
set "LAUNCH_PS1=%SCRIPT_DIR%tools\launch_pyrpl_bridge_try.ps1"

if not exist "%LAUNCH_PS1%" (
  echo Missing launcher:
  echo   %LAUNCH_PS1%
  pause
  exit /b 1
)

powershell -NoProfile -ExecutionPolicy Bypass -File "%LAUNCH_PS1%" %*
set "RC=%ERRORLEVEL%"
if not "%RC%"=="0" (
  echo.
  echo Launch failed with code %RC%.
  echo.
  pause
)
exit /b %RC%
