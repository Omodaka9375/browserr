// Post-build DLL staging (Windows): the `no-wgl` feature routes GL through
// mozangle's ANGLE (libEGL.dll + libGLESv2.dll), which servo loads at
// runtime by NAME, so they must sit next to engine.exe or the very first
// EGL call panics ("egl function was not loaded"). cargo builds the DLLs
// into mozangle's OUT_DIR at <target>/<profile>/build/mozangle-<hash>/out;
// OUR OUT_DIR is a sibling engine-<hash>/ dir, so walk up to the build/
// root and sweep every crate's out/ for the DLLs. No-op on non-Windows
// (Linux/macOS use system GL; mozangle never enters the build).

use std::path::PathBuf;

fn main() {
    println!("cargo:rerun-if-changed=build.rs");
    let target_os = std::env::var("CARGO_CFG_TARGET_OS").unwrap_or_default();
    if target_os != "windows" {
        return;
    }
    let out_dir = PathBuf::from(std::env::var("OUT_DIR").unwrap_or_default());
    // OUT_DIR = <target>/<profile>/build/<crate>-<hash>/out
    let crate_dir = match out_dir.parent() {
        Some(p) => p.to_path_buf(),
        None => return,
    };
    let build_root = match crate_dir.parent() {
        Some(p) => p.to_path_buf(), // <target>/<profile>/build
        None => return,
    };
    let profile_dir = match build_root.parent() {
        Some(p) => p.to_path_buf(), // <target>/<profile> - where the binary lands
        None => return,
    };

    let mut staged = 0;
    let entries = match std::fs::read_dir(&build_root) {
        Ok(e) => e,
        Err(_) => return,
    };
    for entry in entries.flatten() {
        let out = entry.path().join("out");
        if !out.is_dir() {
            continue;
        }
        for dll in ["libEGL.dll", "libGLESv2.dll"] {
            let src = out.join(dll);
            if src.is_file() {
                let dst = profile_dir.join(dll);
                if let Err(e) = std::fs::copy(&src, &dst) {
                    panic!("failed to stage {dll} next to the engine binary: {e}");
                }
                staged += 1;
            }
        }
    }
    if staged > 0 {
        println!("cargo:warning=staged {staged} ANGLE dll(s) next to the engine binary");
    }
}
