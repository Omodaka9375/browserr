//! Engine client for the chrome: spawns the engine process, sends commands,
//! receives frames (pushed into a Rune NativeSurface) and events (url/title/
//! history, stored in a shared Ui map the view reads).
use std::collections::HashMap;
use std::io::{BufReader, Read, Write};
use std::net::TcpStream;
use std::process::{Child, Command, Stdio};
use std::sync::atomic::{AtomicU64, Ordering};
use std::sync::{Arc, Mutex};

use rune::{NativeSurface, Pixels, Waker};

#[cfg(windows)]
use rune::native_surface::windows::SharedTexture;

use crate::store;

/// Put the engine in a job object that is killed when the chrome dies. The
/// engine normally exits when the socket closes, but a hung engine (paint/GL
/// deadlock) would linger forever; this guarantees cleanup.
#[cfg(windows)]
fn bind_job(child: &Child) -> Option<windows::Win32::Foundation::HANDLE> {
    use std::os::windows::io::AsRawHandle;
    use windows::Win32::Foundation::HANDLE;
    use windows::Win32::System::JobObjects::{
        AssignProcessToJobObject, CreateJobObjectW, JobObjectExtendedLimitInformation,
        SetInformationJobObject, JOBOBJECT_EXTENDED_LIMIT_INFORMATION,
        JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE,
    };
    unsafe {
        let job = CreateJobObjectW(None, windows::core::PCWSTR::null()).ok()?;
        let mut info = JOBOBJECT_EXTENDED_LIMIT_INFORMATION::default();
        info.BasicLimitInformation.LimitFlags = JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE;
        SetInformationJobObject(
            job,
            JobObjectExtendedLimitInformation,
            &info as *const _ as *const core::ffi::c_void,
            std::mem::size_of::<JOBOBJECT_EXTENDED_LIMIT_INFORMATION>() as u32,
        )
        .ok()?;
        AssignProcessToJobObject(job, HANDLE(child.as_raw_handle() as _)).ok()?;
        Some(job)
    }
}

const PORT: u16 = 7411;
const MAGIC: u32 = 0x46_52_4D_31; // FRM1
const EV_MAGIC: u32 = 0x45_56_54_31; // EVT1

/// A login captured from a submitted password form.
#[derive(Clone, Debug)]
pub struct PwCapture {
    pub tab: u64,
    pub user: String,
    pub pass: String,
    pub origin: String,
}

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
    /// Latest password-form capture (replaced by newer ones).
    pub pw: Mutex<Option<PwCapture>>,
    /// Decoded favicons keyed by page origin host.
    pub icons: Mutex<HashMap<String, Pixels>>,
    /// Page-requested cursor shape per tab id (servo Debug name, lowercase).
    pub cursors: Mutex<HashMap<u64, String>>,
}

impl Ui {
    pub fn new(active: u64) -> Arc<Ui> {
        Arc::new(Ui {
            active: AtomicU64::new(active),
            urls: Mutex::new(HashMap::new()),
            titles: Mutex::new(HashMap::new()),
            history: Mutex::new(HashMap::new()),
            pw: Mutex::new(None),
            icons: Mutex::new(HashMap::new()),
            cursors: Mutex::new(HashMap::new()),
        })
    }

    pub fn peek_pw(&self) -> Option<PwCapture> {
        self.pw.lock().unwrap().clone()
    }

    pub fn icon(&self, host: &str) -> Option<Pixels> {
        self.icons.lock().unwrap().get(host).cloned()
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

    pub fn cursor(&self, tab: u64) -> Option<String> {
        self.cursors.lock().unwrap().get(&tab).cloned()
    }

    /// Drop all remembered cursor shapes (used when the engine restarts:
    /// fresh webviews start at the default cursor).
    pub fn clear_cursors(&self) {
        self.cursors.lock().unwrap().clear();
    }
}

/// Everything Engine::start needs, kept around so the engine can be
/// restarted in place when settings that require a respawn change.
struct StartArgs {
    surface: NativeSurface,
    waker: Waker,
    ui: Arc<Ui>,
    user_agent: String,
    profile_dir: String,
    adblock_list: String,
}

pub struct Engine {
    stream: TcpStream,
    child: Child,
    /// Job object handle; the engine is killed when this is dropped (chrome exit).
    #[cfg(windows)]
    job: Option<windows::Win32::Foundation::HANDLE>,
    reader: Option<std::thread::JoinHandle<()>>,
    args: StartArgs,
}

impl Engine {
    /// Spawn the engine sibling process and start the frame reader.
    /// `user_agent` (may be empty) is forwarded as an option; `profile_dir`
    /// gives the engine its own per-profile storage dir (may be empty);
    /// `adblock_list` (may be empty) enables request blocking.
    pub fn start(
        surface: NativeSurface,
        waker: Waker,
        ui: Arc<Ui>,
        user_agent: &str,
        profile_dir: &str,
        adblock_list: &str,
    ) -> std::io::Result<Engine> {
        let args = StartArgs {
            surface,
            waker,
            ui,
            user_agent: user_agent.to_string(),
            profile_dir: profile_dir.to_string(),
            adblock_list: adblock_list.to_string(),
        };
        Engine::spawn(args)
    }

    fn spawn(args: StartArgs) -> std::io::Result<Engine> {
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
        let mut cmd = Command::new(exe);
        if !args.user_agent.is_empty() {
            cmd.arg(format!("--user-agent={}", args.user_agent));
        }
        if !args.profile_dir.is_empty() {
            cmd.arg(format!("--profile-dir={}", args.profile_dir));
        }
        if !args.adblock_list.is_empty() {
            cmd.arg(format!("--adblock-list={}", args.adblock_list));
        }
        let child = cmd
            .stdout(Stdio::null())
            .stderr(stderr.map(Stdio::from).unwrap_or(Stdio::null()))
            .spawn()?;
        #[cfg(windows)]
        let job = bind_job(&child);

        // Connect with retries while the engine boots.
        let mut stream = None;
        for _ in 0..100 {
            if let Ok(s) = TcpStream::connect(("127.0.0.1", PORT)) {
                stream = Some(s);
                break;
            }
            std::thread::sleep(std::time::Duration::from_millis(100));
        }
        let stream = stream.ok_or_else(|| {
            std::io::Error::new(std::io::ErrorKind::NotFound, "engine: could not connect")
        })?;

        let reader_stream = stream.try_clone()?;
        let surface = args.surface.clone();
        let waker = args.waker.clone();
        let ui = args.ui.clone();
        let reader = std::thread::spawn(move || read_frames(reader_stream, surface, waker, ui));

        Ok(Engine {
            stream,
            child,
            #[cfg(windows)]
            job,
            reader: Some(reader),
            args,
        })
    }

    /// Kill the engine process and respawn it with the current start args
    /// (used when user agent or adblock settings change). The caller must
    /// re-navigate the open tabs afterwards.
    pub fn restart(&mut self) -> std::io::Result<()> {
        eprintln!("chrome: restarting engine");
        let _ = self.child.kill();
        let _ = self.child.wait();
        #[cfg(windows)]
        if let Some(job) = self.job.take() {
            unsafe {
                let _ = windows::Win32::Foundation::CloseHandle(job);
            }
        }
        // The old reader thread exits on its own when the socket closes.
        self.reader = None;
        let fresh = Engine::spawn(StartArgs {
            surface: self.args.surface.clone(),
            waker: self.args.waker.clone(),
            ui: self.args.ui.clone(),
            user_agent: self.args.user_agent.clone(),
            profile_dir: self.args.profile_dir.clone(),
            adblock_list: self.args.adblock_list.clone(),
        })?;
        self.stream = fresh.stream;
        self.child = fresh.child;
        #[cfg(windows)]
        {
            self.job = fresh.job;
        }
        self.reader = fresh.reader;
        self.args = fresh.args;
        Ok(())
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

    /// Run JS in a tab's main frame (password autofill; json-escaped here).
    pub fn eval(&mut self, tab: u64, js: &str) {
        let json = serde_json::json!({ "cmd": "eval", "tab": tab, "js": js }).to_string();
        self.send(&json);
    }

    /// Forward pointer input on the page area: kind is "down", "up" or "move",
    /// button "left", "right" or "none". Coordinates are page-local points.
    pub fn pointer(&mut self, tab: u64, kind: &str, x: f32, y: f32, button: &str) {
        self.send(&format!(
            "{{\"cmd\":\"input\",\"tab\":{tab},\"kind\":\"{kind}\",\"x\":{x},\"y\":{y},\"button\":\"{button}\"}}"
        ));
    }

    /// Forward a wheel event (rune's convention: positive dy moves content down).
    pub fn wheel(&mut self, tab: u64, dx: f32, dy: f32, x: f32, y: f32) {
        self.send(&format!(
            "{{\"cmd\":\"wheel\",\"tab\":{tab},\"dx\":{dx},\"dy\":{dy},\"x\":{x},\"y\":{y}}}"
        ));
    }

    /// Set a tab's page zoom (1.0 = 100%; the engine clamps).
    pub fn zoom(&mut self, tab: u64, zoom: f32) {
        self.send(&format!("{{\"cmd\":\"zoom\",\"tab\":{tab},\"zoom\":{zoom}}}"));
    }

    /// Forward a keyboard event: name from key_name(), text for text keys,
    /// mods bitmask (1 shift, 2 ctrl, 4 alt, 8 cmd).
    pub fn key(&mut self, tab: u64, name: &str, text: &str, mods: u8, down: bool) {
        let json = serde_json::json!({
            "cmd": "key", "tab": tab, "name": name, "text": text,
            "mods": mods, "down": down,
        });
        self.send(&json.to_string());
    }
}

fn read_frames(stream: TcpStream, surface: NativeSurface, waker: Waker, ui: Arc<Ui>) {
    let mut reader = BufReader::new(stream);
    let counter = AtomicU64::new(1);
    let mut header = [0u8; 20];

    // The engine falls back to socket bytes if we cannot import its textures
    // (adapter mismatch); ask it to when a push fails.
    let request_bytes = move |reader: &BufReader<TcpStream>| {
        if let Ok(mut s) = reader.get_ref().try_clone() {
            let _ = writeln!(s, "{{\"cmd\":\"frames\",\"mode\":\"bytes\"}}");
        }
    };

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
                            let mut urls = ui.urls.lock().unwrap();
                            let changed = urls.get(&tab) != Some(&u.to_string());
                            urls.insert(tab, u.to_string());
                            drop(urls);
                            // Dedupe: the engine re-sends the same url on activate.
                            if changed {
                                store::append_history(u);
                            }
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
                    Some("favicon") => {
                        let origin = v["origin"].as_str().unwrap_or("");
                        let w = v["w"].as_u64().unwrap_or(0) as u32;
                        let h = v["h"].as_u64().unwrap_or(0) as u32;
                        let host = store::host(origin);
                        if !host.is_empty() && w > 0 && h > 0 {
                            let rgba: Vec<u8> = v["rgba"]
                                .as_array()
                                .map(|a| {
                                    a.iter()
                                        .filter_map(|x| x.as_u64().map(|n| n as u8))
                                        .collect()
                                })
                                .unwrap_or_default();
                            if rgba.len() == (w * h * 4) as usize {
                                let px = Pixels::new();
                                px.set(w, h, rgba);
                                ui.icons.lock().unwrap().insert(host, px);
                            }
                        }
                    }
                    Some("cursor") => {
                        if let Some(c) = v["cursor"].as_str() {
                            ui.cursors.lock().unwrap().insert(tab, c.to_string());
                        }
                    }
                    Some("pwcapture") => {
                        let d = &v["data"];
                        let user = d["u"].as_str().unwrap_or("").to_string();
                        let pass = d["p"].as_str().unwrap_or("").to_string();
                        let origin = d["o"].as_str().unwrap_or("").to_string();
                        if !pass.is_empty() {
                            *ui.pw.lock().unwrap() = Some(PwCapture { tab, user, pass, origin });
                        }
                    }
                    #[cfg(windows)]
                    Some("frame") => {
                        let name = v["name"].as_str().unwrap_or("");
                        let w = v["w"].as_u64().unwrap_or(0) as u32;
                        let h = v["h"].as_u64().unwrap_or(0) as u32;
                        let fence = v["fence"].as_str().unwrap_or("");
                        let value = v["value"].as_u64().unwrap_or(0);
                        if !name.is_empty() && w > 0 && h > 0 {
                            let frame = SharedTexture::named(name, w, h).release_fence(fence, value);
                            let id = counter.fetch_add(1, Ordering::SeqCst);
                            if surface.push_shared(frame, id).is_err() {
                                request_bytes(&reader);
                            }
                        }
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
