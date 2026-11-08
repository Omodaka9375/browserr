use rune::tok::{self, type_};
use rune::{App, AppSpec, Color, Cx, El, Icon, NativeSurface, SurfaceFit, TextEdit, Tx, WindowOptions, col, el, icon, row, spacer, text, text_input};

mod engine;

use engine::Engine;

const BAR_H: f32 = 40.0;

struct Tab {
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
}

impl Default for Tab {
    fn default() -> Self {
        Self { url: String::new(), title: "New Tab".into() }
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
    }
}

fn setup(s: &mut State, wake: rune::Waker) {
    s.surface.set_waker(wake.clone());
    match Engine::start(s.surface.clone(), wake) {
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

fn icon_btn(id: &'static str, ic: &'static Icon, enabled: bool) -> El {
    let t = tok::theme(true);
    let e = el().key(id).size(28.0, 28.0).radius(14.0).center_y().child(icon(ic, 16.0));
    if enabled {
        e.fill(Color::TRANSPARENT).hover(t.hover).on_press(|s: &mut State| {
            s.tabs.push(Tab::default());
            s.active = s.tabs.len() - 1;
            s.address = TextEdit::new("");
            s.editing = true;
        })
    } else {
        e.layer_opacity(0.35)
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

    let tab = &s.tabs[s.active];
    let pill = row().key("address-pill").w(420.0).h(28.0).px(12.0).radius(14.0).center_y()
        .fill(t.card)
        .child(
            text_input(&s.address, type_::BODY.lh(20.0)).key("address").single_line().grow(1.0)
                .color(t.text)
                .placeholder("Search or enter website name", t.text_secondary)
                .on_edit(|s: &mut State| &mut s.address)
                .on_input_change(|s: &mut State, _| s.editing = true)
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
                    let tab = &mut s.tabs[s.active];
                    tab.url = url.clone();
                    tab.title = url
                        .split("://").nth(1).unwrap_or(&url)
                        .split('/').next().unwrap_or(&url)
                        .to_string();
                    s.address = TextEdit::new("");
                    s.editing = false;
                    if let Some(engine) = &mut s.engine {
                        engine.navigate(&url);
                    }
                }),
        );

    let tab_pill = el().key("tab").h(28.0).px(12.0).radius(14.0).center_y()
        .fill(t.card)
        .child(text(tab.title.clone()).font(type_::BODY.lh(20.0)).color(t.text_secondary).truncate());

    let bar = row().key("bar").h(BAR_H).w_full().px(14.0).gap(10.0).center_y()
        .window_drag()
        .child(controls)
        .child(spacer())
        .child(icon_btn("br-back", &ICON_BACK, false))
        .child(icon_btn("br-fwd", &ICON_FWD, false))
        .child(pill)
        .child(tab_pill)
        .child(icon_btn("br-plus", &ICON_PLUS, true))
        .child(icon_btn("br-menu", &ICON_ELLIPSIS, false))
        .child(spacer())
        .child(el().w(78.0));

    col().w_full().h_full().fill(t.bg)
        .child(bar)
        .child(
            el().key("page").grow(1.0).min_h(0.0).fill(t.bg)
                .native_surface(&s.surface, SurfaceFit::Fit),
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
