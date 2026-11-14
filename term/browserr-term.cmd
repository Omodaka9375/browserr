@echo off
setlocal
set "ROOT=%~dp0.."
set "ENGINE=%ROOT%\engine\target\debug\engine.exe"
if exist "%ROOT%\tools\engine.exe" set "ENGINE=%ROOT%\tools\engine.exe"
set "BROWSERR_ENGINE=%ENGINE%"
python "%~dp0browserr_term.py" %*
