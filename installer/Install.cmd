@echo off
rem SemSearch: double-click to install or upgrade. Windows asks for administrator rights (UAC),
rem then install.ps1 runs in this window. Extra arguments are passed to install.ps1 when this is
rem started from an elevated prompt, e.g.  Install.cmd -Roots "D:\Notes"
setlocal
cd /d "%~dp0"
net session >nul 2>&1
if errorlevel 1 (
  rem not elevated: start this same file again as administrator (the path travels in an
  rem environment variable, so quotes or apostrophes in it cannot break the command line)
  set "SEMSEARCH_SELF=%~f0"
  powershell -NoProfile -ExecutionPolicy Bypass -Command "Start-Process -FilePath $env:SEMSEARCH_SELF -Verb RunAs"
  exit /b
)
powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0install.ps1" %*
echo.
pause
