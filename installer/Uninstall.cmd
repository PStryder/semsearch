@echo off
rem SemSearch: double-click to uninstall. The index and settings in %ProgramData%\SemSearch are
rem KEPT; to delete them too, run from an elevated prompt:  Uninstall.cmd -PurgeData
setlocal
cd /d "%~dp0"
net session >nul 2>&1
if errorlevel 1 (
  set "SEMSEARCH_SELF=%~f0"
  powershell -NoProfile -ExecutionPolicy Bypass -Command "Start-Process -FilePath $env:SEMSEARCH_SELF -Verb RunAs"
  exit /b
)
powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0uninstall.ps1" %*
echo.
pause
