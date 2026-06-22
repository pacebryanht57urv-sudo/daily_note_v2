param(
  [string]$TaskName = "DailyNoteNightlyDraft",
  [string]$DataRoot = $env:DAILY_NOTE_DATA_ROOT,
  [string]$At = "23:00"
)

$ErrorActionPreference = "Stop"

if (-not $DataRoot) {
  throw "DAILY_NOTE_DATA_ROOT is not set. Pass -DataRoot or set the environment variable first."
}

if (-not (Test-Path -LiteralPath $DataRoot -PathType Container)) {
  throw "Data root does not exist: $DataRoot"
}

$scriptDir = Split-Path -Parent $MyInvocation.MyCommand.Path
$runner = Join-Path $scriptDir "run_nightly_daily_draft.bat"

if (-not (Test-Path -LiteralPath $runner -PathType Leaf)) {
  throw "Runner not found: $runner"
}

$argument = "/c `"`"$runner`" --data-root `"$DataRoot`"`""
$action = New-ScheduledTaskAction -Execute "cmd.exe" -Argument $argument
$trigger = New-ScheduledTaskTrigger -Daily -At ([datetime]::ParseExact($At, "HH:mm", $null))
$principal = New-ScheduledTaskPrincipal -UserId $env:USERNAME -LogonType Interactive -RunLevel LeastPrivilege
$settings = New-ScheduledTaskSettingsSet -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries -StartWhenAvailable

Register-ScheduledTask `
  -TaskName $TaskName `
  -Action $action `
  -Trigger $trigger `
  -Principal $principal `
  -Settings $settings `
  -Description "Generate conservative campaign-local daily drafts from experiment file timestamps." `
  -Force | Out-Null

Write-Host "Registered scheduled task '$TaskName' at $At."
Write-Host "Data root: $DataRoot"
