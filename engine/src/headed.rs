//! Native window mode. The engine keeps its existing socket loop; winit only
//! supplies the OS window, native input and the presentation surface.

use std::rc::Rc;
use std::time::Duration;

use servo::{InputEvent, RenderingContext, WindowRenderingContext};
use winit::application::ApplicationHandler;
use winit::event::{ElementState, MouseButton, MouseScrollDelta, WindowEvent};
use winit::event_loop::{ActiveEventLoop, EventLoop};
use winit::keyboard::{Key, KeyCode, ModifiersState, PhysicalKey};
use winit::platform::pump_events::{EventLoopExtPumpEvents, PumpStatus};
use winit::raw_window_handle::{HasDisplayHandle, HasWindowHandle};
use winit::window::{Icon, Window, WindowAttributes, WindowId};

/// Embedded 64x64 RGBA window icon (title bar + taskbar while running).
/// Rendered from docs/icon.svg; regenerate via .iconbuild/. The exe icon
/// comes from the .ico resource embedded at build time (build.rs).
const WINDOW_ICON_PNG: &[u8] = include_bytes!("../assets/icon.png");

fn window_icon() -> Option<Icon> {
    let img = image::load_from_memory(WINDOW_ICON_PNG).ok()?.to_rgba8();
    let (w, h) = (img.width(), img.height());
    Icon::from_rgba(img.into_raw(), w, h).ok()
}

pub enum NativeEvent {
    Close,
    Redraw,
    Resize(dpi::PhysicalSize<u32>),
    Scale(f64),
    Focus(bool),
    Move(f32, f32),
    Button(servo::MouseButtonAction, servo::MouseButton, f32, f32),
    Wheel(f64, f64, f32, f32, servo::WheelMode),
    Key(InputEvent),
}

pub struct Headed {
    event_loop: Option<EventLoop<()>>,
    window: Option<Window>,
    context: Option<Rc<WindowRenderingContext>>,
    events: Vec<NativeEvent>,
    cursor: (f32, f32),
    modifiers: ModifiersState,
    size: dpi::PhysicalSize<u32>,
}

impl Headed {
    pub fn new(size: dpi::PhysicalSize<u32>) -> Result<Self, String> {
        let event_loop = EventLoop::new().map_err(|e| format!("event loop: {e}"))?;
        let mut app = Self {
            event_loop: Some(event_loop),
            window: None,
            context: None,
            events: Vec::new(),
            cursor: (0.0, 0.0),
            modifiers: ModifiersState::empty(),
            size,
        };
        app.pump();
        if app.window.is_none() || app.context.is_none() {
            return Err("native window initialization did not complete".into());
        }
        eprintln!("engine: visible native window (--headed)");
        Ok(app)
    }

    pub fn context(&self) -> Rc<dyn RenderingContext> {
        self.context
            .as_ref()
            .expect("native context exists after Headed::new")
            .clone()
    }

    pub fn scale(&self) -> f64 {
        self.window
            .as_ref()
            .map(Window::scale_factor)
            .unwrap_or(1.0)
    }

    pub fn set_title(&self, title: &str) {
        if let Some(window) = &self.window {
            window.set_title(&format!("{title} - Browserr"));
        }
    }

    pub fn resize(&self, size: dpi::PhysicalSize<u32>) {
        if let Some(window) = &self.window {
            let _ = window.request_inner_size(size);
        }
    }

    /// Pump OS messages without waiting, retaining input for the Servo loop.
    pub fn pump(&mut self) -> bool {
        let Some(mut event_loop) = self.event_loop.take() else {
            return false;
        };
        let result = matches!(
            event_loop.pump_app_events(Some(Duration::ZERO), self),
            PumpStatus::Exit(_)
        );
        self.event_loop = Some(event_loop);
        result
    }

    pub fn take_events(&mut self) -> Vec<NativeEvent> {
        std::mem::take(&mut self.events)
    }
}

impl ApplicationHandler for Headed {
    fn resumed(&mut self, event_loop: &ActiveEventLoop) {
        if self.window.is_some() {
            return;
        }
        let attributes = WindowAttributes::default()
            .with_title("Browserr")
            .with_inner_size(self.size)
            .with_visible(true)
            .with_window_icon(window_icon());
        let window = match event_loop.create_window(attributes) {
            Ok(window) => window,
            Err(e) => {
                eprintln!("engine: cannot create native window: {e}");
                event_loop.exit();
                return;
            }
        };
        let display = match event_loop.display_handle() {
            Ok(display) => display,
            Err(e) => {
                eprintln!("engine: cannot get native display: {e}");
                event_loop.exit();
                return;
            }
        };
        let handle = match window.window_handle() {
            Ok(handle) => handle,
            Err(e) => {
                eprintln!("engine: cannot get native window handle: {e}");
                event_loop.exit();
                return;
            }
        };
        match WindowRenderingContext::new(display, handle, window.inner_size()) {
            Ok(context) => {
                let context = Rc::new(context);
                let _ = context.make_current();
                self.context = Some(context);
                self.window = Some(window);
            }
            Err(e) => {
                eprintln!("engine: cannot create native rendering context: {e:?}");
                event_loop.exit();
            }
        }
    }

    fn window_event(
        &mut self,
        _event_loop: &ActiveEventLoop,
        _window_id: WindowId,
        event: WindowEvent,
    ) {
        match event {
            WindowEvent::CloseRequested => self.events.push(NativeEvent::Close),
            WindowEvent::RedrawRequested => self.events.push(NativeEvent::Redraw),
            WindowEvent::Resized(size) => {
                self.size = size;
                self.events.push(NativeEvent::Resize(size));
            }
            WindowEvent::ScaleFactorChanged { scale_factor, .. } => {
                self.events.push(NativeEvent::Scale(scale_factor));
            }
            WindowEvent::Focused(focus) => self.events.push(NativeEvent::Focus(focus)),
            WindowEvent::CursorMoved { position, .. } => {
                self.cursor = (position.x as f32, position.y as f32);
                self.events
                    .push(NativeEvent::Move(self.cursor.0, self.cursor.1));
            }
            WindowEvent::MouseInput { state, button, .. } => {
                let action = if state == ElementState::Pressed {
                    servo::MouseButtonAction::Down
                } else {
                    servo::MouseButtonAction::Up
                };
                let button = match button {
                    MouseButton::Left => servo::MouseButton::Primary,
                    MouseButton::Right => servo::MouseButton::Secondary,
                    MouseButton::Middle => servo::MouseButton::Auxiliary,
                    _ => return,
                };
                self.events.push(NativeEvent::Button(
                    action,
                    button,
                    self.cursor.0,
                    self.cursor.1,
                ));
            }
            WindowEvent::MouseWheel { delta, .. } => {
                let (x, y, mode) = match delta {
                    MouseScrollDelta::LineDelta(x, y) => (
                        (x * 40.0) as f64,
                        (y * 40.0) as f64,
                        servo::WheelMode::DeltaLine,
                    ),
                    MouseScrollDelta::PixelDelta(p) => (p.x, p.y, servo::WheelMode::DeltaPixel),
                };
                self.events
                    .push(NativeEvent::Wheel(x, y, self.cursor.0, self.cursor.1, mode));
            }
            WindowEvent::ModifiersChanged(modifiers) => self.modifiers = modifiers.state(),
            WindowEvent::KeyboardInput { event, .. } => {
                let key = match event.logical_key {
                    Key::Named(key) => keyboard_types::Key::Named(native_named_key(key)),
                    Key::Character(c) => {
                        let text = event.text.as_ref().map(|t| t.as_str()).unwrap_or(&c);
                        if text.chars().any(char::is_control) {
                            return;
                        }
                        keyboard_types::Key::Character(text.to_string())
                    }
                    Key::Unidentified(_) | Key::Dead(_) => return,
                };
                let PhysicalKey::Code(code) = event.physical_key else {
                    return;
                };
                let Some(code) = native_code(code) else {
                    return;
                };
                let mut modifiers = keyboard_types::Modifiers::empty();
                if self.modifiers.shift_key() {
                    modifiers.insert(keyboard_types::Modifiers::SHIFT);
                }
                if self.modifiers.control_key() {
                    modifiers.insert(keyboard_types::Modifiers::CONTROL);
                }
                if self.modifiers.alt_key() {
                    modifiers.insert(keyboard_types::Modifiers::ALT);
                }
                if self.modifiers.super_key() {
                    modifiers.insert(keyboard_types::Modifiers::META);
                }
                let mut event = if event.state == ElementState::Pressed {
                    keyboard_types::KeyboardEvent::key_down(key, code)
                } else {
                    keyboard_types::KeyboardEvent::key_up(key, code)
                };
                event.modifiers = modifiers;
                self.events.push(NativeEvent::Key(InputEvent::Keyboard(
                    servo::KeyboardEvent::new(event),
                )));
            }
            _ => {}
        }
    }
}

fn native_code(code: KeyCode) -> Option<keyboard_types::Code> {
    format!("{code:?}").parse().ok()
}

fn native_named_key(key: winit::keyboard::NamedKey) -> keyboard_types::NamedKey {
    // winit and Servo use the same UI Events names but different crate types.
    format!("{key:?}")
        .parse()
        .unwrap_or(keyboard_types::NamedKey::Unidentified)
}
