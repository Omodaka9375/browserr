# Browserr Term

An interactive browser in a real terminal, backed by the Browserr Servo engine.

## Requirements

- The development engine at `engine/target/debug/engine[.exe]`, or a release
  `engine` binary next to `tools/driver.py`.
- Python 3.12.
- A terminal with Sixel or Kitty graphics for native-resolution output.
  Windows Terminal 1.22+ supports Sixel. Use a real console, not an embedded
  terminal without image support.
- `numpy` only for the low-resolution half-block fallback. Pillow is needed
  only by the Kitty compositor and pixel-comparison test, not Sixel mode.

Build the engine with Cargo (the `cargo` executable may live under
`%USERPROFILE%\.cargo\bin` on Windows):

```powershell
cargo build --manifest-path engine\Cargo.toml
```

## Run

In a real PowerShell or CMD terminal:

```powershell
python term\browserr_term.py https://browserr.dev/ --fps 30
```

The client detects graphics support and uses native Sixel or Kitty frames.
Without either protocol it falls back to low-resolution half-blocks.

Options:

- `--renderer sixel`, `--renderer kitty`, or `--renderer half` forces a mode.
- `--cell 16:32` overrides the terminal cell pixel size (tmux, zoomed fonts).
- `--fps 30` sets the frame polling cap (1-60).
- `--scale 0.75` reduces the native viewport for slower systems.
- `--profile hn` uses a persistent named engine profile.
- `--self-test URL` checks the native protocol without a terminal UI.

Keys: `ctrl+l` URL, `ctrl+t` tab, `ctrl+]`/`ctrl+[` next/previous tab,
`ctrl+r` reload, `ctrl+q` quit. Arrows, PageUp/PageDown, mouse clicks and
wheel go to the page. Pass `--gpu` to render with hardware GL (ANGLE/D3D11)
when software raster is the bottleneck, and `--fps 60` for the frame cap.

For tmux, the client enables `allow-passthrough` itself on the running
server (it must be able to run `tmux set` from inside the pane; otherwise
`set -g allow-passthrough on` in `~/.tmux.conf`). Image packets are wrapped
in tmux's DCS passthrough envelope with inner ESC bytes doubled. tmux does
not forward pixel-size queries, so pass `--cell 16:32` (or set
`BROWSERR_TERM_CELL`) to match your font size. Use `--renderer half` only
if the outer terminal has no Sixel support.

## Native frame pipeline

`screenshot` remains the existing full-frame BGRA API. Terminal rendering
uses a separate `frame` command and FRM2 packets:

```json
{"cmd":"frame", "tab":0, "id":1, "format":"sixel", "stream":0,
 "cell_w":10, "cell_h":20, "force":false}
```

The engine reads Servo's native RGBA framebuffer, compares it with the
reference for that tab/stream, and encodes only dirty cell-aligned rectangles.
Each FRM2 packet has a 44-byte header and a PNG or terminal-ready Sixel
payload. An empty payload ends the request. Independent streams retain
independent references; navigation and viewport changes force a full frame.

This removes the old 3.6 MB raw screenshot transfer and Python Sixel encoder
from interactive rendering. Full-screen animation and scrolling still need
to transmit their changed pixels; this is not a GPU shared-memory transport.

## Verification

From the repository root:

```powershell
python term\browserr_term.py --self-test file:///$PWD/term/native-fixture.html
python term\test_native_frame.py file:///$PWD/term/native-fixture.html
python term\test_native_interaction.py
python term\test_term.py
python term\bench_native.py
```

The tests cover decoded PNG pixels, Sixel framing, dirty patches, viewport
flags, wheel scrolling, tab/navigation state isolation, keyboard and click
handling. The existing `scripts/ci.ps1` suite (29 tests) remains green.

## Current limits

- Sixel uses a native 216-color cube plus 32 grayscale levels. Text and
  geometry retain native resolution; color gradients are quantized. The
  encoder uses square pixels (P1=7), RGB percentages, and explicit raster
  dimensions for Windows Terminal compatibility. Two terminal rows are
  reserved for URL/status and help; graphics are restricted to the top area.
- Kitty retransmits its composited image when a patch arrives, because the
  protocol has no universal in-place rectangle replacement operation.
- Terminal resize currently requires restarting the client.
- This is a Servo browser; its site compatibility limits apply here too.
