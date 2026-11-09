//! User data storage: everything lives in %APPDATA%\browserr as plain files.
//!   settings.json  { "homepage": "...", "user_agent": "..." }  (hand-edited)
//!   history.jsonl  one {"ts": secs, "url": "..."} per navigation, appended
//!   bookmarks.json [ {"url": "...", "title": "..."} ]
//!   session.json   { "urls": ["..."], "active": N }  (written on tab changes)

use std::io::Write;
use std::sync::OnceLock;

/// Profile name from `--profile=X` or `--profile X` (sanitized); "default"
/// when absent. Every file below lives under profiles/<name>/ so profiles
/// are fully separate data dirs.
static PROFILE: OnceLock<String> = OnceLock::new();

fn profile_from_args() -> Option<String> {
    let args: Vec<String> = std::env::args().skip(1).collect();
    let mut it = args.iter();
    while let Some(a) = it.next() {
        if let Some(v) = a.strip_prefix("--profile=") {
            return Some(v.to_string());
        }
        if a == "--profile" {
            return it.next().cloned();
        }
    }
    None
}

pub fn sanitize_profile(v: &str) -> String {
    v.chars()
        .filter(|c| c.is_alphanumeric() || *c == '-' || *c == '_')
        .collect()
}

/// Whether an explicit profile was requested (else: show the picker).
pub fn profile_selected() -> bool {
    profile_from_args().is_some()
}

pub fn profile() -> &'static str {
    PROFILE.get_or_init(|| {
        profile_from_args()
            .map(|v| sanitize_profile(&v))
            .filter(|s| !s.is_empty())
            .unwrap_or_else(|| "default".to_string())
    })
}

/// %APPDATA%\browserr (falls back to %TEMP% if APPDATA is unset).
pub fn root_dir() -> std::path::PathBuf {
    std::env::var("APPDATA")
        .map(std::path::PathBuf::from)
        .unwrap_or_else(|_| std::env::temp_dir())
        .join("browserr")
}

/// %APPDATA%\browserr\profiles\<name> (falls back to %TEMP% if APPDATA is unset).
pub fn data_dir() -> std::path::PathBuf {
    root_dir().join("profiles").join(profile())
}

/// Known profile names: "default" plus every dir under profiles/.
pub fn list_profiles() -> Vec<String> {
    let mut out = vec!["default".to_string()];
    if let Ok(rd) = std::fs::read_dir(root_dir().join("profiles")) {
        for e in rd.flatten() {
            if e.path().is_dir() {
                let name = e.file_name().to_string_lossy().to_string();
                if name != "default" {
                    out.push(name);
                }
            }
        }
    }
    out
}

pub struct Settings {
    pub homepage: String,
    pub user_agent: String,
}

pub fn load_settings() -> Settings {
    let v: serde_json::Value = std::fs::read_to_string(data_dir().join("settings.json"))
        .ok()
        .and_then(|s| serde_json::from_str(&s).ok())
        .unwrap_or(serde_json::json!({}));
    Settings {
        homepage: v["homepage"].as_str().unwrap_or("").to_string(),
        user_agent: v["user_agent"].as_str().unwrap_or("").to_string(),
    }
}

pub fn append_history(url: &str) {
    let dir = data_dir();
    if std::fs::create_dir_all(&dir).is_err() {
        return;
    }
    let ts = std::time::SystemTime::now()
        .duration_since(std::time::UNIX_EPOCH)
        .map(|d| d.as_secs())
        .unwrap_or(0);
    let line = serde_json::json!({ "ts": ts, "url": url }).to_string();
    if let Ok(mut f) = std::fs::OpenOptions::new()
        .create(true)
        .append(true)
        .open(dir.join("history.jsonl"))
    {
        let _ = writeln!(f, "{line}");
    }
}

/// The most recent `max` history urls, newest first.
pub fn read_history(max: usize) -> Vec<String> {
    let Ok(f) = std::fs::File::open(data_dir().join("history.jsonl")) else {
        return Vec::new();
    };
    let lines: Vec<String> = std::io::BufRead::lines(std::io::BufReader::new(f))
        .map_while(Result::ok)
        .collect();
    let mut out = Vec::new();
    for line in lines.iter().rev() {
        if out.len() >= max {
            break;
        }
        if let Ok(v) = serde_json::from_str::<serde_json::Value>(line) {
            if let Some(u) = v["url"].as_str() {
                out.push(u.to_string());
            }
        }
    }
    out
}

pub fn load_bookmarks() -> Vec<(String, String)> {
    std::fs::read_to_string(data_dir().join("bookmarks.json"))
        .ok()
        .and_then(|s| serde_json::from_str::<serde_json::Value>(&s).ok())
        .and_then(|v| {
            v.as_array().map(|a| {
                a.iter()
                    .filter_map(|e| {
                        Some((
                            e["url"].as_str()?.to_string(),
                            e["title"].as_str().unwrap_or("").to_string(),
                        ))
                    })
                    .collect()
            })
        })
        .unwrap_or_default()
}

pub fn save_bookmarks(bookmarks: &[(String, String)]) {
    let dir = data_dir();
    if std::fs::create_dir_all(&dir).is_err() {
        return;
    }
    let v: Vec<_> = bookmarks
        .iter()
        .map(|(url, title)| serde_json::json!({ "url": url, "title": title }))
        .collect();
    let _ = std::fs::write(
        dir.join("bookmarks.json"),
        serde_json::to_string_pretty(&v).unwrap_or_else(|_| "[]".into()),
    );
}

pub fn save_session(urls: &[String], active: usize) {
    let dir = data_dir();
    if std::fs::create_dir_all(&dir).is_err() {
        return;
    }
    let v = serde_json::json!({ "urls": urls, "active": active });
    let _ = std::fs::write(dir.join("session.json"), v.to_string());
}

pub struct Session {
    pub urls: Vec<String>,
    pub active: usize,
}

pub fn load_session() -> Option<Session> {
    let v: serde_json::Value = serde_json::from_str(
        &std::fs::read_to_string(data_dir().join("session.json")).ok()?,
    )
    .ok()?;
    let urls: Vec<String> = v["urls"]
        .as_array()?
        .iter()
        .filter_map(|u| u.as_str().map(|s| s.to_string()))
        .collect();
    if urls.is_empty() {
        return None;
    }
    let active = v["active"].as_u64().unwrap_or(0) as usize;
    let len = urls.len();
    Some(Session {
        urls,
        active: active.min(len - 1),
    })
}
