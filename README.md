<h1><img src="docs/icon.svg" alt="Browserr" height="56" style="vertical-align:-14px"> Browserr</h1>

[![CI](https://github.com/Omodaka9375/browserr/actions/workflows/ci.yml/badge.svg)](https://github.com/Omodaka9375/browserr/actions/workflows/ci.yml)

A Servo-based, headless browser engine built for AI agents.

Browserr is an alternative to embedding Chromium in an agent stack: a single
`engine` binary (Rust + Servo) that renders pages headlessly, blocks ads, and
exposes a 20-command loopback protocol.

Rendering is CPU by default; `--gpu` opts into hardware rendering. It runs
on Windows, Linux (x64/ARM64) and macOS.

Agents drive it through a Python SDK or through any MCP-capable client.
The automation surface is deliberately small: the entire protocol is
auditable in an afternoon, unlike CDP.


## Features

### Page engine

- Servo 0.8 rendering, CPU by default (instant start on GPU-less machines,
  containers, CI); `--gpu` opts into hardware GL with automatic CPU fallback
- Tabs, navigation, per-tab history, zoom, pointer/wheel/keyboard input
- JavaScript evaluation with results returned as JSON
- On-demand screenshots of the active tab; the SDK saves them as PNG
- Console capture: page messages plus network and TLS load errors
- Downloads fetched straight to disk, without page cookies
- EasyList-based adblock, refreshed weekly

### Reading pages

- `read_page()`: token-cheap prose extraction
- `read_structure()`: every interactive element with its rect, selector and
  a stable ref (`e7`); fields carry label, constraints, state and validity;
  shadow DOM included

### Acting on pages

- Selector interaction: `click_element`, `set_value` (React/Vue-safe),
  `wait_for_selector`
- Ref interaction: `click_ref`, `set_field`, `fill_form` - stable refs,
  no selector fragility; a whole form in one round trip, with type-aware
  writes that fire input/change/blur so page validators run
- Submit cycle: `submit_form` runs the page's own validation, then
  `read_outcome` reports the url, alert texts and invalid fields
- File uploads: picker events answered with local file paths

### Network control

- Fail-closed interception: requests are held for an allow / deny /
  respond decision; unanswered requests deny after 5 s
- Net evidence: every `fetch`/XHR recorded in a ring buffer, in every frame
- Auth challenges surface as events; wrong or missing credentials fail
  closed
- Frame tree listing (cross-origin frames report their url only)

### Human in the loop and trust

- `detect_handoff()`: spots CAPTCHAs, one-time codes, empty passwords,
  payment fields and consent walls
- `pause()` / `resume()`: mutating actions fail loudly while a human has
  control; reads keep working; `wait_for_user()` waits for the handoff
  to clear
- Annotated screenshots: every element boxed and numbered by ref, so an
  agent and a human point at the same element
- Persistent profiles: logins survive sessions
- Evidence: timestamped screenshots plus an append-only action log;
  credentials are never logged
- Credential store: auth challenges answered automatically for known
  hosts, denied for unknown ones
- Confirm gate: submitting a payment or CAPTCHA page needs an explicit
  human confirm

### Operations

- `ping` health check; fast failure when the engine dies
- Ephemeral throwaway profiles for per-run isolation
- Concurrent driver clients over `--driver-port`,
  optional token authentication

## Requirements
- Python 3.10+ for the SDK and MCP server (standard library only)
- Rust stable only if you build from source

## Install

Download the archive for your platform from
[releases](https://github.com/Omodaka9375/browserr/releases) and unpack it
anywhere (no admin rights, no installer). You get:

```text
engine(.exe)      the headless browser engine
driver.py         Python SDK (stdlib only)
browserr_mcp.py   MCP server (stdlib only)
mcp_wrap.cmd      Windows MCP launcher (cmd wrapper)
README.md, LICENSE
```

Requirements: Python 3.10+ on PATH. `driver.py` finds the engine binary
next to itself; override with `BROWSERR_ENGINE=/path/to/engine`.

### Connect an MCP client (Kai, Claude Desktop, ...)

Add the Browserr server in your client's MCP settings:

```json
{
  "mcpServers": {
    "browserr": {
      "command": "python",
      "args": ["C:/path/to/browserr/browserr_mcp.py"],
      "cwd": "C:/path/to/browserr"
    }
  }
}
```

On Windows, you can also command at the bundled wrapper:

```json
{
  "mcpServers": {
    "browserr": {
      "command": "C:/path/to/browserr/mcp_wrap.cmd",
      "cwd": "C:/path/to/browserr"
    }
  }
}
```

The server spawns its own engine (ephemeral profile) on first tool use;
`browser_stop` shuts it down. To share one engine across sessions, start
it yourself with a driver port and attach instead:

```powershell
engine --ephemeral --driver-port=7412 --driver-token=secret
```

```json
{ "mcpServers": { "browserr": {
    "command": "python",
    "args": ["C:/path/to/browserr/browserr_mcp.py", "--port", "7412", "--token", "secret"]
} } }
```

### Use the Python SDK

```python
import sys; sys.path.insert(0, "tools")
from driver import Driver, save_png

d = Driver(ephemeral=True)          # spawns the engine, throwaway profile
try:
    d.navigate("https://example.com"); d.wait_load(); d.wait_ready()
    print(d.eval_js("document.title"))       # results returned as JSON
    title, text = d.read_page()              # token-cheap prose
    structure = d.read_structure()          # headings/buttons/inputs/...
    d.set_value("#q", "search terms")         # React/Vue-safe
    d.click_element("#go")                    # scrolls into view, real pointer
    w, h, bgra = d.screenshot(); save_png("shot.png", w, h, bgra)
    print(d.ping())                          # {'tabs': 1, 'active': 0}
finally:
    d.close()                                 # socket close = engine exit
```

### Run the MCP server directly

```text
python tools\browserr_mcp.py              # headless, ephemeral profile, MCP on stdio
python tools\browserr_mcp.py --port 7412  # attach to an engine started with --driver-port
```

### Build from source

```powershell
cargo build --release --manifest-path engine\Cargo.toml
```

Package a distributable (builds into `dist\` and zips it):

```powershell
powershell -File scripts\package.ps1
```

## Architecture

```text
engine/   Servo webviews (one per tab), CPU-default rendering (--gpu for
          hardware GL), request interception (adblock), automation
          protocol, driver port.
tools/    driver.py (Python SDK), browserr_mcp.py (MCP server), tests.
scripts/  dist/package/ci for Windows (ps1) and Linux (sh).
```

## Protocol

Commands are newline-delimited JSON, tab-addressed, over a loopback socket
(127.0.0.1:7411) that accepts a single client - the automation host:

```text
{"cmd":"navigate","url":"...","tab":N}
{"cmd":"back"|"forward","tab":N}
{"cmd":"new_tab"|"activate"|"close_tab","tab":N}
{"cmd":"resize","w":N,"h":N}
{"cmd":"input"|"wheel"|"key","tab":N,...}     page input forwarding
{"cmd":"eval","tab":N,"js":"..."}             run JS, discard the result
{"cmd":"eval","tab":N,"js":"...","id":X}      run JS, report via evalresult
{"cmd":"screenshot","tab":N}                 paint, answer with a SHT1 frame
{"cmd":"zoom","tab":N,"zoom":F}              page zoom
{"cmd":"ping","id":X}                         health check
{"cmd":"download","url":"...","path":"...","id":X}   fetch straight to disk
{"cmd":"intercept","on":B}                    arm/disarm fail-closed interception
{"cmd":"request","id":X,"action":"allow"|"deny"|"respond",...}   decide held req
{"cmd":"auth","id":X,"user":"...","pass":"..."}       answer an auth challenge
{"cmd":"files","id":X,"paths":[...]}                  answer a file picker
{"cmd":"frames","id":X}                       frame tree (evalresult answer)
```

Responses use a 20-byte header (magic u32, id u64, two u32 fields):

```text
FRM1 + id + w + h    -> w*h*4 BGRA pixels (active tab, deduped, 30 fps cap)
EVT1 + id + len + 0  -> JSON events
SHT1 + id + w + h    -> w*h*4 BGRA pixels (screenshot answer, automation only)
```

Events: `url`, `title`, `history`, `cursor`, `favicon`, `pwcapture`,
`evalresult`, `pong`, `console` (page messages; Servo also routes network
and TLS load errors there), `download` (ok/bytes/error), `request` (held
interception decision), `auth` (challenge), `filepicker` (picker opened).
Only the active tab is painted; switching tabs re-sends that tab's
url/title/history/cursor.

### Computer-use mode

`engine.exe --driver-port=N [--driver-token=T]` opens a second loopback
listener (after the main client connects) that accepts driver clients:

- Commands are validated against an allow-list (page-level only) and
  queued into the main loop, which owns Servo
- Events and SHT1 screenshot answers are mirrored to every attached driver
- With `--driver-token=T`, the first line a driver sends must be
  `{"cmd":"auth","token":"T"}`; unauthenticated sockets receive no events
  and are dropped
- Command lines over 8 MB are rejected without parsing
- `Driver.attach(port, token=...)` handles the handshake

Timing rules worth knowing:

- The url event fires when navigation commits, before the page accepts
  input. Call `wait_ready()` before interacting, and leave a short beat
  between a focus click and `type_text()`.
- The bare engine accepts exactly one main-connection, so headless mode
  always spawns its own engine.

## MCP server

`tools/browserr_mcp.py` is a Model Context Protocol server (standard
library only, newline JSON-RPC on stdio) exposing 36 tools:

- session: start (with optional persistent `profile`), stop, ping
- page: navigate, read, read_structure, links, forms
- interaction: click, click_text, click_element, click_ref, fill,
  set_field, fill_form, key, set_value, submit_form, read_outcome
- capture: screenshot, screenshot_annotated, wait_for, tabs, console,
  evidence, download
- network: intercept, pending_requests, intercept_answer, net, frames
- trust: pause, resume, detect_handoff, wait_for_user, set_credentials

It runs headless with an ephemeral profile by default; `--port N`
attaches to a running engine's driver port. `browser_start(profile=...)`
uses a persistent profile (logins survive sessions) with a per-profile
evidence dir and action log; evidence shots, auto-auth and the submit
confirm gate are always on for MCP-started engines.

## Profiles

All engine data lives under a profile directory as plain files:

```text
settings.json   homepage, user_agent, adblock, search, default_zoom
history.jsonl   one entry per navigation (timestamp, url)
bookmarks.json  url/title pairs
engine\         Servo storage (cookies, HSTS, localStorage), per profile
easylist.txt    adblock filter list
```

`--profile-dir=PATH` selects a profile; `--ephemeral` uses a throwaway
directory deleted on engine exit.

## Build details

- Servo is built with `default-features = false`, dropping devtools,
  clipboard and multiprocess. 
- Single crypto stack: servo-net pins rustls with aws-lc-rs; the engine's
  own downloads (EasyList, favicons) use a minimal rustls client with the
  platform verifier.

## Security notes

- The engine socket and the driver port are loopback-only, but any local
  process can connect by default; use `--driver-token` when that matters.
- No site sandbox on Windows: page code runs in-process with the engine,
  so treat page loads as untrusted; prefer ephemeral profiles for agent
  work.
- Automation downloads send no page cookies; use them for public URLs only.

## Known limitations

- Web compatibility is bounded by Servo 0.8: text, documents, search and
  forms work well; heavy SPAs, DRM video and some modern APIs may not.
- No devtools, no find-in-page.
- History records urls only, without page titles.

## Tests

`tools/test_*.py` exercise the engine as a raw protocol client or as an MCP
client:

```text
test_driver.py           SDK: eval results, click, typing, screenshot pixels
test_agent.py            ping, ephemeral profiles, extraction helpers
test_elements.py         click_element / set_value / wait_for_selector
test_structure.py        read_structure selectors round-trip through set_value
test_features.py         console capture, downloads, load-error surfacing
test_watchdog.py         driver fails fast when the engine dies
test_scroll.py           scroll position survives tab switches
test_bind.py             port handover between dying and booting engines
test_attach.py           computer use: attach through the driver port
test_hardening.py        driver-token gate, oversized-line rejection
test_mcp.py              full MCP session over stdio
test_intercept.py        fail-closed interception: allow / deny / respond
test_auth.py             auth challenges, credential answers, fail-closed case
test_filepicker.py       file picker events answered with local paths
test_net.py              fetch/XHR evidence ring in every frame
test_frames.py           frame tree listing and same-origin walking
test_form.py             form model: labels, constraints, validity, refs
test_flow.py             fill_form / submit / read_outcome end to end
test_annotate.py         annotated screenshots (box + glyph pixels exact)
test_handoff.py           pause/resume, detect_handoff, reads while paused
test_trust.py            evidence dir, action log, auto-auth, confirm gate
test_software_render.py  CPU-default and --gpu rendering log markers
test_input.py            raw-socket input forwarding
test_cursor_zoom.py      cursor events + zoom via devicePixelRatio
test_close.py            tab close must not kill the engine
test_https.py            engine TLS client against the real EasyList endpoint
test_thin.py             release build smoke test
```

Run the suite (fmt + clippy gate, build, tests):

```powershell
powershell -File scripts\ci.ps1
```

