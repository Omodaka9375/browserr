@echo off
rem Browserr updater: delegates to update.ps1 so the user only runs a
rem double-clickable file. PowerShell is always present on Windows.
powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0update.ps1" %*
pause
