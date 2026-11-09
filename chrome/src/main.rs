use rune::tok::{self, type_};
use rune::{App, AppSpec, Color, Cx, El, Icon, NativeSurface, SurfaceFit, TextEdit, Tx, WindowOptions, col, el, icon, row, spacer, text, text_input};

mod engine;
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
}

impl Default for Tab {
    fn default() -> Self {
        Self { id: 0, url: String::new(), title: "New Tab".into() }
    }
}

fn host(url: &str) -> String {
    url.split("://").nth(1).unwrap_or(url)
        .split('/').next().unwrap_or(url)
        .to_string()
}

fn state() -> State {
    // No --profile argument: this window is the profile picker, not a browser.
    let picker = !store::profile_selected();
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
    let settings = store::load_settings();
    let profile_dir = store::data_dir().join("engine");
    match Engine::start(
        s.surface.clone(),
        wake,
        s.ui.clone(),
        &settings.user_agent,
        &profile_dir.to_string_lossy(),
    ) {
        Ok(mut e) => {
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
            } else if !settings.homepage.is_empty() {
                e.navigate(&settings.homepage, 0);
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
static ICON_ELLIPSIS: Icon = Icon {
    name: "br-ellipsis",
    view: 16.0,
    body: r#"<circle cx="3.6" cy="8" r="1.1" fill="currentColor"/><circle cx="8" cy="8" r="1.1" fill="currentColor"/><circle cx="12.4" cy="8" r="1.1" fill="currentColor"/>"#,
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

fn icon_btn(id: &'static str, ic: &'static Icon, enabled: bool, action: Option<impl Fn(&mut State) + 'static>) -> El {
    let t = tok::theme(true);
    let e = el().key(id).size(28.0, 28.0).radius(14.0).center_y().child(icon(ic, 16.0));
    match (enabled, action) {
        (true, Some(action)) => e.fill(Color::TRANSPARENT).hover(t.hover).on_press(action),
        _ => e.layer_opacity(0.35),
    }
}

fn view(s: &State, _: &mut Cx<State>) -> El {
    let t = tok::theme(true);

    // Profile picker: pick an existing profile or type a new name + Enter.
    if s.picker {
        let mut p = col().w_full().h_full().fill(t.bg).px(20.0).py(14.0).gap(6.0).window_drag();
        p = p.child(
            row().w_full().center_y()
                .child(text("Choose a profile").font(type_::BODY.lh(24.0)).color(t.text).grow(1.0))
                .child(
                    el().size(11.0, 11.0).radius(5.5)
                        .fill(Color { r: 1.0, g: 0.37, b: 0.34, a: 1.0 })
                        .on_press_tx(|_: &mut State, tx: &mut Tx| tx.close_window()),
                ),
        );
        for name in &s.profiles {
            let n = name.clone();
            p = p.child(
                el().key("profile").w_full().py(6.0).px(10.0).radius(8.0).fill(t.card).hover(t.hover)
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

    // Window controls: minimal traffic lights, macOS style.
    let dot = |key: &'static str, c: Color| {
        el().key(key).size(11.0, 11.0).radius(5.5).fill(c)
            .on_press_tx(move |_: &mut State, tx: &mut Tx| match key {
                "br-close" => tx.close_window(),
                "br-min" => tx.minimize(),
                _ => {}
            })
    };
    let controls = row().gap(7.0).center_y()
        .child(dot("br-close", Color { r: 1.0, g: 0.37, b: 0.34, a: 1.0 }))
        .child(dot("br-min", Color { r: 1.0, g: 0.74, b: 0.18, a: 1.0 }))
        .child(dot("br-max", Color { r: 0.16, g: 0.79, b: 0.25, a: 1.0 }));

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
        p
    };

    // Address pill: shows the page's live URL; click to edit.
    let pill = row().key("address-pill").w(420.0).h(28.0).px(12.0).radius(14.0).center_y()
        .fill(t.card)
        .on_press(|s: &mut State| {
            if !s.editing {
                s.editing = true;
                s.address = TextEdit::new("");
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
                        format!("https://duckduckgo.com/?q={raw}")
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
            let mut pill_el = el().key("tab").h(28.0).px(12.0).radius(14.0).center_y()
                .fill(if is_active { t.hover } else { t.card })
                .child(text(title).font(type_::BODY.lh(20.0)).color(label_color))
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
        .child(controls)
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
            save_session(s);
        })))
        .child(icon_btn("br-menu", &ICON_ELLIPSIS, true, Some(|s: &mut State| {
            s.menu_open = !s.menu_open;
            if s.menu_open {
                s.history_view = store::read_history(15);
            }
        })))
        .child(spacer())
        .child(el().w(78.0));

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
