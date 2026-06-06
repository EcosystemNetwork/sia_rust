//! axum app + launchers for the runs visualizer. Port of `sia/web/server.py`.
//!
//! `create_app(runs_dir)` builds the router; `serve(...)` runs it in the foreground
//! (the `sia web` command); `serve_in_background(...)` starts it on a daemon thread
//! so the orchestrator can expose a live dashboard during `sia run`.

use std::net::ToSocketAddrs;
use std::path::PathBuf;
use std::sync::Arc;

use axum::extract::{Path, State};
use axum::http::{header, StatusCode};
use axum::response::{IntoResponse, Response};
use axum::routing::get;
use axum::{Json, Router};

use crate::assets;
use crate::web::runs as runs_data;

type AppState = Arc<PathBuf>;

/// Build the axum application serving the runs under `runs_dir` (no credential
/// store — house competitors disabled).
pub fn create_app(runs_dir: impl Into<PathBuf>) -> Router {
    let runs_root = canonical_runs_root(runs_dir);
    // The Superradiant (waiting room + battle coordinator) shares this server. Its
    // admin/control endpoints require `SUPERRADIANT_ADMIN_TOKEN` when set.
    let superradiant = crate::superradiant::router(crate::superradiant::SuperradiantHandle::new(
        runs_root.clone(),
        admin_token_from_env(),
    ));
    assemble(runs_root, superradiant)
}

/// Like [`create_app`] but with an optional Postgres credential store, enabling
/// the credential UI + in-process house competitors (the Vercel/Railway path).
#[cfg(feature = "superradiant-db")]
pub fn create_app_with_store(
    runs_dir: impl Into<PathBuf>,
    store: Option<crate::superradiant::credentials::CredentialStore>,
) -> Router {
    let runs_root = canonical_runs_root(runs_dir);
    let superradiant = crate::superradiant::router(
        crate::superradiant::SuperradiantHandle::with_credentials(
            runs_root.clone(),
            admin_token_from_env(),
            store,
        ),
    );
    assemble(runs_root, superradiant)
}

fn canonical_runs_root(runs_dir: impl Into<PathBuf>) -> PathBuf {
    let runs_dir = runs_dir.into();
    std::fs::canonicalize(&runs_dir).unwrap_or(runs_dir)
}

fn admin_token_from_env() -> Option<String> {
    std::env::var("SUPERRADIANT_ADMIN_TOKEN")
        .ok()
        .filter(|s| !s.is_empty())
}

/// Assemble the runs-visualizer routes, merge the Superradiant router, and apply
/// CORS (when built with `superradiant-db`, for the Vercel/Railway split).
fn assemble(runs_root: PathBuf, superradiant: Router) -> Router {
    let state: AppState = Arc::new(runs_root);
    #[allow(unused_mut)]
    let mut app = Router::new()
        .route("/api/runs", get(api_runs))
        .route("/api/runs/:run_name", get(api_run))
        .route(
            "/api/runs/:run_name/gens/:gen_name/eval",
            get(api_eval_details),
        )
        .route(
            "/api/runs/:run_name/gens/:gen_name/artifact/:label",
            get(api_artifact),
        )
        .route(
            "/api/runs/:run_name/gens/:gen_name/trajectory/:qid",
            get(api_trajectory),
        )
        .route(
            "/api/runs/:run_name/gens/:gen_name/openhands",
            get(api_openhands_sessions),
        )
        .route(
            "/api/runs/:run_name/gens/:gen_name/openhands/:session",
            get(api_openhands_events),
        )
        .route(
            "/api/runs/:run_name/gens/:gen_name/telemetry",
            get(api_gen_telemetry),
        )
        .route("/api/runs/:run_name/telemetry", get(api_run_telemetry))
        .route("/api/runs/:run_name/metrics", get(api_run_metrics))
        .route("/api/runs/:run_name/scheduler", get(api_scheduler_timeline))
        .route(
            "/api/runs/:run_name/conservation",
            get(api_conservation_timeline),
        )
        .route(
            "/api/runs/:run_name/gens/:gen_name/scheduler",
            get(api_scheduler_decision),
        )
        .route(
            "/api/runs/:run_name/gens/:gen_name/weights",
            get(api_weight_update),
        )
        .route("/", get(index))
        .route("/config.js", get(config_js))
        .with_state(state)
        .merge(superradiant);

    #[cfg(feature = "superradiant-db")]
    {
        app = app.layer(cors_layer());
    }
    app
}

/// CORS for the static frontend (Vercel) → backend (Railway) split. Origins
/// come from `SUPERRADIANT_CORS_ORIGIN` (comma-separated); unset = permissive
/// (dev). `X-Admin-Token` is allowed so the dashboard can authenticate.
#[cfg(feature = "superradiant-db")]
fn cors_layer() -> tower_http::cors::CorsLayer {
    use axum::http::{header, HeaderName, Method};
    use tower_http::cors::{Any, CorsLayer};

    let methods = [Method::GET, Method::POST, Method::DELETE, Method::OPTIONS];
    let headers = [header::CONTENT_TYPE, HeaderName::from_static("x-admin-token")];

    match std::env::var("SUPERRADIANT_CORS_ORIGIN") {
        Ok(origins) if !origins.trim().is_empty() => {
            let list: Vec<axum::http::HeaderValue> = origins
                .split(',')
                .filter_map(|o| o.trim().parse().ok())
                .collect();
            CorsLayer::new()
                .allow_origin(list)
                .allow_methods(methods)
                .allow_headers(headers)
        }
        _ => CorsLayer::new()
            .allow_origin(Any)
            .allow_methods(methods)
            .allow_headers(headers),
    }
}

/// Connect the Postgres credential store from `DATABASE_URL`, if set. Logs and
/// returns `None` on any failure so the server still boots (external workers
/// keep working; the credential UI is disabled).
#[cfg(feature = "superradiant-db")]
pub async fn connect_credential_store(
) -> Option<crate::superradiant::credentials::CredentialStore> {
    let url = std::env::var("DATABASE_URL")
        .ok()
        .filter(|u| !u.trim().is_empty())?;
    match crate::superradiant::credentials::CredentialStore::connect(&url).await {
        Ok(store) => {
            println!("Superradiant credential store connected (Postgres).");
            Some(store)
        }
        Err(e) => {
            eprintln!("WARNING: credential store disabled — {e}");
            None
        }
    }
}

async fn api_runs(State(root): State<AppState>) -> Json<Vec<runs_data::RunSummary>> {
    Json(runs_data::list_runs(&root))
}

async fn api_run(State(root): State<AppState>, Path(run_name): Path<String>) -> Response {
    match runs_data::get_run(&root, &run_name) {
        Some(detail) => Json(detail).into_response(),
        None => not_found(&format!("Run not found: {run_name}")),
    }
}

async fn api_eval_details(
    State(root): State<AppState>,
    Path((run_name, gen_name)): Path<(String, String)>,
) -> Response {
    match runs_data::get_eval_details(&root, &run_name, &gen_name) {
        Some(details) => Json(details).into_response(),
        None => not_found("No evaluation details found"),
    }
}

async fn api_artifact(
    State(root): State<AppState>,
    Path((run_name, gen_name, label)): Path<(String, String, String)>,
) -> Response {
    match runs_data::get_artifact_text(&root, &run_name, &gen_name, &label) {
        Some(text) => ([(header::CONTENT_TYPE, "text/plain; charset=utf-8")], text).into_response(),
        None => not_found(&format!("Artifact not found: {label}")),
    }
}

async fn api_trajectory(
    State(root): State<AppState>,
    Path((run_name, gen_name, qid)): Path<(String, String, i64)>,
) -> Response {
    match runs_data::get_trajectory(&root, &run_name, &gen_name, qid) {
        Some(turns) => Json(turns).into_response(),
        None => not_found(&format!("Trajectory not found: q{qid}")),
    }
}

async fn api_openhands_sessions(
    State(root): State<AppState>,
    Path((run_name, gen_name)): Path<(String, String)>,
) -> Response {
    match runs_data::list_openhands_sessions(&root, &run_name, &gen_name) {
        Some(sessions) => Json(sessions).into_response(),
        None => not_found("Generation not found"),
    }
}

async fn api_openhands_events(
    State(root): State<AppState>,
    Path((run_name, gen_name, session)): Path<(String, String, String)>,
) -> Response {
    match runs_data::get_openhands_events(&root, &run_name, &gen_name, &session) {
        Some(events) => Json(events).into_response(),
        None => not_found("Session not found"),
    }
}

async fn api_gen_telemetry(
    State(root): State<AppState>,
    Path((run_name, gen_name)): Path<(String, String)>,
) -> Response {
    match runs_data::get_generation_telemetry(&root, &run_name, &gen_name) {
        Some(telemetry) => Json(telemetry).into_response(),
        None => not_found("No telemetry found"),
    }
}

async fn api_run_telemetry(State(root): State<AppState>, Path(run_name): Path<String>) -> Response {
    match runs_data::get_run_telemetry(&root, &run_name) {
        Some(telemetry) => Json(telemetry).into_response(),
        None => not_found(&format!("Run not found: {run_name}")),
    }
}

async fn api_run_metrics(State(root): State<AppState>, Path(run_name): Path<String>) -> Response {
    match runs_data::get_run_metrics_summary(&root, &run_name) {
        Some(metrics) => Json(metrics).into_response(),
        None => not_found(&format!("Run not found: {run_name}")),
    }
}

// Run-level scheduler decision timeline (issue #85). File-backed: returns the
// ordered decisions recorded so far; there is no SSE/live streaming.
async fn api_scheduler_timeline(
    State(root): State<AppState>,
    Path(run_name): Path<String>,
) -> Response {
    match runs_data::get_scheduler_timeline(&root, &run_name) {
        Some(timeline) => Json(timeline).into_response(),
        None => not_found(&format!("Run not found: {run_name}")),
    }
}

// Run-level capability-conservation timeline (anti-Goodhart guard). File-backed:
// aggregates each generation's conservation.json.
async fn api_conservation_timeline(
    State(root): State<AppState>,
    Path(run_name): Path<String>,
) -> Response {
    match runs_data::get_conservation_timeline(&root, &run_name) {
        Some(timeline) => Json(timeline).into_response(),
        None => not_found(&format!("Run not found: {run_name}")),
    }
}

async fn api_scheduler_decision(
    State(root): State<AppState>,
    Path((run_name, gen_name)): Path<(String, String)>,
) -> Response {
    match runs_data::get_scheduler_decision(&root, &run_name, &gen_name) {
        Some(decision) => Json(decision).into_response(),
        None => not_found("No scheduler decision found"),
    }
}

async fn api_weight_update(
    State(root): State<AppState>,
    Path((run_name, gen_name)): Path<(String, String)>,
) -> Response {
    match runs_data::get_weight_update(&root, &run_name, &gen_name) {
        Some(update) => Json(update).into_response(),
        None => not_found("No weight update found"),
    }
}

async fn index() -> Response {
    match assets::web_static_bytes("index.html") {
        Some(bytes) => {
            ([(header::CONTENT_TYPE, "text/html; charset=utf-8")], bytes).into_response()
        }
        None => not_found("index.html not found"),
    }
}

/// Serve the Superradiant frontend config (backend base URL). Harmless 404 when
/// not bundled; the page then defaults to same-origin.
async fn config_js() -> Response {
    match assets::web_static_bytes("config.js") {
        Some(bytes) => (
            [(header::CONTENT_TYPE, "application/javascript; charset=utf-8")],
            bytes,
        )
            .into_response(),
        None => not_found("config.js not found"),
    }
}

fn not_found(detail: &str) -> Response {
    (
        StatusCode::NOT_FOUND,
        Json(serde_json::json!({"detail": detail})),
    )
        .into_response()
}

/// Run the server in the foreground (blocks). Used by `sia web`.
pub fn serve(host: &str, port: u16, runs_dir: &str, _open_browser: bool) -> crate::SiaResult<()> {
    let addr = resolve_addr(host, port)?;
    let resolved = std::fs::canonicalize(runs_dir).unwrap_or_else(|_| PathBuf::from(runs_dir));
    println!(
        "SIA visualizer serving {} at http://{}",
        resolved.display(),
        addr
    );
    let runs_dir = runs_dir.to_string();

    let rt = tokio::runtime::Runtime::new().map_err(|e| crate::SiaError::new(e.to_string()))?;
    rt.block_on(async move {
        // Build the app inside the runtime so the credential store (async
        // connect) can be wired in when `superradiant-db` is enabled.
        #[cfg(feature = "superradiant-db")]
        let app = {
            let store = connect_credential_store().await;
            create_app_with_store(&runs_dir, store)
        };
        #[cfg(not(feature = "superradiant-db"))]
        let app = create_app(&runs_dir);

        let listener = tokio::net::TcpListener::bind(addr)
            .await
            .map_err(|e| crate::SiaError::new(format!("Could not bind {addr}: {e}")))?;
        axum::serve(listener, app)
            .await
            .map_err(|e| crate::SiaError::new(e.to_string()))
    })
}

/// Start the server on a daemon thread; never blocks. Returns the thread handle.
pub fn serve_in_background(
    host: &str,
    port: u16,
    runs_dir: &str,
) -> Option<std::thread::JoinHandle<()>> {
    let host_owned = host.to_string();
    let runs_dir = runs_dir.to_string();
    let handle = std::thread::Builder::new()
        .name("sia-web".to_string())
        .spawn(move || {
            let _ = serve(&host_owned, port, &runs_dir, false);
        })
        .ok()?;
    println!("Live dashboard: http://{host}:{port}");
    Some(handle)
}

fn resolve_addr(host: &str, port: u16) -> crate::SiaResult<std::net::SocketAddr> {
    format!("{host}:{port}")
        .to_socket_addrs()
        .map_err(|e| crate::SiaError::new(format!("Invalid host/port {host}:{port}: {e}")))?
        .next()
        .ok_or_else(|| crate::SiaError::new(format!("Could not resolve {host}:{port}")))
}
