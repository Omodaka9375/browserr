@echo off
rem Browserr Term launcher (Windows): terminal browser on the packaged engine.
rem Works from a release unpack (engine.exe next to browserr_term.py) and
rem from a source tree (engine discovery is done in browserr_term.py).
setlocal
set "HERE=%~dp0"
if exist "%HERE%engine.exe" set "BROWSERR_ENGINE=%HERE%engine.exe"
python "%HERE%browserr_term.py" %*
