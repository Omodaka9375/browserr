# Browserr

A thin browser engine for agents, and a minimal browser for people.

Browserr is a servo-based browser built as two small processes: a headless
`engine.exe` that renders pages and speaks a 15-command loopback protocol, and
an optional `chrome.exe` (Rune UI) for when a human wants to watch. Agents can
drive it headless, through a Python SDK, or through MCP with zero integration
code. It is not another Chromium wrapper: it is an alternative engine in an
agent stack, with an automation surface small enough to audit in an afternoon.

```text
engine/  Rust + Servo. Per-tab webviews, GPU render, adblock, automation.
         Runs headless. One binary, one crypto stack, ~105 MB release.
chrome/  Rust + Rune. 40px bar, address pill, tabs, settings, passwords.
         Optional; the engine never needs it.
tools/   driver.py (SDK), browserr_mcp.py (MCP server), test suite.
```

The split exists because the dependency trees conflict (Rune's parley needs
icu 2.3+, Servo's mozjs_sys pins icu_capi =2.1.2). They talk over
127.0.0.1:7411.

## Quick start

Build (each crate has its own target dir):

```powershell
cargo build --release --manifest-path engine\Cargo.toml
cargo build --release --manifest-path chrome\Cargo.toml
```

Run the browser (dev tree needs the engine path; dist/ does not):

```powershell
$env:BROWSERR_ENGINE = "D:\Code\2026\Browserr\engine\target\release\engine.exe"
chrome\target\release\chrome.exe --profile work
```

Ship a zip: `scripts\package.ps1` builds both into `dist\` and compresses to
`browserr-<version>-windows-x64.zip` (measured: engine 105.4 MB, zip 47.8 MB).

Drive it as an agent:

```text
python tools\browserr_mcp.py              # headless, ephemeral profile, MCP on stdio
python tools\browserr_mcp.py --port 7412  # drive a visible --driver-port window
```

Computer-use hardening: `chrome.exe --driver-port=N` accepts any local process
by default. Start it with `--driver-token=T` to require drivers to send
`{"cmd":"auth","token":"T"}` as their first line; unauthenticated sockets
get no engine events and are dropped. `Driver.attach(port, token=T)` and the
MCP `browser_start` `token` argument handle this automatically.

## Protocol (v2)

Commands are newline JSON, tab-addressed. The socket is loopback and single
client (the chrome, or one driver, or the MCP server).

```text
{"cmd":"navigate","url":"...","tab":N}
{"cmd":"back"|"forward","tab":N}
{"cmd":"new_tab"|"activate"|"close_tab","tab":N}
{"cmd":"resize","w":N,"h":N}
{"cmd":"input"|"wheel"|"key","tab":N,...}     page input forwarding
{"cmd":"eval","tab":N,"js":"..."}            run JS, discard the result
{"cmd":"eval","tab":N,"js":"...","id":X}     run JS, report via evalresult
{"cmd":"screenshot","tab":N}                 paint, answer with a SHT1 frame
{"cmd":"zoom","tab":N,"zoom":F}              page zoom (chrome: Ctrl+wheel)
{"cmd":"ping","id":X}                        health check
{"cmd":"download","url":"...","path":"...","id":X}  fetch straight to disk
```

Responses use a 20-byte header (magic u32, id u64, then two u32 fields):

```text
FRM1 + id + w + h    -> w*h*4 BGRA pixels (active tab, deduped, 30fps cap)
EVT1 + id + len + 0  -> JSON events, see below
SHT1 + id + w + h    -> w*h*4 BGRA pixels (screenshot answer, automation only)
```

Events: `url`, `title`, `history`, `cursor`, `favicon`, `pwcapture`,
`evalresult`, `pong`, `console`, `download`. The `pong` answers `ping` with
the tab count and the active tab. `console` carries page console messages
(log/warn/error/...); servo also routes network and TLS load errors there,
so agents see why a page failed. `download` answers the download command
with ok/bytes/error. `evalresult` returns any JSON value (arrays, objects,
booleans, numbers, strings; DOM handles come back tagged). Only the active
tab is painted; switching tabs re-sends that tab's url/title/history/cursor.

Frames normally cross the socket as D3D12 named shared textures plus fences
(no pixel bytes); if the chrome cannot import them, it asks the engine to
fall back to byte frames. `engine --probe-shared` tests the path.

## Automation

`tools/driver.py` is the SDK. Two modes, one API.

Headless (spawn a bare engine, no UI):

```python
import sys; sys.path.insert(0, "tools")
from driver import Driver, save_png

d = Driver(ephemeral=True)          # throwaway profile, auto-removed on exit
try:
    d.navigate("https://example.com"); d.wait_load(); d.wait_ready()
    print(d.eval_js("document.title"))       # results returned
    title, text = d.read_page()              # token-cheap markdown-ish text
    d.fill("q", "search terms"); d.click_text("Next page")
    w, h, bgra = d.screenshot(); save_png("shot.png", w, h, bgra)
    print(d.ping())                          # {'tabs': 1, 'active': 0}
finally:
    d.close()                                 # socket close = engine exit
```

Computer use (the window stays visible and interactive):

```text
chrome.exe --driver-port=7412 --profile=test
```

```python
d = Driver.attach(7412)   # same API; input coords are page-relative
...
d.close()                  # detaches, the window keeps running
```

The driver port is opt-in, loopback-only, and accepts multiple drivers:
commands are validated (page-level commands only; resize and frame-stream
controls stay chrome-owned) and forwarded to the engine; engine events and
screenshot answers are mirrored to every attached driver. With
`--driver-token=T` the first line must authenticate or the socket is dropped
without events. Command lines over 8 MB are dropped without parsing.

Extraction helpers: `read_page()` (title plus body text, links as
`[label](href)`, invisible elements skipped), `read_structure()` (headings,
buttons, inputs, selects, links, images - each with a rect and a usable CSS
selector that feeds click_element/set_value; prefer for locating UI),
`links()` and `forms()` (visible items with viewport rectangles),
`click_text()`, `fill()`.
Element commands (CSS-selector based, no coordinates or text guessing):
`element_rect()`, `click_element()` (scrolls into view, real pointer event
at the center), `set_value()` (native value setter + input/change events,
so React/Vue controlled inputs accept it), `wait_for_selector()` (SPA
render waiting). Observability: `console()` / `wait_console()` (page
messages and load errors), `ping()`, `EngineDeadError` on engine death.
Files: `download(url, path)` (no page cookies, public URLs only).

Timing rules that matter:

- The url event fires when navigation commits, before the page accepts
  input. Call `wait_ready()` (polls `document.readyState`) before clicking
  or typing, and leave a beat between a focus click and `type_text()`.
- In attach mode the driver port opens before the engine finishes booting.
  Wait for the first url event before the first navigate.
- The engine accepts exactly one connection, so headless mode always spawns
  its own engine.

## Agent browser (MCP)

`tools/browserr_mcp.py` is a Model Context Protocol server (stdlib only,
newline JSON-RPC on stdio) with 19 tools: start, stop, ping, navigate, read,
read_structure, links, forms, click, click_text, click_element, set_value,
wait_for, fill, key, screenshot, tabs, console, download. Headless with an
ephemeral profile by default; `--port N` drives a visible window.

Reliability for unattended runs: `ping` health check, `--ephemeral` profiles
(deleted on exit; a detached helper finishes the delete after servo releases
its SQLite handles), graceful shutdown on socket close.

## The chrome (human mode)

- Frameless Rune window, Windows-style controls, hamburger menu, scrollable
  tab strip with live titles and favicons
- Address pill: live URL, click to edit, Enter navigates or searches
  (configurable engine: DuckDuckGo, Google, Bing)
- Multi-tab with per-tab history, back/forward, session restore
  (urls plus per-tab zoom)
- Page input forwarding with cursor-shape feedback (hand over links, I-beam
  over text), Ctrl+wheel zoom with a toolbar chip and Ctrl+0/-/= shortcuts
- Bookmarks (star button), history panel, full settings page: homepage,
  search engine, default page zoom, user agent, adblock toggle, clear
  browsing history, clear cookies and site data, saved-password list
- Passwords: Windows Credential Manager per profile, autofill, save-on-submit
  prompt, masked fields with a reveal toggle. Fill runs in-page via the
  engine's eval command. Form detection is heuristic; SPA logins without a
  submit event are not captured.
- Adblock: the engine cancels subresource requests that match an EasyList
  list (never main-frame documents; a cancelled document wedges the
  pipeline). The list downloads on first use and refreshes weekly.

## Profiles

Everything lives in `%APPDATA%\browserr\profiles\<name>\` as plain files:

```text
settings.json   { homepage, user_agent, adblock, search, default_zoom }
history.jsonl   appended per navigation: { "ts": secs, "url": "..." }
bookmarks.json  [{ "url": "...", "title": "..." }]
session.json    { urls, active, zooms }   (restored on launch)
engine\         servo storage (cookies, HSTS, localStorage), per profile
easylist.txt    adblock filter list
```

No `--profile` opens the picker window instead of a browser. `--ephemeral`
(engine flag) uses a throwaway dir under %TEMP% instead.

## Build notes (thin)

- servo features: `default-features = false, features = ["bundled", "js_jit"]`
  drops devtools, clipboard and multiprocess; media is already the dummy
  backend on Windows. Release profile: thin LTO, codegen-units 1, symbols
  stripped. `panic` stays unwind: tab close needs catch_unwind.
- One crypto stack: servo-net pins rustls with aws-lc-rs. The engine's two
  small downloads (EasyList, favicons) use a minimal rustls client with the
  platform verifier instead of pulling a second stack (ureq/ring).

## Known limitations

- No site sandbox. Servo's multiprocess mode is compiled out on Windows, so
  page code runs in-process with the engine. The engine is in a job object
  (killed when the chrome dies) and self-exits on disconnect. Agents should
  still treat it as untrusted code execution.
- Web compatibility is servo 0.8's ceiling: an experimental engine. Text,
  docs, search and forms work well; heavy SPAs, DRM video and some modern
  APIs may not.
- No downloads handling, no find-in-page, no devtools UI.
- No CDP. The automation protocol is our own; deliberately small instead.
- Do Not Track is impossible without forking servo-net (the request hook is
  read-only; it can cancel or replace responses but not edit headers).
- History is url-only (no titles stored).
- Windows only.

## Tests

`tools/test_*.py` act as the chrome (raw protocol) or as MCP clients:

```text
test_close.py        tab close must not kill the engine
test_input.py        clicks, wheel, keys reach the page
test_cursor_zoom.py  cursor events + zoom via devicePixelRatio
test_scroll.py       scroll survives tab switches
test_driver.py       SDK: eval results, click, typing, screenshot pixels
test_attach.py       computer use: drive a real window, detach keeps it alive
test_agent.py        ping, ephemeral, read_page/links/forms/fill/click_text
test_features.py     console capture, downloads, load-error surfacing
test_elements.py     click_element / set_value / wait_for_selector (SPA + scroll)
test_structure.py    read_structure: selectors usable by the element commands
test_hardening.py    driver-token gate + oversized-line rejection
test_mcp.py          full MCP session over stdio
test_https.py        engine TLS client: real EasyList download
test_thin.py         release build smoke (set BROWSERR_ENGINE)
```

Engine logs go to `%TEMP%\browserr-engine.log`; the chrome logs to its
console and the frame reader reports engine drops.
