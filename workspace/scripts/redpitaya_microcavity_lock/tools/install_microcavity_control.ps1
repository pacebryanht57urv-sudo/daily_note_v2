param(
    [switch]$Reset,
    [switch]$ForceManaged,
    [string]$InstallRoot
)

$ErrorActionPreference = "Stop"

$ToolsDir = Split-Path -Parent $MyInvocation.MyCommand.Path
$PackageDir = Split-Path -Parent $ToolsDir
$RequirementsFile = Join-Path $PackageDir "requirements\requirements-pyrpl.txt"
$PackageRuntimeConfig = Join-Path $PackageDir "runtime.local.json"
$RequiredPyrplVersion = "0.9.8.0"

if (-not $InstallRoot) {
    if ($env:MICROCAVITY_INSTALL_ROOT) {
        $InstallRoot = $env:MICROCAVITY_INSTALL_ROOT
    } elseif ($env:LOCALAPPDATA) {
        $InstallRoot = Join-Path $env:LOCALAPPDATA "MicrocavityControl"
    } else {
        $InstallRoot = Join-Path $env:USERPROFILE "AppData\Local\MicrocavityControl"
    }
}

$InstallRoot = [System.IO.Path]::GetFullPath($InstallRoot)
$EnvRoot = Join-Path $InstallRoot "envs"
$ManagedEnv = Join-Path $EnvRoot "pyrpl-0.9.8.0-py310"
$ManagedPython = Join-Path $ManagedEnv "Scripts\python.exe"
$InstallRuntimeConfig = Join-Path $InstallRoot "runtime.local.json"

function Write-Step($Message) {
    Write-Host ""
    Write-Host "== $Message =="
}

function Resolve-PythonExe([string]$Command, [string[]]$Arguments) {
    try {
        $result = & $Command @Arguments -c "import sys; print(sys.executable)" 2>$null
        if ($LASTEXITCODE -eq 0 -and $result) {
            return [string]($result | Select-Object -First 1)
        }
    } catch {
        return $null
    }
    return $null
}

function Test-RuntimePython([string]$PythonExe) {
    if (-not $PythonExe -or -not (Test-Path $PythonExe)) {
        return @{
            ok = $false
            python = $PythonExe
            reason = "python_missing"
        }
    }
    $probe = @'
import importlib, json, sys

required_version = "0.9.8.0"
modules = ["numpy", "scipy", "matplotlib", "pyqtgraph", "qtpy", "PyQt5", "pyrpl", "serial", "pyvisa"]
errors = {}
versions = {}
for name in modules:
    try:
        mod = importlib.import_module(name)
        versions[name] = getattr(mod, "__version__", None)
    except Exception as exc:
        errors[name] = repr(exc)

pyrpl_version = versions.get("pyrpl")
ok = (not errors) and (pyrpl_version == required_version)
print(json.dumps({
    "ok": ok,
    "python": sys.executable,
    "pyrpl_version": pyrpl_version,
    "versions": versions,
    "errors": errors,
}, ensure_ascii=False))
'@
    $tmp = Join-Path ([System.IO.Path]::GetTempPath()) ("microcavity_runtime_probe_{0}.py" -f ([System.Guid]::NewGuid().ToString("N")))
    try {
        Set-Content -LiteralPath $tmp -Value $probe -Encoding UTF8
        $json = & $PythonExe $tmp 2>$null
        if ($LASTEXITCODE -ne 0 -or -not $json) {
            return @{
                ok = $false
                python = $PythonExe
                reason = "probe_failed"
            }
        }
        $parsed = $json | ConvertFrom-Json
        return @{
            ok = [bool]$parsed.ok
            python = [string]$parsed.python
            pyrpl_version = [string]$parsed.pyrpl_version
            versions = $parsed.versions
            errors = $parsed.errors
            reason = if ($parsed.ok) { "ok" } else { "missing_or_wrong_version" }
        }
    } catch {
        return @{
            ok = $false
            python = $PythonExe
            reason = "exception"
            error = $_.Exception.Message
        }
    } finally {
        Remove-Item -LiteralPath $tmp -Force -ErrorAction SilentlyContinue
    }
}

function Read-RuntimeConfigPython([string]$Path) {
    if (-not (Test-Path $Path)) {
        return $null
    }
    try {
        $cfg = Get-Content -LiteralPath $Path -Encoding UTF8 -Raw | ConvertFrom-Json
        if ($cfg.runtime_python) {
            return [string]$cfg.runtime_python
        }
    } catch {
        return $null
    }
    return $null
}

function Find-BootstrapPython {
    $candidates = @()
    $py310 = Resolve-PythonExe "py" @("-3.10")
    if ($py310) { $candidates += $py310 }
    $py311 = Resolve-PythonExe "py" @("-3.11")
    if ($py311) { $candidates += $py311 }
    $python = Resolve-PythonExe "python" @()
    if ($python) { $candidates += $python }
    $candidates = $candidates | Where-Object { $_ } | Select-Object -Unique
    foreach ($candidate in $candidates) {
        try {
            $version = & $candidate -c "import sys; print(f'{sys.version_info.major}.{sys.version_info.minor}')"
            if ($LASTEXITCODE -eq 0 -and $version -in @("3.10", "3.11")) {
                return $candidate
            }
        } catch {
            continue
        }
    }
    return $null
}

function Write-RuntimeConfig([string]$PythonExe, [string]$RuntimeKind, [object]$Probe) {
    New-Item -ItemType Directory -Path $InstallRoot -Force | Out-Null
    $state = [ordered]@{
        runtime_python = $PythonExe
        runtime_kind = $RuntimeKind
        install_root = $InstallRoot
        managed_env = $ManagedEnv
        required_pyrpl_version = $RequiredPyrplVersion
        pyrpl_version = $Probe.pyrpl_version
        package_dir = $PackageDir
        updated_at = (Get-Date).ToString("s")
    }
    $json = $state | ConvertTo-Json -Depth 6
    Set-Content -LiteralPath $PackageRuntimeConfig -Value $json -Encoding UTF8
    Set-Content -LiteralPath $InstallRuntimeConfig -Value $json -Encoding UTF8
}

Write-Step "Microcavity Control runtime installer"
Write-Host "package:      $PackageDir"
Write-Host "install root: $InstallRoot"
Write-Host "managed env:  $ManagedEnv"

if (-not (Test-Path $RequirementsFile)) {
    throw "Missing requirements file: $RequirementsFile"
}

if ($Reset) {
    Write-Step "Reset requested"
    Remove-Item -LiteralPath $PackageRuntimeConfig -Force -ErrorAction SilentlyContinue
    Remove-Item -LiteralPath $InstallRuntimeConfig -Force -ErrorAction SilentlyContinue
    if (Test-Path $ManagedEnv) {
        Write-Host "Removing managed env:"
        Write-Host "  $ManagedEnv"
        Remove-Item -LiteralPath $ManagedEnv -Recurse -Force
    }
}

$candidatePythons = New-Object System.Collections.Generic.List[string]
foreach ($path in @(
    $env:MICROCAVITY_RUNTIME_PYTHON,
    (Read-RuntimeConfigPython $PackageRuntimeConfig),
    (Read-RuntimeConfigPython $InstallRuntimeConfig),
    $env:PYTHON_EXE,
    $ManagedPython
)) {
    if ($path -and -not $candidatePythons.Contains($path)) {
        [void]$candidatePythons.Add($path)
    }
}

if (-not $ForceManaged) {
    foreach ($exe in @(
        (Resolve-PythonExe "py" @("-3.10")),
        (Resolve-PythonExe "py" @("-3.11")),
        (Resolve-PythonExe "python" @())
    )) {
        if ($exe -and -not $candidatePythons.Contains($exe)) {
            [void]$candidatePythons.Add($exe)
        }
    }
}

if (-not $ForceManaged) {
    Write-Step "Checking existing Python/PyRPL environments"
    foreach ($candidate in $candidatePythons) {
        Write-Host "candidate: $candidate"
        $probe = Test-RuntimePython $candidate
        if ($probe.ok) {
            Write-Host "  ok: PyRPL $($probe.pyrpl_version)"
            Write-RuntimeConfig $probe.python "existing" $probe
            Write-Step "Selected existing runtime"
            Write-Host "runtime python: $($probe.python)"
            Write-Host "runtime config: $PackageRuntimeConfig"
            exit 0
        }
        Write-Host "  skip: $($probe.reason)"
        if ($probe.pyrpl_version) {
            Write-Host "  pyrpl: $($probe.pyrpl_version)"
        }
    }
}

Write-Step "Creating/checking managed runtime"
if (Test-Path $ManagedPython) {
    $probe = Test-RuntimePython $ManagedPython
    if ($probe.ok) {
        Write-Host "Managed runtime already valid."
        Write-RuntimeConfig $probe.python "managed" $probe
        Write-Host "runtime python: $($probe.python)"
        Write-Host "runtime config: $PackageRuntimeConfig"
        exit 0
    }
    Write-Host "Managed runtime exists but is invalid; replacing it."
    Remove-Item -LiteralPath $ManagedEnv -Recurse -Force
}

$bootstrap = Find-BootstrapPython
if (-not $bootstrap) {
    throw "No Python 3.10/3.11 found. Install Python 3.10/3.11 first, then rerun install_microcavity_control.bat."
}

Write-Host "bootstrap python: $bootstrap"
New-Item -ItemType Directory -Path $EnvRoot -Force | Out-Null
& $bootstrap -m venv $ManagedEnv
if ($LASTEXITCODE -ne 0) {
    throw "Failed to create managed venv: $ManagedEnv"
}

& $ManagedPython -m pip install --upgrade pip
if ($LASTEXITCODE -ne 0) {
    throw "Failed to upgrade pip in: $ManagedPython"
}

& $ManagedPython -m pip install -r $RequirementsFile
if ($LASTEXITCODE -ne 0) {
    throw "Failed to install requirements from: $RequirementsFile"
}

$finalProbe = Test-RuntimePython $ManagedPython
if (-not $finalProbe.ok) {
    throw "Managed runtime was created but validation failed: $($finalProbe.reason)"
}

Write-RuntimeConfig $finalProbe.python "managed" $finalProbe
Write-Step "Selected managed runtime"
Write-Host "runtime python: $($finalProbe.python)"
Write-Host "runtime config: $PackageRuntimeConfig"
Write-Host "install config:  $InstallRuntimeConfig"
