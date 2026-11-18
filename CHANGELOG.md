# Changelog

All notable changes to Browserr are documented here. The format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/); the versioning
scheme is [SemVer](https://semver.org/). Releases are published by CI on
tag push; asset archives and full diffs live on the
[releases page](https://github.com/Omodaka9375/browserr/releases).

## [0.9.0] - 2026-11-18

### Added

- **Android (Termux) support.** The engine ships as an android-arm64
  release asset; inside Termux, `pip install browserr-mcp` detects the
  environment and downloads it automatically. Requirements: Android 10+
  (API 29), 64-bit userland; headless only. The MCP server and Python
  SDK run unmodified under Termux Python.
- eval now awaits promises: async IIFEs and fetch/await scripts return
  the resolved value instead of an empty object. Sync scripts keep
  completion-value semantics.
- Humanized clicks: click_element and click_ref click a jittered point
  inside the element with short approach moves and small pauses,
  instead of an instant exact-center click.

### Fixed

- Linux servers without mesa packages failed at startup with a raw
  surfman panic ("Unable to load the libEGL shared object"). The engine
  now catches it and prints the packages to install
  (libegl1 libgl1 libgbm1 libgl1-mesa-dri). EGL is required even for
  CPU (llvmpipe) rendering - it is the context-creation API.

## [0.8.2] - 2026-11-18

### Added

- Built-in User-Agent profiles on `browser_start`: pass `user_agent:
  "chrome"` (also `chrome_mac`, `chrome_linux`, `edge`, `firefox`,
  `safari`, `android`, `iphone`) instead of pasting a UA string; any
  other value passes through verbatim.
- The MCP server verifies and reports the engine binary version at
  startup: `serverInfo` carries `engineVersion` and `enginePath`, and a
  stale cached engine triggers a loud warning with the fix-it steps.

### Fixed

- The control plane can no longer be wedged by a stuck page tool:
  `browser_ping` and `browser_stop` are answered immediately while
  other tools run serialized on a single worker thread.
- Orphaned engines: a killed MCP server used to leave a wedged engine
  alive forever (holding port 7411). The engine is now bound to its
  parent's lifetime at the OS level - Job Object `KILL_ON_JOB_CLOSE`
  on Windows, `PDEATHSIG` on Linux - so it dies with the server however
  the server dies.

### Changed

- **Breaking:** timeout parameters on `browser_wait_for`,
  `browser_click_element`, `browser_set_value`, `browser_wait_auth`,
  `browser_wait_filepicker` and `browser_wait_for_user` are now
  `timeout_ms` (milliseconds, capped at 120000), matching
  `wait_ms`/`settle_ms` on `browser_navigate`. The old ambiguous names
  (`timeout`, `timeout_seconds`) are removed - the unit now lives in
  the name, so a `20000` means 20 seconds and not 5.5 hours.

## [0.8.1] - 2026-11-17

### Fixed

- `browser_wait_for` could wedge the engine on large slow SPAs (the
  Oracle careers site): the poll ran a forced layout (`scrollIntoView`)
  per tick and stacked new evals while previous ones were still queued,
  leaving the page's script thread grinding a backlog long after the
  call had timed out. Polling is now scroll-free and keeps at most one
  eval outstanding, so a slow page can never accumulate a queued-eval
  backlog. `click_element` still scrolls its target into view.

## [0.8.0] - 2026-11-17

### Added

- A driver-level `stop` command can terminate the engine even when a page
  has stalled Servo's main loop. `browser_ping` is answered directly by
  the driver connection, so health checks keep working during a stall.

### Fixed

- WebGL no longer kills the engine on the default software-rendering path.
  WebGL and the renderer now share the renderer's D3D11 adapter; failed
  surface imports or swaps skip the affected frame instead of panicking.
- `browser_read` now pierces open shadow roots and expands `<slot>`
  contents, so web-component pages expose the same application text as
  `browser_read_structure`.
- The "browser session not started" error lists running sessions and
  notes that every browser tool accepts a `session` argument.

## [0.7.1] - 2026-11-15

### Fixed

- `read_structure` silently clipped reported field values to 200 chars,
  so an agent verifying a long write through it could mistake the
  preview for the stored value and retry writes that had already
  succeeded. Fields now also report `value_clipped` with the true value
  length whenever the preview applies.

## [0.7.0] - 2026-11-15

### Added

- Terminal client (`term/`): interactive TUI that renders the engine's
  native frames. Native Sixel with dirty-rectangle patches (cell-aligned,
  band-quantized to stop scroll corruption), Kitty graphics, half-block
  fallback. Runs on Windows Terminal, conhost, and inside tmux (passthrough
  envelope with DECSC/DECRC cursor-state bracketing, auto-enabled
  `allow-passthrough`). Launchers: `term\browserr-term.cmd`,
  `term/browserr-term`. Shipped in the release archives.
- Engine `frame` command and FRM2 protocol: native RGBA readback encoded to
  PNG or Sixel in Rust, per-tab/per-stream dirty tracking, cell-aligned
  patch rectangles, explicit END packet on every path. The engine accepts
  `--width=`, `--height=`, and `--port=`.
- MCP named sessions: `browser_start` accepts a `session` name; each
  session is its own engine (one per proxy/user-agent group) on its own
  port. `browser_start` also accepts a `user_agent` (engine argv, never
  logged). Profile sessions now get unique ports too.
- MCP `browser_navigate` fails last instead of fail first: a redirect that
  rewrites the URL or a late commit is verified against the engine event
  stream; the result carries `settled`, `error_page`, and a note instead of
  throwing. Only a stalled engine raises.
- llms.txt served from the landing page root for agent consumption.
- In-place updater shipped in the release archives: `update.ps1` /
  `update.cmd` / `update.sh`. Resolves the latest GitHub release (or an
  explicit `-Version` tag), verifies the asset's sha256 digest (refuses to
  install unverified payloads; override with `-AllowNoDigest` /
  `ALLOW_NO_DIGEST=1`), backs up the current install outside the tree,
  copies the new payload, and confirms the replacement by `--version`.
- Engine `--version`: prints `browserr <version>` and exits before any GL
  or servo init, so it works on machines without a display. Engines older
  than 0.7.0 ignore the flag and boot the browser instead, so the updater
  always probes it under a timeout.

### Fixed

- Terminal client: swapped Home/End key translation on Windows; astral
  (emoji) input crashed the input loop on a lone UTF-16 surrogate; engine
  death left a frozen screen labeled "loading"; split escape sequences were
  typed into pages as garbage; QuickEdit consumed mouse input on conhost;
  teardown could be swallowed by an unterminated Sixel DCS; half-block
  clicks mapped to cell corners instead of centers; forms without submit
  buttons could not be submitted by Enter.
- MCP: `browser_stop` did not clear the legacy driver alias.
- Driver: a timed-out native frame request leaked its patches into later
  scans; the FRM2 header-size comment was wrong.
- Engine: the frame command sent no END packet for an unknown tab, stalling
  the client until timeout.
- Updater: the Linux/macOS digest extraction never matched GitHub's compact
  release JSON (a nested uploader object sits between the asset name and
  its digest), so updates installed unverified; the check now anchors on
  digest -> download URL and fails closed when a digest is absent.
- Tests: file:// fixture URLs were built as "file:///" + an absolute path,
  which yields four slashes on POSIX; all sites now use
  `pathlib.Path.as_uri()`.

### Changed

- Test suite restructured into `tests/` (42 files, git renames); icon
  build tooling moved into `scripts/`; the CI gate runs the new session,
  user-agent, navigate-quirk, and terminal tests (39 total).

## [0.6.2] - 2026-11-14

### Added

- Application icon: chevron embedded as an exe resource and shown on the
  headed window title bar and taskbar. Regeneration tooling included.

### Fixed

- `wait_ready` no longer reports ready during Servo's transient
  about:blank commit document; reads, evals, and screenshots no longer see
  a blank page right after navigation.
- Non-Windows builds: the icon embed code is cfg-gated so the build script
  compiles on every platform.

## [0.6.1] - 2026-11-14

### Fixed

- HTTP auth: a same-credentials re-challenge within 20 seconds is treated
  as a rejection; stored credentials are invalidated, the challenge fails
  closed, and the event stream records `auth_rejected`.
- The driver strips userinfo from re-challenge URLs (Servo places
  `user:pass@host` there) so credential matching works.
- `submit_and_wait` no longer breaks early on network idle; a diagnostic
  note reports what it observed.
- Test honesty: the cross-origin local-navigation block in Servo is
  Windows-only; the flow test asserts the real behavior on each platform.

## [0.6.0] - 2026-11-13

### Added

- Upstream proxy support (HTTP/HTTPS with optional basic auth in the URL),
  `no_proxy` exemptions, and the `browser_document_status` probe (method,
  status, content-type of the current document, no render needed).
- Settle-aware `browser_navigate`: commit budget (`wait_ms`) plus a settle
  budget (`settle_ms`) with `settled: false` results instead of errors on
  slow SPAs.

## [0.5.0] - 2026-11-13

### Added

- SPA and JavaScript compatibility: pref-gated Servo APIs enabled, WebGL
  through ANGLE, crypto and idle shims.

### Changed

- Linux CI pinned to ubuntu-24.04 (mozangle bindgen is incompatible with
  LLVM 21); ANGLE DLL packaging hardened.

## [0.4.0] - 2026-11-13

### Added

- Headed native window: real OS window for human handoff (logins, captchas,
  payments) driven by the same engine and driver port.
- Download buttons on the landing page resolve real assets from the GitHub
  release API, with separate Linux x64 and ARM64 links.

### Fixed

- Stale element refs raise clear errors; the action log is readable.

## [0.3.1] - 2026-11-12

### Changed

- CPU rendering is the default: instant start and it never fails on
  GPU-less machines (Raspberry Pi, containers, CI). `--gpu` opts into
  hardware rendering with automatic CPU fallback; `--software` is kept as
  an explicit alias.
- linux-arm64 release target: native Ubuntu ARM64 runner, arch-aware cache
  keys, Raspberry Pi 4/5 documented.

### Fixed

- Load retries are time-based (2 s deadline), not tick-based; a tick costs
  16 ms on GPU but about 1 s on paravirtualized CI GL, starving the retry.
- macOS: surface FBO binding before rendering (CGL reported FB0 undefined
  and killed the engine at first paint); CI matrix leg and release asset.

## [0.3.0] - 2026-11-12

### Added

- Trust layer: persistent profiles on `browser_start`, evidence shots
  (plain and annotated) plus an append-only action log, stored credentials
  auto-answering HTTP auth (unknown hosts fail closed, credentials are
  never logged), and a submit confirm gate on payment and captcha pages.
- Human-in-the-loop primitives: pause/resume (mutating actions raise while
  paused, reads keep working), `detect_handoff` for captcha, OTP, password,
  payment, and consent pages, and the `browser_pause`, `browser_resume`,
  `browser_detect_handoff`, and `browser_wait_for_user` MCP tools.
- Annotated screenshots: numbered boxes by stable ref with a built-in
  digit font (no dependencies) and an item list.
- Form flow: `fill_form` (one round trip, type-aware), `submit_form`
  (native submit; Servo's `requestSubmit` never reaches submit listeners),
  `read_outcome`, `wait_network_idle`, `submit_and_wait`.
- Form model v2: labels, constraints, computed validity, select option
  labels, form grouping, stable refs, shadow DOM piercing.
- Fail-closed request interception: hold, allow, deny, or respond, with a
  pending-requests listing; net evidence userscript records every fetch
  and XHR in every frame at document start; frame-tree command with
  same-origin walking.
- Auth challenge and file picker handling; MCP tool count grew from 19 to
  36.

### Changed

- MCP tools: 19 to 36; test suite: 15 to 24 tests.

## [0.2.2] - 2026-11-11

### Added

- Install story: `mcp_wrap` uses a relative script directory (no hardcoded
  dev path); the driver finds an engine bundled next to itself in unpacked
  releases; README install and MCP client setup docs.

### Fixed

- Landing page `og:url` points at browserr.dev.

## [0.2.1] - 2026-11-11

### Added

- macOS port: CI probe leg, release matrix with macos-arm64, packaging
  label and platform copy.
- Landing page CNAME (browserr.dev) and monochrome chevron icon matching
  the site's ink/paper system.

### Fixed

- Rendering prepare step binds the surface FBO before painting on macOS
  (see 0.3.1 note for the related time-based retry fix).
- CI artifact path uses `runner.os` (the matrix OS string is lowercase).

## [0.2.0] - 2026-11-11

### Added

- Linux port: platform-aware SDK paths, cfg-gated Windows-only code, Linux
  CI, dist, and package scripts, xvfb-backed render suite, release matrix
  with linux-x64 asset.
- Frames announced as events with pixels fetched via screenshot on demand
  (continuous push wedged undrained sockets on Linux); resize uses the
  surface-bound path (EGL framebuffer-undefined panic).

### Fixed

- Engine exit skips atexit handlers (Servo teardown deadlock stalled exit
  on Linux); screenshots answer only the requester (main-socket wedge).
- Patient pong handshake: cold engine init exceeds 2 s on llvmpipe, and a
  short timeout killed the engine via main-client disconnect.

## [0.1.0] - 2026-11-10

### Added

- First release: Servo-based engine in one Rust binary speaking a JSON
  lines protocol over TCP; Rune chrome child process with resize wiring;
  screenshot and live-frame streaming; driver SDK and MCP server;
  fail-closed defaults; pump self-deadlock fix verified by navigation and
  frame streaming.
