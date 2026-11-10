# Browserr

A Servo-based browser built for AI agents, with a minimal desktop UI for
humans.

Browserr is a two-process alternative to embedding Chromium in an agent
stack:

- **`engine.exe`** - a headless page engine (Rust + Servo) that renders on
  the GPU, blocks ads, and exposes a 15-command loopback protocol. One
  binary, one crypto stack, ~105 MB release.
- **`chrome.exe`** - an optional desktop UI (Rust + Rune): address bar,
  tabs, bookmarks, settings, password management. The engine never requires
  it.

Agents drive the engine headless, through a Python SDK (`tools/driver.py`),
or through any MCP-capable client with zero integration code
(`tools/browserr_mcp.py`). The automation surface is deliberately small:
the entire protocol is auditable in an afternoon, unlike CDP.

## Features

**Agent automation**

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
- Crash recovery: a watchdog respawns a dead engine and reopens every tab

**Desktop UI (optional)**

- Frameless window, address pill with search fallback, multi-tab with
  session restore, bookmarks and history panels, full settings page
- Passwords: Windows Credential Manager storage, autofill, save-on-submit
  prompt
- Adblock: EasyList-based subresource blocking, refreshed weekly

## Requirements

- Windows 10/11 (x64)
- Rust stable, MSVC toolchain
- Python 3.10+ (test suite and MCP server; standard library only)

## Getting started

Build (each crate is independent):

```powershell
cargo build --release --manifest-path engine\Cargo.toml
cargo build --release --manifest-path chrome\Cargo.toml
```

Run the desktop browser (a dev-tree chrome needs the engine path; dist
builds find it next to the exe):

```powershell
$env:BROWSERR_ENGINE = "D:\Code\2026\Browserr\engine\target\release\engine.exe"
chrome\target\release\chrome.exe --profile work
```

Package a distributable (builds both into `dist\` and zips it; measured:
engine 105.4 MB, zip 47.8 MB):

```powershell
powershell -File scripts\package.ps1
```

Drive it as an agent:

```text
python tools\browserr_mcp.py              # headless, ephemeral profile, MCP on stdio
python tools\browserr_mcp.py --port 7412  # attach to a visible --driver-port window
```

## Architecture

The engine and the chrome are separate processes because their dependency
trees conflict (Rune's parley requires icu 2.3+, Servo's mozjs_sys pins
icu_capi 2.1.2). They communicate over 127.0.0.1:7411.

```text
engine/   Per-tab Servo webviews, GPU rendering (D3D12 shared textures,
          byte frames as fallback), request interception, automation.
chrome/   Rune UI, profile management, frame reader, driver fan-out hub.
tools/    driver.py (Python SDK), browserr_mcp.py (MCP server), tests.
scripts/  dist.ps1, package.ps1, ci.ps1 (local CI runner).
```

## Protocol

Commands are newline-delimited JSON, tab-addressed, over a loopback socket
that accepts a single client:

```text
{"cmd":"navigate","url":"...","tab":N}
{"cmd":"back"|"forward","tab":N}
{"cmd":"new_tab"|"activate"|"close_tab","tab":N}
{"cmd":"resize","w":N,"h":N}
{"cmd":"input"|"wheel"|"key","tab":N,...}     page input forwarding
{"cmd":"eval","tab":N,"js":"..."}             run JS, discard the result
{"cmd":"eval","tab":N,"js":"...","id":X}      run JS, report via evalresult
{"cmd":"screenshot","tab":N}                  paint, answer with a SHT1 frame
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

Frames normally cross the socket as D3D12 named shared textures plus fences
(no pixel bytes); if the chrome cannot import them it asks the engine to
fall back to byte frames. `engine --probe-shared` tests the path.

## Python SDK

Headless (spawn a bare engine, no UI):

```python
import sys; sys.path.insert(0, "tools")
from driver import Driver, save_png

d = Driver(ephemeral=True)          # throwaway profile, auto-removed on exit
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

Computer use (attach to a visible, still-interactive window):

```powershell
chrome.exe --driver-port=7412 --profile=test
```

```python
d = Driver.attach(7412)   # same API; input coordinates are page-relative
...
d.close()                  # detaches, the window keeps running
```

The driver port is opt-in, loopback-only, and supports multiple concurrent
drivers: commands are validated (page-level only; resize and frame-stream
controls stay chrome-owned), and engine events and screenshot answers are
mirrored to every attached driver. With `--driver-token=T`, the first line
a driver sends must be `{"cmd":"auth","token":"T"}`; unauthenticated sockets
receive no events and are dropped. Command lines over 8 MB are rejected
without parsing.

Timing rules worth knowing:

- The url event fires when navigation commits, before the page accepts
  input. Call `wait_ready()` before interacting, and leave a short beat
  between a focus click and `type_text()`.
- In attach mode the driver port opens before the engine finishes booting;
  wait for the first url event before the first navigate.
- The bare engine accepts exactly one connection, so headless mode always
  spawns its own engine.

## MCP server

`tools/browserr_mcp.py` is a Model Context Protocol server (standard
library only, newline JSON-RPC on stdio) exposing 19 tools: start, stop,
ping, navigate, read, read_structure, links, forms, click, click_text,
click_element, set_value, wait_for, fill, key, screenshot, tabs, console,
download. It runs headless with an ephemeral profile by default;
`--port N` attaches to a visible window.

## Profiles

All user data lives in `%APPDATA%\browserr\profiles\<name>\` as plain
files:

```text
settings.json   homepage, user_agent, adblock, search, default_zoom
history.jsonl   one entry per navigation (timestamp, url)
bookmarks.json  url/title pairs
session.json    open tabs and zoom levels, restored on launch
engine\         Servo storage (cookies, HSTS, localStorage), per profile
easylist.txt    adblock filter list
```

Launching without `--profile` opens the profile picker instead of a
browser. The engine's `--ephemeral` flag substitutes a throwaway directory
under `%TEMP%`, deleted on exit (a detached helper finishes the delete
after Servo releases its SQLite handles).

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
  page code runs in-process with the engine. The engine is in a job object
  (killed when the chrome dies) and self-exits on disconnect, but treat
  page loads as untrusted code execution; prefer ephemeral profiles for
  agent work.
- Automation downloads send no page cookies; use them for public URLs only.

## Known limitations

- Web compatibility is bounded by Servo 0.8: text, documents, search and
  forms work well; heavy SPAs, DRM video and some modern APIs may not.
- No CDP; the automation protocol is Browserr's own (by design).
- No find-in-page or devtools UI.
- Do Not Track would require forking servo-net (the interception hook can
  cancel or replace responses but cannot edit request headers).
- History records urls only, without page titles.
- Windows only.

## Tests

`tools/test_*.py` exercise the engine as a raw protocol client or as an MCP
client:

```text
test_driver.py           SDK: eval results, click, typing, screenshot pixels
test_agent.py             ping, ephemeral profiles, extraction helpers
test_elements.py          click_element / set_value / wait_for_selector
test_structure.py         read_structure selectors round-trip through set_value
test_features.py          console capture, downloads, load-error surfacing
test_watchdog.py          driver fails fast when the engine dies
test_scroll.py            scroll position survives tab switches
test_bind.py              port handover between dying and booting engines
test_input.py             raw-socket input forwarding
test_cursor_zoom.py       cursor events + zoom via devicePixelRatio
test_close.py             tab close must not kill the engine
test_attach.py            computer use: drive a real window, detach keeps it alive
test_hardening.py         driver-token gate, oversized-line rejection
test_chrome_watchdog.py   chrome respawns a killed engine
test_mcp.py               full MCP session over stdio
test_https.py             engine TLS client against the real EasyList endpoint
test_thin.py              release build smoke test
```

Run the suite (fmt + clippy gate, build, tests):

```powershell
powershell -File scripts\ci.ps1     # add $env:CI_WINDOWED=1 for the windowed-chrome tests
```

A GitHub Actions workflow (`.github/workflows/ci.yml`) runs the same gate
on every push. Engine logs go to `%TEMP%\browserr-engine.log`.