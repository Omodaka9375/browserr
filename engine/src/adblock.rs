//! EasyList-format adblock: the servo delegate that blocks matching
//! requests, plus filter-list download/loading. Split out of main.rs.

use crate::net::http_get;

pub(crate) struct ServoAdblockDelegate {
    pub(crate) adblock: Option<std::rc::Rc<::adblock::engine::Engine>>,
}

impl servo::ServoDelegate for ServoAdblockDelegate {
    fn load_web_resource(&self, load: servo::WebResourceLoad) {
        // Requests with a target webview go to the WebViewDelegate; this
        // catches the rest (no target webview known at the net layer).
        if let Some(engine) = &self.adblock
            && adblock_matches(engine, &load)
        {
            eprintln!("engine: adblock blocked {}", load.request().url);
            let response_url = load.request().url.clone().into_url();
            load.intercept(servo::WebResourceResponse::new(response_url))
                .cancel();
        }
    }
}

/// Whether the filter list matches this request (metadata only; the load
/// is not consumed here).
pub(crate) fn adblock_matches(
    engine: &::adblock::engine::Engine,
    load: &servo::WebResourceLoad,
) -> bool {
    // Main-frame documents are never blocked: a cancelled document response
    // wedges the pipeline for later navigations on that tab.
    if load.request().is_for_main_frame {
        return false;
    }
    let url = load.request().url.to_string();
    let req_type = request_type(load.request());
    let method = load.request().method.as_str();
    let Ok(req) = ::adblock::request::Request::new(&url, "", &req_type, method) else {
        return false;
    };
    engine.check_network_request(&req).should_block()
}

/// The EasyList request type of a load. A WebSocket handshake reaches the
/// embedder as an http(s) fetch with no destination, the same as fetch()
/// and XHR, so it is told apart by its `Upgrade: websocket` header.
fn request_type(request: &servo::WebResourceRequest) -> String {
    let is_websocket = request
        .headers
        .get(http::header::UPGRADE)
        .and_then(|value| value.to_str().ok())
        .is_some_and(|value| value.eq_ignore_ascii_case("websocket"));
    if is_websocket {
        return "websocket".to_string();
    }
    format!("{:?}", request.destination).to_lowercase()
}

const EASYLIST_URL: &str = "https://easylist.to/easylist/easylist.txt";
/// Re-download after a week.
const FILTER_LIST_MAX_AGE_SECS: u64 = 7 * 24 * 3600;

/// Download the EasyList-format filter file if missing or stale. Best effort:
/// a failed download keeps any existing file and just logs.
pub(crate) fn ensure_filter_list(path: &std::path::Path) {
    let stale = match std::fs::metadata(path) {
        Ok(m) => m
            .modified()
            .ok()
            .and_then(|t| t.elapsed().ok())
            .map(|e| e.as_secs() > FILTER_LIST_MAX_AGE_SECS)
            .unwrap_or(true),
        Err(_) => true,
    };
    if !stale {
        return;
    }
    eprintln!(
        "engine: downloading adblock filter list -> {}",
        path.display()
    );
    if let Ok(dir) = path.parent().ok_or(()) {
        let _ = std::fs::create_dir_all(dir);
    }
    match http_get(EASYLIST_URL, 32 << 20) {
        Ok(body) => {
            let _ = std::fs::write(path, body);
            eprintln!("engine: filter list saved");
        }
        Err(e) => eprintln!("engine: filter list download failed: {e}"),
    }
}

pub(crate) fn load_adblock(
    path: &std::path::Path,
) -> Option<std::rc::Rc<::adblock::engine::Engine>> {
    let text = std::fs::read_to_string(path).ok()?;
    eprintln!("engine: adblock enabled");
    Some(std::rc::Rc::new(
        ::adblock::engine::Engine::new_with_list_text(text),
    ))
}
