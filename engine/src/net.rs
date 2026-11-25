//! Minimal HTTP client (one crypto stack, rustls + the platform
//! verifier) and the favicon decoder. Split out of main.rs.

use std::io::{BufRead, Read, Write};
use std::sync::Arc;

/// Minimal HTTP/HTTPS GET client (rustls + the platform cert verifier; the
/// aws-lc-rs provider installed at startup). Replaces ureq so the binary
/// ships exactly one crypto stack - servo-net already pins aws-lc-rs, and
/// ureq would have dragged in ring as a second one.
/// Follows up to 5 redirects; caps the body at `max_bytes`.
pub(crate) fn http_get(url: &str, max_bytes: u64) -> Result<Vec<u8>, String> {
    let mut current = url::Url::parse(url).map_err(|e| e.to_string())?;
    if current.scheme() != "http" && current.scheme() != "https" {
        return Err(format!("unsupported scheme: {}", current.scheme()));
    }
    for _ in 0..6 {
        // Redirect-hop: re-validate scheme and require an http host before
        // connecting (the join() below can otherwise turn a Location into
        // any scheme, and a relative Location can point at loopback).
        if current.scheme() != "http" && current.scheme() != "https" {
            return Err(format!("redirect scheme rejected: {}", current.scheme()));
        }
        let host = current.host_str().ok_or("no host")?.to_string();
        let mut path = current.path().to_string();
        if path.is_empty() {
            path = "/".into();
        }
        if let Some(q) = current.query() {
            path.push('?');
            path.push_str(q);
        }
        let secure = current.scheme() == "https";
        let port = current
            .port_or_known_default()
            .unwrap_or(if secure { 443 } else { 80 });

        let tcp = std::net::TcpStream::connect((host.as_str(), port))
            .map_err(|e| format!("connect {host}:{port}: {e}"))?;
        tcp.set_read_timeout(Some(std::time::Duration::from_secs(15)))
            .ok();
        tcp.set_write_timeout(Some(std::time::Duration::from_secs(15)))
            .ok();

        let stream: Box<dyn std::io::Read> = if secure {
            let verifier = rustls_platform_verifier::Verifier::new(Arc::new(
                rustls::crypto::aws_lc_rs::default_provider(),
            ))
            .map_err(|e| format!("platform verifier: {e}"))?;
            let config = rustls::ClientConfig::builder()
                .dangerous()
                .with_custom_certificate_verifier(std::sync::Arc::new(verifier))
                .with_no_client_auth();
            let name = rustls_pki_types::ServerName::try_from(host.clone())
                .map_err(|_| format!("bad server name: {host}"))?;
            let conn = rustls::ClientConnection::new(std::sync::Arc::new(config), name)
                .map_err(|e| format!("tls setup: {e}"))?;
            let mut tls = rustls::StreamOwned::new(conn, tcp);
            let req = format!(
                "GET {path} HTTP/1.1\r\nHost: {host}\r\nUser-Agent: BrowserrEngine/0.1\r\nAccept: */*\r\nConnection: close\r\n\r\n"
            );
            tls.write_all(req.as_bytes())
                .map_err(|e| format!("send: {e}"))?;
            Box::new(tls)
        } else {
            let mut tcp = tcp;
            let req = format!(
                "GET {path} HTTP/1.1\r\nHost: {host}\r\nUser-Agent: BrowserrEngine/0.1\r\nAccept: */*\r\nConnection: close\r\n\r\n"
            );
            tcp.write_all(req.as_bytes())
                .map_err(|e| format!("send: {e}"))?;
            Box::new(tcp)
        };

        let (status, location, body) = read_response(stream, max_bytes)?;
        if (301..=308).contains(&status) && status != 306 {
            let loc = location.ok_or("redirect without Location")?;
            current = current
                .join(&loc)
                .map_err(|e| format!("bad redirect {loc}: {e}"))?;
            continue;
        }
        if status != 200 {
            return Err(format!("HTTP {status}"));
        }
        return Ok(body);
    }
    Err("too many redirects".into())
}

/// Read one HTTP response off `stream`: status line + headers, then the body
/// (Content-Length, chunked, or read-to-end), capped at `max_bytes`.
fn read_response(
    mut stream: Box<dyn std::io::Read>,
    max_bytes: u64,
) -> Result<(u16, Option<String>, Vec<u8>), String> {
    let mut reader = std::io::BufReader::new(&mut stream);
    let mut status = 0u16;
    let mut location = None;
    let mut content_len: Option<u64> = None;
    let mut chunked = false;

    // Header section: read line by line until the empty line.
    let mut first = true;
    loop {
        let mut line = Vec::new();
        // Width-bounded read: a hostile server can claim an endless header
        // line without ever sending a newline; cap what we buffer before
        // checking. We stop at the first newline after MAX_HEADER_LINE
        // bytes or when the stream closes; either way the line must be
        // newline-terminated (and therefore bounded).
        loop {
            let mut b = [0u8; 1];
            let n = reader
                .read(&mut b)
                .map_err(|e| format!("header read: {e}"))?;
            if n == 0 {
                return Err("connection closed in headers".into());
            }
            line.push(b[0]);
            if b[0] == b'\n' {
                break;
            }
            if line.len() > (64 << 10) {
                return Err("headers too large".into());
            }
        }
        let trimmed = String::from_utf8_lossy(&line);
        let trimmed = trimmed.trim_end();
        if trimmed.is_empty() {
            break; // end of headers
        }
        if first {
            // "HTTP/1.1 200 OK"
            status = trimmed
                .split_whitespace()
                .nth(1)
                .and_then(|s| s.parse().ok())
                .ok_or(format!("bad status line: {trimmed}"))?;
            first = false;
            continue;
        }
        let lower = trimmed.to_ascii_lowercase();
        if let Some(v) = lower.strip_prefix("location:") {
            // Keep the original-case value (the header bytes, not lowered).
            location = Some(trimmed[trimmed.len() - v.trim().len()..].to_string());
        } else if let Some(v) = lower.strip_prefix("content-length:") {
            content_len = v.trim().parse().ok();
        } else if lower.starts_with("transfer-encoding:") && lower.contains("chunked") {
            chunked = true;
        }
    }

    let mut body = Vec::new();
    if chunked {
        loop {
            let mut size_line = Vec::new();
            reader
                .read_until(b'\n', &mut size_line)
                .map_err(|e| format!("chunk size read: {e}"))?;
            let size_str = String::from_utf8_lossy(&size_line);
            let size = u64::from_str_radix(size_str.trim().split(';').next().unwrap_or(""), 16)
                .map_err(|_| format!("bad chunk size: {size_str:?}"))?;
            if size == 0 {
                break;
            }
            if body.len() as u64 + size > max_bytes {
                return Err("body too large".into());
            }
            let start = body.len();
            body.resize(start + size as usize, 0);
            reader
                .read_exact(&mut body[start..])
                .map_err(|e| e.to_string())?;
            let mut crlf = [0u8; 2];
            let _ = reader.read_exact(&mut crlf); // chunk trailer
        }
    } else if let Some(n) = content_len {
        let n = n.min(max_bytes);
        let mut chunk = vec![0u8; n as usize];
        reader.read_exact(&mut chunk).map_err(|e| e.to_string())?;
        body = chunk;
    } else {
        reader
            .take(max_bytes)
            .read_to_end(&mut body)
            .map_err(|e| e.to_string())?;
    }
    Ok((status, location, body))
}

/// Fetch and decode an icon into 16x16 RGBA. Any failure returns None.
pub(crate) fn fetch_favicon(url: &str) -> Option<(Vec<u8>, u32, u32)> {
    if !url.starts_with("http://") && !url.starts_with("https://") {
        return None;
    }
    let bytes = http_get(url, 1 << 20).ok()?;
    let img = image::load_from_memory(&bytes).ok()?;
    let rgba = img
        .resize_exact(16, 16, image::imageops::FilterType::Lanczos3)
        .into_rgba8()
        .into_raw();
    Some((rgba, 16, 16))
}
