<h1><img src="docs/icon.svg" alt="Browserr" height="56" style="vertical-align:-14px"> Browserr</h1>

[![CI](https://github.com/Omodaka9375/browserr/actions/workflows/ci.yml/badge.svg)](https://github.com/Omodaka9375/browserr/actions/workflows/ci.yml)

A Servo-based, headless browser engine built for AI agents.

Browserr is an alternative to embedding Chromium in an agent stack: a single
`engine` binary (Rust + Servo) that renders pages on the GPU, blocks ads, and
exposes a 20-command loopback protocol - one binary, one crypto stack, ~105 MB
release (measured: 105.4 MB on Windows, 103.6 MB on Linux; ~40 MB archived).
Rendering is CPU by default: the engine starts instantly and never fails on
GPU-less machines (CI runners, containers, boards like the Raspberry Pi).
`--gpu` opts into hardware rendering (falling back to CPU if no GL context
can be created); `--software` is accepted as the explicit default.
Agents drive it through a Python SDK
(`tools/driver.py`) or through any MCP-capable client with zero integration
code (`tools/browserr_mcp.py`).
The automation surface is deliberately small: the entire protocol is auditable
in an afternoon, unlike CDP.

There is no UI and none is planned. `screenshot` returns what the page looks
like; that is the whole "visual" story.

## Features

- Navigation, tabs, back/forward, page zoom, per-tab input (pointer, wheel,
  keyboard); JavaScript evaluation with results returned as JSON
- Prose extraction (`read_page`) and structured page inventory
  (`read_structure`: headings, buttons, inputs, selects, links, images -
  each with a viewport rect, a usable CSS selector and a stable ref like
  `e7`). Form fields carry their label (label for/wrapping/aria), their
  constraints (required, pattern, min/max, maxlength, autocomplete), their
  current state (value, checked, focused) and computed validity; forms
  group their fields and submit controls. Shadow DOM is pierced
- Selector-based interaction: `click_element` (scrolls into view, real
  pointer event), `set_value` (native setter, React/Vue controlled inputs
  accepted), `wait_for_selector` (SPA render waiting)
- Ref-based interaction: `click_ref('e7')` and `set_field(ref, value)` use
  the stable refs from `read_structure`, so no selector fragility
- Shared references: `screenshot_annotated(path)` boxes and numbers every
  interactive element by ref and returns the item list, so an agent and a
  human can both point at "element 7"
- User handoff: `detect_handoff()` scans for things a human must do
  (CAPTCHA iframes, one-time codes, empty passwords, payment fields,
  consent walls); `pause()`/`resume()` hand control over and back, with
  every mutating action failing loudly (PausedError) while reads keep
  working so the agent can watch the page; `wait_for_user()` (MCP
  `browser_wait_for_user`) long-polls until the handoff clears or the
  page navigates
- Trust layer: persistent profiles (`browser_start(profile="...")`,
  logins survive sessions); evidence shots and an append-only action log
  (navigate/fill/submit/click/auth with timestamps, never credentials) in
  a per-profile evidence dir; stored credentials auto-answer HTTP auth
  challenges for their host and unknown hosts fail closed; submitting a
  payment or captcha page refuses without an explicit human confirm
  (`submit_form(confirm=True)`, NeedsConfirmError otherwise)
- Form filling: `fill_form({ref: value, ...})` writes a whole form in one
  round trip with type-aware writes (checkbox/radio, select by value or
  label, date, contenteditable), firing input/change/blur so validators run
- Submit cycle: `submit_form()` dispatches the page's submit event (its
  validation runs, preventDefault is honored), submits natively, waits for
  the navigation or network quiet, and `read_outcome()` reports url,
  alert/aria-live texts and fields that violate required/pattern
- Console capture (page messages plus Servo network/TLS load errors), file
  downloads, screenshots
- Fail-closed request interception: arm it, and qualifying requests
  (main-frame documents, fetch/XHR, JSON) are held for your decision -
  allow, deny (synthetic 403), or fulfill with a synthetic response;
  unanswered requests deny after 5 s
- Auth challenge handling: HTTP 401/proxy challenges surface as events
  with per-challenge credential answers; wrong or missing credentials fail
  closed (drop)
- File pickers: `input type=file` clicks surface as events answered with
  file paths (or cancelled); the page reads the picked file normally
- Net evidence: a document-start user script wraps `fetch`/XHR in every
  frame (pages cannot evade it by grabbing references first) into a ring
  buffer, polled via eval
- Frame control (tier 1): frame tree listing (url, origin, same-origin
  walking with titles); same-origin frames reachable via
  `window.frames[i].document` in evals
- Reliability: `ping` health check, `EngineDeadError` fast failure on
  engine death (0.07 s instead of a timeout), ephemeral throwaway profiles
- Adblock: EasyList-based subresource blocking, refreshed weekly
- Computer-use mode: `--driver-port=N` accepts multiple concurrent driver
  clients (events and screenshots mirrored to all), optional `--driver-token`
  authentication
- GPU rendering (hardware GL, software-GL fallback); screenshots are painted
  on demand

## Requirements

- Windows 10/11 (x64), Linux x64 or ARM64 (X11; Mesa llvmpipe works headless -
  Raspberry Pi 4/5 included, use the linux-arm64 archive), or
  macOS (Apple Silicon; aarch64)
- Rust stable (MSVC toolchain on Windows)
- Python 3.10+ (test suite and MCP server; standard library only)

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

Requirements: Python 3.10+ on PATH, and on Windows a GPU with a D3D11
driver (Linux works headless on llvmpipe; macOS works out of the box).
`driver.py` finds the engine binary next to itself - no configuration
needed. To use a custom engine location, set `BROWSERR_ENGINE=/path/to/engine`.

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

On Windows, some clients (Kai) cannot launch bare `python`; point the
command at the bundled wrapper instead:

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
from driver import Driver
d = Driver(ephemeral=True)          # spawns the engine, throwaway profile
```

### Build from source

```powershell
cargo build --release --manifest-path engine\Cargo.toml
```

Drive it as an agent:

```text
python tools\browserr_mcp.py              # headless, ephemeral profile, MCP on stdio
python tools\browserr_mcp.py --port 7412  # attach to an engine started with --driver-port
```

Or with the Python SDK:

```python
import sys; sys.path.insert(0, "tools")
from driver import Driver, save_png

d = Driver(ephemeral=True)          # spawns engine.exe, throwaway profile
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

Package a distributable (builds into `dist\` and zips it):

```powershell
powershell -File scripts\package.ps1
```

## Architecture

```text
engine/   Servo webviews (one per tab), GPU rendering (hardware GL with
          software-GL fallback), request interception (adblock), automation
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

`--profile-dir=PATH` selects a profile; `--ephemeral` substitutes a
throwaway directory under `%TEMP%`, deleted on engine exit (a detached
helper finishes the delete after Servo releases its SQLite handles).

## Build details

- Servo is built with `default-features = false, features = ["bundled",
  "js_jit"]`, dropping devtools, clipboard and multiprocess (media is
  already the dummy backend on Windows). Release profile: thin LTO, one
  codegen unit, stripped symbols. `panic` remains unwind: tab close needs
  catch_unwind.
- Single crypto stack: servo-net pins rustls with aws-lc-rs; the engine's
  own downloads (EasyList, favicons) use a minimal rustls client with the
  platform verifier rather than pulling in a second TLS stack.

## Security notes

- The engine socket and the driver port are loopback-only, but any local
  process can connect by default; use `--driver-token` when that matters.
- No site sandbox: Servo's multiprocess mode is unavailable on Windows, so
  page code runs in-process with the engine. The engine self-exits on main
  disconnect, but treat page loads as untrusted code execution; prefer
  ephemeral profiles for agent work.
- Automation downloads send no page cookies; use them for public URLs only.

## Known limitations

- Web compatibility is bounded by Servo 0.8: text, documents, search and
  forms work well; heavy SPAs, DRM video and some modern APIs may not.
- No CDP; the automation protocol is Browserr's own (by design).
- No devtools, no find-in-page.
- Do Not Track would require forking servo-net (the interception hook can
  cancel or replace responses but cannot edit request headers).
- History records urls only, without page titles.
- Windows x64, Linux x64, and macOS (Apple Silicon).
- Windows: rendering needs a GPU with a D3D11 driver (surfman WGL
  requirement); Linux renders with any GL, including llvmpipe; macOS
  renders via CGL (works headless on CI runners).

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
test_input.py            raw-socket input forwarding
test_cursor_zoom.py      cursor events + zoom via devicePixelRatio
test_close.py            tab close must not kill the engine
test_mcp.py              full MCP session over stdio
test_https.py            engine TLS client against the real EasyList endpoint
test_thin.py             release build smoke test
```

Run the suite (fmt + clippy gate, build, tests):

```powershell
powershell -File scripts\ci.ps1
```

