//! Browserr page engine: a Servo child process.
//! Listens on 127.0.0.1:7411. Protocol v2:
//!   in:  newline JSON:
//!        {"cmd":"navigate","url":"...","tab":N}   load in that tab (created on demand)
//!        {"cmd":"back"|"forward","tab":N}         per-tab history
//!        {"cmd":"new_tab","tab":N}                create a tab session
//!        {"cmd":"activate","tab":N}               make that tab the rendered one
//!        {"cmd":"close_tab","tab":N}              drop that tab
//!        {"cmd":"resize","w":N,"h":N}             resize the active tab (others on activate)
//!   out: frames:  u32 magic 'FRM1', u64 id, u32 w, u32 h, w*h*4 BGRA bytes
//!        events:  u32 magic 'EVT1', u64 id, u32 len, u32 0, len bytes JSON
//!                 {"ev":"url","tab":N,"url":"..."} | {"ev":"title","tab":N,"title":"..."}
//! Only the active tab is painted; switching tabs re-sends that tab's url/title.
//! Options: --user-agent=STRING overrides the HTTP user agent (from settings.json).
//!           --profile-dir=PATH gives Servo a per-profile config/storage dir.
//!           --adblock-list=PATH enables request blocking against an EasyList-format file.
use std::collections::HashMap;
use std::io::{BufRead, BufReader, Write};
use servo::RenderingContext;

mod d3d;
mod hw;
use hw::HardwareRenderingContext;
use std::net::TcpListener;
use std::sync::atomic::{AtomicBool, AtomicU64, Ordering};
use std::sync::{Arc, Condvar, Mutex};

const PORT: u16 = 7411;
const MAGIC: u32 = 0x46_52_4D_31; // FRM1
const EV_MAGIC: u32 = 0x45_56_54_31; // EVT1

#[derive(Clone)]
struct Pump(Arc<(Mutex<bool>, Condvar)>);

impl Pump {
    fn kick(&self) {
        let (lock, cv) = &*self.0;
        *lock.lock().unwrap() = true;
        cv.notify_one();
    }
    fn wait(&self, timeout: std::time::Duration) {
        let (lock, cv) = &*self.0;
        let mut guard = lock.lock().unwrap();
        if !*guard {
            // No pending kick: park until kicked or timeout. wait_timeout hands back
            // the guard in its result tuple, so reuse it; re-locking here would
            // self-deadlock while the first guard is still held.
            let (g, _timed_out) = cv.wait_timeout(guard, timeout).unwrap();
            guard = g;
        }
        *guard = false;
    }
}

struct ServoWaker(Pump);
impl servo::EventLoopWaker for ServoWaker {
    fn clone_box(&self) -> Box<dyn servo::EventLoopWaker> {
        Box::new(ServoWaker(self.0.clone()))
    }
    fn wake(&self) {
        self.0.kick();
    }
}

/// Updates reported by delegates, drained and forwarded to the chrome each tick.
struct Update {
    tab: u64,
    url: Option<String>,
    title: Option<String>,
    history: Option<(bool, bool)>,
    /// Captured login data (JSON {u,p,o}) from a submitted password form.
    pw: Option<String>,
    /// Decoded favicon: (page origin, width, height, RGBA bytes).
    icon: Option<(String, u32, u32, Vec<u8>)>,
}

/// Runs on load complete: if the page has a password field, capture its
/// (username, password) into sessionStorage when the form is submitted.
/// sessionStorage is origin-scoped, so the data never leaks to other sites.
const PW_INSTALL_JS: &str = r#"(function(){try{
if(!document.querySelector('input[type=password]')) return 0;
if(window.__browserr_pw) return 1;
window.__browserr_pw=1;
addEventListener('submit',function(){
  try{
    var pw=document.querySelector('input[type=password]');
    var form=pw.closest('form')||document;
    var re=/(user|email|login|account)/i; var u='';
    for(const t of form.querySelectorAll('input')){
      if(t.type==='password')continue;
      var k=(t.name||'')+' '+(t.id||'')+' '+(t.type||'')+' '+(t.autocomplete||'');
      if((!t.type||t.type==='text'||t.type==='email')&&re.test(k)){u=t.value;break;}
    }
    sessionStorage.setItem('browserr',JSON.stringify({u:u,p:pw.value,o:location.origin}));
  }catch(e){}
},true);
}catch(e){}})()"#;

/// Runs after every navigation: read and clear a pending capture.
const PW_READ_JS: &str = r#"(function(){try{
var d=sessionStorage.getItem('browserr');
if(d){sessionStorage.removeItem('browserr');return d;}
}catch(e){} return '';})()"#;

/// Returns JSON {icon, origin}: the page's favicon url (or '') and its origin.
const FAVICON_JS: &str = r#"(function(){try{
var l=document.querySelector('link[rel~="icon"]')||document.querySelector('link[rel="shortcut icon"]');
return JSON.stringify({icon:l?l.href:'',origin:location.origin});
}catch(e){return '';}})()"#;

struct Shared {
    pump: Pump,
    /// A new frame is ready on the active tab.
    frame_ready: AtomicBool,
    /// Which tab the chrome is looking at.
    active: AtomicU64,
    /// Delegate-reported url/title changes, drained each tick.
    updates: Mutex<Vec<Update>>,
    /// Tabs whose current load has reported LoadStatus::Started.
    started: Mutex<std::collections::HashSet<u64>>,
    /// Decoded favicons keyed by icon URL (16x16 RGBA), filled by fetch threads.
    icons: Mutex<std::collections::HashMap<String, Vec<u8>>>,
}

struct ServoAdblockDelegate {
    adblock: Option<std::rc::Rc<adblock::engine::Engine>>,
}

impl servo::ServoDelegate for ServoAdblockDelegate {
    fn load_web_resource(&self, load: servo::WebResourceLoad) {
        // Requests with a target webview go to the WebViewDelegate; this
        // catches the rest (no target webview known at the net layer).
        if let Some(engine) = &self.adblock {
            block_if_matching(engine, load);
        }
    }
}

/// Block the request if the filter list matches; default is pass-through.
/// Main-frame documents are never blocked: a cancelled document response
/// wedges the pipeline for later navigations on that tab.
fn block_if_matching(
    engine: &adblock::engine::Engine,
    load: servo::WebResourceLoad,
) {
    if load.request().is_for_main_frame {
        return;
    }
    let url = load.request().url.to_string();
    let req_type = format!("{:?}", load.request().destination).to_lowercase();
    let method = load.request().method.as_str();
    let Ok(req) = adblock::request::Request::new(&url, "", &req_type, method) else {
        return;
    };
    if engine.check_network_request(&req).should_block() {
        eprintln!("engine: adblock blocked {url}");
        let response_url = load.request().url.clone().into_url();
        load.intercept(servo::WebResourceResponse::new(response_url)).cancel();
    }
}

struct FrameDelegate {
    tab: u64,
    shared: Arc<Shared>,
    adblock: Option<std::rc::Rc<adblock::engine::Engine>>,
}

impl FrameDelegate {
    /// Block the request if the filter list matches; default is pass-through.
    fn maybe_block(&self, load: servo::WebResourceLoad) {
        if let Some(engine) = &self.adblock {
            block_if_matching(engine, load);
        }
    }

    /// Eval a password-capture script; a string result is reported to the chrome.
    fn eval_pw(&self, webview: servo::WebView, script: &'static str) {
        let shared = self.shared.clone();
        let tab = self.tab;
        webview.evaluate_javascript(script, move |result| {
            if let Ok(servo::JSValue::String(data)) = result {
                if !data.is_empty() {
                    shared.updates.lock().unwrap().push(Update {
                        tab,
                        url: None,
                        title: None,
                        history: None,
                        pw: Some(data),
                        icon: None,
                    });
                    shared.pump.kick();
                }
            }
        });
    }

    /// Eval the favicon lookup; fetch + decode the icon off-thread if new.
    fn eval_favicon(&self, webview: servo::WebView) {
        let shared = self.shared.clone();
        let tab = self.tab;
        webview.evaluate_javascript(FAVICON_JS, move |result| {
            let Ok(servo::JSValue::String(data)) = result else { return };
            let Ok(v) = serde_json::from_str::<serde_json::Value>(&data) else { return };
            let icon = v["icon"].as_str().unwrap_or("").to_string();
            let origin = v["origin"].as_str().unwrap_or("").to_string();
            if icon.is_empty() || origin.is_empty() {
                return;
            }
            // Already decoded for this icon URL: report the cached copy.
            if let Some(rgba) = shared.icons.lock().unwrap().get(&icon) {
                let rgba = rgba.clone();
                shared.updates.lock().unwrap().push(Update {
                    tab,
                    url: None,
                    title: None,
                    history: None,
                    pw: None,
                    icon: Some((origin, 16, 16, rgba)),
                });
                shared.pump.kick();
                return;
            }
            // Fetch + decode on a worker thread; the result lands in updates.
            let shared = shared.clone();
            std::thread::spawn(move || {
                if let Some((rgba, w, h)) = fetch_favicon(&icon) {
                    shared.icons.lock().unwrap().insert(icon, rgba.clone());
                    shared.updates.lock().unwrap().push(Update {
                        tab,
                        url: None,
                        title: None,
                        history: None,
                        pw: None,
                        icon: Some((origin, w, h, rgba)),
                    });
                    shared.pump.kick();
                }
            });
        });
    }
}

/// Fetch and decode an icon into 16x16 RGBA. Any failure returns None.
fn fetch_favicon(url: &str) -> Option<(Vec<u8>, u32, u32)> {
    if !url.starts_with("http://") && !url.starts_with("https://") {
        return None;
    }
    let resp = ureq::get(url).call().ok()?;
    let mut bytes = Vec::new();
    use std::io::Read as _;
    std::io::Read::take(resp.into_reader(), 1 << 20)
        .read_to_end(&mut bytes)
        .ok()?;
    let img = image::load_from_memory(&bytes).ok()?;
    let rgba = img
        .resize_exact(16, 16, image::imageops::FilterType::Lanczos3)
        .into_rgba8()
        .into_raw();
    Some((rgba, 16, 16))
}

impl servo::WebViewDelegate for FrameDelegate {
    fn notify_new_frame_ready(&self, _webview: servo::WebView) {
        if self.shared.active.load(Ordering::SeqCst) == self.tab {
            self.shared.frame_ready.store(true, Ordering::SeqCst);
            self.shared.pump.kick();
        }
    }

    fn notify_url_changed(&self, webview: servo::WebView, url: url::Url) {
        eprintln!("engine: url changed (tab {}) {url}", self.tab);
        self.shared.updates.lock().unwrap().push(Update {
            tab: self.tab,
            url: Some(url.to_string()),
            title: None,
            history: None,
            pw: None,
            icon: None,
        });
        // Pick up a capture that a submitted form left behind.
        self.eval_pw(webview, PW_READ_JS);
        self.shared.pump.kick();
    }

    fn notify_load_status_changed(&self, webview: servo::WebView, status: servo::LoadStatus) {
        if matches!(status, servo::LoadStatus::Started) {
            self.shared.started.lock().unwrap().insert(self.tab);
            self.shared.pump.kick();
        } else if matches!(status, servo::LoadStatus::Complete) {
            // Page is ready: arm the submit capture if it has a password field,
            // and look up the favicon.
            self.eval_pw(webview.clone(), PW_INSTALL_JS);
            self.eval_favicon(webview);
        }
    }

    fn notify_favicon_changed(&self, webview: servo::WebView) {
        self.eval_favicon(webview);
    }

    fn notify_page_title_changed(&self, _webview: servo::WebView, title: Option<String>) {
        self.shared.updates.lock().unwrap().push(Update {
            tab: self.tab,
            url: None,
            title,
            history: None,
            pw: None,
            icon: None,
        });
        self.shared.pump.kick();
    }

    fn notify_history_changed(&self, _webview: servo::WebView, entries: Vec<url::Url>, current: usize) {
        let back = current > 0;
        let forward = current + 1 < entries.len();
        self.shared.updates.lock().unwrap().push(Update {
            tab: self.tab,
            url: None,
            title: None,
            history: Some((back, forward)),
            pw: None,
            icon: None,
        });
        self.shared.pump.kick();
    }

    fn load_web_resource(&self, _webview: servo::WebView, load: servo::WebResourceLoad) {
        self.maybe_block(load);
    }

    fn notify_input_event_handled(
        &self,
        _webview: servo::WebView,
        _event_id: servo::InputEventId,
        result: servo::InputEventResult,
    ) {
        eprintln!(
            "engine: input handled: default_prevented={} consumed={} dispatch_failed={}",
            result.contains(servo::InputEventResult::DefaultPrevented),
            result.contains(servo::InputEventResult::Consumed),
            result.contains(servo::InputEventResult::DispatchFailed),
        );
    }
}

struct Tab {
    webview: servo::WebView,
    context: std::rc::Rc<dyn RenderingContext>,
    pending: Option<url::Url>,
    pending_sent: bool,
    pending_wait: u32,
    pending_retries: u32,
    last_url: Option<String>,
    last_title: Option<String>,
    last_history: Option<(bool, bool)>,
}

const EASYLIST_URL: &str = "https://easylist.to/easylist/easylist.txt";
/// Re-download after a week.
const FILTER_LIST_MAX_AGE_SECS: u64 = 7 * 24 * 3600;

/// Download the EasyList-format filter file if missing or stale. Best effort:
/// a failed download keeps any existing file and just logs.
fn ensure_filter_list(path: &std::path::Path) {
    let stale = match std::fs::metadata(path) {
        Ok(m) => m
            .modified()
            .ok()
            .and_then(|t| t.elapsed().ok())
            .map(|e| e.as_secs() > FILTER_LIST_MAX_AGE_SECS)
            .unwrap_or(true),
        Err(_) => true,
    };
    if !stale {
        return;
    }
    eprintln!("engine: downloading adblock filter list -> {}", path.display());
    if let Ok(dir) = path.parent().ok_or(()) {
        let _ = std::fs::create_dir_all(dir);
    }
    match ureq::get(EASYLIST_URL).call() {
        Ok(resp) => match resp.into_string() {
            Ok(body) => {
                let _ = std::fs::write(path, body);
                eprintln!("engine: filter list saved");
            }
            Err(e) => eprintln!("engine: filter list body read failed: {e}"),
        },
        Err(e) => eprintln!("engine: filter list download failed: {e}"),
    }
}

fn load_adblock(path: &std::path::Path) -> Option<std::rc::Rc<adblock::engine::Engine>> {
    let text = std::fs::read_to_string(path).ok()?;
    eprintln!("engine: adblock enabled");
    Some(std::rc::Rc::new(adblock::engine::Engine::new_with_list_text(
        text,
    )))
}

fn make_context(size: dpi::PhysicalSize<u32>) -> Option<std::rc::Rc<dyn RenderingContext>> {
    match HardwareRenderingContext::new(size) {
        Ok(c) => {
            eprintln!("engine: hardware (GPU) rendering context");
            return Some(std::rc::Rc::new(c));
        }
        Err(e) => eprintln!("engine: hardware context failed ({e:?}), falling back to software"),
    }
    match servo::SoftwareRenderingContext::new(size) {
        Ok(c) => Some(std::rc::Rc::new(c)),
        Err(e) => {
            eprintln!("engine: no rendering context: {e:?}");
            None
        }
    }
}

/// Map a chrome key event (name, text, modifier bitmask: 1 shift, 2 ctrl,
/// 4 alt, 8 cmd/logo, down state) to a servo input event.
fn map_key(name: &str, text: &str, mods: u8, down: bool) -> Option<servo::InputEvent> {
    use keyboard_types::{Code, Key as K, KeyboardEvent, Location, Modifiers, NamedKey, KeyState};
    let mut modifiers = Modifiers::empty();
    if mods & 1 != 0 {
        modifiers.insert(Modifiers::SHIFT);
    }
    if mods & 2 != 0 {
        modifiers.insert(Modifiers::CONTROL);
    }
    if mods & 4 != 0 {
        modifiers.insert(Modifiers::ALT);
    }
    if mods & 8 != 0 {
        modifiers.insert(Modifiers::META);
    }
    let (key, code) = match name {
        "text" => (K::Character(text.to_string()), Code::Unidentified),
        "Enter" => (K::Named(NamedKey::Enter), Code::Enter),
        "Backspace" => (K::Named(NamedKey::Backspace), Code::Backspace),
        "Delete" => (K::Named(NamedKey::Delete), Code::Delete),
        "Left" => (K::Named(NamedKey::ArrowLeft), Code::ArrowLeft),
        "Right" => (K::Named(NamedKey::ArrowRight), Code::ArrowRight),
        "Up" => (K::Named(NamedKey::ArrowUp), Code::ArrowUp),
        "Down" => (K::Named(NamedKey::ArrowDown), Code::ArrowDown),
        "Home" => (K::Named(NamedKey::Home), Code::Home),
        "End" => (K::Named(NamedKey::End), Code::End),
        "PageUp" => (K::Named(NamedKey::PageUp), Code::PageUp),
        "PageDown" => (K::Named(NamedKey::PageDown), Code::PageDown),
        "Insert" => (K::Named(NamedKey::Insert), Code::Insert),
        "Escape" => (K::Named(NamedKey::Escape), Code::Escape),
        "Tab" => (K::Named(NamedKey::Tab), Code::Tab),
        other => {
            let Some(n) = other.strip_prefix('F').and_then(|n| n.parse::<u8>().ok()) else {
                return None;
            };
            let named = match n {
                1..=12 => match n {
                    1 => NamedKey::F1,
                    2 => NamedKey::F2,
                    3 => NamedKey::F3,
                    4 => NamedKey::F4,
                    5 => NamedKey::F5,
                    6 => NamedKey::F6,
                    7 => NamedKey::F7,
                    8 => NamedKey::F8,
                    9 => NamedKey::F9,
                    10 => NamedKey::F10,
                    11 => NamedKey::F11,
                    _ => NamedKey::F12,
                },
                _ => return None,
            };
            let code = match n {
                1 => Code::F1,
                2 => Code::F2,
                3 => Code::F3,
                4 => Code::F4,
                5 => Code::F5,
                6 => Code::F6,
                7 => Code::F7,
                8 => Code::F8,
                9 => Code::F9,
                10 => Code::F10,
                11 => Code::F11,
                _ => Code::F12,
            };
            (K::Named(named), code)
        }
    };
    let mut ev = if down {
        KeyboardEvent::key_down(key, code)
    } else {
        KeyboardEvent::key_up(key, code)
    };
    ev.modifiers = modifiers;
    Some(servo::InputEvent::Keyboard(servo::KeyboardEvent::new(ev)))
}

fn send_event(out: &mut impl Write, id: u64, json: &str) {
    let bytes = json.as_bytes();
    out.write_all(&EV_MAGIC.to_le_bytes()).ok();
    out.write_all(&id.to_le_bytes()).ok();
    out.write_all(&(bytes.len() as u32).to_le_bytes()).ok();
    out.write_all(&0u32.to_le_bytes()).ok();
    out.write_all(bytes).ok();
    let _ = out.flush();
}

fn main() {
    let _ = rustls::crypto::aws_lc_rs::default_provider().install_default();

    // The tab-drop panic (see close_tab) is expected and contained; keep the
    // log clean by not printing that one known message.
    let default_hook = std::panic::take_hook();
    std::panic::set_hook(Box::new(move |info| {
        if !info.to_string().contains("destroyed the surface") {
            default_hook(info);
        }
    }));

    let mut user_agent = None;
    let mut profile_dir = None;
    let mut adblock_list = None;
    for a in std::env::args().skip(1) {
        if let Some(v) = a.strip_prefix("--user-agent=") {
            user_agent = Some(v.to_string());
        } else if let Some(v) = a.strip_prefix("--profile-dir=") {
            profile_dir = Some(std::path::PathBuf::from(v));
        } else if let Some(v) = a.strip_prefix("--adblock-list=") {
            adblock_list = Some(std::path::PathBuf::from(v));
        }
    }

    // Startup probe: --probe-shared tests Pool::new before anything else.
    if std::env::args().any(|a| a == "--probe-shared") {
        match d3d::Pool::new(64, 64) {
            Ok(p) => eprintln!("engine: probe-shared OK luid={}", p.adapter_luid()),
            Err(e) => eprintln!("engine: probe-shared FAILED: {e}"),
        }
    }

    // Retry the bind: on an in-place engine restart the previous instance may
    // still be releasing port 7411 for a moment.
    let listener = {
        let mut bound = None;
        for _ in 0..50 {
            match TcpListener::bind(("127.0.0.1", PORT)) {
                Ok(l) => {
                    bound = Some(l);
                    break;
                }
                Err(_) => std::thread::sleep(std::time::Duration::from_millis(100)),
            }
        }
        bound.expect("engine: bind failed")
    };
    eprintln!("engine: listening on {PORT}");
    let (stream, _peer) = listener.accept().expect("engine: accept failed");
    eprintln!("engine: chrome connected");
    let out_stream = stream.try_clone().expect("engine: clone stream");
    let mut reader = BufReader::new(stream);

    let pump = Pump(Arc::new((Mutex::new(false), Condvar::new())));
    let shared = Arc::new(Shared {
        pump: pump.clone(),
        frame_ready: AtomicBool::new(false),
        active: AtomicU64::new(0),
        updates: Mutex::new(Vec::new()),
        started: Mutex::new(std::collections::HashSet::new()),
        icons: Mutex::new(std::collections::HashMap::new()),
    });

    let mut opts = servo::Opts::default();
    if let Some(dir) = profile_dir {
        eprintln!("engine: profile dir {}", dir.display());
        opts.config_dir = Some(dir);
    }
    let mut prefs = servo::Preferences::default();
    if let Some(ua) = user_agent {
        eprintln!("engine: custom user agent set");
        prefs.user_agent = ua;
    }
    let servo = servo::ServoBuilder::default()
        .event_loop_waker(Box::new(ServoWaker(pump.clone())))
        .opts(opts)
        .preferences(prefs)
        .build();
    servo.setup_logging();

    // Adblock: load (and if needed download) the filter list before any loads.
    let adblock = adblock_list.and_then(|p| {
        ensure_filter_list(&p);
        load_adblock(&p)
    });
    if adblock.is_some() {
        servo.set_delegate(std::rc::Rc::new(ServoAdblockDelegate {
            adblock: adblock.clone(),
        }));
    }

    let initial_size = dpi::PhysicalSize::new(1200u32, 760u32);

    let mut make_tab = |id: u64| -> Option<Tab> {
        let context = make_context(initial_size)?;
        let _ = context.make_current();
        let delegate = std::rc::Rc::new(FrameDelegate {
            tab: id,
            shared: shared.clone(),
            adblock: adblock.clone(),
        });
        let webview = servo::WebViewBuilder::new(&servo, context.clone())
            .delegate(delegate)
            .build();
        // Complete the webview's registration handshake before any LoadUrl.
        servo.spin_event_loop();
        Some(Tab {
            webview,
            context,
            pending: None,
            pending_sent: false,
            pending_wait: 0,
            pending_retries: 0,
            last_url: None,
            last_title: None,
            last_history: None,
        })
    };

    // Tab 0 exists from the start (the chrome's initial tab).
    let mut tabs: HashMap<u64, Tab> = HashMap::new();
    if let Some(t) = make_tab(0) {
        tabs.insert(0, t);
    }

    let mut out = std::io::BufWriter::new(out_stream);
    let mut current_size = initial_size;
    let mut frame_id: u64 = 0;
    let mut event_id: u64 = 0;
    let mut last_hash: u64 = 0;
    let mut last_push: Option<std::time::Instant> = None;
    let mut tick: u64 = 0;

    // Watchdog: proves the main thread's state without a debugger.
    {
        let pump = pump.clone();
        std::thread::spawn(move || {
            loop {
                std::thread::sleep(std::time::Duration::from_secs(2));
                eprintln!("engine: watchdog alive");
            }
        });
    }

    // Shared GPU textures: try first; fall back to socket bytes on any
    // failure or if the chrome reports it cannot import them.
    #[derive(Clone, Copy, PartialEq)]
    enum FrameMode {
        TryShared,
        Bytes,
    }
    let mut frame_mode = FrameMode::TryShared;
    let mut shared_pool: Option<d3d::Pool> = None;

    loop {
        reader
            .get_ref()
            .set_read_timeout(Some(std::time::Duration::from_millis(1)))
            .ok();

        loop {
            let mut line = String::new();
            match reader.read_line(&mut line) {
                Ok(0) => {
                    eprintln!("engine: chrome disconnected");
                    // Exit without running destructors: dropping live tabs would
                    // trip the surfman surface assertion (cosmetic noise).
                    std::process::exit(0);
                }
                Ok(_) => {
                    let line = line.trim();
                    if line.is_empty() {
                        break;
                    }
                    if let Ok(v) = serde_json::from_str::<serde_json::Value>(line) {
                        let tab_id = v["tab"].as_u64().unwrap_or(0);
                        match v["cmd"].as_str() {
                            Some("navigate") => {
                                if let Some(u) = v["url"].as_str()
                                    && let Ok(url) = url::Url::parse(u)
                                {
                                    eprintln!("engine: queue navigate (tab {tab_id}) {url}");
                                    if !tabs.contains_key(&tab_id) {
                                        match make_tab(tab_id) {
                                            Some(t) => {
                                                tabs.insert(tab_id, t);
                                            }
                                            None => std::process::exit(0),
                                        }
                                    }
                                    let tab = tabs.get_mut(&tab_id).unwrap();
                                    tab.pending = Some(url);
                                    tab.pending_sent = false;
                                }
                            }
                            Some("new_tab") => {
                                if !tabs.contains_key(&tab_id) {
                                    eprintln!("engine: new tab {tab_id}");
                                    match make_tab(tab_id) {
                                        Some(t) => {
                                            tabs.insert(tab_id, t);
                                        }
                                        None => std::process::exit(0),
                                    }
                                }
                            }
                            Some("activate") => {
                                if !tabs.contains_key(&tab_id) {
                                    match make_tab(tab_id) {
                                        Some(t) => {
                                            tabs.insert(tab_id, t);
                                        }
                                        None => std::process::exit(0),
                                    }
                                }
                                eprintln!("engine: activate tab {tab_id}");
                                shared.active.store(tab_id, Ordering::SeqCst);
                                tabs.get(&tab_id).unwrap().webview.resize(current_size);
                                // Re-send this tab's last known state so the chrome's
                                // pill and title follow the switch.
                                let tab = tabs.get(&tab_id).unwrap();
                                if let Some(u) = &tab.last_url {
                                    event_id += 1;
                                    send_event(
                                        &mut out,
                                        event_id,
                                        &format!("{{\"ev\":\"url\",\"tab\":{tab_id},\"url\":\"{u}\"}}"),
                                    );
                                }
                                if let Some(t) = &tab.last_title {
                                    event_id += 1;
                                    let esc = t.replace('\\', "\\\\").replace('"', "\\\"");
                                    send_event(
                                        &mut out,
                                        event_id,
                                        &format!("{{\"ev\":\"title\",\"tab\":{tab_id},\"title\":\"{esc}\"}}"),
                                    );
                                }
                                if let Some((b, f)) = tab.last_history {
                                    event_id += 1;
                                    send_event(
                                        &mut out,
                                        event_id,
                                        &format!("{{\"ev\":\"history\",\"tab\":{tab_id},\"back\":{b},\"forward\":{f}}}"),
                                    );
                                }
                                shared.frame_ready.store(true, Ordering::SeqCst);
                            }
                            Some("close_tab") => {
                                eprintln!("engine: close tab {tab_id}");
                                // Dropping a webview + context trips a surfman
                                // assertion ("should have destroyed the surface
                                // first"): Servo tears the GL surface down
                                // asynchronously, after the context's Drop ran.
                                // Contain the panic so one closed tab can't kill
                                // the whole engine.
                                if let Some(tab) = tabs.remove(&tab_id) {
                                    let _ = std::panic::catch_unwind(
                                        std::panic::AssertUnwindSafe(|| drop(tab)),
                                    );
                                }
                                shared.started.lock().unwrap().remove(&tab_id);
                                if shared.active.load(Ordering::SeqCst) == tab_id {
                                    let next = tabs.keys().copied().min().unwrap_or(0);
                                    shared.active.store(next, Ordering::SeqCst);
                                    if let Some(t) = tabs.get(&next) {
                                        t.webview.resize(current_size);
                                    }
                                    shared.frame_ready.store(true, Ordering::SeqCst);
                                }
                            }
                            Some("back") => {
                                if let Some(tab) = tabs.get(&tab_id)
                                    && tab.webview.can_go_back()
                                {
                                    eprintln!("engine: go back (tab {tab_id})");
                                    let _ = tab.webview.go_back(1);
                                }
                            }
                            Some("forward") => {
                                if let Some(tab) = tabs.get(&tab_id)
                                    && tab.webview.can_go_forward()
                                {
                                    eprintln!("engine: go forward (tab {tab_id})");
                                    let _ = tab.webview.go_forward(1);
                                }
                            }
                            Some("eval") => {
                                // Trusted: the socket is loopback and only the
                                // chrome connects. Used for password autofill.
                                if let Some(js) = v["js"].as_str()
                                    && let Some(tab) = tabs.get(&tab_id)
                                {
                                    eprintln!("engine: eval (tab {tab_id})");
                                    tab.webview.evaluate_javascript(js.to_string(), |_| {});
                                }
                            }
                            Some("input") => {
                                // Pointer input from the chrome's page area;
                                // coordinates are the page's device pixels.
                                if let (Some(kind), Some(tab)) =
                                    (v["kind"].as_str(), tabs.get(&tab_id))
                                {
                                    eprintln!("engine: input (tab {tab_id}) {kind}");
                                    let x = v["x"].as_f64().unwrap_or(0.0) as f32;
                                    let y = v["y"].as_f64().unwrap_or(0.0) as f32;
                                    let point = servo::WebViewPoint::Device(
                                        webrender_api::units::DevicePoint::new(x, y),
                                    );
                                    match kind {
                                        "down" | "up" => {
                                            let button = match v["button"].as_str() {
                                                Some("right") => servo::MouseButton::Secondary,
                                                _ => servo::MouseButton::Primary,
                                            };
                                            let action = if kind == "down" {
                                                servo::MouseButtonAction::Down
                                            } else {
                                                servo::MouseButtonAction::Up
                                            };
                                            // A click on the page focuses the webview;
                                            // keyboard events are dropped without it.
                                            if kind == "down" {
                                                tab.webview.set_focused(true);
                                            }
                                            tab.webview.notify_input_event(
                                                servo::InputEvent::MouseButton(
                                                    servo::MouseButtonEvent {
                                                        action,
                                                        button,
                                                        point,
                                                        is_compatibility_event_for_touch: false,
                                                    },
                                                ),
                                            );
                                        }
                                        "move" => {
                                            tab.webview.notify_input_event(
                                                servo::InputEvent::MouseMove(
                                                    servo::MouseMoveEvent {
                                                        point,
                                                        is_compatibility_event_for_touch: false,
                                                    },
                                                ),
                                            );
                                        }
                                        _ => {}
                                    }
                                    shared.pump.kick();
                                }
                            }
                            Some("wheel") => {
                                if let Some(tab) = tabs.get(&tab_id) {
                                    eprintln!("engine: wheel (tab {tab_id})");
                                    let dx = v["dx"].as_f64().unwrap_or(0.0);
                                    let dy = v["dy"].as_f64().unwrap_or(0.0);
                                    let x = v["x"].as_f64().unwrap_or(0.0) as f32;
                                    let y = v["y"].as_f64().unwrap_or(0.0) as f32;
                                    let point = servo::WebViewPoint::Device(
                                        webrender_api::units::DevicePoint::new(x, y),
                                    );
                                    // Same sign convention as the chrome: a
                                    // positive dy means the content moves down,
                                    // i.e. the view scrolls up (servo agrees).
                                    tab.webview.notify_input_event(servo::InputEvent::Wheel(
                                        servo::WheelEvent {
                                            delta: servo::WheelDelta {
                                                x: dx,
                                                y: dy,
                                                z: 0.0,
                                                mode: servo::WheelMode::DeltaPixel,
                                            },
                                            point,
                                        },
                                    ));
                                    shared.pump.kick();
                                }
                            }
                            Some("key") => {
                                if let Some(name) = v["name"].as_str()
                                    && let Some(tab) = tabs.get(&tab_id)
                                {
                                    eprintln!("engine: key (tab {tab_id}) {name}");
                                    let text = v["text"].as_str().unwrap_or("");
                                    let mods = v["mods"].as_u64().unwrap_or(0) as u8;
                                    let down = v["down"].as_bool().unwrap_or(true);
                                    if let Some(ev) = map_key(name, text, mods, down) {
                                        tab.webview.notify_input_event(ev);
                                        shared.pump.kick();
                                    }
                                }
                            }
                            Some("resize") => {
                                let (w, h) = (
                                    v["w"].as_u64().unwrap_or(1200) as u32,
                                    v["h"].as_u64().unwrap_or(760) as u32,
                                );
                                let s = dpi::PhysicalSize::new(w.max(1), h.max(1));
                                if s != current_size {
                                    current_size = s;
                                    let active = shared.active.load(Ordering::SeqCst);
                                    if let Some(tab) = tabs.get(&active) {
                                        tab.webview.resize(s);
                                    }
                                }
                            }
                            Some("frames") => {
                                if v["mode"].as_str() == Some("bytes") {
                                    eprintln!("engine: chrome requested byte frames");
                                    frame_mode = FrameMode::Bytes;
                                }
                            }
                            _ => {}
                        }
                    }
                }
                Err(e) if e.kind() == std::io::ErrorKind::WouldBlock
                    || e.kind() == std::io::ErrorKind::TimedOut => break,
                Err(_) => std::process::exit(0),
            }
        }

        // Pending loads: send each, then wait for LoadStatus::Started to know it
        // committed (the first LoadUrl after startup can be silently dropped).
        // A handful of retries, then give up rather than reload forever.
        for (id, tab) in tabs.iter_mut() {
            if let Some(u) = tab.pending.clone() {
                if !tab.pending_sent {
                    shared.started.lock().unwrap().remove(id);
                    eprintln!("engine: load (tab {id}) {u}");
                    tab.webview.load(u);
                    tab.pending_sent = true;
                    tab.pending_wait = 0;
                } else if shared.started.lock().unwrap().contains(id)
                    || tab.webview.url().map(|v| v == u).unwrap_or(false)
                {
                    // Started fired, or the webview's url already matches the
                    // pending load: it was received, not dropped. (Checking the
                    // url avoids false positives from the initial about:blank.)
                    tab.pending = None;
                    tab.pending_sent = false;
                    tab.pending_retries = 0;
                } else {
                    // ~16ms per tick: 120 ticks ~= 2s of patience per attempt.
                    tab.pending_wait += 1;
                    if tab.pending_wait > 120 {
                        if tab.pending_retries < 3 {
                            tab.pending_sent = false;
                            tab.pending_retries += 1;
                        } else {
                            eprintln!("engine: load (tab {id}) never started, giving up");
                            tab.pending = None;
                            tab.pending_retries = 0;
                        }
                    }
                }
            }
        }

        servo.spin_event_loop();

        tick += 1;
        if tick % 60 == 0 {
            eprintln!("engine: tick {tick}");
        }

        // Forward delegate updates to the chrome as events.
        {
            let mut updates = shared.updates.lock().unwrap();
            for u in updates.drain(..) {
                if let Some(t) = tabs.get_mut(&u.tab) {
                    if let Some(url) = &u.url {
                        t.last_url = Some(url.clone());
                    }
                    if let Some(title) = &u.title {
                        t.last_title = Some(title.clone());
                    }
                    if let Some(h) = &u.history {
                        t.last_history = Some(*h);
                    }
                }
                event_id += 1;
                if let Some(url) = &u.url {
                    send_event(
                        &mut out,
                        event_id,
                        &format!("{{\"ev\":\"url\",\"tab\":{},\"url\":\"{url}\"}}", u.tab),
                    );
                }
                if let Some(title) = &u.title {
                    let esc = title.replace('\\', "\\\\").replace('"', "\\\"");
                    send_event(
                        &mut out,
                        event_id,
                        &format!("{{\"ev\":\"title\",\"tab\":{},\"title\":\"{esc}\"}}", u.tab),
                    );
                }
                if let Some((b, f)) = &u.history {
                    send_event(
                        &mut out,
                        event_id,
                        &format!("{{\"ev\":\"history\",\"tab\":{},\"back\":{},\"forward\":{}}}", u.tab, b, f),
                    );
                }
                if let Some(data) = &u.pw {
                    // data is already valid JSON ({u,p,o}); embed it directly.
                    send_event(
                        &mut out,
                        event_id,
                        &format!("{{\"ev\":\"pwcapture\",\"tab\":{},\"data\":{}}}", u.tab, data),
                    );
                }
                if let Some((origin, w, h, rgba)) = &u.icon {
                    let pixels: Vec<String> = rgba.iter().map(|b| b.to_string()).collect();
                    send_event(
                        &mut out,
                        event_id,
                        &format!(
                            "{{\"ev\":\"favicon\",\"tab\":{},\"origin\":\"{}\",\"w\":{},\"h\":{},\"rgba\":[{}]}}",
                            u.tab,
                            origin.replace('\\', "\\\\").replace('"', "\\\""),
                            w,
                            h,
                            pixels.join(",")
                        ),
                    );
                }
            }
        }

        if shared.frame_ready.swap(false, Ordering::SeqCst) {
            let active = shared.active.load(Ordering::SeqCst);
            if let Some(tab) = tabs.get(&active) {
                // Cap animated pages at ~30fps: beyond that we drop intermediate
                // frames (latest-wins on the chrome side) to bound IPC traffic.
                if let Some(elapsed) = last_push
                    && elapsed.elapsed() < std::time::Duration::from_millis(33)
                {
                    shared.frame_ready.store(true, Ordering::SeqCst);
                    pump.wait(std::time::Duration::from_millis(4));
                    continue;
                }
                let t0 = std::time::Instant::now();
                // Composite the page into the rendering context; without this the
                // context surface stays blank and read_to_image returns white.
                tab.webview.paint();
                let t_paint = t0.elapsed();
                // Servo's render machinery runs GL on its own threads and steals
                // currentness from us; a software GL context reads blank otherwise.
                let _ = tab.context.make_current();
                let rect = webrender_api::units::DeviceIntRect::new(
                    webrender_api::units::DeviceIntPoint::zero(),
                    webrender_api::units::DeviceIntPoint::new(
                        current_size.width as i32,
                        current_size.height as i32,
                    ),
                );
                if let Some(image) = tab.context.read_to_image(rect) {
                    let mut bgra = image.into_raw();
                    for px in bgra.chunks_exact_mut(4) {
                        px.swap(0, 2);
                    }
                    // Skip identical frames: Servo re-renders even when nothing
                    // changed, and each push forces a full chrome redraw.
                    let mut hasher = std::collections::hash_map::DefaultHasher::new();
                    use std::hash::Hasher;
                    hasher.write(&bgra);
                    let hash = hasher.finish();
                    if hash != last_hash || bgra.is_empty() {
                        last_hash = hash;
                        last_push = Some(std::time::Instant::now());
                        eprintln!(
                            "engine: frame {}x{} paint={:?} total={:?}",
                            current_size.width,
                            current_size.height,
                            t_paint,
                            t0.elapsed()
                        );
                        frame_id += 1;

                        // Shared-texture path: no pixel bytes over the socket.
                        let mut sent_shared = false;
                        if frame_mode == FrameMode::TryShared {
                            let need_resize = shared_pool
                                .as_ref()
                                .map(|p| p.size() != (current_size.width, current_size.height))
                                .unwrap_or(true);
                            if need_resize {
                                match d3d::Pool::new(current_size.width, current_size.height) {
                                    Ok(p) => shared_pool = Some(p),
                                    Err(e) => {
                                        eprintln!("engine: shared frames unavailable ({e}); using bytes");
                                        frame_mode = FrameMode::Bytes;
                                    }
                                }
                            }
                        }
                        if frame_mode == FrameMode::TryShared {
                            let pool = shared_pool.as_mut().unwrap();
                            match pool.write(&bgra) {
                                Ok(()) => {
                                    let (name, fence, value) = pool.info();
                                    event_id += 1;
                                    send_event(
                                        &mut out,
                                        event_id,
                                        &format!(
                                            "{{\"ev\":\"frame\",\"tab\":{active},\"w\":{},\"h\":{},\"name\":\"{name}\",\"fence\":\"{fence}\",\"value\":{value}}}",
                                            current_size.width, current_size.height
                                        ),
                                    );
                                    sent_shared = true;
                                }
                                Err(e) => {
                                    eprintln!("engine: shared frame write failed ({e}); using bytes");
                                    frame_mode = FrameMode::Bytes;
                                }
                            }
                        }
                        if !sent_shared {
                            out.write_all(&MAGIC.to_le_bytes()).ok();
                            out.write_all(&frame_id.to_le_bytes()).ok();
                            out.write_all(&current_size.width.to_le_bytes()).ok();
                            out.write_all(&current_size.height.to_le_bytes()).ok();
                            out.write_all(&bgra).ok();
                            let _ = out.flush();
                        }
                    }
                }
            }
        }

        pump.wait(std::time::Duration::from_millis(16));
    }
}
