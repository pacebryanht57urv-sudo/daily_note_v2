@echo off
setlocal

set "SCRIPT_DIR=%~dp0"
set "CONTROL_PANEL=%SCRIPT_DIR%microcavity_control_panel.py"
set "CONFIG_EXAMPLE=%SCRIPT_DIR%config.local.example.json"
set "CONFIG_FILE=%SCRIPT_DIR%config.local.json"
set "PYRPL_CONFIG_TEMPLATE=%SCRIPT_DIR%pyrpl_configs\try_bridge_safe.yml"
set "PYRPL_CONFIG_NAME=try_bridge_safe.yml"

if "%PYTHON_EXE%"=="" set "PYTHON_EXE=%USERPROFILE%\pyrpl_bridge_venv\Scripts\python.exe"
if "%MICROCAVITY_CONTROL_PORT%"=="" set "MICROCAVITY_CONTROL_PORT=7880"

REM Avoid stressing the NVIDIA/DWM/remote-display graphics path with Qt GUI rendering.
REM This keeps PyRPL's Qt widgets on software rendering where the Qt backend honors it.
if "%QT_OPENGL%"=="" set "QT_OPENGL=software"
if "%QT_QUICK_BACKEND%"=="" set "QT_QUICK_BACKEND=software"
if "%QTWEBENGINE_DISABLE_GPU%"=="" set "QTWEBENGINE_DISABLE_GPU=1"

if not exist "%PYTHON_EXE%" (
  echo Python environment not found:
  echo   %PYTHON_EXE%
  echo.
  echo Create it first, for example:
  echo   py -3.10 -m venv %%USERPROFILE%%\pyrpl_bridge_venv
  echo   %%USERPROFILE%%\pyrpl_bridge_venv\Scripts\python.exe -m pip install -r "%SCRIPT_DIR%requirements-pyrpl.txt"
  pause
  exit /b 1
)

if not exist "%CONFIG_FILE%" (
  if not exist "%CONFIG_EXAMPLE%" (
    echo Missing config template:
    echo   %CONFIG_EXAMPLE%
    pause
    exit /b 1
  )
  copy /Y "%CONFIG_EXAMPLE%" "%CONFIG_FILE%" >nul
  echo Created local config:
  echo   %CONFIG_FILE%
  echo.
  echo Please edit RP hostname, laser type, and COM ports. Save and close Notepad to continue.
  start /wait notepad "%CONFIG_FILE%"
)

if "%DAILY_NOTE_DATA_ROOT%"=="" echo DAILY_NOTE_DATA_ROOT is not set. Scope captures that save files need the external data root.

set "PYRPL_CONFIG_DIR="
for /f "usebackq delims=" %%I in (`"%PYTHON_EXE%" -c "from pyrpl import memory; print(memory.user_config_dir)" 2^>nul`) do set "PYRPL_CONFIG_DIR=%%I"
if "%PYRPL_CONFIG_DIR%"=="" set "PYRPL_CONFIG_DIR=%USERPROFILE%\pyrpl_user_dir\config"
if not exist "%PYRPL_CONFIG_DIR%" mkdir "%PYRPL_CONFIG_DIR%"
if not exist "%PYRPL_CONFIG_DIR%\%PYRPL_CONFIG_NAME%" (
  if exist "%PYRPL_CONFIG_TEMPLATE%" (
    copy /Y "%PYRPL_CONFIG_TEMPLATE%" "%PYRPL_CONFIG_DIR%\%PYRPL_CONFIG_NAME%" >nul
    echo Installed PyRPL config template:
    echo   %PYRPL_CONFIG_DIR%\%PYRPL_CONFIG_NAME%
  ) else (
    echo Missing PyRPL config template:
    echo   %PYRPL_CONFIG_TEMPLATE%
  )
)

set "EXTRA_ARGS="
if not "%RP_HOSTNAME%"=="" set EXTRA_ARGS=%EXTRA_ARGS% --rp-host "%RP_HOSTNAME%"
if not "%TOPTICA_HOST%"=="" set EXTRA_ARGS=%EXTRA_ARGS% --toptica-host "%TOPTICA_HOST%"
if not "%TOPTICA_PYTHON_EXE%"=="" set EXTRA_ARGS=%EXTRA_ARGS% --toptica-python "%TOPTICA_PYTHON_EXE%"
if not "%PYRPL_BRIDGE_AUTO_START%"=="" (
  if "%PYRPL_BRIDGE_AUTO_START%"=="0" (
    set EXTRA_ARGS=%EXTRA_ARGS% --no-auto-start-bridge
  ) else (
    set EXTRA_ARGS=%EXTRA_ARGS% --auto-start-bridge
  )
)
if not "%PYRPL_BRIDGE_GUI%"=="" (
  if "%PYRPL_BRIDGE_GUI%"=="1" (
    set EXTRA_ARGS=%EXTRA_ARGS% --auto-start-bridge-gui
  ) else (
    set EXTRA_ARGS=%EXTRA_ARGS% --no-auto-start-bridge-gui
  )
)

start "Microcavity control panel" "%PYTHON_EXE%" "%CONTROL_PANEL%" --config-file "%CONFIG_FILE%" --listen-port "%MICROCAVITY_CONTROL_PORT%" %EXTRA_ARGS%
start "" "http://127.0.0.1:%MICROCAVITY_CONTROL_PORT%/"
