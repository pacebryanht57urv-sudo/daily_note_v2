@echo off
setlocal

if "%PYTHON_EXE%"=="" set "PYTHON_EXE=%USERPROFILE%\pyrpl_bridge_venv\Scripts\python.exe"
set "SCRIPT_DIR=%~dp0"
set "SCRIPT=%SCRIPT_DIR%pyrpl_live_bridge.py"

if "%DAILY_NOTE_DATA_ROOT%"=="" (
  echo DAILY_NOTE_DATA_ROOT is not set. Scope captures will fail until it points to the external data root.
)

"%PYTHON_EXE%" "%SCRIPT%" --config try_bridge_safe --hostname 192.168.1.34 --listen-host 127.0.0.1 --listen-port 7870 --allow-risky

