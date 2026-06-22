@echo off
setlocal

set "SCRIPT_DIR=%~dp0"
set "INSTALL_PS1=%SCRIPT_DIR%tools\install_microcavity_control.ps1"

if not exist "%INSTALL_PS1%" (
  echo Missing installer:
  echo   %INSTALL_PS1%
  pause
  exit /b 1
)

powershell -NoProfile -ExecutionPolicy Bypass -File "%INSTALL_PS1%" %*
set "RC=%ERRORLEVEL%"
echo.
if "%RC%"=="0" (
  echo Install/check completed.
) else (
  echo Install/check failed with code %RC%.
)
echo.
echo To rebuild from scratch later, run:
echo   %~nx0 -Reset -ForceManaged
echo.
pause
exit /b %RC%
