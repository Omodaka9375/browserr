@echo off
rem MCP launcher: Kai's MCP client does not resolve bare `python` on
rem Windows (no PATHEXT resolution); cmd.exe does. Keep stdout/stderr
rem flowing (stderr is diagnostic only). %~dp0 expands to this script's
rem directory, so this works both from the repo and from an unpacked
rem release archive (where it sits next to browserr_mcp.py).
python "%~dp0browserr_mcp.py" %*
