@echo off
rem MCP launcher: Kai's MCP client does not resolve bare `python` on
rem Windows (no PATHEXT resolution); cmd.exe does. Keep stdout/stderr
rem flowing (stderr is diagnostic only).
python D:/Code/2026/Browserr/tools/browserr_mcp.py %*