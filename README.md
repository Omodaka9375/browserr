# Browserr

A minimal, Safari-like browser for Windows.

```text
chrome/  Rust + Rune (GPU-drawn UI: 40px top bar, address pill, tabs, traffic lights)
engine/  Rust + Servo (page engine child process: per-tab webviews, GPU render, BGRA frames)
```

The chrome and the engine are separate processes (their dependency trees conflict:
Rune's parley needs icu_normalizer 2.3+, Servo's mozjs_sys pins icu_capi =2.1.2).
They talk over 127.0.0.1:7411.

## Build

```powershell
cd engine; cargo build --release
cd chrome; cargo build --release
```

## Run (dev)

```powershell
$env:BROWSERR_ENGINE = "D:\Code\2026\Browserr\engine\target\release\engine.exe"
D:\Code\2026\Browserr\chrome\target\debug\chrome.exe
```

The engine's stderr goes to `%TEMP%\browserr-engine.log`; the chrome logs
commands and frame pushes to its own console.

## Run (dist)

```powershell
powershell -File scripts\dist.ps1   # release-builds both into dist\
.\dist\chrome.exe                   # finds engine.exe next to itself
```

## Protocol (v2)

Commands (chrome -> engine, newline JSON, all tab-addressed):

```text
{"cmd":"navigate","url":"...","tab":N}
{"cmd":"back"|"forward","tab":N}
{"cmd":"new_tab"|"activate"|"close_tab","tab":N}
{"cmd":"resize","w":N,"h":N}
{"cmd":"input"|"wheel"|"key","tab":N,...}   page input forwarding
{"cmd":"eval","tab":N,"js":"..."}          run JS (password autofill; discarded)
{"cmd":"eval","tab":N,"js":"...","id":X}   run JS, report result as an evalresult event
{"cmd":"screenshot","tab":N}              paint + answer with a SHT1 frame
{"cmd":"zoom","tab":N,"zoom":F}           page zoom (chrome: Ctrl+wheel)
```

Responses (engine -> chrome, 20-byte header + payload):

```text
FRM1 + id + w + h      -> w*h*4 BGRA pixels (active tab, deduped, capped at 30fps)
EVT1 + id + len + 0    -> JSON events: {"ev":"url"|"title"|"history"|"cursor"|"favicon"|"pwcapture"|"evalresult","tab":N,...}
SHT1 + id + w + h      -> w*h*4 BGRA pixels (screenshot answer, automation only)
```

Only the active tab is painted; switching tabs re-sends that tab's url/title/
history/cursor. Tab 0 exists from startup. The cursor event carries the page's
requested pointer shape (servo Cursor name, lowercase); the chrome maps it onto
rune's cursor set over the page area.

## Status / v2 scope (done)

- Frameless Rune window, drag region, traffic-light close/minimize
- Address pill: live page URL, click to edit (URL or DuckDuckGo search), Enter navigates
- Real multi-tab: one Servo webview + GPU context per tab, per-tab history,
  tab pills with live titles and close buttons
- Back/forward wired to Servo history, greyed out per active tab
- User data: per-profile dirs (chrome data + Servo storage via --profile-dir),
  settings.json (homepage, user agent, search engine, default page zoom),
  history.jsonl, bookmarks (star button), session restore across restarts
  (urls + per-tab zoom), bookmarks/history panel
- Page input forwarding: pointer down/up/move, wheel, keyboard; cursor shape
  feedback from the page (hand over links, I-beam over text, ...); Ctrl+wheel
  page zoom per tab with a toolbar chip and Ctrl+0/-/= shortcuts
- Clear browsing history and clear cookies/site data (engine storage wiped
  in place, tabs reloaded) from the settings page
- GPU rendering: surfman WGL hardware context (software fallback), ~3-20ms per
  frame cycle; identical frames skipped, animated pages capped at 30fps
- Two-exe distribution via scripts/dist.ps1 (side-by-side, no env var needed);
  scripts/package.ps1 adds a browserr-<version>-windows-x64.zip

## User data (Phase 1)

Profiles are separate data dirs, selected on the command line:

```text
chrome.exe --profile work
```

Everything lives in `%APPDATA%\browserr\profiles\<name>\` as plain files:

```text
settings.json   { "homepage": "...", "user_agent": "...", "adblock": bool,
                  "search": "...", "default_zoom": 1.0 }
history.jsonl   appended on every navigation: {"ts": secs, "url": "..."}
bookmarks.json  [{"url": "...", "title": "..."}]  (star button in the toolbar)
session.json    {"urls": [...], "active": N, "zooms": [...]}  (restored on launch)
engine\         Servo's own storage (cookies, HSTS, auth cache, localStorage), per profile
easylist.txt    adblock filter list (downloaded once, refreshed weekly)
```

No `--profile` argument opens the profile picker window instead of a browser:
click a profile to open it in a new window, or type a name and press Enter to
create one. The picker itself is CLI-only free; profile names are sanitized to
letters, digits, dashes and underscores.

Settings live on a full settings page (hamburger menu -> open settings):
homepage, search engine cycle, default page zoom (applies to new tabs), user
agent, adblock toggle, clear browsing history, clear cookies and site data,
and the saved-password list. User agent and adblock restart the engine in
place; so does clearing site data (tabs are reloaded afterwards).

## Automation (driver)

The engine is fully drivable from an external script - not via CDP (servo has
no CDP server), but over its own loopback protocol. `tools/driver.py` wraps it:

```python
import sys; sys.path.insert(0, "tools")
from driver import Driver, save_png

d = Driver(log="engine.log")   # spawns engine.exe headless
try:
    d.navigate("https://example.com"); d.wait_load(); d.wait_ready()
    title = d.eval_js("document.title")       # results returned (any JSON value)
    w, h, bgra = d.screenshot(); save_png("shot.png", w, h, bgra)
    d.click(300, 300); d.type_text("hi")
finally:
    d.close()
```

Notes: the engine accepts exactly one connection, so the driver talks to a
headless engine it spawns, not to a running Browserr UI. The url event fires
when navigation commits, before the page accepts input - call `wait_ready()`
after `wait_load()` before clicking or typing. See `tools/test_driver.py`.

## Passwords

Passwords are stored per profile in the Windows Credential Manager (encrypted
at rest under the logged-in user), one entry per site host. The key button
fills the current page's login form; the menu panel's Passwords section saves,
fills or forgets the credential for the current host. Fill runs a small script
in the page via the engine's eval command (loopback-only, chrome -> engine).
Known limits: the panel's password field is masked (no reveal toggle yet) and
form detection is heuristic (first password input + first username-like
input).

## Adblock

The engine implements `load_web_resource` (Servo's request interception hook)
and cancels every subresource request that matches an EasyList-format filter
list via the `adblock` crate. Main-frame documents are never blocked (a
cancelled document wedges the pipeline). The list is downloaded from
easylist.to on first run and re-downloaded after a week.

- The chrome passes `user_agent` to the engine as `--user-agent=...`; it
  overrides Servo's preference (engine/src/main.rs).
- The ellipsis button opens a panel with bookmarks and the last 15 history
  entries; click an entry to navigate.
- Do Not Track and adblocking need a request hook inside Servo's network
  stack, which servo 0.8 does not expose. Parked with the Phase 2 adblock work.

## GPU shared frames

Frames normally cross the engine->chrome socket as ~4MB BGRA buffers. When
the engine can create a D3D12 device, it instead renders each frame into a
named shared BGRA8 texture (per size) plus a named shared fence, and sends a
small {"ev":"frame"} event; the chrome imports the texture via
`NativeSurface::push_shared` and copies it on its own GPU queue, signaling
the fence when done. No pixel bytes cross the socket. If anything fails
(no D3D12, adapter mismatch), the chrome asks the engine to fall back to
byte frames; `engine --probe-shared` tests the path at startup.

## Save-on-submit password prompt

Pages with a password field get a submit listener installed by the engine
(capture-phase, stashes username + password + origin into sessionStorage).
After the next navigation the engine reads and clears it and reports a
{"ev":"pwcapture"} event; the chrome shows an inline "Save password for
host?" prompt with Save / Not now. sessionStorage is origin-scoped, so the
data never reaches other sites. Limitations: SPA logins without a submit
event or navigation are not captured, and the prompt's username is not
editable (use the menu panel for that).

Favicons: the engine reads the page's `<link rel=icon>` via JS after load,
fetches and decodes the image (PNG/ICO/JPEG/WebP/BMP/GIF), resizes it to
16x16 RGBA and sends it once per icon url; the chrome renders it in tab
pills. `--probe-shared` on the engine tests the shared-frame path.

## Known limitations

- Frames stream over loopback TCP (~4MB each); a GPU shared-texture path would
  remove the copy
- No favicon, no scroll position persistence across tab switches, no downloads
- History is url-only (no page titles stored)
- No site sandbox: servo's multiprocess/sandbox feature is compiled out on
  Windows (`not(target_os = "windows")` in servo 0.8). The engine runs in a
  job object so it is killed if the chrome dies, and it self-exits when the
  chrome disconnects, but content runs in-process with the engine.
- Do Not Track: no request-header hook in servo 0.8 (the interception hook is
  read-only and can only cancel or replace responses)
- Engine exits with a cosmetic surfman panic on shutdown ("Should have
  destroyed the surface first")
