# browserr-mcp

[MCP](https://modelcontextprotocol.io) server for
[Browserr](https://browserr.dev) — the Servo-based browser engine built
for AI agents. One command; the platform engine binary is downloaded and
sha256-verified on first run.

```json
{
  "mcpServers": {
    "browserr": {
      "command": "uvx",
      "args": ["browserr-mcp"]
    }
  }
}
```

or, installed directly:

```json
{
  "mcpServers": {
    "browserr": { "command": "browserr-mcp" }
  }
}
```

## What you get

42 tools covering navigation, token-cheap page reads, structured page
inventories with stable element refs (`e7`), type-aware form filling,
annotated screenshots, fail-closed request interception, HTTP-auth and
file-picker events, evidence capture, and human handoff (pause the
page, let a person handle the CAPTCHA, resume).

The engine is a single Rust binary (Servo 0.8): CPU rendering by
default, EasyList adblock built in, no Chromium, no CDP, no driver
downloads. It runs on Windows, Linux (x64/ARM64) and macOS.

Browserr fails closed by design: uncertain requests are blocked, not
guessed; credentials are never logged; every run leaves timestamped
evidence.

## Environment variables

| Variable | Default | Purpose |
| --- | --- | --- |
| `BROWSERR_VERSION` | latest release | pin a release tag, e.g. `v0.8.1`; if that version is already installed, starts fully offline |
| `BROWSERR_HOME` | `~/.browserr/versions/<version>` | install dir; an existing unpack with the engine binary skips the download |
| `BROWSERR_UPDATE` | check daily | `never` disables the release check entirely; the newest installed engine starts |

## First run

The bootstrap queries the official GitHub release API, downloads the
platform asset, verifies it against the sha256 digest GitHub reports
for that asset, and unpacks it under `~/.browserr/versions/<version>`.
A missing digest is a hard stop — the digest is the corruption guard;
TLS only covers the transport.

Prefer to install ahead of time?

```sh
uvx --from browserr-mcp browserr-install
```

(`uvx` runs entry points from the package; the bare `--from` form is
needed because `browserr-install` is an entry point of `browserr-mcp`,
not a package name.)

## Links

- Browserr: https://browserr.dev/
- Source: https://github.com/Omodaka9375/browserr
- Changelog: https://github.com/Omodaka9375/browserr/blob/main/CHANGELOG.md

Apache-2.0. Engine and Python tooling are stdlib-only; this bootstrap
adds no third-party Python dependencies.

<!-- mcp-name: io.github.Omodaka9375/browserr -->
