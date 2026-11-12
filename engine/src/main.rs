//! Browserr page engine: a Servo child process.
//! Listens on 127.0.0.1:7411. Protocol v2:
//!   in:  newline JSON:
//!        {"cmd":"navigate","url":"...","tab":N}   load in that tab (created on demand)
//!        {"cmd":"back"|"forward","tab":N}         per-tab history
//!        {"cmd":"new_tab","tab":N}                create a tab session
//!        {"cmd":"activate","tab":N}               make that tab the rendered one
//!        {"cmd":"close_tab","tab":N}              drop that tab
//!        {"cmd":"resize","w":N,"h":N}             resize the active tab (others on activate)
//!   out: events:  u32 magic 'EVT1', u64 id, u32 len, u32 0, len bytes JSON
//!                 {"ev":"url","tab":N,"url":"..."} | {"ev":"title","tab":N,"title":"..."}
//!        Composited frames are announced as {"ev":"frame","tab":N,"w":W,"h":H}
//!        events; pixels are fetched on demand via the screenshot command
//!        (SHT1 frame: magic, id, w, h, w*h*4 BGRA). Nothing pushes a
//!        continuous pixel stream anymore (the chrome UI is gone).
//! Only the active tab is painted; switching tabs re-sends that tab's url/title.
//! Page zoom (Ctrl+wheel in the chrome) is per tab, set by the chrome via
//! {"cmd":"zoom","tab":N,"zoom":F}; the page's requested cursor shape is
//! forwarded as {"ev":"cursor","tab":N,"cursor":"name"}.
//! Options: --user-agent=STRING overrides the HTTP user agent (from settings.json).
//!           --profile-dir=PATH gives Servo a per-profile config/storage dir.
//!           --adblock-list=PATH enables request blocking against an EasyList-format file.
//!           --ephemeral uses a throwaway profile dir under %TEMP% (deleted on exit).
//!           --software forces CPU rendering (GPU is the default; without the
//!           flag a GPU-less machine falls back to software automatically).
//! Automation: eval with an "id" reports {"ev":"evalresult","tab","id","value"};
//! {"cmd":"screenshot","tab":N} answers with a SHT1 frame (magic, id, w, h, w*h*4 BGRA).
//! {"cmd":"ping","id":N} answers {"ev":"pong","id":N,"tabs":K,"active":A} (health check).
//! Interception (fail-closed): {"cmd":"intercept","on":bool} toggles it; when on,
//! each qualifying HTTP request emits {"ev":"request","id","tab","method","url",
//! "main":bool,"destination":...} and holds. The driver answers with
//! {"cmd":"request","id","action":"allow"|"deny"|"respond","status":N,"body":...,
//! "content_type":...}; "deny" fulfills with a synthetic 403 (never cancel():
//! a cancelled response wedges the tab pipeline). Unanswered requests deny
//! after 5s. Auth challenges (main-frame navigations, incl. proxy 407) emit
//! {"ev":"auth","id","tab","url","proxy":bool}; {"cmd":"auth","id","user","pass"}
//! answers, anything else fails closed. File pickers emit {"ev":"filepicker",
//! "id","tab","multiple":bool}; {"cmd":"files","id","paths":[...]} answers
//! (empty = cancel). {"cmd":"frames","id":N} answers {"ev":"evalresult",
//! "id":N,"value":<frame tree JSON>} (same-origin walking; cross-origin
//! frames report url only). Net evidence: a document-start user script in
//! every frame wraps fetch/XHR into window.__browserr_net (ring, 200
//! entries); agents poll it via eval.
use servo::RenderingContext;
use std::cell::RefCell;
use std::collections::HashMap;
use std::io::{BufRead, BufReader, Read, Write};

mod hw;
use hw::HardwareRenderingContext;
use std::net::TcpListener;
use std::sync::atomic::{AtomicBool, AtomicU64, Ordering};
use std::sync::{Arc, Condvar, Mutex};

const PORT: u16 = 7411;
const EV_MAGIC: u32 = 0x45_56_54_31; // EVT1
const SHOT_MAGIC: u32 = 0x53_48_54_31; // SHT1 (screenshot)

/// Terminate the process immediately, skipping libc atexit handlers and
/// static destructors. std::process::exit runs them; on Linux servo/mozglue
/// teardown deadlocks there (pthread_mutex_destroy on mutexes owned by
/// still-running threads), which stalled engine exit for the full client
/// timeout. Destructors must not run for live tabs anyway (surfman surface
/// assertion noise).
fn hard_exit() -> ! {
    unsafe extern "C" {
        fn _exit(status: i32) -> !;
    }
    unsafe { _exit(0) }
}

/// Commands an attached driver may send; validated before queueing.
const DRIVER_CMDS: [&str; 19] = [
    "navigate",
    "back",
    "forward",
    "new_tab",
    "activate",
    "close_tab",
    "input",
    "wheel",
    "key",
    "eval",
    "screenshot",
    "zoom",
    "ping",
    "download",
    "intercept",
    "request",
    "auth",
    "files",
    "frames",
];

/// Attached-driver hub (computer-use mode: `--driver-port=N`). Driver
/// command lines are validated, queued, and drained by the main loop
/// (which owns servo); engine events and SHT1 screenshot answers are
/// mirrored to every attached driver. Optional `--driver-token=T`:
/// drivers must send {"cmd":"auth","token":T} as their first line or
/// the socket is dropped without receiving anything.
struct DriverHub {
    clients: std::sync::Mutex<Vec<std::net::TcpStream>>,
    queue: std::sync::Mutex<std::collections::VecDeque<String>>,
}

/// Prefix marking a queued command as coming from an attached driver
/// (the main loop strips it and answers via the driver broadcast only).
const DRIVER_PREFIX: &str = "\u{1}drv:";

static HUB: std::sync::OnceLock<DriverHub> = std::sync::OnceLock::new();

/// Monotonic id for held interception requests.
static INTERCEPT_ID: AtomicU64 = AtomicU64::new(1);

impl DriverHub {
    fn global() -> &'static DriverHub {
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
    fn broadcast(&self, bytes: &[u8]) {
        let mut clients = self.clients.lock().unwrap();
        clients.retain_mut(|c| c.write_all(bytes).is_ok() && c.flush().is_ok());
    }

    fn has_clients(&self) -> bool {
        !self.clients.lock().unwrap().is_empty()
    }
}

/// Accept driver connections on 127.0.0.1:port; validate + queue their
/// commands; drop unauthenticated sockets when a token is required.
fn start_driver_listener(port: u16, token: Option<String>) {
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
        if !DRIVER_CMDS.contains(&cmd) {
            eprintln!("engine: driver command rejected: {cmd}");
            continue;
        }
        hub.push(trimmed);
    }
    eprintln!("engine: driver detached");
}

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
    /// Page-requested cursor shape (servo's Debug name, lowercase).
    cursor: Option<String>,
    /// Result of an eval-with-id: (request id, JSON-encoded value).
    eval: Option<(u64, serde_json::Value)>,
    /// Page console message: (level, message). Servo also routes network
    /// and TLS load errors here, so agents see why a page failed.
    console: Option<(String, String)>,
    /// Held request awaiting a driver decision: (id, method, url, is_main).
    intercept: Option<(u64, String, String, bool)>,
    /// Auth challenge awaiting credentials: (id, url, for_proxy).
    auth: Option<(u64, String, bool)>,
    /// File picker awaiting a file list: (id, allow_multiple).
    filepicker: Option<(u64, bool)>,
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

/// Net evidence (document-start user script, injected in EVERY frame via
/// the UserContentManager): wraps fetch() and XMLHttpRequest into a ring
/// buffer on window.__browserr_net. Runs before page scripts, so pages
/// cannot evade it by grabbing references first. Polled via eval:
/// window.__browserr_net.slice(-N).
const NET_EVIDENCE_JS: &str = r#"(function(){
if(window.__browserr_net) return;
var ring=[];
function push(kind,url,extra){
  try{
    var e={kind:kind,url:String(url),t:Date.now()};
    if(extra){for(var k in extra){e[k]=extra[k];}}
    ring.push(e);
    if(ring.length>200){ring.splice(0,ring.length-200);}
  }catch(_){}
}
window.__browserr_net=ring;
var of=window.fetch;
if(of){
  window.fetch=function(input,init){
    var url;
    try{url=(input&&input.url)?input.url:String(input);}catch(_){url='?';}
    push('fetch',url,{method:(init&&init.method)||'GET'});
    return of.apply(this,arguments).then(function(resp){
      push('fetch-done',url,{status:resp.status});
      return resp;
    },function(err){
      push('fetch-error',url,{error:String(err)});
      throw err;
    });
  };
}
if(window.XMLHttpRequest){
  var oo=window.XMLHttpRequest.prototype.open;
  window.XMLHttpRequest.prototype.open=function(method,url){
    this.__b_url=url;
    push('xhr',url,{method:method});
    var xhr=this;
    this.addEventListener('load',function(){push('xhr-done',xhr.__b_url,{status:xhr.status});});
    this.addEventListener('error',function(){push('xhr-error',xhr.__b_url,{});});
    return oo.apply(this,arguments);
  };
}
})()"#;

/// Returns JSON for the page's frame tree (tier-1 frame control):
/// same-origin frames are walked via contentDocument (cross-origin frames
/// only report their url/origin from the parent's perspective; servo 0.8
/// has no cross-frame JS entry point). Top level:
///   {url, origin, children:[{name, url, same_origin, children:[...]}]}
const FRAME_TREE_JS: &str = r#"(function(){
function frame_info(f){
  var o={name:f.name||'',url:'',same_origin:false,children:[]};
  try{
    o.url=String(f.location.href);
    o.same_origin=true;
    try{
      o.title=String(f.document.title||'');
    }catch(_){}
  }catch(_){
    try{o.url=String(f.location)||'';}catch(__){}
  }
  try{
    var docs=f.document?null:null;
  }catch(_){}
  return o;
}
function walk(win,out){
  var frames;
  try{frames=win.frames;}catch(_){return out;}
  for(var i=0;i<frames.length;i++){
    var fo={name:frames[i].name||'',url:'',same_origin:false,children:[]};
    try{
      fo.url=String(frames[i].location.href);
      fo.same_origin=true;
      try{fo.title=String(frames[i].document.title||'');}catch(_){}
    }catch(_){
      try{fo.url=String(frames[i].location)||'';fo.same_origin=false;}catch(__){}
    }
    out.children.push(fo);
    // Only same-origin frames can be walked recursively.
    if(fo.same_origin){
      try{walk(frames[i],fo);}catch(_){}
    }
  }
  return out;
}
var root={url:'',origin:'',children:[]};
try{root.url=String(location.href);root.origin=location.origin;}catch(_){}
walk(window,root);
return JSON.stringify(root);
})()"#;

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
    /// Fail-closed interception armed by the driver ({"cmd":"intercept","on":true}).
    intercept_on: AtomicBool,
}

/// An auth challenge held for a driver answer. Keeping the
/// AuthenticationRequest alive holds the challenge; dropping it answers
/// "no credentials" (fail-closed).
struct AuthHold {
    since: std::time::Instant,
    request: servo::AuthenticationRequest,
}

/// A file picker held for a driver answer. Dropping it dismisses the picker
/// (no selection).
struct FileHold {
    since: std::time::Instant,
    picker: servo::FilePicker,
}

/// A load held for a driver decision. The WebResourceLoad must stay alive
/// while held: dropping it sends DoNotIntercept (pass-through). Deny or
/// respond = intercept() + finish(); servo's request_interceptor blocks a
/// net-pool worker per pending decision, so we cap waiting time.
struct PendingLoad {
    since: std::time::Instant,
    load: servo::WebResourceLoad,
}

// Held requests and their driver answers. The servo responder types above
// are !Send (Rc-based), so these live in thread-locals: every producer and
// consumer runs on the main thread that owns servo (delegate callbacks fire
// during spin_event_loop; the command dispatch and the timeout sweep run in
// the same loop). Answers, holds and pickers never cross threads.
thread_local! {
    /// Held loads awaiting a driver decision (see Shared::intercept_on).
    static PENDING_LOADS: RefCell<HashMap<u64, PendingLoad>> =
        RefCell::new(HashMap::new());
    /// Driver decisions queued by request-id.
    static ANSWERS: RefCell<HashMap<u64, serde_json::Value>> =
        RefCell::new(HashMap::new());
    /// Auth credentials queued per auth request-id.
    static AUTH_ANSWERS: RefCell<HashMap<u64, Option<(String, String)>>> =
        RefCell::new(HashMap::new());
    /// File paths queued per filepicker request-id (None = cancel).
    static FILE_ANSWERS:
        RefCell<HashMap<u64, Option<Vec<std::path::PathBuf>>>> =
        RefCell::new(HashMap::new());
    /// Held auth requests awaiting driver credentials. Drop = no creds.
    static AUTH_HOLDS: RefCell<HashMap<u64, AuthHold>> =
        RefCell::new(HashMap::new());
    /// Held file pickers awaiting driver file lists. Drop = cancel.
    static FILE_HOLDS: RefCell<HashMap<u64, FileHold>> =
        RefCell::new(HashMap::new());
}

struct ServoAdblockDelegate {
    adblock: Option<std::rc::Rc<adblock::engine::Engine>>,
}

impl servo::ServoDelegate for ServoAdblockDelegate {
    fn load_web_resource(&self, load: servo::WebResourceLoad) {
        // Requests with a target webview go to the WebViewDelegate; this
        // catches the rest (no target webview known at the net layer).
        if let Some(engine) = &self.adblock
            && adblock_matches(engine, &load)
        {
            eprintln!("engine: adblock blocked {}", load.request().url);
            let response_url = load.request().url.clone().into_url();
            load.intercept(servo::WebResourceResponse::new(response_url))
                .cancel();
        }
    }
}

/// Whether the filter list matches this request (metadata only; the load
/// is not consumed here).
fn adblock_matches(engine: &adblock::engine::Engine, load: &servo::WebResourceLoad) -> bool {
    // Main-frame documents are never blocked: a cancelled document response
    // wedges the pipeline for later navigations on that tab.
    if load.request().is_for_main_frame {
        return false;
    }
    let url = load.request().url.to_string();
    let req_type = format!("{:?}", load.request().destination).to_lowercase();
    let method = load.request().method.as_str();
    let Ok(req) = adblock::request::Request::new(&url, "", &req_type, method) else {
        return false;
    };
    engine.check_network_request(&req).should_block()
}

struct FrameDelegate {
    tab: u64,
    shared: Arc<Shared>,
    adblock: Option<std::rc::Rc<adblock::engine::Engine>>,
}

impl FrameDelegate {
    /// Eval a password-capture script; a string result is reported to the chrome.
    fn eval_pw(&self, webview: servo::WebView, script: &'static str) {
        let shared = self.shared.clone();
        let tab = self.tab;
        webview.evaluate_javascript(script, move |result| {
            if let Ok(servo::JSValue::String(data)) = result
                && !data.is_empty()
            {
                shared.updates.lock().unwrap().push(Update {
                    tab,
                    url: None,
                    title: None,
                    history: None,
                    pw: Some(data),
                    icon: None,
                    cursor: None,
                    eval: None,
                    console: None,
                    intercept: None,
                    auth: None,
                    filepicker: None,
                });
                shared.pump.kick();
            }
        });
    }

    /// Eval the favicon lookup; fetch + decode the icon off-thread if new.
    fn eval_favicon(&self, webview: servo::WebView) {
        let shared = self.shared.clone();
        let tab = self.tab;
        webview.evaluate_javascript(FAVICON_JS, move |result| {
            let Ok(servo::JSValue::String(data)) = result else {
                return;
            };
            let Ok(v) = serde_json::from_str::<serde_json::Value>(&data) else {
                return;
            };
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
                    cursor: None,
                    eval: None,
                    console: None,
                    intercept: None,
                    auth: None,
                    filepicker: None,
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
                        cursor: None,
                        eval: None,
                        console: None,
                        intercept: None,
                        auth: None,
                        filepicker: None,
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
    let bytes = http_get(url, 1 << 20).ok()?;
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
            cursor: None,
            eval: None,
            console: None,
            intercept: None,
            auth: None,
            filepicker: None,
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

    /// Console messages from the page (and servo's own load-failure logs:
    /// network and TLS errors land here). Forwarded as events so agents can
    /// read page output and load failures.
    fn show_console_message(
        &self,
        _webview: servo::WebView,
        level: servo::ConsoleLogLevel,
        message: String,
    ) {
        let name = format!("{level:?}").to_lowercase();
        self.shared.updates.lock().unwrap().push(Update {
            tab: self.tab,
            url: None,
            title: None,
            history: None,
            pw: None,
            icon: None,
            cursor: None,
            eval: None,
            console: Some((name, message)),
            intercept: None,
            auth: None,
            filepicker: None,
        });
        self.shared.pump.kick();
    }

    fn notify_cursor_changed(&self, _webview: servo::WebView, cursor: servo::Cursor) {
        let name = format!("{cursor:?}").to_lowercase();
        self.shared.updates.lock().unwrap().push(Update {
            tab: self.tab,
            url: None,
            title: None,
            history: None,
            pw: None,
            icon: None,
            cursor: Some(name),
            eval: None,
            console: None,
            intercept: None,
            auth: None,
            filepicker: None,
        });
        self.shared.pump.kick();
    }

    fn notify_page_title_changed(&self, _webview: servo::WebView, title: Option<String>) {
        self.shared.updates.lock().unwrap().push(Update {
            tab: self.tab,
            url: None,
            title,
            history: None,
            pw: None,
            icon: None,
            cursor: None,
            eval: None,
            console: None,
            intercept: None,
            auth: None,
            filepicker: None,
        });
        self.shared.pump.kick();
    }

    fn notify_history_changed(
        &self,
        _webview: servo::WebView,
        entries: Vec<url::Url>,
        current: usize,
    ) {
        let back = current > 0;
        let forward = current + 1 < entries.len();
        self.shared.updates.lock().unwrap().push(Update {
            tab: self.tab,
            url: None,
            title: None,
            history: Some((back, forward)),
            pw: None,
            icon: None,
            cursor: None,
            eval: None,
            console: None,
            intercept: None,
            auth: None,
            filepicker: None,
        });
        self.shared.pump.kick();
    }

    fn load_web_resource(&self, _webview: servo::WebView, load: servo::WebResourceLoad) {
        // Adblock first; a blocked load is consumed (no hold).
        if let Some(engine) = &self.adblock
            && adblock_matches(engine, &load)
        {
            eprintln!("engine: adblock blocked {}", load.request().url);
            let response_url = load.request().url.clone().into_url();
            load.intercept(servo::WebResourceResponse::new(response_url))
                .cancel();
            return;
        }
        // Fail-closed interception: hold qualifying loads for a driver
        // decision (auto-allow everything else so the net pool never starves).
        if !self.shared.intercept_on.load(Ordering::SeqCst) {
            return;
        }
        let main = load.request().is_for_main_frame;
        // Only documents and data requests are surfaced for decisions;
        // images/styles/fonts and other subresources pass through unheld.
        // fetch()/XHR loads carry Destination::None in servo's request
        // pipeline, so those ride the None arm below.
        let dest = format!("{:?}", load.request().destination).to_lowercase();
        if !main && !matches!(dest.as_str(), "none" | "json" | "document") {
            return;
        }
        let url = load.request().url.to_string();
        let method = load.request().method.as_str().to_string();
        // Hold the load itself: alive = held (drop would pass through).
        let id = INTERCEPT_ID.fetch_add(1, Ordering::SeqCst);
        PENDING_LOADS.with(|m| {
            m.borrow_mut().insert(
                id,
                PendingLoad {
                    since: std::time::Instant::now(),
                    load,
                },
            )
        });
        self.shared.updates.lock().unwrap().push(Update {
            tab: self.tab,
            url: None,
            title: None,
            history: None,
            pw: None,
            icon: None,
            cursor: None,
            eval: None,
            console: None,
            intercept: Some((id, method, url, main)),
            auth: None,
            filepicker: None,
        });
        self.shared.pump.kick();
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

    /// Auth challenge (main-frame navigations, incl. proxy 407): hold for a
    /// driver answer; drop = no credentials (fail closed).
    fn request_authentication(
        &self,
        _webview: servo::WebView,
        request: servo::AuthenticationRequest,
    ) {
        let id = INTERCEPT_ID.fetch_add(1, Ordering::SeqCst);
        let url = request.url().to_string();
        let proxy = request.for_proxy();
        AUTH_HOLDS.with(|m| {
            m.borrow_mut().insert(
                id,
                AuthHold {
                    since: std::time::Instant::now(),
                    request,
                },
            )
        });
        self.shared.updates.lock().unwrap().push(Update {
            tab: self.tab,
            url: None,
            title: None,
            history: None,
            pw: None,
            icon: None,
            cursor: None,
            eval: None,
            console: None,
            intercept: None,
            auth: Some((id, url, proxy)),
            filepicker: None,
        });
        self.shared.pump.kick();
    }

    /// File picker (input type=file): hold for a driver file list; drop =
    /// dismissed with no selection. Other controls are ignored.
    fn show_embedder_control(&self, _webview: servo::WebView, control: servo::EmbedderControl) {
        let servo::EmbedderControl::FilePicker(picker) = control else {
            return;
        };
        let id = INTERCEPT_ID.fetch_add(1, Ordering::SeqCst);
        let multiple = picker.allow_select_multiple();
        FILE_HOLDS.with(|m| {
            m.borrow_mut().insert(
                id,
                FileHold {
                    since: std::time::Instant::now(),
                    picker,
                },
            )
        });
        self.shared.updates.lock().unwrap().push(Update {
            tab: self.tab,
            url: None,
            title: None,
            history: None,
            pw: None,
            icon: None,
            cursor: None,
            eval: None,
            console: None,
            intercept: None,
            auth: None,
            filepicker: Some((id, multiple)),
        });
        self.shared.pump.kick();
    }
}

struct Tab {
    webview: servo::WebView,
    context: std::rc::Rc<dyn RenderingContext>,
    pending: Option<url::Url>,
    pending_sent: bool,
    /// Wall-clock deadline for the current load attempt; retries are
    /// time-based (not tick-based) because a tick can cost 16ms (GPU) or
    /// ~1s (software/paravirtualized GL on CI runners).
    pending_deadline: Option<std::time::Instant>,
    pending_retries: u32,
    last_url: Option<String>,
    last_title: Option<String>,
    last_history: Option<(bool, bool)>,
    last_cursor: Option<String>,
}

const EASYLIST_URL: &str = "https://easylist.to/easylist/easylist.txt";
/// Re-download after a week.
const FILTER_LIST_MAX_AGE_SECS: u64 = 7 * 24 * 3600;

/// Serialize a servo JS evaluation result to JSON for the automation driver.
fn jsvalue_to_json(
    result: Result<servo::JSValue, servo::JavaScriptEvaluationError>,
) -> serde_json::Value {
    use servo::JSValue;
    fn conv(v: JSValue) -> serde_json::Value {
        match v {
            JSValue::Undefined | JSValue::Null => serde_json::Value::Null,
            JSValue::Boolean(b) => serde_json::Value::Bool(b),
            JSValue::Number(n) => serde_json::Number::from_f64(n)
                .map(serde_json::Value::Number)
                .unwrap_or(serde_json::Value::Null),
            JSValue::String(s) => serde_json::Value::String(s),
            // Handles (element/shadow root/frame/window) are opaque strings;
            // tag them so callers can tell them apart from plain strings.
            JSValue::Element(s) => serde_json::json!({ "__kind": "element", "handle": s }),
            JSValue::ShadowRoot(s) => serde_json::json!({ "__kind": "shadowroot", "handle": s }),
            JSValue::Frame(s) => serde_json::json!({ "__kind": "frame", "handle": s }),
            JSValue::Window(s) => serde_json::json!({ "__kind": "window", "handle": s }),
            JSValue::Array(items) => {
                serde_json::Value::Array(items.into_iter().map(conv).collect())
            }
            JSValue::Object(map) => {
                serde_json::Value::Object(map.into_iter().map(|(k, v)| (k, conv(v))).collect())
            }
        }
    }
    match result {
        Ok(v) => conv(v),
        Err(e) => serde_json::json!({ "__error": format!("{e:?}") }),
    }
}

/// Minimal HTTP/HTTPS GET client (rustls + the platform cert verifier; the
/// aws-lc-rs provider installed at startup). Replaces ureq so the binary
/// ships exactly one crypto stack - servo-net already pins aws-lc-rs, and
/// ureq would have dragged in ring as a second one.
/// Follows up to 5 redirects; caps the body at `max_bytes`.
fn http_get(url: &str, max_bytes: u64) -> Result<Vec<u8>, String> {
    let mut current = url::Url::parse(url).map_err(|e| e.to_string())?;
    for _ in 0..6 {
        let host = current.host_str().ok_or("no host")?.to_string();
        let mut path = current.path().to_string();
        if path.is_empty() {
            path = "/".into();
        }
        if let Some(q) = current.query() {
            path.push('?');
            path.push_str(q);
        }
        let secure = current.scheme() == "https";
        let port = current
            .port_or_known_default()
            .unwrap_or(if secure { 443 } else { 80 });

        let tcp = std::net::TcpStream::connect((host.as_str(), port))
            .map_err(|e| format!("connect {host}:{port}: {e}"))?;
        tcp.set_read_timeout(Some(std::time::Duration::from_secs(15)))
            .ok();
        tcp.set_write_timeout(Some(std::time::Duration::from_secs(15)))
            .ok();

        let stream: Box<dyn std::io::Read> = if secure {
            let verifier = rustls_platform_verifier::Verifier::new(Arc::new(
                rustls::crypto::aws_lc_rs::default_provider(),
            ))
            .map_err(|e| format!("platform verifier: {e}"))?;
            let config = rustls::ClientConfig::builder()
                .dangerous()
                .with_custom_certificate_verifier(std::sync::Arc::new(verifier))
                .with_no_client_auth();
            let name = rustls_pki_types::ServerName::try_from(host.clone())
                .map_err(|_| format!("bad server name: {host}"))?;
            let conn = rustls::ClientConnection::new(std::sync::Arc::new(config), name)
                .map_err(|e| format!("tls setup: {e}"))?;
            let mut tls = rustls::StreamOwned::new(conn, tcp);
            let req = format!(
                "GET {path} HTTP/1.1\r\nHost: {host}\r\nUser-Agent: BrowserrEngine/0.1\r\nAccept: */*\r\nConnection: close\r\n\r\n"
            );
            tls.write_all(req.as_bytes())
                .map_err(|e| format!("send: {e}"))?;
            Box::new(tls)
        } else {
            let mut tcp = tcp;
            let req = format!(
                "GET {path} HTTP/1.1\r\nHost: {host}\r\nUser-Agent: BrowserrEngine/0.1\r\nAccept: */*\r\nConnection: close\r\n\r\n"
            );
            tcp.write_all(req.as_bytes())
                .map_err(|e| format!("send: {e}"))?;
            Box::new(tcp)
        };

        let (status, location, body) = read_response(stream, max_bytes)?;
        if (301..=308).contains(&status) && status != 306 {
            let loc = location.ok_or("redirect without Location")?;
            current = current
                .join(&loc)
                .map_err(|e| format!("bad redirect {loc}: {e}"))?;
            continue;
        }
        if status != 200 {
            return Err(format!("HTTP {status}"));
        }
        return Ok(body);
    }
    Err("too many redirects".into())
}

/// Read one HTTP response off `stream`: status line + headers, then the body
/// (Content-Length, chunked, or read-to-end), capped at `max_bytes`.
fn read_response(
    mut stream: Box<dyn std::io::Read>,
    max_bytes: u64,
) -> Result<(u16, Option<String>, Vec<u8>), String> {
    let mut reader = std::io::BufReader::new(&mut stream);
    let mut status = 0u16;
    let mut location = None;
    let mut content_len: Option<u64> = None;
    let mut chunked = false;

    // Header section: read line by line until the empty line.
    let mut first = true;
    loop {
        let mut line = Vec::new();
        reader
            .read_until(b'\n', &mut line)
            .map_err(|e| format!("header read: {e}"))?;
        if line.is_empty() {
            return Err("connection closed in headers".into());
        }
        let trimmed = String::from_utf8_lossy(&line);
        let trimmed = trimmed.trim_end();
        if trimmed.is_empty() {
            break; // end of headers
        }
        if first {
            // "HTTP/1.1 200 OK"
            status = trimmed
                .split_whitespace()
                .nth(1)
                .and_then(|s| s.parse().ok())
                .ok_or(format!("bad status line: {trimmed}"))?;
            first = false;
            continue;
        }
        let lower = trimmed.to_ascii_lowercase();
        if let Some(v) = lower.strip_prefix("location:") {
            // Keep the original-case value (the header bytes, not lowered).
            location = Some(trimmed[trimmed.len() - v.trim().len()..].to_string());
        } else if let Some(v) = lower.strip_prefix("content-length:") {
            content_len = v.trim().parse().ok();
        } else if lower.starts_with("transfer-encoding:") && lower.contains("chunked") {
            chunked = true;
        }
        if line.len() > (64 << 10) {
            return Err("headers too large".into());
        }
    }

    let mut body = Vec::new();
    if chunked {
        loop {
            let mut size_line = Vec::new();
            reader
                .read_until(b'\n', &mut size_line)
                .map_err(|e| format!("chunk size read: {e}"))?;
            let size_str = String::from_utf8_lossy(&size_line);
            let size = u64::from_str_radix(size_str.trim().split(';').next().unwrap_or(""), 16)
                .map_err(|_| format!("bad chunk size: {size_str:?}"))?;
            if size == 0 {
                break;
            }
            if body.len() as u64 + size > max_bytes {
                return Err("body too large".into());
            }
            let start = body.len();
            body.resize(start + size as usize, 0);
            reader
                .read_exact(&mut body[start..])
                .map_err(|e| e.to_string())?;
            let mut crlf = [0u8; 2];
            let _ = reader.read_exact(&mut crlf); // chunk trailer
        }
    } else if let Some(n) = content_len {
        let n = n.min(max_bytes);
        let mut chunk = vec![0u8; n as usize];
        reader.read_exact(&mut chunk).map_err(|e| e.to_string())?;
        body = chunk;
    } else {
        reader
            .take(max_bytes)
            .read_to_end(&mut body)
            .map_err(|e| e.to_string())?;
    }
    Ok((status, location, body))
}

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
    eprintln!(
        "engine: downloading adblock filter list -> {}",
        path.display()
    );
    if let Ok(dir) = path.parent().ok_or(()) {
        let _ = std::fs::create_dir_all(dir);
    }
    match http_get(EASYLIST_URL, 32 << 20) {
        Ok(body) => {
            let _ = std::fs::write(path, body);
            eprintln!("engine: filter list saved");
        }
        Err(e) => eprintln!("engine: filter list download failed: {e}"),
    }
}

fn load_adblock(path: &std::path::Path) -> Option<std::rc::Rc<adblock::engine::Engine>> {
    let text = std::fs::read_to_string(path).ok()?;
    eprintln!("engine: adblock enabled");
    Some(std::rc::Rc::new(
        adblock::engine::Engine::new_with_list_text(text),
    ))
}

fn make_context(
    size: dpi::PhysicalSize<u32>,
    force_software: bool,
) -> Option<std::rc::Rc<dyn RenderingContext>> {
    // --software: CPU rendering on request (RPis without GL, containers,
    // machines with a broken driver). Default is still GPU-first with a
    // software fallback for GPU-less CI runners.
    if force_software {
        return match servo::SoftwareRenderingContext::new(size) {
            Ok(c) => {
                eprintln!("engine: software rendering context (--software)");
                Some(std::rc::Rc::new(c))
            }
            Err(e) => {
                eprintln!("engine: no rendering context: {e:?}");
                None
            }
        };
    }
    // GPU-less / driver-less machines (CI runners) can flake on context
    // creation: retry briefly, then fall back to software.
    for attempt in 0..3 {
        match HardwareRenderingContext::new(size) {
            Ok(c) => {
                eprintln!(
                    "engine: hardware (GPU) rendering context (attempt {})",
                    attempt + 1
                );
                return Some(std::rc::Rc::new(c));
            }
            Err(e) => {
                eprintln!(
                    "engine: hardware context failed ({e:?}), attempt {} of 3",
                    attempt + 1
                );
                std::thread::sleep(std::time::Duration::from_millis(250));
            }
        }
    }
    match servo::SoftwareRenderingContext::new(size) {
        Ok(c) => {
            eprintln!("engine: software rendering context");
            Some(std::rc::Rc::new(c))
        }
        Err(e) => {
            eprintln!("engine: no rendering context: {e:?}");
            None
        }
    }
}

/// Map a chrome key event (name, text, modifier bitmask: 1 shift, 2 ctrl,
/// 4 alt, 8 cmd/logo, down state) to a servo input event.
fn map_key(name: &str, text: &str, mods: u8, down: bool) -> Option<servo::InputEvent> {
    use keyboard_types::{Code, Key as K, KeyboardEvent, Modifiers, NamedKey};
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
            let n = other.strip_prefix('F').and_then(|n| n.parse::<u8>().ok())?;
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

/// Remove the ephemeral profile dir (best effort) before an engine exit.
/// Servo's background threads still hold SQLite handles (clientstorage) while
/// the process lives, so the in-process delete can race and fail. Belt and
/// braces: try now, and leave a one-shot scheduled task that waits for our
/// pid to exit and deletes afterwards.
fn cleanup_ephemeral(dir: &Option<std::path::PathBuf>) {
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

fn send_event(out: &mut impl Write, id: u64, json: &str) {
    let bytes = json.as_bytes();
    let mut frame = Vec::with_capacity(20 + bytes.len());
    frame.extend_from_slice(&EV_MAGIC.to_le_bytes());
    frame.extend_from_slice(&id.to_le_bytes());
    frame.extend_from_slice(&(bytes.len() as u32).to_le_bytes());
    frame.extend_from_slice(&0u32.to_le_bytes());
    frame.extend_from_slice(bytes);
    out.write_all(&frame).ok();
    let _ = out.flush();
    // Mirror the event to every attached driver (computer-use mode).
    let hub = DriverHub::global();
    if hub.has_clients() {
        hub.broadcast(&frame);
    }
}

/// Mirror a framed SHT1 screenshot answer to attached drivers.
fn broadcast_shot(bytes: &[u8]) {
    let hub = DriverHub::global();
    if hub.has_clients() {
        hub.broadcast(bytes);
    }
}

fn main() {
    let _ = rustls::crypto::aws_lc_rs::default_provider().install_default();

    // The tab-drop panic (see close_tab) is expected and contained; keep the
    // log clean by not printing that one known message.
    let default_hook = std::panic::take_hook();
    std::panic::set_hook(Box::new(move |info| {
        let msg = info.to_string();
        // Known contained panics from closing a tab: surfman drop assertions
        // (WGL: "destroyed the surface", EGL: "Contexts must be destroyed
        // explicitly with `destroy_context`"). Servo tears GL state down
        // asynchronously, after the context's Drop ran; the process and the
        // other tabs are fine (close_tab catches it).
        let known = msg.contains("destroyed the surface")
            || msg.contains("Contexts must be destroyed explicitly");
        if !known {
            default_hook(info);
        }
    }));

    let mut user_agent = None;
    let mut profile_dir = None;
    let mut adblock_list = None;
    let mut ephemeral = false;
    let mut software = false;
    let mut driver_port = None;
    let mut driver_token = None;
    for a in std::env::args().skip(1) {
        if let Some(v) = a.strip_prefix("--user-agent=") {
            user_agent = Some(v.to_string());
        } else if let Some(v) = a.strip_prefix("--profile-dir=") {
            profile_dir = Some(std::path::PathBuf::from(v));
        } else if let Some(v) = a.strip_prefix("--adblock-list=") {
            adblock_list = Some(std::path::PathBuf::from(v));
        } else if a == "--ephemeral" {
            ephemeral = true;
        } else if a == "--software" {
            software = true;
        } else if let Some(v) = a.strip_prefix("--driver-port=") {
            driver_port = v.parse::<u16>().ok();
        } else if let Some(v) = a.strip_prefix("--driver-token=") {
            driver_token = Some(v.to_string());
        }
    }

    // Ephemeral profiles: a unique throwaway dir under %TEMP%, removed when
    // the engine exits through its normal paths (agents get free per-run
    // isolation). A hard kill or crash can leave it behind; the next run
    // with the same pid and the OS temp cleaner take care of that.
    let ephemeral_dir: Option<std::path::PathBuf> = if ephemeral {
        let dir = std::env::temp_dir().join(format!("browserr-ephemeral-{}", std::process::id()));
        let _ = std::fs::remove_dir_all(&dir); // a crashed predecessor
        Some(dir)
    } else {
        None
    };

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

    // Computer-use mode: accept automation drivers on a second port. Opened
    // AFTER the main client connects (drivers attach to a running engine).
    if let Some(port) = driver_port {
        start_driver_listener(port, driver_token);
    }

    let pump = Pump(Arc::new((Mutex::new(false), Condvar::new())));
    let shared = Arc::new(Shared {
        pump: pump.clone(),
        frame_ready: AtomicBool::new(false),
        active: AtomicU64::new(0),
        updates: Mutex::new(Vec::new()),
        started: Mutex::new(std::collections::HashSet::new()),
        icons: Mutex::new(std::collections::HashMap::new()),
        intercept_on: AtomicBool::new(false),
    });

    let mut opts = servo::Opts::default();
    // Ephemeral wins over an explicit --profile-dir (throwaway wins by intent).
    if let Some(dir) = ephemeral_dir.clone().or(profile_dir) {
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

    // Net evidence: a document-start user script injected into every frame
    // of every tab (a page cannot evade it by grabbing references first).
    // Shared UserContentManager; updates only take effect on reload, but
    // we add scripts before any load happens.
    let ucm = std::rc::Rc::new(servo::UserContentManager::new(&servo));
    ucm.add_script(std::rc::Rc::new(servo::UserScript::new(
        NET_EVIDENCE_JS.to_string(),
        None,
    )));

    let make_tab = |id: u64| -> Option<Tab> {
        let context = make_context(initial_size, software)?;
        let _ = context.make_current();
        let delegate = std::rc::Rc::new(FrameDelegate {
            tab: id,
            shared: shared.clone(),
            adblock: adblock.clone(),
        });
        let webview = servo::WebViewBuilder::new(&servo, context.clone())
            .delegate(delegate)
            .user_content_manager(ucm.clone())
            .build();
        // Complete the webview's registration handshake before any LoadUrl.
        servo.spin_event_loop();
        Some(Tab {
            webview,
            context,
            pending: None,
            pending_sent: false,
            pending_deadline: None,
            pending_retries: 0,
            last_url: None,
            last_title: None,
            last_history: None,
            last_cursor: None,
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
        let _pump = pump.clone();
        std::thread::spawn(move || {
            loop {
                std::thread::sleep(std::time::Duration::from_secs(2));
                eprintln!("engine: watchdog alive");
            }
        });
    }

    // Shared GPU textures: try first; fall back to socket bytes on any
    // failure or if the chrome reports it cannot import them.

    loop {
        reader
            .get_ref()
            .set_read_timeout(Some(std::time::Duration::from_millis(1)))
            .ok();

        loop {
            let mut line = String::new();
            // Driver commands (computer-use mode) drain into the same
            // dispatch as socket lines; the socket itself when none.
            let queued = DriverHub::global().queue.lock().unwrap().pop_front();
            let mut from_driver = false;
            let res = if let Some(q) = queued {
                from_driver = q.starts_with(DRIVER_PREFIX);
                if from_driver {
                    line.push_str(&q[DRIVER_PREFIX.len()..]);
                } else {
                    line.push_str(&q);
                }
                Ok(1_usize)
            } else {
                reader.read_line(&mut line)
            };
            match res {
                Ok(0) => {
                    eprintln!("engine: chrome disconnected");
                    // Exit without running destructors: dropping live tabs would
                    // trip the surfman surface assertion (cosmetic noise).
                    cleanup_ephemeral(&ephemeral_dir);
                    hard_exit();
                }
                Ok(_) => {
                    let line = line.trim();
                    if line.is_empty() {
                        break;
                    }
                    // Hardening: a command line larger than 8 MB is not a
                    // legitimate command (the biggest legitimate payload is
                    // an eval script); drop it instead of parsing (a hostile
                    // local process could otherwise balloon memory).
                    if line.len() > 8 << 20 {
                        eprintln!("engine: dropping oversized command ({} bytes)", line.len());
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
                                    if let std::collections::hash_map::Entry::Vacant(e) =
                                        tabs.entry(tab_id)
                                    {
                                        match make_tab(tab_id) {
                                            Some(t) => {
                                                e.insert(t);
                                            }
                                            None => {
                                                eprintln!(
                                                    "engine: make_tab {tab_id} failed (context?) - ignoring command"
                                                );
                                                continue;
                                            }
                                        }
                                    }
                                    let tab = tabs.get_mut(&tab_id).unwrap();
                                    tab.pending = Some(url);
                                    tab.pending_sent = false;
                                }
                            }
                            Some("new_tab") => {
                                if let std::collections::hash_map::Entry::Vacant(e) =
                                    tabs.entry(tab_id)
                                {
                                    eprintln!("engine: new tab {tab_id}");
                                    match make_tab(tab_id) {
                                        Some(t) => {
                                            e.insert(t);
                                        }
                                        None => {
                                            eprintln!(
                                                "engine: make_tab {tab_id} failed (context?) - ignoring command"
                                            );
                                            continue;
                                        }
                                    }
                                }
                            }
                            Some("activate") => {
                                if let std::collections::hash_map::Entry::Vacant(e) =
                                    tabs.entry(tab_id)
                                {
                                    match make_tab(tab_id) {
                                        Some(t) => {
                                            e.insert(t);
                                        }
                                        None => {
                                            eprintln!(
                                                "engine: make_tab {tab_id} failed (context?) - ignoring command"
                                            );
                                            continue;
                                        }
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
                                        &format!(
                                            "{{\"ev\":\"url\",\"tab\":{tab_id},\"url\":\"{u}\"}}"
                                        ),
                                    );
                                }
                                if let Some(t) = &tab.last_title {
                                    event_id += 1;
                                    let esc = t.replace('\\', "\\\\").replace('"', "\\\"");
                                    send_event(
                                        &mut out,
                                        event_id,
                                        &format!(
                                            "{{\"ev\":\"title\",\"tab\":{tab_id},\"title\":\"{esc}\"}}"
                                        ),
                                    );
                                }
                                if let Some((b, f)) = tab.last_history {
                                    event_id += 1;
                                    send_event(
                                        &mut out,
                                        event_id,
                                        &format!(
                                            "{{\"ev\":\"history\",\"tab\":{tab_id},\"back\":{b},\"forward\":{f}}}"
                                        ),
                                    );
                                }
                                if let Some(c) = &tab.last_cursor {
                                    event_id += 1;
                                    send_event(
                                        &mut out,
                                        event_id,
                                        &format!(
                                            "{{\"ev\":\"cursor\",\"tab\":{tab_id},\"cursor\":\"{c}\"}}"
                                        ),
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
                                    let _ = std::panic::catch_unwind(std::panic::AssertUnwindSafe(
                                        || drop(tab),
                                    ));
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
                                // chrome connects. With an "id" the result is
                                // reported back as an evalresult event
                                // (automation driver); without one it is
                                // discarded (password autofill).
                                if let Some(js) = v["js"].as_str()
                                    && let Some(tab) = tabs.get(&tab_id)
                                {
                                    eprintln!("engine: eval (tab {tab_id})");
                                    if let Some(req) = v["id"].as_u64() {
                                        let shared = shared.clone();
                                        tab.webview.evaluate_javascript(
                                            js.to_string(),
                                            move |result| {
                                                let value = jsvalue_to_json(result);
                                                shared.updates.lock().unwrap().push(Update {
                                                    tab: tab_id,
                                                    url: None,
                                                    title: None,
                                                    history: None,
                                                    pw: None,
                                                    icon: None,
                                                    cursor: None,
                                                    eval: Some((req, value)),
                                                    console: None,
                                                    intercept: None,
                                                    auth: None,
                                                    filepicker: None,
                                                });
                                                shared.pump.kick();
                                            },
                                        );
                                    } else {
                                        tab.webview.evaluate_javascript(js.to_string(), |_| {});
                                    }
                                    shared.pump.kick();
                                }
                            }
                            Some("screenshot") => {
                                // Automation driver: paint the tab and return its
                                // pixels as a SHT1 frame (BGRA).
                                if let Some(tab) = tabs.get(&tab_id) {
                                    eprintln!("engine: screenshot (tab {tab_id})");
                                    tab.webview.paint();
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
                                        for px in bgra.as_chunks_mut::<4>().0 {
                                            px.swap(0, 2);
                                        }
                                        frame_id += 1;
                                        let mut frame = Vec::with_capacity(20 + bgra.len());
                                        frame.extend_from_slice(&SHOT_MAGIC.to_le_bytes());
                                        frame.extend_from_slice(&frame_id.to_le_bytes());
                                        frame.extend_from_slice(&current_size.width.to_le_bytes());
                                        frame.extend_from_slice(&current_size.height.to_le_bytes());
                                        frame.extend_from_slice(&bgra);
                                        // Answer the REQUESTER only. Writing the
                                        // multi-MB frame to the main socket
                                        // when a driver asked wedges the main
                                        // loop if that socket is not drained
                                        // (the attach test holds it unread);
                                        // the mirrored write has a timeout.
                                        if from_driver {
                                            broadcast_shot(&frame);
                                        } else {
                                            out.write_all(&frame).ok();
                                            let _ = out.flush();
                                        }
                                    }
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
                            Some("ping") => {
                                // Health check for automation drivers: answered
                                // inline with an event, no page involvement.
                                let req = v["id"].as_u64().unwrap_or(0);
                                let tabs_n = tabs.len();
                                let active = shared.active.load(Ordering::SeqCst);
                                event_id += 1;
                                send_event(
                                    &mut out,
                                    event_id,
                                    &format!(
                                        "{{\"ev\":\"pong\",\"tab\":{tab_id},\"id\":{req},\"tabs\":{tabs_n},\"active\":{active}}}"
                                    ),
                                );
                            }
                            Some("frames") => {
                                // Tier-1 frame control: the page's frame tree
                                // (same-origin walking; cross-origin frames
                                // report url/origin only - servo 0.8 has no
                                // cross-frame JS entry point). Answered as
                                // an evalresult-style event with the tree JSON.
                                let req = v["id"].as_u64().unwrap_or(0);
                                if let Some(tab) = tabs.get(&tab_id) {
                                    eprintln!("engine: frames (tab {tab_id})");
                                    let shared = shared.clone();
                                    tab.webview.evaluate_javascript(
                                        FRAME_TREE_JS.to_string(),
                                        move |result| {
                                            let value = jsvalue_to_json(result);
                                            shared.updates.lock().unwrap().push(Update {
                                                tab: tab_id,
                                                url: None,
                                                title: None,
                                                history: None,
                                                pw: None,
                                                icon: None,
                                                cursor: None,
                                                eval: Some((req, value)),
                                                console: None,
                                                intercept: None,
                                                auth: None,
                                                filepicker: None,
                                            });
                                            shared.pump.kick();
                                        },
                                    );
                                }
                            }
                            Some("download") => {
                                // Fetch a URL straight to disk (agents fetch
                                // files; servo 0.8 has no download hook).
                                // No page cookies: only use it for public
                                // files. Answered with an event.
                                let req = v["id"].as_u64().unwrap_or(0);
                                let url = v["url"].as_str().unwrap_or("");
                                let path = v["path"].as_str().unwrap_or("");
                                let max = v["max_bytes"].as_u64().unwrap_or(64 << 20);
                                eprintln!("engine: download {url} -> {path}");
                                let result = if url.is_empty() || path.is_empty() {
                                    Err("missing url or path".to_string())
                                } else {
                                    http_get(url, max).and_then(|body| {
                                        if let Some(dir) = std::path::Path::new(path).parent()
                                            && !dir.as_os_str().is_empty()
                                        {
                                            let _ = std::fs::create_dir_all(dir);
                                        }
                                        let n = body.len() as u64;
                                        std::fs::write(path, body)
                                            .map(|_| n)
                                            .map_err(|e| e.to_string())
                                    })
                                };
                                event_id += 1;
                                let payload = match result {
                                    Ok(n) => format!(
                                        "{{\"ev\":\"download\",\"tab\":{tab_id},\"id\":{req},\"path\":\"{}\",\"ok\":true,\"bytes\":{n}}}",
                                        path.replace('\\', "\\\\").replace('"', "\\\"")
                                    ),
                                    Err(e) => format!(
                                        "{{\"ev\":\"download\",\"tab\":{tab_id},\"id\":{req},\"ok\":false,\"error\":\"{}\"}}",
                                        e.replace('\\', "\\\\").replace('"', "\\\"")
                                    ),
                                };
                                send_event(&mut out, event_id, &payload);
                            }
                            Some("zoom") => {
                                // Page zoom (Ctrl+wheel in the chrome); overrides
                                // the webview's current page zoom.
                                if let Some(z) = v["zoom"].as_f64()
                                    && let Some(tab) = tabs.get(&tab_id)
                                {
                                    let z = (z as f32).clamp(0.25, 3.0);
                                    eprintln!("engine: zoom (tab {tab_id}) {z}");
                                    tab.webview.set_page_zoom(z);
                                    shared.pump.kick();
                                }
                            }
                            Some("intercept") => {
                                // Arm/disarm fail-closed interception. Arming
                                // affects future loads only; already-held
                                // requests still need answers (or time out).
                                let on = v["on"].as_bool().unwrap_or(false);
                                eprintln!("engine: intercept {on} (tab {tab_id})");
                                shared.intercept_on.store(on, Ordering::SeqCst);
                            }
                            Some("request") => {
                                // Driver decision for a held request.
                                let Some(req) = v["id"].as_u64() else {
                                    continue;
                                };
                                eprintln!("engine: request decision (id {req})");
                                ANSWERS.with(|a| a.borrow_mut().insert(req, v.clone()));
                                shared.pump.kick();
                            }
                            Some("auth") => {
                                // Driver credentials for an auth challenge.
                                let Some(req) = v["id"].as_u64() else {
                                    continue;
                                };
                                eprintln!("engine: auth answer (id {req})");
                                let creds = match (v["user"].as_str(), v["pass"].as_str()) {
                                    (Some(u), Some(p)) => Some((u.to_string(), p.to_string())),
                                    // Missing credentials = fail closed.
                                    _ => None,
                                };
                                AUTH_ANSWERS.with(|a| a.borrow_mut().insert(req, creds));
                                shared.pump.kick();
                            }
                            Some("files") => {
                                // Driver answer for a file picker.
                                let Some(req) = v["id"].as_u64() else {
                                    continue;
                                };
                                eprintln!("engine: files answer (id {req})");
                                let paths = v["paths"].as_array().map(|a| {
                                    a.iter()
                                        .filter_map(|p| p.as_str())
                                        .map(std::path::PathBuf::from)
                                        .collect::<Vec<_>>()
                                });
                                FILE_ANSWERS.with(|a| a.borrow_mut().insert(req, paths));
                                shared.pump.kick();
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
                            _ => {}
                        }
                    }
                }
                Err(e)
                    if e.kind() == std::io::ErrorKind::WouldBlock
                        || e.kind() == std::io::ErrorKind::TimedOut =>
                {
                    break;
                }
                Err(_) => {
                    cleanup_ephemeral(&ephemeral_dir);
                    hard_exit();
                }
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
                    tab.pending_deadline =
                        Some(std::time::Instant::now() + std::time::Duration::from_secs(2));
                } else if shared.started.lock().unwrap().contains(id)
                    || tab.webview.url().map(|v| v == u).unwrap_or(false)
                {
                    // Started fired, or the webview's url already matches the
                    // pending load: it was received, not dropped. (Checking the
                    // url avoids false positives from the initial about:blank.)
                    tab.pending = None;
                    tab.pending_sent = false;
                    tab.pending_retries = 0;
                } else if tab
                    .pending_deadline
                    .is_some_and(|d| std::time::Instant::now() > d)
                {
                    if tab.pending_retries < 3 {
                        eprintln!("engine: load (tab {id}) not started, retrying");
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

        // Held interception requests: apply driver answers; deny on timeout.
        // Servo's request_interceptor blocks a net-pool worker per pending
        // decision, so this sweep runs every tick and held requests always
        // resolve within the timeout below (deny = fail closed).
        // All maps are thread-locals: the responder types are !Send and both
        // this sweep and the delegate callbacks run on the servo main thread.
        PENDING_LOADS.with(|pending| {
            let mut pending = pending.borrow_mut();
            let answers: Vec<(u64, serde_json::Value)> = ANSWERS
                .with(|a| std::mem::take(&mut *a.borrow_mut()))
                .into_iter()
                .collect();
            for (id, answer) in answers {
                let Some(p) = pending.remove(&id) else {
                    continue;
                };
                let action = answer["action"].as_str().unwrap_or("deny");
                match action {
                    "allow" => {
                        // Drop the load: its Drop sends DoNotIntercept and
                        // servo proceeds with the original network load.
                        eprintln!("engine: intercept allow (id {id})");
                        drop(p.load);
                    }
                    "respond" => {
                        eprintln!("engine: intercept respond (id {id})");
                        let status = answer["status"].as_u64().unwrap_or(200) as u16;
                        let body = answer["body"].as_str().unwrap_or("").as_bytes().to_vec();
                        let ct = answer["content_type"].as_str().unwrap_or("text/plain");
                        let response_url = p.load.request().url.clone().into_url();
                        let mut response = servo::WebResourceResponse::new(response_url);
                        let mut headers = http::HeaderMap::new();
                        if let Ok(v) = http::HeaderValue::from_str(ct) {
                            headers.insert(http::header::CONTENT_TYPE, v);
                        }
                        response.headers = headers;
                        response.status_code = http::StatusCode::from_u16(status)
                            .unwrap_or(http::StatusCode::FORBIDDEN);
                        let mut held = p.load.intercept(response);
                        held.send_body_data(body);
                        held.finish();
                    }
                    // "deny" and anything unrecognized: fail closed with a
                    // synthetic 403 (cancel() on a document wedges the tab).
                    _ => {
                        eprintln!("engine: intercept deny (id {id})");
                        let response_url = p.load.request().url.clone().into_url();
                        let mut response = servo::WebResourceResponse::new(response_url);
                        response.status_code = http::StatusCode::FORBIDDEN;
                        let mut held = p.load.intercept(response);
                        held.send_body_data(b"blocked by driver".to_vec());
                        held.finish();
                    }
                }
            }
            // Timeout: unresolved holds deny after 5s (fail closed).
            let expired: Vec<u64> = pending
                .iter()
                .filter(|(_, p)| p.since.elapsed() > std::time::Duration::from_secs(5))
                .map(|(id, _)| *id)
                .collect();
            for id in expired {
                if let Some(p) = pending.remove(&id) {
                    eprintln!("engine: intercept timeout, denying (id {id})");
                    let response_url = p.load.request().url.clone().into_url();
                    let mut response = servo::WebResourceResponse::new(response_url);
                    response.status_code = http::StatusCode::FORBIDDEN;
                    let mut held = p.load.intercept(response);
                    held.send_body_data(b"blocked (timeout)".to_vec());
                    held.finish();
                }
            }
        });
        // Auth challenges: apply queued credentials; drop (no creds)
        // after 10s. Main-frame navigations only in servo 0.8.
        AUTH_HOLDS.with(|holds| {
            let mut holds = holds.borrow_mut();
            let answers: Vec<(u64, Option<(String, String)>)> = AUTH_ANSWERS
                .with(|a| std::mem::take(&mut *a.borrow_mut()))
                .into_iter()
                .collect();
            for (id, creds) in answers {
                if let Some(hold) = holds.remove(&id) {
                    eprintln!("engine: auth answered (id {id})");
                    match creds {
                        Some((user, pass)) => hold.request.authenticate(user, pass),
                        // No credentials: drop = fail closed.
                        None => drop(hold),
                    }
                }
            }
            let expired: Vec<u64> = holds
                .iter()
                .filter(|(_, h)| h.since.elapsed() > std::time::Duration::from_secs(10))
                .map(|(id, _)| *id)
                .collect();
            for id in expired {
                if let Some(hold) = holds.remove(&id) {
                    eprintln!("engine: auth timeout, no credentials (id {id})");
                    drop(hold);
                }
            }
        });
        // File pickers: apply queued paths; dismiss after 60s.
        FILE_HOLDS.with(|holds| {
            let mut holds = holds.borrow_mut();
            let answers: Vec<(u64, Option<Vec<std::path::PathBuf>>)> = FILE_ANSWERS
                .with(|a| std::mem::take(&mut *a.borrow_mut()))
                .into_iter()
                .collect();
            for (id, paths) in answers {
                if let Some(mut hold) = holds.remove(&id) {
                    eprintln!("engine: files answered (id {id})");
                    match paths {
                        Some(paths) if !paths.is_empty() => {
                            hold.picker.select(&paths);
                            hold.picker.submit();
                        }
                        _ => {
                            hold.picker.dismiss();
                        }
                    }
                }
            }
            let expired: Vec<u64> = holds
                .iter()
                .filter(|(_, h)| h.since.elapsed() > std::time::Duration::from_secs(60))
                .map(|(id, _)| *id)
                .collect();
            for id in expired {
                if let Some(hold) = holds.remove(&id) {
                    eprintln!("engine: file picker timeout, dismissing (id {id})");
                    hold.picker.dismiss();
                }
            }
        });

        servo.spin_event_loop();

        tick += 1;
        if tick.is_multiple_of(60) {
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
                    if let Some(c) = &u.cursor {
                        t.last_cursor = Some(c.clone());
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
                        &format!(
                            "{{\"ev\":\"history\",\"tab\":{},\"back\":{},\"forward\":{}}}",
                            u.tab, b, f
                        ),
                    );
                }
                if let Some(c) = &u.cursor {
                    send_event(
                        &mut out,
                        event_id,
                        &format!(
                            "{{\"ev\":\"cursor\",\"tab\":{},\"cursor\":\"{}\"}}",
                            u.tab, c
                        ),
                    );
                }
                if let Some((req, value)) = &u.eval {
                    send_event(
                        &mut out,
                        event_id,
                        &format!(
                            "{{\"ev\":\"evalresult\",\"tab\":{},\"id\":{},\"value\":{}}}",
                            u.tab, req, value
                        ),
                    );
                }
                if let Some((level, message)) = &u.console {
                    let esc = message
                        .replace('\\', "\\\\")
                        .replace('"', "\\\"")
                        .replace('\n', "\\n")
                        .replace('\r', "\\r");
                    send_event(
                        &mut out,
                        event_id,
                        &format!(
                            "{{\"ev\":\"console\",\"tab\":{},\"level\":\"{}\",\"message\":\"{esc}\"}}",
                            u.tab, level
                        ),
                    );
                }
                if let Some((id, method, url, main)) = &u.intercept {
                    let esc = url
                        .replace('\\', "\\\\")
                        .replace('"', "\\\"")
                        .replace('\n', "\\n");
                    send_event(
                        &mut out,
                        event_id,
                        &format!(
                            "{{\"ev\":\"request\",\"tab\":{},\"id\":{},\"method\":\"{method}\",\"url\":\"{esc}\",\"main\":{}}}",
                            u.tab, id, main
                        ),
                    );
                }
                if let Some((id, url, proxy)) = &u.auth {
                    let esc = url
                        .replace('\\', "\\\\")
                        .replace('"', "\\\"")
                        .replace('\n', "\\n");
                    send_event(
                        &mut out,
                        event_id,
                        &format!(
                            "{{\"ev\":\"auth\",\"tab\":{},\"id\":{},\"url\":\"{esc}\",\"proxy\":{}}}",
                            u.tab, id, proxy
                        ),
                    );
                }
                if let Some((id, multiple)) = &u.filepicker {
                    send_event(
                        &mut out,
                        event_id,
                        &format!(
                            "{{\"ev\":\"filepicker\",\"tab\":{},\"id\":{},\"multiple\":{}}}",
                            u.tab, id, multiple
                        ),
                    );
                }
                if let Some(data) = &u.pw {
                    // data is already valid JSON ({u,p,o}); embed it directly.
                    send_event(
                        &mut out,
                        event_id,
                        &format!(
                            "{{\"ev\":\"pwcapture\",\"tab\":{},\"data\":{}}}",
                            u.tab, data
                        ),
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
                    for px in bgra.as_chunks_mut::<4>().0 {
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

                        // Headless SDK consumers read pixels via screenshot
                        // (SHT1) on demand; nothing drains a continuous
                        // FRM1 stream anymore (the chrome UI is gone), and
                        // pushing megabytes to an unread socket wedges the
                        // main loop once the TCP window fills (seen on
                        // Linux). Announce the frame as a small event
                        // instead; the pixels stay in the engine until a
                        // screenshot asks for them.
                        event_id += 1;
                        send_event(
                            &mut out,
                            event_id,
                            &format!(
                                "{{\"ev\":\"frame\",\"tab\":{active},\"w\":{},\"h\":{}}}",
                                current_size.width, current_size.height
                            ),
                        );
                    }
                }
            }
        }

        pump.wait(std::time::Duration::from_millis(16));
    }
}
