use rune::tok::{self, type_};
use rune::{App, AppSpec, Color, Cx, El, Icon, NativeSurface, SurfaceFit, TextEdit, Tx, WindowOptions, col, el, icon, row, spacer, text, text_input};

mod engine;

use engine::{Engine, Ui};
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
}

impl Default for Tab {
    fn default() -> Self {
        Self { id: 0, url: String::new(), title: "New Tab".into() }
    }
}

fn state() -> State {
    State {
        tabs: vec![Tab::default()],
        active: 0,
        address: TextEdit::new(""),
        editing: false,
        engine: None,
        surface: NativeSurface::new(),
        last_size: None,
        next_tab_id: 1,
        ui: Ui::new(0),
    }
}

fn setup(s: &mut State, wake: rune::Waker) {
    s.surface.set_waker(wake.clone());
    match Engine::start(s.surface.clone(), wake, s.ui.clone()) {
        Ok(e) => s.engine = Some(e),
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
                    let tab_id = s.tabs[s.active].id;
                    let tab = &mut s.tabs[s.active];
                    tab.url = url.clone();
                    tab.title = url
                        .split("://").nth(1).unwrap_or(&url)
                        .split('/').next().unwrap_or(&url)
                        .to_string();
                    s.address = TextEdit::new("");
                    s.editing = false;
                    if let Some(engine) = &mut s.engine {
                        engine.navigate(&url, tab_id);
                    }
                })
        } else {
            let placeholder = shown_url.is_empty();
            text(if placeholder { "Search or enter website name".to_string() } else { shown_url })
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
        })))
        .child(icon_btn("br-menu", &ICON_ELLIPSIS, false, None::<fn(&mut State)>))
        .child(spacer())
        .child(el().w(78.0));

    col().w_full().h_full().fill(t.bg)
        .child(bar)
        .child(
            el().key("page").grow(1.0).min_h(0.0).fill(t.bg)
                .native_surface(&s.surface, SurfaceFit::Fit)
                .on_layout(|s: &mut State, rect: [f32; 4]| {
                    let (w, h) = (rect[2].max(1.0) as u32, rect[3].max(1.0) as u32);
                    if Some((w, h)) != s.last_size {
                        s.last_size = Some((w, h));
                        if let Some(engine) = &mut s.engine {
                            engine.resize(w, h);
                        }
                    }
                }),
        )
}

fn main() {
    rune::run_windows(|_| {
        let app = App::new(AppSpec {
            setup: Some(setup),
            ..AppSpec::new("Browserr", (1280.0, 800.0), state, view)
        })
        .window_options(WindowOptions { borderless: true, ..Default::default() });
        let windows: Vec<rune::AnyWindowSpec> =
            vec![rune::WindowSpec::from_app("browserr", app).into()];
        windows
    });
}
