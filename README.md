<img src="docs/icon.svg" alt="Browserr" height="56" style="vertical-align:-14px"> <h1>Browserr</h1>

[![CI](https://github.com/Omodaka9375/browserr/actions/workflows/ci.yml/badge.svg)](https://github.com/Omodaka9375/browserr/actions/workflows/ci.yml)

A Servo-based, headless browser engine built for AI agents.

Browserr is an alternative to embedding Chromium in an agent stack: a single
`engine` binary (Rust + Servo) that renders pages on the GPU, blocks ads, and
exposes a 15-command loopback protocol - one binary, one crypto stack, ~105 MB
release on Windows, smaller on Linux. Agents drive it through a Python SDK
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
  each with a viewport rect and a usable CSS selector)
- Selector-based interaction: `click_element` (scrolls into view, real
  pointer event), `set_value` (native setter, React/Vue controlled inputs
  accepted), `wait_for_selector` (SPA render waiting)
- Console capture (page messages plus Servo network/TLS load errors), file
  downloads, screenshots
- Reliability: `ping` health check, `EngineDeadError` fast failure on
  engine death (0.07 s instead of a timeout), ephemeral throwaway profiles
- Adblock: EasyList-based subresource blocking, refreshed weekly
- Computer-use mode: `--driver-port=N` accepts multiple concurrent driver
  clients (events and screenshots mirrored to all), optional `--driver-token`
  authentication
- GPU rendering (hardware GL, software-GL fallback); screenshots are painted
  on demand

## Requirements

- Windows 10/11 (x64), or Linux x64 (X11; Mesa llvmpipe works headless)
- Rust stable (MSVC toolchain on Windows)
- Python 3.10+ (test suite and MCP server; standard library only)

## Getting started

Build:

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
```

Responses use a 20-byte header (magic u32, id u64, two u32 fields):

```text
FRM1 + id + w + h    -> w*h*4 BGRA pixels (active tab, deduped, 30 fps cap)
EVT1 + id + len + 0  -> JSON events
SHT1 + id + w + h    -> w*h*4 BGRA pixels (screenshot answer, automation only)
```

Events: `url`, `title`, `history`, `cursor`, `favicon`, `pwcapture`,
`evalresult`, `pong`, `console` (page messages; Servo also routes network
and TLS load errors there), `download` (ok/bytes/error). Only the active
tab is painted; switching tabs re-sends that tab's url/title/history/cursor.

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
library only, newline JSON-RPC on stdio) exposing 19 tools: start, stop,
ping, navigate, read, read_structure, links, forms, click, click_text,
click_element, set_value, wait_for, fill, key, screenshot, tabs, console,
download. It runs headless with an ephemeral profile by default;
`--port N` attaches to a running engine's driver port.

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
- Windows x64 and Linux x64; macOS is not yet ported (CGL path).
- Windows: rendering needs a GPU with a D3D11 driver (surfman WGL
  requirement); Linux renders with any GL, including llvmpipe.

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

