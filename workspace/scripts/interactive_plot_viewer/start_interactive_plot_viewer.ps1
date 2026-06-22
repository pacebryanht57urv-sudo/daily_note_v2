$ErrorActionPreference = "Stop"

$scriptDir = Split-Path -Parent $MyInvocation.MyCommand.Path
$serverPath = Join-Path $scriptDir "server.py"

Write-Host "Stopping existing interactive plot viewer servers..."
$existingServers = Get-CimInstance Win32_Process -Filter "name = 'python.exe'" |
  Where-Object {
    $_.CommandLine -and
    (
      $_.CommandLine -like "*interactive_plot_viewer*server.py*" -or
      $_.CommandLine -like "*$serverPath*"
    )
  }

foreach ($process in $existingServers) {
  Write-Host "Stopping PID $($process.ProcessId)"
  Stop-Process -Id $process.ProcessId -Force
}

Add-Type -AssemblyName System.Windows.Forms
$dialog = New-Object System.Windows.Forms.FolderBrowserDialog
$dialog.Description = "Select the data folder containing .npz/.mat files"
$dialog.ShowNewFolderButton = $false

if ($env:DAILY_NOTE_DATA_ROOT -and (Test-Path -LiteralPath $env:DAILY_NOTE_DATA_ROOT -PathType Container)) {
  $dialog.SelectedPath = $env:DAILY_NOTE_DATA_ROOT
}

$result = $dialog.ShowDialog()
if ($result -ne [System.Windows.Forms.DialogResult]::OK -or -not $dialog.SelectedPath) {
  Write-Host "No data folder selected. Nothing started."
  exit 1
}

$dataRoot = $dialog.SelectedPath
Write-Host "Data root: $dataRoot"

python $serverPath --data-root $dataRoot --download-plotly --open-browser
