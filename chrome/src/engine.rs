//! Engine client for the chrome: spawns the engine process, sends commands,
//! receives frames (pushed into a Rune NativeSurface) and events (url/title/
//! history, stored in a shared Ui map the view reads).
use std::collections::HashMap;
use std::io::{BufReader, Read, Write};
use std::net::TcpStream;
use std::process::{Child, Command, Stdio};
use std::sync::atomic::{AtomicU64, Ordering};
use std::sync::{Arc, Mutex};

use rune::{NativeSurface, Waker};

const PORT: u16 = 7411;
const MAGIC: u32 = 0x46_52_4D_31; // FRM1
const EV_MAGIC: u32 = 0x45_56_54_31; // EVT1

/// Engine-reported UI state, shared between the reader thread and the view.
pub struct Ui {
    /// Which tab the chrome renders (the engine paints this one).
    pub active: AtomicU64,
    /// Last reported URL per tab id.
    pub urls: Mutex<HashMap<u64, String>>,
    /// Last reported title per tab id.
    pub titles: Mutex<HashMap<u64, String>>,
    /// (can_go_back, can_go_forward) per tab id.
    pub history: Mutex<HashMap<u64, (bool, bool)>>,
}

impl Ui {
    pub fn new(active: u64) -> Arc<Ui> {
        Arc::new(Ui {
            active: AtomicU64::new(active),
            urls: Mutex::new(HashMap::new()),
            titles: Mutex::new(HashMap::new()),
            history: Mutex::new(HashMap::new()),
        })
    }

    pub fn url(&self, tab: u64) -> Option<String> {
        self.urls.lock().unwrap().get(&tab).cloned()
    }

    pub fn title(&self, tab: u64) -> Option<String> {
        self.titles.lock().unwrap().get(&tab).cloned()
    }

    pub fn history(&self, tab: u64) -> Option<(bool, bool)> {
        self.history.lock().unwrap().get(&tab).copied()
    }
}

pub struct Engine {
    stream: TcpStream,
    _child: Child,
    _reader: Option<std::thread::JoinHandle<()>>,
}

impl Engine {
    /// Spawn the engine sibling process and start the frame reader.
    pub fn start(surface: NativeSurface, waker: Waker, ui: Arc<Ui>) -> std::io::Result<Engine> {
        let exe = std::env::var("BROWSERR_ENGINE")
            .map(std::path::PathBuf::from)
            .unwrap_or_else(|_| {
                let here = std::env::current_exe().ok()
                    .and_then(|p| p.parent().map(|p| p.to_path_buf()))
                    .unwrap_or_default();
                here.join("engine.exe")
            });
        // Keep the engine's stderr in a log file: it is the only window into
        // the child process (servo logs, panics, our eprintln trace).
        let engine_log = std::env::temp_dir().join("browserr-engine.log");
        let stderr = std::fs::File::create(&engine_log).ok();
        eprintln!("chrome: engine stderr -> {}", engine_log.display());
        let child = Command::new(exe)
            .stdout(Stdio::null())
            .stderr(stderr.map(Stdio::from).unwrap_or(Stdio::null()))
            .spawn()?;

        // Connect with retries while the engine boots.
        let mut stream = None;
        for _ in 0..100 {
            if let Ok(s) = TcpStream::connect(("127.0.0.1", PORT)) {
                stream = Some(s);
                break;
            }
            std::thread::sleep(std::time::Duration::from_millis(100));
        }
        let stream = stream.expect("engine: could not connect");

        let reader_stream = stream.try_clone()?;
        let reader = std::thread::spawn(move || read_frames(reader_stream, surface, waker, ui));

        Ok(Engine { stream, _child: child, _reader: Some(reader) })
    }

    fn send(&mut self, json: &str) {
        eprintln!("chrome: -> engine {json}");
        let _ = writeln!(&mut self.stream, "{json}");
        let _ = self.stream.flush();
    }

    pub fn navigate(&mut self, url: &str, tab: u64) {
        let safe = url.replace('\\', "\\\\").replace('"', "\\\"");
        self.send(&format!("{{\"cmd\":\"navigate\",\"url\":\"{safe}\",\"tab\":{tab}}}"));
    }

    pub fn resize(&mut self, w: u32, h: u32) {
        self.send(&format!("{{\"cmd\":\"resize\",\"w\":{w},\"h\":{h}}}"));
    }

    pub fn back(&mut self, tab: u64) {
        self.send(&format!("{{\"cmd\":\"back\",\"tab\":{tab}}}"));
    }

    pub fn forward(&mut self, tab: u64) {
        self.send(&format!("{{\"cmd\":\"forward\",\"tab\":{tab}}}"));
    }

    pub fn new_tab(&mut self, tab: u64) {
        self.send(&format!("{{\"cmd\":\"new_tab\",\"tab\":{tab}}}"));
    }

    pub fn activate(&mut self, tab: u64) {
        self.send(&format!("{{\"cmd\":\"activate\",\"tab\":{tab}}}"));
    }

    pub fn close_tab(&mut self, tab: u64) {
        self.send(&format!("{{\"cmd\":\"close_tab\",\"tab\":{tab}}}"));
    }
}

fn read_frames(stream: TcpStream, surface: NativeSurface, waker: Waker, ui: Arc<Ui>) {
    let mut reader = BufReader::new(stream);
    let counter = AtomicU64::new(1);
    let mut header = [0u8; 20];

    loop {
        if reader.read_exact(&mut header).is_err() {
            eprintln!("chrome: frame reader: header read failed (engine gone?)");
            return;
        }
        let magic = u32::from_le_bytes(header[0..4].try_into().unwrap());
        let _id = u64::from_le_bytes(header[4..12].try_into().unwrap());
        let a = u32::from_le_bytes(header[12..16].try_into().unwrap());
        let b = u32::from_le_bytes(header[16..20].try_into().unwrap());

        match magic {
            MAGIC => {
                let (w, h) = (a, b);
                let len = w as usize * h as usize * 4;
                let mut pixels = vec![0u8; len];
                if reader.read_exact(&mut pixels).is_err() {
                    eprintln!("chrome: frame reader: pixel read failed");
                    return;
                }
                let id = counter.fetch_add(1, Ordering::SeqCst);
                if let Err(e) = surface.push_bytes(
                    rune::native_surface::ByteFrame {
                        width: w,
                        height: h,
                        format: rune::native_surface::Format::Bgra,
                        color: Default::default(),
                        planes: [pixels, Vec::new()],
                    },
                    id,
                ) {
                    eprintln!("chrome: push_bytes failed: {e:?}");
                    return;
                }
                if id % 60 == 1 {
                    eprintln!("chrome: frame {id} {w}x{h} pushed");
                }
            }
            EV_MAGIC => {
                let len = a as usize;
                let mut json = vec![0u8; len];
                if reader.read_exact(&mut json).is_err() {
                    eprintln!("chrome: frame reader: event read failed");
                    return;
                }
                let Ok(v) = serde_json::from_slice::<serde_json::Value>(&json) else {
                    waker();
                    continue;
                };
                let tab = v["tab"].as_u64().unwrap_or(0);
                match v["ev"].as_str() {
                    Some("url") => {
                        if let Some(u) = v["url"].as_str() {
                            ui.urls.lock().unwrap().insert(tab, u.to_string());
                        }
                    }
                    Some("title") => {
                        if let Some(t) = v["title"].as_str() {
                            ui.titles.lock().unwrap().insert(tab, t.to_string());
                        }
                    }
                    Some("history") => {
                        let back = v["back"].as_bool().unwrap_or(false);
                        let forward = v["forward"].as_bool().unwrap_or(false);
                        ui.history.lock().unwrap().insert(tab, (back, forward));
                    }
                    _ => {}
                }
            }
            _ => {
                eprintln!("chrome: frame reader: bad magic {magic:#x}");
                return;
            }
        }
        waker();
    }
}
