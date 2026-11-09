use rune::tok::{self, type_};
use rune::{App, AppSpec, Color, Cx, El, Icon, NativeSurface, ScrollSpec, SurfaceFit, TextEdit, Tx, WindowOptions, col, el, icon, row, spacer, text, text_input};

mod engine;
mod creds;
mod store;

use engine::{Engine, Ui};
use std::process::Command;
use std::sync::Arc;

const BAR_H: f32 = 40.0;

struct Tab {
    id: u64,
    url: String,
    title: String,
}

struct State {
    tabs: Vec<Tab>,
    active: usize,
    address: TextEdit,
    editing: bool,
    engine: Option<Engine>,
    surface: NativeSurface,
    last_size: Option<(u32, u32)>,
    next_tab_id: u64,
    /// Engine-reported UI state shared with the frame reader thread.
    ui: Arc<Ui>,
    /// Saved bookmarks: (url, title).
    bookmarks: Vec<(String, String)>,
    /// Whether the bookmarks/history panel is open.
    menu_open: bool,
    /// History urls shown in the panel (refreshed when it opens).
    history_view: Vec<String>,
    /// True when no --profile was given: this window is the profile picker.
    picker: bool,
    /// Known profile names (picker mode only).
    profiles: Vec<String>,
    /// New-profile name input (picker mode only).
    profile_input: TextEdit,
    /// Current settings (loaded at startup, saved from the menu panel).
    settings: store::Settings,
    /// Editable copies of the text settings for the panel.
    settings_home: TextEdit,
    settings_ua: TextEdit,
    /// Password capture fields for the current host (menu panel).
    cred_user: TextEdit,
    /// Masked password input (secure mode).
    cred_pass: TextEdit,
    /// Whether a password is already saved for the current host.
    cred_saved: bool,
    /// Host whose capture was already saved or dismissed.
    pw_handled: Option<String>,
    /// True while the full settings page is open (replaces the browser view).
    settings_open: bool,
    /// Saved (host, username) pairs shown on the settings page.
    creds_view: Vec<(String, String)>,
    /// User agent and adblock flag the running engine was started with, so
    /// saves that change them can restart the engine in place.
    engine_ua: String,
    engine_adblock: bool,
}

impl Default for Tab {
    fn default() -> Self {
        Self { id: 0, url: String::new(), title: "New Tab".into() }
    }
}

pub fn host(url: &str) -> String {
    crate::store::host(url)
}

/// Minimal percent-encoding for search query strings.
fn urlencode(s: &str) -> String {
    let mut out = String::new();
    for b in s.bytes() {
        match b {
            b'A'..=b'Z' | b'a'..=b'z' | b'0'..=b'9' | b'-' | b'_' | b'.' | b'~' => {
                out.push(b as char)
            }
            _ => out.push_str(&format!("%{b:02X}")),
        }
    }
    out
}

/// Search presets cycled by the settings panel button; "{}" is the query.
const SEARCHES: [&str; 3] = [
    "https://duckduckgo.com/?q={}",
    "https://www.google.com/search?q={}",
    "https://www.bing.com/search?q={}",
];

/// Short label for a search template ("https://www.google.com/..." -> "google.com").
fn search_name(t: &str) -> String {
    let h = crate::store::host(t);
    h.strip_prefix("www.").unwrap_or(&h).to_string()
}

fn state() -> State {
    // No --profile argument: this window is the profile picker, not a browser.
    let picker = !store::profile_selected();
    let settings = store::load_settings();
    let settings_home = TextEdit::new(settings.homepage.clone());
    let settings_ua = TextEdit::new(settings.user_agent.clone());
    // Restore the last session, or start with a single empty tab.
    let (tabs, active, next_id) = if picker {
        (vec![Tab::default()], 0, 1)
    } else {
        match store::load_session() {
            Some(s) => {
                let tabs: Vec<Tab> = s.urls.iter().enumerate()
                    .map(|(i, u)| Tab { id: i as u64, url: u.clone(), title: host(u) })
                    .collect();
                (tabs, s.active, s.urls.len() as u64)
            }
            None => (vec![Tab::default()], 0, 1),
        }
    };
    State {
        tabs,
        active,
        address: TextEdit::new(""),
        editing: false,
        engine: None,
        surface: NativeSurface::new(),
        last_size: None,
        next_tab_id: next_id,
        ui: Ui::new(active as u64),
        bookmarks: store::load_bookmarks(),
        menu_open: false,
        history_view: Vec::new(),
        picker,
        profiles: if picker { store::list_profiles() } else { Vec::new() },
        profile_input: TextEdit::new(""),
        settings,
        settings_home,
        settings_ua,
        cred_user: TextEdit::new(""),
        cred_pass: TextEdit::new("").secure(),
        cred_saved: false,
        pw_handled: None,
        settings_open: false,
        creds_view: Vec::new(),
        engine_ua: String::new(),
        engine_adblock: false,
    }
}

/// Load the current host's saved password state into the panel fields.
fn refresh_creds(s: &mut State) {
    let url = s.ui.url(s.tabs[s.active].id).unwrap_or_default();
    let host = host(&url);
    match creds::load(&host) {
        Some((user, _)) => {
            s.cred_user = TextEdit::new(user);
            s.cred_pass = TextEdit::new("").secure();
            s.cred_saved = true;
        }
        None => {
            s.cred_user = TextEdit::new("");
            s.cred_pass = TextEdit::new("").secure();
            s.cred_saved = false;
        }
    }
}

/// Fill the page's login form via the engine's eval command.
fn fill_password(s: &mut State) {
    let url = s.ui.url(s.tabs[s.active].id).unwrap_or_default();
    let host = host(&url);
    if host.is_empty() {
        return;
    }
    if let Some((user, pass)) = creds::load(&host) {
        let tab_id = s.tabs[s.active].id;
        if let Some(engine) = &mut s.engine {
            engine.eval(tab_id, &fill_js(&user, &pass));
        }
    }
}

/// JS that fills the first username-ish input and the first password input.
/// Dispatches input/change events so framework listeners notice.
fn fill_js(user: &str, pass: &str) -> String {
    let u = serde_json::to_string(user).unwrap_or_else(|_| "\"\"".into());
    let p = serde_json::to_string(pass).unwrap_or_else(|_| "\"\"".into());
    format!(
        r#"(()=>{{const u={u},p={p};const set=(i,v)=>{{i.value=v;i.dispatchEvent(new Event('input',{{bubbles:true}}));i.dispatchEvent(new Event('change',{{bubbles:true}}));}};const pw=document.querySelector('input[type=password]');if(pw)set(pw,p);const re=/(user|email|login|account)/i;for(const t of document.querySelectorAll('input')){{if(t.type==='password')continue;const k=(t.name||'')+' '+(t.id||'')+' '+(t.type||'')+' '+(t.autocomplete||'');if((!t.type||t.type==='text'||t.type==='email')&&re.test(k)){{set(t,u);break;}}}}}})()"#
    )
}

/// Kill and respawn the engine (settings that need a new process), then
/// re-open every tab on the fresh engine.
fn restart_engine(s: &mut State) {
    let Some(e) = &mut s.engine else { return };
    if e.restart().is_err() {
        eprintln!("chrome: engine restart failed");
        return;
    }
    let (w, h) = s.last_size.unwrap_or((1200, 760));
    e.resize(w, h);
    // Prefer the engine-reported url; fall back to the chrome-side record.
    let tabs: Vec<(u64, String)> = s.tabs.iter().map(|t| {
        let url = s.ui.url(t.id).filter(|u| !u.is_empty())
            .unwrap_or_else(|| t.url.clone());
        (t.id, url)
    }).collect();
    for (id, url) in tabs {
        if !url.is_empty() {
            e.navigate(&url, id);
        }
    }
    e.activate(s.tabs[s.active].id);
    s.engine_ua = s.settings.user_agent.clone();
    s.engine_adblock = s.settings.adblock;
}

/// Pull the panel's TextEdits into s.settings and persist them. Restarts the
/// engine in place if the user agent or adblock flag changed.
fn save_settings_ui(s: &mut State) {
    s.settings.homepage = s.settings_home.text.trim().to_string();
    s.settings.user_agent = s.settings_ua.text.clone();
    store::save_settings(&s.settings);
    if s.engine_ua != s.settings.user_agent || s.engine_adblock != s.settings.adblock {
        restart_engine(s);
    }
}

/// Open a new chrome window for `name` and close the picker.
fn launch_profile(tx: &mut Tx, name: &str) {
    if let Ok(exe) = std::env::current_exe() {
        if Command::new(exe).arg(format!("--profile={name}")).spawn().is_ok() {
            tx.close_window();
        }
    }
}

fn save_session(s: &State) {
    let urls: Vec<String> = s.tabs.iter().map(|t| t.url.clone()).collect();
    store::save_session(&urls, s.active);
}

/// Navigate the active tab (address bar, bookmarks, history all land here).
fn go(s: &mut State, url: String) {
    let tab_id = s.tabs[s.active].id;
    s.tabs[s.active].url = url.clone();
    s.tabs[s.active].title = host(&url);
    if let Some(engine) = &mut s.engine {
        engine.navigate(&url, tab_id);
    }
    save_session(s);
}

fn setup(s: &mut State, wake: rune::Waker) {
    s.surface.set_waker(wake.clone());
    if s.picker {
        return; // profile picker: no engine
    }
    let user_agent = s.settings.user_agent.clone();
    let adblock_list = if s.settings.adblock {
        store::data_dir().join("easylist.txt").to_string_lossy().to_string()
    } else {
        String::new()
    };
    let profile_dir = store::data_dir().join("engine");
    match Engine::start(
        s.surface.clone(),
        wake,
        s.ui.clone(),
        &user_agent,
        &profile_dir.to_string_lossy(),
        &adblock_list,
    ) {
        Ok(mut e) => {
            s.engine_ua = user_agent.clone();
            s.engine_adblock = s.settings.adblock;
            // Restore open tabs: navigate() creates missing engine tabs on demand.
            let active_id = s.tabs[s.active].id;
            let restored: Vec<(u64, String)> = s.tabs.iter()
                .map(|t| (t.id, t.url.clone()))
                .filter(|(_, u)| !u.is_empty())
                .collect();
            if !restored.is_empty() {
                for (id, url) in restored {
                    e.navigate(&url, id);
                }
                e.activate(active_id);
                s.ui.active.store(active_id, std::sync::atomic::Ordering::SeqCst);
            } else if !s.settings.homepage.is_empty() {
                e.navigate(&s.settings.homepage, 0);
            }
            s.engine = Some(e);
        }
        Err(err) => eprintln!("chrome: engine failed to start: {err}"),
    }
}

static ICON_BACK: Icon = Icon {
    name: "br-back",
    view: 16.0,
    body: r#"<path d="M10 3.5L5.5 8L10 12.5" stroke="currentColor" stroke-width="1.3" stroke-linecap="round" stroke-linejoin="round" fill="none"/>"#,
};
static ICON_FWD: Icon = Icon {
    name: "br-fwd",
    view: 16.0,
    body: r#"<path d="M6 3.5L10.5 8L6 12.5" stroke="currentColor" stroke-width="1.3" stroke-linecap="round" stroke-linejoin="round" fill="none"/>"#,
};
static ICON_PLUS: Icon = Icon {
    name: "br-plus",
    view: 16.0,
    body: r#"<path d="M8 3.5V12.5M3.5 8H12.5" stroke="currentColor" stroke-width="1.3" stroke-linecap="round" fill="none"/>"#,
};
static ICON_HAMBURGER: Icon = Icon {
    name: "br-hamburger",
    view: 16.0,
    body: r#"<path d="M3 4.5H13M3 8H13M3 11.5H13" stroke="currentColor" stroke-width="1.3" stroke-linecap="round" fill="none"/>"#,
};
static ICON_WIN_MIN: Icon = Icon {
    name: "br-win-min",
    view: 16.0,
    body: r#"<path d="M4.5 5.5H11.5L8 10.5Z" stroke="currentColor" stroke-width="1.2" stroke-linejoin="round" fill="none"/>"#,
};
static ICON_WIN_MAX: Icon = Icon {
    name: "br-win-max",
    view: 16.0,
    body: r#"<path d="M4.5 10.5H11.5L8 5.5Z" stroke="currentColor" stroke-width="1.2" stroke-linejoin="round" fill="none"/>"#,
};
static ICON_WIN_CLOSE: Icon = Icon {
    name: "br-win-close",
    view: 16.0,
    body: r#"<path d="M5.5 10.5H10.5V5.5Z" stroke="currentColor" stroke-width="1.2" stroke-linejoin="round" fill="none"/>"#,
};
static ICON_CLOSE: Icon = Icon {
    name: "br-tab-close",
    view: 16.0,
    body: r#"<path d="M5 5L11 11M11 5L5 11" stroke="currentColor" stroke-width="1.3" stroke-linecap="round" fill="none"/>"#,
};
static ICON_STAR: Icon = Icon {
    name: "br-star",
    view: 16.0,
    body: r#"<path d="M8 2.8l1.6 3.3 3.6.5-2.6 2.5.6 3.6L8 11l-3.2 1.7.6-3.6L2.8 6.6l3.6-.5z" stroke="currentColor" stroke-width="1.2" stroke-linejoin="round" fill="none"/>"#,
};
static ICON_STAR_FILLED: Icon = Icon {
    name: "br-star-filled",
    view: 16.0,
    body: r#"<path d="M8 2.8l1.6 3.3 3.6.5-2.6 2.5.6 3.6L8 11l-3.2 1.7.6-3.6L2.8 6.6l3.6-.5z" fill="currentColor"/>"#,
};
static ICON_KEY: Icon = Icon {
    name: "br-key",
    view: 16.0,
    body: r#"<circle cx="5.5" cy="10.5" r="2.7" stroke="currentColor" stroke-width="1.3" fill="none"/><path d="M7.5 8.5L13 3M10.5 3.5L12.5 5.5" stroke="currentColor" stroke-width="1.3" stroke-linecap="round" fill="none"/>"#,
};
static ICON_EYE: Icon = Icon {
    name: "br-eye",
    view: 16.0,
    body: r#"<path d="M2.5 8s2-4 5.5-4 5.5 4 5.5 4-2 4-5.5 4S2.5 8 2.5 8z" stroke="currentColor" stroke-width="1.2" fill="none"/><circle cx="8" cy="8" r="1.6" fill="currentColor"/>"#,
};

fn icon_btn(id: &'static str, ic: &'static Icon, enabled: bool, action: Option<impl Fn(&mut State) + 'static>) -> El {
    let t = tok::theme(true);
    let e = el().key(id).size(28.0, 28.0).radius(14.0).center_y().child(icon(ic, 16.0));
    match (enabled, action) {
        (true, Some(action)) => e.fill(Color::TRANSPARENT).hover(t.hover).on_press(action),
        _ => e.layer_opacity(0.35),
    }
}

/// Windows-style window control: outline triangle glyph, hover highlight.
fn win_btn(key: &'static str, ic: &'static Icon, action: impl Fn(&mut State, &mut Tx) + 'static) -> El {
    let t = tok::theme(true);
    el().key(key).w(32.0).h(24.0).center().radius(6.0)
        .fill(Color::TRANSPARENT).hover(t.hover).press(t.hover)
        .on_press_tx(action)
        .child(icon(ic, 12.0))
}

fn text_btn(id: &'static str, label: String, action: impl Fn(&mut State) + 'static) -> El {
    let t = tok::theme(true);
    el().key(id).px(12.0).py(4.0).radius(14.0).fill(t.hover)
        .on_press(action)
        .child(text(label).font(type_::BODY.lh(18.0)).color(t.text))
}

fn view(s: &State, _: &mut Cx<State>) -> El {
    let t = tok::theme(true);

    // Full settings page: opened from the menu, closed by its back button.
    if s.settings_open {
        let label = |txt: String| text(txt).font(type_::BODY.lh(16.0)).color(t.text_secondary);
        let section = |txt: &str| text(txt).font(type_::BODY.lh(24.0)).color(t.text);

        let mut c = col().key("settings-page").w_full().grow(1.0).min_h(0.0)
            .px(28.0).py(18.0).gap(14.0).scroll(ScrollSpec { stick_bottom: false });
        c = c.child(section("General"));
        c = c.child(label("Homepage".into()));
        c = c.child(
            text_input(&s.settings_home, type_::BODY.lh(20.0)).key("set-home").single_line().w(420.0)
                .color(t.text)
                .placeholder("Homepage url", t.text_secondary)
                .on_edit(|s: &mut State| &mut s.settings_home),
        );
        c = c.child(label("Search engine (for searches typed in the address bar)".into()));
        c = c.child(
            text_btn("set-search", format!("Use {}", search_name(&s.settings.search)), |s: &mut State| {
                let next = SEARCHES.iter().position(|t| *t == s.settings.search)
                    .map(|i| (i + 1) % SEARCHES.len())
                    .unwrap_or(0);
                s.settings.search = SEARCHES[next].to_string();
                save_settings_ui(s);
            }),
        );

        c = c.child(section("Privacy and network"));
        c = c.child(
            row().gap(8.0).center_y()
                .child(text_btn("set-adblock", format!("Adblock: {}", if s.settings.adblock { "on" } else { "off" }), |s: &mut State| {
                    s.settings.adblock = !s.settings.adblock;
                    save_settings_ui(s);
                }))
                .child(label("(applies on save; the engine restarts)".into())),
        );
        c = c.child(
            text_btn("clear-history", "Clear browsing history".to_string(), |s: &mut State| {
                store::clear_history();
                s.history_view = Vec::new();
            }),
        );
        c = c.child(label("User agent (applies on save; the engine restarts)".into()));
        c = c.child(
            text_input(&s.settings_ua, type_::BODY.lh(20.0)).key("set-ua").single_line().w(420.0)
                .color(t.text)
                .placeholder("Leave empty for default", t.text_secondary)
                .on_edit(|s: &mut State| &mut s.settings_ua),
        );

        c = c.child(section("Passwords"));
        if s.creds_view.is_empty() {
            c = c.child(label("No passwords saved yet.".into()));
        }
        for (h, u) in s.creds_view.clone() {
            c = c.child(
                row().key(format!("cred-{h}")).w_full().py(4.0).gap(12.0).center_y()
                    .child(text(h.clone()).font(type_::BODY.lh(18.0)).color(t.text).grow(1.0))
                    .child(label(u))
                    .child(text_btn("pw-forget", "Forget".to_string(), move |s: &mut State| {
                        creds::forget(&h);
                        s.creds_view = creds::list();
                    })),
            );
        }
        c = c.child(label("New logins are offered for saving when you submit a login form.".into()));

        c = c.child(section("About"));
        c = c.child(label(format!("Profile: {}", store::profile())));
        c = c.child(label("Browserr 0.1.0".into()));

        let header = row().key("settings-header").h(BAR_H).w_full().px(14.0).gap(10.0).center_y()
            .window_drag()
            .child(icon_btn("set-back", &ICON_BACK, true, Some(|s: &mut State| {
                s.settings_open = false;
            })))
            .child(text("Settings").font(type_::BODY.lh(24.0)).color(t.text))
            .child(spacer())
            .child(win_btn("win-min", &ICON_WIN_MIN, |_, tx: &mut Tx| tx.minimize()))
            .child(win_btn("win-max", &ICON_WIN_MAX, |_, tx: &mut Tx| tx.toggle_maximized()))
            .child(win_btn("win-close", &ICON_WIN_CLOSE, |_, tx: &mut Tx| tx.close_window()));
        return col().w_full().h_full().fill(t.bg).child(header).child(c);
    }

    // Profile picker: pick an existing profile or type a new name + Enter.
    if s.picker {
        let mut p = col().w_full().h_full().fill(t.bg).px(20.0).py(14.0).gap(6.0).window_drag();
        p = p.child(
            row().w_full().center_y()
                .child(text("Choose a profile").font(type_::BODY.lh(24.0)).color(t.text).grow(1.0))
                .child(win_btn("picker-close", &ICON_WIN_CLOSE, |_, tx: &mut Tx| tx.close_window())),
        );
        for name in &s.profiles {
            let n = name.clone();
            p = p.child(
                el().key(format!("profile-{name}")).w_full().py(6.0).px(10.0).radius(8.0).fill(t.card).hover(t.hover)
                    .on_press_tx(move |_: &mut State, tx: &mut Tx| launch_profile(tx, &n))
                    .child(text(name.clone()).font(type_::BODY.lh(20.0)).color(t.text)),
            );
        }
        p = p.child(text("New profile (Enter)").font(type_::BODY.lh(18.0)).color(t.text_secondary));
        p = p.child(
            text_input(&s.profile_input, type_::BODY.lh(20.0)).key("profile-input").single_line()
                .color(t.text)
                .placeholder("Profile name", t.text_secondary)
                .on_edit(|s: &mut State| &mut s.profile_input)
                .on_submit(|s: &mut State, tx: &mut Tx| {
                    let name = store::sanitize_profile(s.profile_input.text.trim());
                    if !name.is_empty() {
                        launch_profile(tx, &name);
                    }
                }),
        );
        return p;
    }

    // Window controls live on the right (Windows style); the menu is a
    // hamburger on the left.
    let win_controls = row().gap(2.0).center_y()
        .child(win_btn("win-min", &ICON_WIN_MIN, |_, tx: &mut Tx| tx.minimize()))
        .child(win_btn("win-max", &ICON_WIN_MAX, |_, tx: &mut Tx| tx.toggle_maximized()))
        .child(win_btn("win-close", &ICON_WIN_CLOSE, |_, tx: &mut Tx| tx.close_window()));

    let active_id = s.tabs[s.active].id;
    let shown_url = s.ui.url(active_id).unwrap_or_else(|| s.tabs[s.active].url.clone());
    let (back_ok, fwd_ok) = s.ui.history(active_id).unwrap_or((false, false));
    let starred = !shown_url.is_empty() && s.bookmarks.iter().any(|(u, _)| u == &shown_url);

    // Bookmarks + history panel, opened by the ellipsis button.
    let menu_panel = {
        let mut p = col().key("menu-panel").w_full().px(14.0).py(10.0).gap(4.0).fill(t.card);
        p = p.child(text("Bookmarks").font(type_::BODY.lh(20.0)).color(t.text_secondary));
        if s.bookmarks.is_empty() {
            p = p.child(text("No bookmarks yet").font(type_::BODY.lh(18.0)).color(t.text_secondary));
        }
        for (url, title) in s.bookmarks.clone() {
            let label = if title.is_empty() { host(&url) } else { title };
            p = p.child(
                el().w_full().py(2.0)
                    .on_press(move |s: &mut State| go(s, url.clone()))
                    .child(text(label).font(type_::BODY.lh(20.0)).color(t.text)),
            );
        }
        p = p.child(text("History").font(type_::BODY.lh(20.0)).color(t.text_secondary));
        if s.history_view.is_empty() {
            p = p.child(text("Nothing yet").font(type_::BODY.lh(18.0)).color(t.text_secondary));
        }
        for url in s.history_view.clone() {
            let u = url.clone();
            p = p.child(
                el().w_full().py(2.0)
                    .on_press(move |s: &mut State| go(s, u.clone()))
                    .child(text(url).font(type_::BODY.lh(18.0)).color(t.text_secondary)),
            );
        }
        p = p.child(text("Settings").font(type_::BODY.lh(20.0)).color(t.text_secondary));
        p = p.child(
            text_btn("open-settings", "Open settings".to_string(), |s: &mut State| {
                s.menu_open = false;
                s.settings_open = true;
                s.creds_view = creds::list();
            }),
        );
        p = p.child(text("Passwords").font(type_::BODY.lh(20.0)).color(t.text_secondary));
        let pw_host = host(&shown_url);
        if pw_host.is_empty() {
            p = p.child(text("Open a site first").font(type_::BODY.lh(16.0)).color(t.text_secondary));
        } else {
            p = p.child(
                text(if s.cred_saved { format!("Saved for {pw_host}") } else { format!("Not saved for {pw_host}") })
                    .font(type_::BODY.lh(16.0)).color(t.text_secondary),
            );
            p = p.child(
                row().gap(8.0).center_y()
                    .child(text_input(&s.cred_user, type_::BODY.lh(20.0)).key("cred-user").single_line().w(180.0)
                        .color(t.text).placeholder("Username", t.text_secondary)
                        .on_edit(|s: &mut State| &mut s.cred_user))
                    .child(text_input(&s.cred_pass, type_::BODY.lh(20.0)).key("cred-pass").single_line().w(150.0)
                        .color(t.text).placeholder("Password", t.text_secondary)
                        .on_edit(|s: &mut State| &mut s.cred_pass))
                    .child(icon_btn("cred-reveal", &ICON_EYE, true, Some(|s: &mut State| {
                        s.cred_pass.set_revealed(!s.cred_pass.is_revealed());
                    }))),
            );
            p = p.child(
                row().gap(8.0).center_y()
                    .child(text_btn("pw-save", "Save".to_string(), |s: &mut State| {
                        let url = s.ui.url(s.tabs[s.active].id).unwrap_or_default();
                        let host = host(&url);
                        if host.is_empty() { return; }
                        creds::save(&host, s.cred_user.text.trim(), &s.cred_pass.text);
                        refresh_creds(s);
                    }))
                    .child(text_btn("pw-fill", "Fill now".to_string(), |s: &mut State| {
                        fill_password(s);
                    }))
                    .child(text_btn("pw-forget", "Forget".to_string(), |s: &mut State| {
                        let url = s.ui.url(s.tabs[s.active].id).unwrap_or_default();
                        let host = host(&url);
                        if host.is_empty() { return; }
                        creds::forget(&host);
                        refresh_creds(s);
                    })),
            );
        }
        p
    };

    // Address pill: shows the page's live URL; click to edit.
    let pill = row().key("address-pill").w(420.0).h(28.0).px(12.0).radius(14.0).center_y()
        .fill(t.card)
        .on_press(|s: &mut State| {
            if !s.editing {
                s.editing = true;
                // Start from the current page url so it can be edited or
                // re-entered, not cleared.
                let shown = s.ui.url(s.tabs[s.active].id)
                    .unwrap_or_else(|| s.tabs[s.active].url.clone());
                s.address = TextEdit::new(&shown);
            }
        })
        .child(if s.editing {
            text_input(&s.address, type_::BODY.lh(20.0)).key("address").single_line().grow(1.0)
                .color(t.text)
                .placeholder("Search or enter website name", t.text_secondary)
                .on_edit(|s: &mut State| &mut s.address)
                .on_input_change(|s: &mut State, _| s.editing = true)
                .on_input_key(|s: &mut State, ev: &rune::KeyEvent, _tx: &mut Tx| {
                    if ev.key == rune::Key::Escape {
                        s.editing = false;
                        return true;
                    }
                    false
                })
                .on_submit(|s: &mut State, _tx: &mut Tx| {
                    let raw = s.address.text.trim().to_string();
                    if raw.is_empty() { return; }
                    let url = if raw.contains("://") || raw.starts_with("about:") {
                        raw
                    } else if raw.contains('.') && !raw.contains(' ') {
                        format!("https://{raw}")
                    } else {
                        s.settings.search.replace("{}", &urlencode(&raw))
                    };
                        s.address = TextEdit::new("");
                        s.editing = false;
                        go(s, url);
                    })
        } else {
            let placeholder = shown_url.is_empty();
            text(if placeholder { "Search or enter website name".to_string() } else { shown_url.clone() })
                .grow(1.0)
                .font(type_::BODY.lh(20.0))
                .color(if placeholder { t.text_secondary } else { t.text })
        });

    // Tab pills: titles come from engine events; an x closes each tab.
    let tabs_row = {
        let mut r = row().gap(6.0).center_y();
        for (i, tab) in s.tabs.iter().enumerate() {
            let is_active = i == s.active;
            let title = if is_active {
                s.ui.title(tab.id).unwrap_or_else(|| tab.title.clone())
            } else {
                s.ui.title(tab.id).unwrap_or_else(|| tab.title.clone())
            };
            let label_color = if is_active { t.text } else { t.text_secondary };
            let tab_host = s.ui.url(tab.id).map(|u| host(&u)).unwrap_or_default();
            let mut pill_el = el().key(format!("tab-{}", tab.id)).h(28.0).px(12.0).radius(14.0).center_y()
                .fill(if is_active { t.hover } else { t.card });
            if let Some(px) = s.ui.icon(&tab_host) {
                pill_el = pill_el.child(el().size(14.0, 14.0).center_y().pixels(&px).px(4.0));
            }
            pill_el = pill_el.child(text(title).font(type_::BODY.lh(20.0)).color(label_color))
                .on_press(move |s: &mut State| {
                    if s.active != i {
                        s.active = i;
                        s.editing = false;
                        let id = s.tabs[i].id;
                        s.ui.active.store(id, std::sync::atomic::Ordering::SeqCst);
                        if let Some(engine) = &mut s.engine {
                            engine.activate(id);
                        }
                        save_session(s);
                    }
                });
            if s.tabs.len() > 1 {
                pill_el = pill_el.child(
                    el().key("tab-close").size(14.0, 14.0).center_y()
                        .child(icon(&ICON_CLOSE, 10.0))
                        .on_press(move |s: &mut State| {
                            let closed_id = s.tabs[i].id;
                            s.tabs.remove(i);
                            if s.tabs.is_empty() {
                                // Last tab closed: start a fresh one.
                                let id = s.next_tab_id;
                                s.next_tab_id += 1;
                                s.tabs.push(Tab { id, url: String::new(), title: "New Tab".into() });
                                s.active = 0;
                                s.address = TextEdit::new("");
                                s.editing = true;
                                s.ui.active.store(id, std::sync::atomic::Ordering::SeqCst);
                                if let Some(engine) = &mut s.engine {
                                    engine.close_tab(closed_id);
                                    engine.new_tab(id);
                                    engine.activate(id);
                                }
                                // New tabs open the homepage (if set); the pill
                                // stays in edit mode so typing works right away.
                                if !s.settings.homepage.is_empty() {
                                    go(s, s.settings.homepage.clone());
                                }
                                save_session(s);
                            } else {
                                if s.active >= s.tabs.len() {
                                    s.active = s.tabs.len() - 1;
                                }
                                let new_active = s.tabs[s.active].id;
                                s.ui.active.store(new_active, std::sync::atomic::Ordering::SeqCst);
                                if let Some(engine) = &mut s.engine {
                                    engine.close_tab(closed_id);
                                    engine.activate(new_active);
                                }
                                save_session(s);
                            }
                        }),
                );
            }
            r = r.child(pill_el);
        }
        r
    };

    let bar = row().key("bar").h(BAR_H).w_full().px(14.0).gap(10.0).center_y()
        .window_drag()
        .child(icon_btn("br-menu", &ICON_HAMBURGER, true, Some(|s: &mut State| {
            s.menu_open = !s.menu_open;
            if s.menu_open {
                s.history_view = store::read_history(15);
                refresh_creds(s);
            }
        })))
        .child(spacer())
        .child(icon_btn("br-back", &ICON_BACK, back_ok, Some(move |s: &mut State| {
            if let Some(e) = &mut s.engine {
                e.back(active_id);
            }
        })))
        .child(icon_btn("br-fwd", &ICON_FWD, fwd_ok, Some(move |s: &mut State| {
            if let Some(e) = &mut s.engine {
                e.forward(active_id);
            }
        })))
        .child(icon_btn("br-star", if starred { &ICON_STAR_FILLED } else { &ICON_STAR },
            !shown_url.is_empty(),
            Some(|s: &mut State| {
                let url = s.ui.url(s.tabs[s.active].id).unwrap_or_default();
                if url.is_empty() { return; }
                if let Some(pos) = s.bookmarks.iter().position(|(u, _)| *u == url) {
                    s.bookmarks.remove(pos);
                } else {
                    let title = s.ui.title(s.tabs[s.active].id).unwrap_or_else(|| url.clone());
                    s.bookmarks.insert(0, (url, title));
                }
                store::save_bookmarks(&s.bookmarks);
            })))
        .child(icon_btn("br-key", &ICON_KEY, !shown_url.is_empty(), Some(|s: &mut State| {
            // Saved password: fill. None: open the panel to save one.
            let url = s.ui.url(s.tabs[s.active].id).unwrap_or_default();
            if url.is_empty() { return; }
            if creds::load(&host(&url)).is_some() {
                fill_password(s);
            } else {
                s.menu_open = true;
                refresh_creds(s);
            }
        })))
        .child(pill)
        .child(tabs_row)
        .child(icon_btn("br-plus", &ICON_PLUS, true, Some(|s: &mut State| {
            let id = s.next_tab_id;
            s.next_tab_id += 1;
            let tab_id = id; // engine tab id == chrome tab id
            s.tabs.push(Tab { id, url: String::new(), title: "New Tab".into() });
            s.active = s.tabs.len() - 1;
            s.address = TextEdit::new("");
            s.editing = true;
            s.ui.active.store(tab_id, std::sync::atomic::Ordering::SeqCst);
            if let Some(engine) = &mut s.engine {
                engine.new_tab(tab_id);
                engine.activate(tab_id);
            }
            // New tabs open the homepage (if set); the pill stays in edit mode
            // so the user can type right away.
            if !s.settings.homepage.is_empty() {
                go(s, s.settings.homepage.clone());
            }
            save_session(s);
        })))
        .child(spacer())
        .child(win_controls);

    // Save-password prompt: appears when a submitted form left a capture.
    let pw_prompt = match s.ui.peek_pw() {
        Some(c) if host(&c.origin) != s.pw_handled.as_deref().unwrap_or("\0") => {
            let host = host(&c.origin);
            let user = c.user.clone();
            let pass = c.pass.clone();
            Some(
                row().key("pw-prompt").w_full().px(14.0).py(8.0).gap(10.0).center_y().fill(t.card)
                    .child(
                        text(format!("Save password for {host}{}", if user.is_empty() { String::new() } else { format!(" ({user})") }))
                            .font(type_::BODY.lh(18.0)).color(t.text).grow(1.0),
                    )
                    .child(text_btn("pw-prompt-save", "Save".to_string(), {
                        let host = host.clone();
                        move |s: &mut State| {
                            creds::save(&host, &user, &pass);
                            s.pw_handled = Some(host.clone());
                        }
                    }))
                    .child(text_btn("pw-prompt-no", "Not now".to_string(), {
                        let host = host.clone();
                        move |s: &mut State| {
                            s.pw_handled = Some(host.clone());
                        }
                    })),
            )
        }
        _ => None,
    };

    let page = el().key("page").grow(1.0).min_h(0.0).fill(t.bg)
        .native_surface(&s.surface, SurfaceFit::Fit)
        .on_layout(|s: &mut State, rect: [f32; 4]| {
            let (w, h) = (rect[2].max(1.0) as u32, rect[3].max(1.0) as u32);
            if Some((w, h)) != s.last_size {
                s.last_size = Some((w, h));
                if let Some(engine) = &mut s.engine {
                    engine.resize(w, h);
                }
            }
        });

    let mut root = col().w_full().h_full().fill(t.bg).child(bar);
    if let Some(prompt) = pw_prompt {
        root = root.child(prompt);
    }
    if s.menu_open {
        root = root.child(menu_panel);
    }
    root.child(page)
}

fn main() {
    rune::run_windows(|_| {
        let picker = !store::profile_selected();
        let size = if picker { (420.0, 520.0) } else { (1280.0, 800.0) };
        let app = App::new(AppSpec {
            setup: Some(setup),
            ..AppSpec::new("Browserr", size, state, view)
        })
        .window_options(WindowOptions { borderless: true, ..Default::default() });
        let windows: Vec<rune::AnyWindowSpec> =
            vec![rune::WindowSpec::from_app("browserr", app).into()];
        windows
    });
}
