@echo off
setlocal

set "SCRIPT_DIR=%~dp0"
set "MICROCAVITY_SCRIPT_DIR=%SCRIPT_DIR%"
if "%MICROCAVITY_CONTROL_PORT%"=="" set "MICROCAVITY_CONTROL_PORT=7880"
if "%PYRPL_BRIDGE_PORT%"=="" set "PYRPL_BRIDGE_PORT=7870"

echo Stopping microcavity dashboard / PyRPL bridge processes...
echo   package: %SCRIPT_DIR%
echo   dashboard port: %MICROCAVITY_CONTROL_PORT%
echo   bridge port: %PYRPL_BRIDGE_PORT%

powershell -NoProfile -ExecutionPolicy Bypass -Command ^
  "$scriptDir = [System.IO.Path]::GetFullPath($env:MICROCAVITY_SCRIPT_DIR).TrimEnd('\');" ^
  "$ports = @([int]$env:MICROCAVITY_CONTROL_PORT, [int]$env:PYRPL_BRIDGE_PORT);" ^
  "$targets = New-Object System.Collections.Generic.HashSet[int];" ^
  "Get-CimInstance Win32_Process | Where-Object { $_.CommandLine -and $_.CommandLine.Contains($scriptDir) -and ($_.CommandLine -match 'microcavity_control_panel.py|pyrpl_live_bridge.py') } | ForEach-Object { [void]$targets.Add([int]$_.ProcessId) };" ^
  "foreach ($port in $ports) { Get-NetTCPConnection -LocalPort $port -State Listen -ErrorAction SilentlyContinue | ForEach-Object { [void]$targets.Add([int]$_.OwningProcess) } };" ^
  "if ($targets.Count -eq 0) { Write-Host 'No dashboard/bridge process found.'; exit 0 };" ^
  "foreach ($targetPid in $targets) { $p = Get-Process -Id $targetPid -ErrorAction SilentlyContinue; if (-not $p) { continue }; try { Write-Host ('Stopping PID {0}: {1}' -f $targetPid, $p.Path); Stop-Process -Id $targetPid -Force -ErrorAction Stop } catch { if ($_.Exception.Message -match 'Cannot find a process|process identifier') { continue }; Write-Host ('Could not stop PID {0}: {1}' -f $targetPid, $_.Exception.Message) } }"

echo Done.
pause
