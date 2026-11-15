"""uvx browserr-mcp smoke, patient-client variant.

Cold run: the engine is pre-installed via `browserr-install` (which
downloads), THEN we start `uvx browserr-mcp` and speak MCP. This
separates "does the packaged server work over uvx" from the cold-start
stdin race (a separate concern to fix in the bootstrap).
"""
import json, os, subprocess, sys, tempfile, time

home = os.path.join(tempfile.gettempdir(), 'browserr-uvx-test-home')
env = {**os.environ, 'BROWSERR_HOME': home}

# Pre-install so the MCP invocation is instant.
t0 = time.time()
r = subprocess.run(['uvx', '--from', 'browserr-mcp', 'browserr-install'],
                   env=env, capture_output=True, text=True, timeout=600)
print(r.stdout.strip() or r.stderr.strip())
assert r.returncode == 0, r.stderr
print(f'install phase: {time.time()-t0:.0f}s')

t0 = time.time()
proc = subprocess.Popen(['uvx', 'browserr-mcp'], stdin=subprocess.PIPE,
                        stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                        text=True, encoding='utf-8', env=env)
proc.stdin.write(json.dumps({'jsonrpc': '2.0', 'id': 0, 'method': 'initialize',
    'params': {'protocolVersion': '2024-11-05', 'capabilities': {},
               'clientInfo': {'name': 'uvx-test', 'version': '0'}}}) + '\n')
proc.stdin.flush()
deadline = time.time() + 120
line = ''
while time.time() < deadline:
    line = proc.stdout.readline()
    if line.strip():
        break
    if proc.poll() is not None:
        print('SERVER EXITED:', proc.stderr.read()[:2000])
        sys.exit(1)
msg = json.loads(line)
print('serverInfo:', msg['result']['serverInfo'])
print(f'server start (installed): {time.time()-t0:.0f}s')
proc.terminate()
err = proc.stderr.read()
print('stderr tail:', err[-200:] if err else '(empty)')
print('UVX-SMOKE-OK')
