//! Driver-side control plane: the attached-driver hub (computer-use
//! mode), the fast paths that must answer while the servo main loop is
//! wedged (ping/stop), command validation, and engine exit/cleanup
//! helpers. Split out of main.rs: this is the auditable control-plane
//! half; the main loop, which owns servo, stays in main.rs.

use std::io::{BufRead, BufReader, Write};
use std::sync::atomic::{AtomicU64, Ordering};

use crate::EV_MAGIC;

/// Terminate the process immediately, skipping libc atexit handlers and
/// static destructors. std::process::exit runs them; on Linux servo/mozglue
/// teardown deadlocks there (pthread_mutex_destroy on mutexes owned by
/// still-running threads), which stalled engine exit for the full client
/// timeout. Destructors must not run for live tabs anyway (surfman surface
/// assertion noise).
pub(crate) fn hard_exit() -> ! {
    unsafe extern "C" {
        fn _exit(status: i32) -> !;
    }
    unsafe { _exit(0) }
}

/// EV event frame with an explicit event id (driver-thread fast path).
fn driver_event(id: u64, json: &str) -> Vec<u8> {
    let bytes = json.as_bytes();
    let mut frame = Vec::with_capacity(20 + bytes.len());
    frame.extend_from_slice(&EV_MAGIC.to_le_bytes());
    frame.extend_from_slice(&id.to_le_bytes());
    frame.extend_from_slice(&(bytes.len() as u32).to_le_bytes());
    frame.extend_from_slice(&0u32.to_le_bytes());
    frame.extend_from_slice(bytes);
    frame
}

/// Commands an attached driver may send; validated before queueing.
const DRIVER_CMDS: [&str; 25] = [
    "navigate",
    "back",
    "forward",
    "new_tab",
    "activate",
    "close_tab",
    "resize",
    "input",
    "wheel",
    "key",
    "eval",
    "screenshot",
    "frame",
    "zoom",
    "ping",
    "stop",
    "download",
    "intercept",
    "request",
    "auth",
    "files",
    "frames",
    "pause",
    "resume",
    "handoff",
];

/// Attached-driver hub (computer-use mode: `--driver-port=N`). Driver
/// command lines are validated, queued, and drained by the main loop
/// (which owns servo); engine events and SHT1 screenshot answers are
/// mirrored to every attached driver. Optional `--driver-token=T`:
/// drivers must send {"cmd":"auth","token":T} as their first line or
/// the socket is dropped without receiving anything.
pub(crate) struct DriverHub {
    pub(crate) clients: std::sync::Mutex<Vec<std::net::TcpStream>>,
    pub(crate) queue: std::sync::Mutex<std::collections::VecDeque<String>>,
}

/// Prefix marking a queued command as coming from an attached driver
/// (the main loop strips it and answers via the driver broadcast only).
pub(crate) const DRIVER_PREFIX: &str = "\u{1}drv:";

/// Approximate open-tab count and active tab, mirrored by the main loop on
/// every processed command. Read by the driver-thread ping fast path: when
/// the servo main loop wedges (a page stuck in a long JS task), queued
/// commands never drain, so the health check must not depend on it.
/// Values may lag while wedged - acceptable for a liveness probe.
pub(crate) static TAB_COUNT: AtomicU64 = AtomicU64::new(0);
pub(crate) static ACTIVE_TAB: AtomicU64 = AtomicU64::new(0);
/// Event ids for fast-path pongs (framing only; drivers match on the JSON id).
static DRIVER_EVENT_SEQ: AtomicU64 = AtomicU64::new(1);
/// Ephemeral profile path, captured at startup so the driver-thread stop
/// fast path can clean it up while the main loop is wedged.
pub(crate) static EPHEMERAL_DIR: std::sync::OnceLock<Option<std::path::PathBuf>> =
    std::sync::OnceLock::new();

static HUB: std::sync::OnceLock<DriverHub> = std::sync::OnceLock::new();

/// Monotonic id for held interception requests.
pub(crate) static INTERCEPT_ID: AtomicU64 = AtomicU64::new(1);

impl DriverHub {
    pub(crate) fn global() -> &'static DriverHub {
        HUB.get_or_init(|| DriverHub {
            clients: std::sync::Mutex::new(Vec::new()),
            queue: std::sync::Mutex::new(std::collections::VecDeque::new()),
        })
    }

    /// Queue a validated driver command line for the main loop to drain.
    fn push(&self, line: &str) {
        self.queue
            .lock()
            .unwrap()
            .push_back(format!("{DRIVER_PREFIX}{line}"));
    }

    fn add_client(&self, stream: &std::net::TcpStream) {
        if let Ok(clone) = stream.try_clone() {
            let mut clients = self.clients.lock().unwrap();
            clients.push(clone);
            eprintln!("engine: driver attached ({} open)", clients.len());
        }
    }

    /// Mirror framed bytes (EVT1/SHT1) to every attached driver; sockets
    /// that went away are pruned. A 2s write timeout (set on accept)
    /// bounds a wedged driver.
    pub(crate) fn broadcast(&self, bytes: &[u8]) {
        let mut clients = self.clients.lock().unwrap();
        clients.retain_mut(|c| c.write_all(bytes).is_ok() && c.flush().is_ok());
    }

    pub(crate) fn has_clients(&self) -> bool {
        !self.clients.lock().unwrap().is_empty()
    }
}

/// Accept driver connections on 127.0.0.1:port; validate + queue their
/// commands; drop unauthenticated sockets when a token is required.
pub(crate) fn start_driver_listener(port: u16, token: Option<String>) {
    std::thread::spawn(move || {
        let listener = match std::net::TcpListener::bind(("127.0.0.1", port)) {
            Ok(l) => l,
            Err(e) => {
                eprintln!("engine: driver port bind failed: {e}");
                return;
            }
        };
        eprintln!("engine: driver port listening on 127.0.0.1:{port}");
        for client in listener.incoming() {
            let Ok(stream) = client else { return };
            let _ = stream.set_write_timeout(Some(std::time::Duration::from_secs(2)));
            let hub = DriverHub::global();
            let token = token.clone();
            std::thread::spawn(move || driver_client_loop(stream, hub, token));
        }
    });
}

/// Read JSON command lines from one driver, validate, and queue the
/// allowed ones for the main loop. First line must authenticate when a
/// token is set. Authenticated sockets are registered for broadcasts.
fn driver_client_loop(stream: std::net::TcpStream, hub: &DriverHub, token: Option<String>) {
    let mut writer = match stream.try_clone() {
        Ok(w) => w,
        Err(_) => return,
    };
    let mut reader = BufReader::new(stream);
    let mut line = String::new();
    let mut authed = token.is_none();
    let mut registered = false;
    loop {
        line.clear();
        match reader.read_line(&mut line) {
            Ok(0) | Err(_) => break,
            Ok(_) => {}
        }
        let trimmed = line.trim();
        if trimmed.is_empty() {
            continue;
        }
        if trimmed.len() > 8 << 20 {
            eprintln!(
                "engine: driver sent oversized line ({} bytes), dropping",
                trimmed.len()
            );
            break;
        }
        let Ok(v) = serde_json::from_str::<serde_json::Value>(trimmed) else {
            continue;
        };
        let Some(cmd) = v["cmd"].as_str() else {
            continue;
        };
        if !authed {
            let ok = cmd == "auth"
                && v["token"]
                    .as_str()
                    .map(|t| t == token.as_deref().unwrap_or(""))
                    .unwrap_or(false);
            if ok {
                authed = true;
                registered = true;
                eprintln!("engine: driver authenticated");
                // Register for broadcasts only now: an unauthed socket gets
                // no engine events or screenshot answers.
                hub.add_client(reader.get_ref());
                continue;
            }
            eprintln!("engine: driver failed auth, dropping connection");
            break;
        }
        if !registered {
            // No token required: register on the first command.
            registered = true;
            hub.add_client(reader.get_ref());
        }
        // Ping and stop are answered HERE, not through the queue: they are
        // the recovery path when the servo main loop is wedged by a page
        // (long JS task) and would otherwise never drain queued commands.
        // pong reports the mirrored tab counters (may lag while wedged -
        // this is a liveness probe, not a state query); stop force-exits
        // the process (the ephemeral profile still gets cleaned up).
        if cmd == "ping" {
            let req = v["id"].as_u64().unwrap_or(0);
            let seq = DRIVER_EVENT_SEQ.fetch_add(1, Ordering::SeqCst);
            let json = format!(
                "{{\"ev\":\"pong\",\"tab\":0,\"id\":{req},\"tabs\":{},\"active\":{}}}",
                TAB_COUNT.load(Ordering::SeqCst),
                ACTIVE_TAB.load(Ordering::SeqCst),
            );
            let _ = writer.write_all(&driver_event(seq, &json));
            let _ = writer.flush();
            continue;
        }
        if cmd == "stop" {
            eprintln!("engine: driver stop; hard exit");
            if let Some(dir) = EPHEMERAL_DIR.get().and_then(|d| d.as_ref()) {
                cleanup_ephemeral(&Some(dir.clone()));
            }
            hard_exit();
        }
        if !DRIVER_CMDS.contains(&cmd) {
            eprintln!("engine: driver command rejected: {cmd}");
            continue;
        }
        hub.push(trimmed);
    }
    eprintln!("engine: driver detached");
}

/// Remove the ephemeral profile dir (best effort) before an engine exit.
/// Servo's background threads still hold SQLite handles (clientstorage) while
/// the process lives, so the in-process delete can race and fail. Belt and
/// braces: try now, and leave a one-shot scheduled task that waits for our
/// pid to exit and deletes afterwards.
pub(crate) fn cleanup_ephemeral(dir: &Option<std::path::PathBuf>) {
    if let Some(d) = dir {
        let _ = std::fs::remove_dir_all(d);
        // Windows only: servo's background threads still hold SQLite handles
        // (clientstorage) while the process lives, so the in-process delete can
        // silently fail. A detached helper waits for THIS pid to die, then
        // deletes. On Unix the plain delete above succeeds (open handles do
        // not block unlink).
        #[cfg(windows)]
        {
            let pid = std::process::id();
            let script = format!(
                "$p = Get-Process -Id {pid} -ErrorAction SilentlyContinue; \
                 if ($p) {{ Wait-Process -Id {pid} -ErrorAction SilentlyContinue }}; \
                 Remove-Item -LiteralPath '{}' -Recurse -Force -ErrorAction SilentlyContinue",
                d.display()
            );
            let _ = std::process::Command::new("powershell")
                .args(["-NoProfile", "-WindowStyle", "Hidden", "-Command", &script])
                .spawn();
        }
        eprintln!("engine: ephemeral profile removed ({})", d.display());
    }
}
