@echo off
rem Browserr Term launcher (Windows): terminal browser on the packaged engine.
rem Works from a release unpack (engine.exe next to driver.py) and a source tree.
setlocal
set "HERE=%~dp0"
set "BROWSERR_ENGINE="
if exist "%HERE%engine.exe" set "BROWSERR_ENGINE=%HERE%engine.exe"
if not defined BROWSERR_ENGINE if exist "%HERE%..\..\engine\target\release\engine.exe" set "BROWSERR_ENGINE=%HERE%..\..\engine\target\release\engine.exe"
if not defined BROWSERR_ENGINE if exist "%HERE%..\..\engine\target\debug\engine.exe" set "BROWSERR_ENGINE=%HERE%..\..\engine\target\debug\engine.exe"
if not defined BROWSERR_ENGINE (
    echo browserr-term: engine.exe not found ^(expected next to browserr_term.py^)
    exit /b 1
)
python "%HERE%browserr_term.py" %*
