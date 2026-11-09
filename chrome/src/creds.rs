//! Windows Credential Manager-backed password storage: one generic credential
//! per (profile, host) named "Browserr:<profile>:<host>", blob = JSON {u, p}.
//! The credential manager encrypts at rest under the logged-in user.

use windows::core::{PCWSTR, PWSTR};
use windows::Win32::Security::Credentials::{
    CredDeleteW, CredFree, CredReadW, CredWriteW, CREDENTIALW, CRED_FLAGS, CRED_PERSIST_LOCAL_MACHINE,
    CRED_TYPE_GENERIC,
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
        let blob = std::slice::from_raw_parts((*pcred).CredentialBlob, (*pcred).CredentialBlobSize as usize);
        let json = String::from_utf8_lossy(blob).to_string();
        CredFree(pcred as *const _);
        let v: serde_json::Value = serde_json::from_str(&json).ok()?;
        Some((
            v["u"].as_str()?.to_string(),
            v["p"].as_str()?.to_string(),
        ))
    }
}

/// Delete the saved credential for a host.
pub fn forget(host: &str) -> bool {
    let t = target(host);
    unsafe { CredDeleteW(PCWSTR(t.as_ptr()), CRED_TYPE_GENERIC, None).is_ok() }
}
