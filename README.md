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
```

Responses (engine -> chrome, 20-byte header + payload):

```text
FRM1 + id + w + h      -> w*h*4 BGRA pixels (active tab, deduped, capped at 30fps)
EVT1 + id + len + 0    -> JSON events: {"ev":"url"|"title"|"history","tab":N,...}
```

Only the active tab is painted; switching tabs re-sends that tab's url/title/
history. Tab 0 exists from startup.

## Status / v2 scope (done)

- Frameless Rune window, drag region, traffic-light close/minimize
- Address pill: live page URL, click to edit (URL or DuckDuckGo search), Enter navigates
- Real multi-tab: one Servo webview + GPU context per tab, per-tab history,
  tab pills with live titles and close buttons
- Back/forward wired to Servo history, greyed out per active tab
- User data: settings.json (homepage, user agent), history.jsonl, bookmarks
  (star button), session restore across restarts, bookmarks/history panel
- GPU rendering: surfman WGL hardware context (software fallback), ~3-20ms per
  frame cycle; identical frames skipped, animated pages capped at 30fps
- Two-exe distribution via scripts/dist.ps1 (side-by-side, no env var needed)

## User data (Phase 1)

Everything lives in `%APPDATA%\browserr` as plain files, no UI required:

```text
settings.json   { "homepage": "https://...", "user_agent": "..." }  (hand-edited, restart to apply)
history.jsonl   appended on every navigation: {"ts": secs, "url": "..."}
bookmarks.json  [{"url": "...", "title": "..."}]  (star button in the toolbar)
session.json    {"urls": [...], "active": N}  (written on tab changes, restored on launch)
```

- The chrome passes `user_agent` to the engine as `--user-agent=...`; it
  overrides Servo's preference (engine/src/main.rs).
- The ellipsis button opens a panel with bookmarks and the last 15 history
  entries; click an entry to navigate.
- Do Not Track and adblocking need a request hook inside Servo's network
  stack, which servo 0.8 does not expose. Parked with the Phase 2 adblock work.

## Known limitations

- Frames stream over loopback TCP (~4MB each); a GPU shared-texture path would
  remove the copy
- No favicon, no scroll position persistence across tab switches, no
  downloads; settings are hand-edited, no settings UI yet
- History is url-only (no page titles stored)
- Engine exits with a cosmetic surfman panic on shutdown ("Should have
  destroyed the surface first")
