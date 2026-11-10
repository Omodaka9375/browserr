//! Windows Credential Manager-backed password storage: one generic credential
//! per (profile, host) named "Browserr:<profile>:<host>", blob = JSON {u, p}.
//! The credential manager encrypts at rest under the logged-in user.

use windows::core::{PCWSTR, PWSTR};
use windows::Win32::Security::Credentials::{
    CredDeleteW, CredEnumerateW, CredFree, CredReadW, CredWriteW, CREDENTIALW, CRED_FLAGS,
    CRED_PERSIST_LOCAL_MACHINE, CRED_TYPE_GENERIC,
};

use crate::store;

fn to_wide(s: &str) -> Vec<u16> {
    s.encode_utf16().chain(std::iter::once(0)).collect()
}

fn target(host: &str) -> Vec<u16> {
    to_wide(&format!("Browserr:{}:{}", store::profile(), host))
}

/// Save username + password for a host (overwrites any existing entry).
pub fn save(host: &str, username: &str, password: &str) -> bool {
    let mut blob = serde_json::json!({ "u": username, "p": password })
        .to_string()
        .into_bytes();
    let mut t = target(host);
    let mut user = to_wide(username);
    let cred = CREDENTIALW {
        Flags: CRED_FLAGS(0),
        Type: CRED_TYPE_GENERIC,
        TargetName: PWSTR(t.as_mut_ptr()),
        Comment: PWSTR::null(),
        LastWritten: Default::default(),
        CredentialBlobSize: blob.len() as u32,
        CredentialBlob: blob.as_mut_ptr(),
        Persist: CRED_PERSIST_LOCAL_MACHINE,
        AttributeCount: 0,
        Attributes: std::ptr::null_mut(),
        TargetAlias: PWSTR::null(),
        UserName: PWSTR(user.as_mut_ptr()),
    };
    unsafe { CredWriteW(&cred, 0).is_ok() }
}

/// Load (username, password) for a host.
pub fn load(host: &str) -> Option<(String, String)> {
    let t = target(host);
    let mut pcred: *mut CREDENTIALW = std::ptr::null_mut();
    unsafe {
        if CredReadW(PCWSTR(t.as_ptr()), CRED_TYPE_GENERIC, None, &mut pcred).is_err() {
            return None;
        }
        let blob = std::slice::from_raw_parts(
            (*pcred).CredentialBlob,
            (*pcred).CredentialBlobSize as usize,
        );
        let json = String::from_utf8_lossy(blob).to_string();
        CredFree(pcred as *const _);
        let v: serde_json::Value = serde_json::from_str(&json).ok()?;
        Some((v["u"].as_str()?.to_string(), v["p"].as_str()?.to_string()))
    }
}

/// Delete the saved credential for a host.
pub fn forget(host: &str) -> bool {
    let t = target(host);
    unsafe { CredDeleteW(PCWSTR(t.as_ptr()), CRED_TYPE_GENERIC, None).is_ok() }
}

/// All saved (host, username) pairs for this profile, unordered.
pub fn list() -> Vec<(String, String)> {
    unsafe {
        let filter = to_wide(&format!("Browserr:{}:*", store::profile()));
        let mut count: u32 = 0;
        let mut buf: *mut *mut CREDENTIALW = std::ptr::null_mut();
        if CredEnumerateW(PCWSTR(filter.as_ptr()), None, &mut count, &mut buf).is_err() {
            return Vec::new();
        }
        let creds = std::slice::from_raw_parts(buf, count as usize);
        let mut out = Vec::new();
        for c in creds {
            let c: &CREDENTIALW = &**c;
            let name = c.TargetName.to_string().unwrap_or_default();
            // Target name is Browserr:<profile>:<host>.
            let Some(host) = name.split(':').nth(2) else {
                continue;
            };
            if c.CredentialBlob.is_null() {
                continue;
            }
            let blob = std::slice::from_raw_parts(c.CredentialBlob, c.CredentialBlobSize as usize);
            let Ok(v) = serde_json::from_str::<serde_json::Value>(&String::from_utf8_lossy(blob))
            else {
                continue;
            };
            out.push((host.to_string(), v["u"].as_str().unwrap_or("").to_string()));
        }
        CredFree(buf as *const _);
        out
    }
}
