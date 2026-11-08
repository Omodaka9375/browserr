# Browserr

A minimal, Safari-like browser for Windows.

```text
chrome/  Rust + Rune (GPU-drawn UI: 40px top bar, address pill, tabs, traffic lights)
engine/  Rust + Servo (page engine child process, renders offscreen, streams BGRA frames)
```

The chrome and the engine are separate processes (their dependency trees conflict:
Rune needs ICU 2.3+, Servo's mozjs pins ICU 2.1). They talk over 127.0.0.1:7411.

## Build

```powershell
cd engine; cargo build --release
cd chrome; cargo build --release
```

## Run

```powershell
$env:BROWSERR_ENGINE = "D:\Code\2026\Browserr\engine\target\release\engine.exe"
D:\Code\2026\Browserr\chrome\target\release\chrome.exe
```

## Status / v1 scope

- Frameless Rune window, drag region, traffic-light close/minimize
- Centered address pill (URL or DuckDuckGo search), Enter navigates
- Multi-tab model in the chrome (tab pill + new tab)
- Servo renders pages offscreen; frames composite into the chrome via Rune NativeSurface
- Fixed 1200x760 viewport (resize wiring pending)
- Back/forward/menu buttons drawn but not wired (pending Servo history API)

## Known limitations

- Software rendering for v1 (CPU readback of frames); GPU shared-texture path planned
- No bookmarks, downloads, history persistence, settings
