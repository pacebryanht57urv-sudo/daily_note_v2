@echo off
setlocal

powershell -NoProfile -STA -ExecutionPolicy Bypass -File "%~dp0start_interactive_plot_viewer.ps1"
pause
exit /b %ERRORLEVEL%
