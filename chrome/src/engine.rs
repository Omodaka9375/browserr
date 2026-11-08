//! Engine client for the chrome: spawns the engine process, sends commands,
//! receives frames and pushes them into a Rune NativeSurface.
use std::io::{BufReader, Read, Write};
use std::net::TcpStream;
use std::process::{Child, Command, Stdio};
use std::sync::atomic::{AtomicU64, Ordering};
use std::sync::Arc;

use rune::{NativeSurface, Waker};

const PORT: u16 = 7411;
const MAGIC: u32 = 0x46_52_4D_31; // FRM1

pub struct Engine {
    stream: TcpStream,
    _child: Child,
    _reader: Option<std::thread::JoinHandle<()>>,
}

impl Engine {
    /// Spawn the engine sibling process and start the frame reader.
    pub fn start(surface: NativeSurface, waker: Waker) -> std::io::Result<Engine> {
        let exe = std::env::var("BROWSERR_ENGINE")
            .map(std::path::PathBuf::from)
            .unwrap_or_else(|_| {
                let here = std::env::current_exe().ok()
                    .and_then(|p| p.parent().map(|p| p.to_path_buf()))
                    .unwrap_or_default();
                here.join("engine.exe")
            });
        let child = Command::new(exe)
            .stdout(Stdio::null())
            .stderr(Stdio::null())
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
        let reader = std::thread::spawn(move || read_frames(reader_stream, surface, waker));

        Ok(Engine { stream, _child: child, _reader: Some(reader) })
    }

    fn send(&mut self, json: &str) {
        let _ = writeln!(&mut self.stream, "{json}");
        let _ = self.stream.flush();
    }

    pub fn navigate(&mut self, url: &str) {
        let safe = url.replace('\\', "\\\\").replace('"', "\\\"");
        self.send(&format!("{{\"cmd\":\"navigate\",\"url\":\"{safe}\"}}"));
    }

    pub fn resize(&mut self, w: u32, h: u32) {
        self.send(&format!("{{\"cmd\":\"resize\",\"w\":{w},\"h\":{h}}}"));
    }
}

fn read_frames(stream: TcpStream, surface: NativeSurface, waker: Waker) {
    let mut reader = BufReader::new(stream);
    let counter = Arc::new(AtomicU64::new(1));
    let mut header = [0u8; 4 + 8 + 4 + 4];

    loop {
        if reader.read_exact(&mut header).is_err() {
            return;
        }
        let magic = u32::from_le_bytes(header[0..4].try_into().unwrap());
        if magic != MAGIC {
            return;
        }
        let _id = u64::from_le_bytes(header[4..12].try_into().unwrap());
        let w = u32::from_le_bytes(header[12..16].try_into().unwrap());
        let h = u32::from_le_bytes(header[16..20].try_into().unwrap());
        let len = w as usize * h as usize * 4;
        let mut pixels = vec![0u8; len];
        if reader.read_exact(&mut pixels).is_err() {
            return;
        }
        let id = counter.fetch_add(1, Ordering::SeqCst);
        if surface
            .push_bytes(
                rune::native_surface::ByteFrame {
                    width: w,
                    height: h,
                    format: rune::native_surface::Format::Bgra,
                    color: Default::default(),
                    planes: [pixels, Vec::new()],
                },
                id,
            )
            .is_err()
        {
            return;
        }
        waker();
    }
}
