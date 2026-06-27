param(
    [int]$Port = $(if ($env:MICROCAVITY_CONTROL_PORT) { [int]$env:MICROCAVITY_CONTROL_PORT } else { 7880 })
)

$ErrorActionPreference = "Stop"

$ToolsDir = Split-Path -Parent $MyInvocation.MyCommand.Path
$PackageDir = Split-Path -Parent $ToolsDir
$ControlPanel = Join-Path $PackageDir "src\dashboard\microcavity_control_panel.py"
$ConfigExample = Join-Path $PackageDir "config\config.local.example.json"
$ConfigFile = Join-Path $PackageDir "config.local.json"
$RuntimeConfig = Join-Path $PackageDir "runtime.local.json"
$Installer = Join-Path $ToolsDir "install_microcavity_control.ps1"
$PyrplConfigTemplate = Join-Path $PackageDir "config\pyrpl_configs\try_bridge_safe.yml"
$PyrplConfigName = "try_bridge_safe.yml"
$BridgePort = if ($env:PYRPL_BRIDGE_PORT) { [int]$env:PYRPL_BRIDGE_PORT } else { 7870 }

function Read-RuntimePython {
    if (-not (Test-Path -LiteralPath $RuntimeConfig)) {
        return $null
    }
    try {
        $cfg = Get-Content -LiteralPath $RuntimeConfig -Encoding UTF8 -Raw | ConvertFrom-Json
        if ($cfg.runtime_python) {
            return [string]$cfg.runtime_python
        }
    } catch {
        return $null
    }
    return $null
}

function Ensure-RuntimePython {
    if ($env:MICROCAVITY_USE_EXTERNAL_PYTHON -eq "1") {
        if (-not $env:PYTHON_EXE) {
            throw "MICROCAVITY_USE_EXTERNAL_PYTHON=1 but PYTHON_EXE is empty."
        }
        return $env:PYTHON_EXE
    }
    if ($env:PYTHON_EXE) {
        Write-Host "Ignoring external PYTHON_EXE because MICROCAVITY_USE_EXTERNAL_PYTHON is not 1:"
        Write-Host "  $env:PYTHON_EXE"
    }
    if ($env:MICROCAVITY_RUNTIME_PYTHON) {
        return $env:MICROCAVITY_RUNTIME_PYTHON
    }
    $python = Read-RuntimePython
    if (-not $python -or -not (Test-Path -LiteralPath $python)) {
        if (-not (Test-Path -LiteralPath $Installer)) {
            throw "Missing installer: $Installer"
        }
        Write-Host "Preparing Microcavity Control runtime..."
        & powershell -NoProfile -ExecutionPolicy Bypass -File $Installer
        if ($LASTEXITCODE -ne 0) {
            throw "Installer failed with code $LASTEXITCODE"
        }
        $python = Read-RuntimePython
    }
    if (-not $python -or -not (Test-Path -LiteralPath $python)) {
        throw "Runtime Python not found. Run install_microcavity_control.bat first."
    }
    return $python
}

function Test-DashboardPort {
    $conn = Get-NetTCPConnection -LocalPort $Port -State Listen -ErrorAction SilentlyContinue | Select-Object -First 1
    if (-not $conn) {
        return
    }
    $proc = Get-CimInstance Win32_Process -Filter ("ProcessId=" + $conn.OwningProcess) -ErrorAction SilentlyContinue
    Write-Host ("Dashboard port {0} is already in use by PID {1}." -f $Port, $conn.OwningProcess)
    if ($proc) {
        Write-Host ("Command line: {0}" -f $proc.CommandLine)
    }
    throw "Stop the existing dashboard first, then run: $PackageDir\stop_microcavity_control.bat"
}

function Stop-ExistingPackageProcesses {
    if ($env:MICROCAVITY_SKIP_AUTOSTOP -eq "1") {
        Write-Host "Skipping launch auto-stop because MICROCAVITY_SKIP_AUTOSTOP=1."
        return
    }
    $packageFull = [System.IO.Path]::GetFullPath($PackageDir).TrimEnd('\')
    $targets = New-Object System.Collections.Generic.HashSet[int]
    Get-CimInstance Win32_Process |
        Where-Object {
            $_.CommandLine -and
            $_.CommandLine.Contains($packageFull) -and
            ($_.CommandLine -match 'microcavity_control_panel.py|pyrpl_live_bridge.py')
        } |
        ForEach-Object { [void]$targets.Add([int]$_.ProcessId) }

    if ($targets.Count -eq 0) {
        return
    }
    Write-Host "Stopping existing dashboard/bridge process from this package before launch..."
    foreach ($targetPid in $targets) {
        $proc = Get-Process -Id $targetPid -ErrorAction SilentlyContinue
        if (-not $proc) {
            continue
        }
        try {
            Write-Host ("  stopping PID {0}: {1}" -f $targetPid, $proc.Path)
            Stop-Process -Id $targetPid -Force -ErrorAction Stop
        } catch {
            if ($_.Exception.Message -match "Cannot find a process|process identifier") {
                continue
            }
            Write-Host ("  could not stop PID {0}: {1}" -f $targetPid, $_.Exception.Message)
        }
    }
    Start-Sleep -Milliseconds 500
}

function Test-PortOwners {
    foreach ($checkPort in @($Port, $BridgePort)) {
        $conn = Get-NetTCPConnection -LocalPort $checkPort -State Listen -ErrorAction SilentlyContinue | Select-Object -First 1
        if (-not $conn) {
            continue
        }
        $proc = Get-CimInstance Win32_Process -Filter ("ProcessId=" + $conn.OwningProcess) -ErrorAction SilentlyContinue
        Write-Host ("Port {0} is already in use by PID {1}." -f $checkPort, $conn.OwningProcess)
        if ($proc) {
            Write-Host ("Command line: {0}" -f $proc.CommandLine)
        }
        throw "Port $checkPort is still occupied after package auto-stop. Close that process or run: $PackageDir\stop_microcavity_control.bat"
    }
}

function Ensure-PyrplConfig([string]$PythonExe) {
    $configDir = $null
    try {
        $configDir = & $PythonExe -c "from pyrpl import memory; print(memory.user_config_dir)" 2>$null
    } catch {
        $configDir = $null
    }
    if (-not $configDir) {
        $configDir = Join-Path $env:USERPROFILE "pyrpl_user_dir\config"
    }
    New-Item -ItemType Directory -Path $configDir -Force | Out-Null
    $target = Join-Path $configDir $PyrplConfigName
    if (-not (Test-Path -LiteralPath $target)) {
        if (Test-Path -LiteralPath $PyrplConfigTemplate) {
            Copy-Item -LiteralPath $PyrplConfigTemplate -Destination $target -Force
            Write-Host "Installed PyRPL config template:"
            Write-Host "  $target"
        } else {
            Write-Host "Missing PyRPL config template:"
            Write-Host "  $PyrplConfigTemplate"
        }
    }
}

Stop-ExistingPackageProcesses
Test-PortOwners
Test-DashboardPort
$PythonExe = Ensure-RuntimePython

if (-not $env:QT_OPENGL) { $env:QT_OPENGL = "software" }
if (-not $env:QT_QUICK_BACKEND) { $env:QT_QUICK_BACKEND = "software" }
if (-not $env:QTWEBENGINE_DISABLE_GPU) { $env:QTWEBENGINE_DISABLE_GPU = "1" }

Write-Host "Using Python:"
Write-Host "  $PythonExe"
& $PythonExe -c "import sys, pyrpl; assert pyrpl.__version__ == '0.9.8.0', pyrpl.__version__; print('Python:', sys.executable); print('PyRPL:', pyrpl.__version__, pyrpl.__file__)"
if ($LASTEXITCODE -ne 0) {
    Write-Host "Runtime Python is missing the required PyRPL environment. Running installer..."
    & powershell -NoProfile -ExecutionPolicy Bypass -File $Installer
    if ($LASTEXITCODE -ne 0) {
        throw "Installer failed with code $LASTEXITCODE"
    }
    $PythonExe = Ensure-RuntimePython
    & $PythonExe -c "import sys, pyrpl; assert pyrpl.__version__ == '0.9.8.0', pyrpl.__version__; print('Python:', sys.executable); print('PyRPL:', pyrpl.__version__, pyrpl.__file__)"
    if ($LASTEXITCODE -ne 0) {
        throw "Runtime still invalid after installer."
    }
}

if (-not (Test-Path -LiteralPath $ConfigFile)) {
    if (-not (Test-Path -LiteralPath $ConfigExample)) {
        throw "Missing config template: $ConfigExample"
    }
    Copy-Item -LiteralPath $ConfigExample -Destination $ConfigFile -Force
    Write-Host "Created local config:"
    Write-Host "  $ConfigFile"
    Write-Host "Please edit RP hostname, laser type, and COM ports. Save and close Notepad to continue."
    Start-Process -FilePath "notepad.exe" -ArgumentList @($ConfigFile) -Wait
}

if (-not $env:DAILY_NOTE_DATA_ROOT) {
    $dataRoot = [Environment]::GetEnvironmentVariable("DAILY_NOTE_DATA_ROOT", "User")
    if (-not $dataRoot) {
        $dataRoot = Join-Path $env:USERPROFILE "daily_note_data"
    }
    New-Item -ItemType Directory -Path $dataRoot -Force | Out-Null
    $env:DAILY_NOTE_DATA_ROOT = $dataRoot
    Write-Host "Using DAILY_NOTE_DATA_ROOT:"
    Write-Host "  $env:DAILY_NOTE_DATA_ROOT"
}

Ensure-PyrplConfig $PythonExe

$extraArgs = @()
if ($env:RP_HOSTNAME) { $extraArgs += @("--rp-host", $env:RP_HOSTNAME) }
if ($env:TOPTICA_HOST) { $extraArgs += @("--toptica-host", $env:TOPTICA_HOST) }
if ($env:TOPTICA_PYTHON_EXE) { $extraArgs += @("--toptica-python", $env:TOPTICA_PYTHON_EXE) }
if ($env:PYRPL_BRIDGE_AUTO_START) {
    if ($env:PYRPL_BRIDGE_AUTO_START -eq "0") {
        $extraArgs += "--no-auto-start-bridge"
    } else {
        $extraArgs += "--auto-start-bridge"
    }
}
if ($env:PYRPL_BRIDGE_GUI) {
    if ($env:PYRPL_BRIDGE_GUI -eq "1") {
        $extraArgs += "--auto-start-bridge-gui"
    } else {
        $extraArgs += "--no-auto-start-bridge-gui"
    }
}

$args = @($ControlPanel, "--config-file", $ConfigFile, "--listen-port", [string]$Port) + $extraArgs
Start-Process -FilePath $PythonExe -ArgumentList $args -WorkingDirectory $PackageDir -WindowStyle Hidden
Start-Process "http://127.0.0.1:$Port/"
