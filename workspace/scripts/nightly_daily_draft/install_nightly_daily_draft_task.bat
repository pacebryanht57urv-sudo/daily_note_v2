@echo off
setlocal

powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0install_nightly_daily_draft_task.ps1" %*
pause
