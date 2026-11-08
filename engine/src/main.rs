//! Browserr page engine: a Servo child process.
//! Listens on 127.0.0.1:7411. Protocol:
//!   in:  newline JSON: {"cmd":"navigate","url":"..."} | {"cmd":"resize","w":N,"h":N}
//!   out: frames: u32 magic 'FRM1', u64 id, u32 w, u32 h, w*h*4 BGRA bytes
use std::io::{BufRead, BufReader, Write};
use std::net::{TcpListener, TcpStream};
use std::sync::atomic::{AtomicBool, Ordering};
use std::sync::{Arc, Condvar, Mutex};

const PORT: u16 = 7411;
const MAGIC: u32 = 0x46_52_4D_31; // FRM1

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
        let (mut pending, _t) = cv.wait_timeout(lock.lock().unwrap(), timeout).unwrap();
        if pending {
            *lock.lock().unwrap() = false;
        }
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

struct FrameDelegate {
    frame_ready: Arc<AtomicBool>,
    pump: Pump,
}

impl servo::WebViewDelegate for FrameDelegate {
    fn notify_new_frame_ready(&self, _webview: servo::WebView) {
        self.frame_ready.store(true, Ordering::SeqCst);
        self.pump.kick();
    }
}

fn main() {
    let _ = rustls::crypto::aws_lc_rs::default_provider().install_default();

    let listener = TcpListener::bind(("127.0.0.1", PORT)).expect("engine: bind failed");
    eprintln!("engine: listening on {PORT}");
    let (stream, _peer) = listener.accept().expect("engine: accept failed");
    eprintln!("engine: chrome connected");
    let out = stream.try_clone().expect("engine: clone stream");
    let mut reader = BufReader::new(stream);

    let pump = Pump(Arc::new((Mutex::new(false), Condvar::new())));
    let frame_ready = Arc::new(AtomicBool::new(false));
    let delegate = FrameDelegate { frame_ready: frame_ready.clone(), pump: pump.clone() };
    let delegate = std::rc::Rc::new(delegate);

    let rendering_context = match servo::SoftwareRenderingContext::new(dpi::PhysicalSize::new(1200, 760)) {
        Ok(c) => std::rc::Rc::new(c),
        Err(e) => {
            eprintln!("engine: no rendering context: {e:?}");
            return;
        }
    };
    let _ = rendering_context.make_current();

    let servo = servo::ServoBuilder::default()
        .event_loop_waker(Box::new(ServoWaker(pump.clone())))
        .build();

    let webview = servo::WebViewBuilder::new(&servo, rendering_context.clone())
        .delegate(delegate)
        .build();

    let mut out = std::io::BufWriter::new(out);
    let mut current_size = dpi::PhysicalSize::new(1200u32, 760u32);
    let mut frame_id: u64 = 0;

    loop {
        // Non-blocking-ish command drain: the reader is on this thread's stream; use a
        // reader thread is unsafe with BufReader ownership, so poll with zero timeout via
        // a small channel fed by a helper thread instead. Simpler: set_read_timeout.
        // (BufReader::buffer may hold partial lines; acceptable for v1.)
        reader
            .get_ref()
            .set_read_timeout(Some(std::time::Duration::from_millis(0)))
            .ok();

        loop {
            let mut line = String::new();
            match reader.read_line(&mut line) {
                Ok(0) => {
                    eprintln!("engine: chrome disconnected");
                    return;
                }
                Ok(_) => {
                    let line = line.trim();
                    if line.is_empty() {
                        break;
                    }
                    if let Ok(v) = serde_json::from_str::<serde_json::Value>(line) {
                        match v["cmd"].as_str() {
                            Some("navigate") => {
                                if let Some(u) = v["url"].as_str()
                                    && let Ok(url) = url::Url::parse(u)
                                {
                                    eprintln!("engine: navigate {url}");
                                    webview.load(url);
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
                                    webview.resize(s);
                                }
                            }
                            _ => {}
                        }
                    }
                }
                Err(ref e) if e.kind() == std::io::ErrorKind::WouldBlock => break,
                Err(_) => return,
            }
        }

        servo.spin_event_loop();

        if frame_ready.swap(false, Ordering::SeqCst) {
            let rect = webrender_api::units::DeviceIntRect::new(
                webrender_api::units::DeviceIntPoint::zero(),
                webrender_api::units::DeviceIntSize::new(
                    current_size.width as i32,
                    current_size.height as i32,
                ),
            );
            if let Some(image) = rendering_context.read_to_image(rect) {
                let mut bgra = image.into_raw();
                for px in bgra.chunks_exact_mut(4) {
                    px.swap(0, 2);
                }
                frame_id += 1;
                out.write_all(&MAGIC.to_le_bytes()).ok();
                out.write_all(&frame_id.to_le_bytes()).ok();
                out.write_all(&current_size.width.to_le_bytes()).ok();
                out.write_all(&current_size.height.to_le_bytes()).ok();
                out.write_all(&bgra).ok();
                let _ = out.flush();
            }
        }

        pump.wait(std::time::Duration::from_millis(16));
    }
}
