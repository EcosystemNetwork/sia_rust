//! Axum HTTP surface for the Superradiant.
//!
//! Two audiences share one router:
//! - **Agent-facing** (`/api/superradiant/register|heartbeat|result`, benchmark spec/
//!   files): the waiting-room protocol an external worker speaks. Each agent
//!   authenticates with the bearer token issued at registration.
//! - **Admin-facing** (`/api/superradiant/state|stream|selection|go|reset|kick`): the
//!   control panel. Protected by `X-Admin-Token` when `SUPERRADIANT_ADMIN_TOKEN`
//!   is configured.

use std::sync::Arc;

use axum::extract::{Path, Query, State};
use axum::http::{header, HeaderMap, StatusCode};
use axum::response::sse::{Event, KeepAlive, Sse};
use axum::response::{IntoResponse, Response};
use axum::routing::{get, post};
use axum::{Json, Router};
use serde::Deserialize;
use serde_json::{json, Value};
use tokio_stream::wrappers::BroadcastStream;
use tokio_stream::StreamExt;

use crate::superradiant::state::{
    AssignmentOutcome, SuperradiantError, SuperradiantHandle, SuperradiantRunConfig,
};
use crate::superradiant::{benchmarks, eval};

/// Shared router state.
pub type SuperradiantState = Arc<SuperradiantHandle>;

/// Build the Superradiant router with its handle baked in. The returned router carries
/// unit state so it can be merged into the runs visualizer router.
pub fn router(handle: SuperradiantState) -> Router {
    #[allow(unused_mut)]
    let mut router = Router::new()
        // Admin / dashboard
        .route("/superradiant", get(superradiant_page))
        .route("/api/superradiant/state", get(api_state))
        .route("/api/superradiant/stream", get(api_stream))
        .route("/api/superradiant/selection", post(api_selection))
        .route("/api/superradiant/go", post(api_go))
        .route("/api/superradiant/reset", post(api_reset))
        .route("/api/superradiant/kick", post(api_kick))
        // Agent-facing
        .route("/api/superradiant/register", post(api_register))
        .route("/api/superradiant/heartbeat", post(api_heartbeat))
        .route("/api/superradiant/result", post(api_result))
        .route("/api/superradiant/benchmarks", get(api_benchmarks))
        .route("/api/superradiant/benchmarks/:id/spec", get(api_bench_spec))
        .route(
            "/api/superradiant/benchmarks/:id/files/*path",
            get(api_bench_file),
        );

    // User-supplied provider credentials + house competitors (Postgres-backed).
    #[cfg(feature = "superradiant-db")]
    {
        router = router
            .route(
                "/api/superradiant/providers",
                get(api_providers_list).post(api_providers_create),
            )
            .route(
                "/api/superradiant/providers/:id",
                axum::routing::delete(api_providers_delete),
            )
            .route("/api/superradiant/house", post(api_house));
    }

    router.with_state(handle)
}

// ---- error mapping -------------------------------------------------------- //

fn err_response(e: SuperradiantError) -> Response {
    let (status, msg) = match e {
        SuperradiantError::UnknownAgent => (StatusCode::NOT_FOUND, "unknown agent"),
        SuperradiantError::BadToken => (StatusCode::UNAUTHORIZED, "bad agent token"),
        SuperradiantError::UnknownAssignment => (StatusCode::NOT_FOUND, "unknown assignment"),
        SuperradiantError::UnknownBenchmark => (StatusCode::BAD_REQUEST, "unknown benchmark"),
        SuperradiantError::Forbidden => (StatusCode::FORBIDDEN, "admin token required"),
    };
    (status, Json(json!({ "detail": msg }))).into_response()
}

fn agent_token(headers: &HeaderMap) -> &str {
    headers
        .get("x-agent-token")
        .and_then(|v| v.to_str().ok())
        .unwrap_or("")
}

fn admin_token<'a>(headers: &'a HeaderMap, q: &'a TokenQuery) -> Option<&'a str> {
    headers
        .get("x-admin-token")
        .and_then(|v| v.to_str().ok())
        .or(q.token.as_deref())
}

#[derive(Debug, Default, Deserialize)]
pub struct TokenQuery {
    /// Fallback admin token for clients (EventSource) that cannot set headers.
    pub token: Option<String>,
}

// ---- admin / dashboard ---------------------------------------------------- //

async fn superradiant_page() -> Response {
    (
        [(header::CONTENT_TYPE, "text/html; charset=utf-8")],
        superradiant_index_html(),
    )
        .into_response()
}

/// The Superradiant dashboard HTML (bundled at build time).
pub fn superradiant_index_html() -> &'static [u8] {
    crate::assets::web_static_bytes("superradiant.html").unwrap_or(b"superradiant.html not bundled")
}

async fn api_state(
    State(h): State<SuperradiantState>,
    headers: HeaderMap,
    Query(q): Query<TokenQuery>,
) -> Response {
    if let Err(e) = h.check_admin(admin_token(&headers, &q)) {
        return err_response(e);
    }
    Json(h.snapshot()).into_response()
}

async fn api_stream(
    State(h): State<SuperradiantState>,
    headers: HeaderMap,
    Query(q): Query<TokenQuery>,
) -> Response {
    if let Err(e) = h.check_admin(admin_token(&headers, &q)) {
        return err_response(e);
    }
    let initial = h.snapshot().to_string();
    let rx = h.subscribe();
    let live = BroadcastStream::new(rx).filter_map(|msg| match msg {
        Ok(s) => Some(Ok::<Event, std::convert::Infallible>(
            Event::default().data(s),
        )),
        Err(_) => None, // lagged receiver: drop the gap, next snapshot is full state
    });
    let stream = tokio_stream::once(Ok::<Event, std::convert::Infallible>(
        Event::default().data(initial),
    ))
    .chain(live);
    Sse::new(stream)
        .keep_alive(KeepAlive::default())
        .into_response()
}

#[derive(Debug, Deserialize)]
struct SelectionBody {
    #[serde(default)]
    benchmark_ids: Vec<String>,
    #[serde(default)]
    config: Option<SuperradiantRunConfig>,
}

async fn api_selection(
    State(h): State<SuperradiantState>,
    headers: HeaderMap,
    Query(q): Query<TokenQuery>,
    Json(body): Json<SelectionBody>,
) -> Response {
    if let Err(e) = h.check_admin(admin_token(&headers, &q)) {
        return err_response(e);
    }
    match h.set_selection(body.benchmark_ids, body.config) {
        Ok(()) => Json(h.snapshot()).into_response(),
        Err(e) => err_response(e),
    }
}

#[derive(Debug, Default, Deserialize)]
struct GoBody {
    #[serde(default)]
    agent_ids: Vec<String>,
    #[serde(default)]
    benchmark_ids: Vec<String>,
}

async fn api_go(
    State(h): State<SuperradiantState>,
    headers: HeaderMap,
    Query(q): Query<TokenQuery>,
    Json(body): Json<GoBody>,
) -> Response {
    if let Err(e) = h.check_admin(admin_token(&headers, &q)) {
        return err_response(e);
    }
    match h.go(body.agent_ids, body.benchmark_ids) {
        Ok(battle) => {
            // Kick off in-process drivers for any house competitors in this battle.
            #[cfg(feature = "superradiant-db")]
            h.spawn_house_drivers();
            (StatusCode::OK, Json(json!({ "battle": battle }))).into_response()
        }
        Err(e) => err_response(e),
    }
}

async fn api_reset(
    State(h): State<SuperradiantState>,
    headers: HeaderMap,
    Query(q): Query<TokenQuery>,
) -> Response {
    if let Err(e) = h.check_admin(admin_token(&headers, &q)) {
        return err_response(e);
    }
    h.reset();
    Json(h.snapshot()).into_response()
}

#[derive(Debug, Deserialize)]
struct KickBody {
    agent_id: String,
}

async fn api_kick(
    State(h): State<SuperradiantState>,
    headers: HeaderMap,
    Query(q): Query<TokenQuery>,
    Json(body): Json<KickBody>,
) -> Response {
    if let Err(e) = h.check_admin(admin_token(&headers, &q)) {
        return err_response(e);
    }
    h.kick(&body.agent_id);
    Json(h.snapshot()).into_response()
}

// ---- agent-facing --------------------------------------------------------- //

#[derive(Debug, Deserialize)]
struct RegisterBody {
    #[serde(default)]
    name: String,
    #[serde(default)]
    kind: String,
    #[serde(default)]
    meta: Value,
}

async fn api_register(
    State(h): State<SuperradiantState>,
    Json(body): Json<RegisterBody>,
) -> Response {
    let meta = if body.meta.is_null() {
        json!({})
    } else {
        body.meta
    };
    let (id, token) = h.register(&body.name, &body.kind, meta);
    (
        StatusCode::CREATED,
        Json(json!({ "agent_id": id, "token": token })),
    )
        .into_response()
}

#[derive(Debug, Deserialize)]
struct HeartbeatBody {
    agent_id: String,
    #[serde(default)]
    progress: Option<String>,
}

async fn api_heartbeat(
    State(h): State<SuperradiantState>,
    headers: HeaderMap,
    Json(body): Json<HeartbeatBody>,
) -> Response {
    match h.heartbeat(&body.agent_id, agent_token(&headers), body.progress) {
        Ok(reply) => Json(reply).into_response(),
        Err(e) => err_response(e),
    }
}

#[derive(Debug, Deserialize)]
struct ResultBody {
    agent_id: String,
    assignment_id: String,
    /// Task-specific predictions; written to `submission.json` for evaluate.py.
    #[serde(default)]
    submission: Value,
    /// Optional trajectory for the dashboard's trajectory viewer.
    #[serde(default)]
    agent_execution: Option<Value>,
    /// Optional token/timing metrics, written to `telemetry.json`.
    #[serde(default)]
    telemetry: Option<Value>,
    /// If set, the agent is reporting a failure rather than a submission.
    #[serde(default)]
    error: Option<String>,
}

async fn api_result(
    State(h): State<SuperradiantState>,
    headers: HeaderMap,
    Json(body): Json<ResultBody>,
) -> Response {
    let ctx = match h.begin_result(&body.agent_id, agent_token(&headers), &body.assignment_id) {
        Ok(c) => c,
        Err(e) => return err_response(e),
    };

    let outcome = if let Some(err) = body.error.clone() {
        AssignmentOutcome {
            accuracy_percent: None,
            run_dir: None,
            error: Some(err),
        }
    } else {
        // Scoring shells out to evaluate.py — do it off the async runtime.
        let runs_root = h.runs_root.clone();
        let battle_id = ctx.battle_id.clone();
        let agent_name = ctx.agent_name.clone();
        let benchmark_id = ctx.benchmark_id.clone();
        let submission = body.submission.clone();
        let exec = body.agent_execution.clone();
        let telemetry = body.telemetry.clone();
        let joined = tokio::task::spawn_blocking(move || {
            eval::persist_and_score(
                &runs_root,
                &battle_id,
                &agent_name,
                &benchmark_id,
                &submission,
                exec.as_ref(),
                telemetry.as_ref(),
            )
        })
        .await;
        match joined {
            Ok(o) => o,
            Err(e) => AssignmentOutcome {
                accuracy_percent: None,
                run_dir: None,
                error: Some(format!("scoring task panicked: {e}")),
            },
        }
    };

    let resp = json!({
        "status": if outcome.error.is_some() { "failed" } else { "scored" },
        "accuracy_percent": outcome.accuracy_percent,
        "run_dir": outcome.run_dir,
        "error": outcome.error,
    });
    if let Err(e) = h.complete_result(&body.agent_id, &body.assignment_id, outcome) {
        return err_response(e);
    }
    Json(resp).into_response()
}

async fn api_benchmarks(State(h): State<SuperradiantState>) -> Response {
    // Re-discover so newly added task dirs appear without a restart.
    let snap = h.snapshot();
    Json(snap.get("benchmarks").cloned().unwrap_or(json!([]))).into_response()
}

async fn api_bench_spec(Path(id): Path<String>) -> Response {
    let Some(task_md) = benchmarks::task_md(&id) else {
        return (
            StatusCode::NOT_FOUND,
            Json(json!({"detail": "unknown benchmark"})),
        )
            .into_response();
    };
    Json(json!({
        "id": id,
        "task_md": task_md,
        "files": benchmarks::public_files(&id),
    }))
    .into_response()
}

async fn api_bench_file(Path((id, path)): Path<(String, String)>) -> Response {
    match benchmarks::read_public_file(&id, &path) {
        Some(bytes) => {
            let ctype = mime_guess::from_path(&path)
                .first_or_octet_stream()
                .to_string();
            ([(header::CONTENT_TYPE, ctype)], bytes).into_response()
        }
        None => (
            StatusCode::NOT_FOUND,
            Json(json!({"detail": "file not found"})),
        )
            .into_response(),
    }
}

// ---- provider credentials + house competitors (superradiant-db) ----------- //

#[cfg(feature = "superradiant-db")]
fn store_unavailable() -> Response {
    (
        StatusCode::SERVICE_UNAVAILABLE,
        Json(json!({"detail": "credential store not configured (set DATABASE_URL)"})),
    )
        .into_response()
}

#[cfg(feature = "superradiant-db")]
fn internal_err(e: crate::error::SiaError) -> Response {
    (
        StatusCode::INTERNAL_SERVER_ERROR,
        Json(json!({"detail": e.to_string()})),
    )
        .into_response()
}

#[cfg(feature = "superradiant-db")]
async fn api_providers_list(
    State(h): State<SuperradiantState>,
    headers: HeaderMap,
    Query(q): Query<TokenQuery>,
) -> Response {
    if let Err(e) = h.check_admin(admin_token(&headers, &q)) {
        return err_response(e);
    }
    let Some(store) = h.credentials.as_ref() else {
        return store_unavailable();
    };
    match store.list().await {
        Ok(rows) => Json(rows).into_response(),
        Err(e) => internal_err(e),
    }
}

#[cfg(feature = "superradiant-db")]
async fn api_providers_create(
    State(h): State<SuperradiantState>,
    headers: HeaderMap,
    Query(q): Query<TokenQuery>,
    Json(body): Json<crate::superradiant::credentials::NewCredential>,
) -> Response {
    if let Err(e) = h.check_admin(admin_token(&headers, &q)) {
        return err_response(e);
    }
    let Some(store) = h.credentials.as_ref() else {
        return store_unavailable();
    };
    match store.create(body).await {
        Ok(row) => (StatusCode::CREATED, Json(row)).into_response(),
        // Validation failures are the user's fault → 400, not 500.
        Err(e) => (
            StatusCode::BAD_REQUEST,
            Json(json!({"detail": e.to_string()})),
        )
            .into_response(),
    }
}

#[cfg(feature = "superradiant-db")]
async fn api_providers_delete(
    State(h): State<SuperradiantState>,
    headers: HeaderMap,
    Query(q): Query<TokenQuery>,
    Path(id): Path<String>,
) -> Response {
    if let Err(e) = h.check_admin(admin_token(&headers, &q)) {
        return err_response(e);
    }
    let Some(store) = h.credentials.as_ref() else {
        return store_unavailable();
    };
    match store.delete(&id).await {
        Ok(true) => (StatusCode::OK, Json(json!({"deleted": id}))).into_response(),
        Ok(false) => (
            StatusCode::NOT_FOUND,
            Json(json!({"detail": "credential not found"})),
        )
            .into_response(),
        Err(e) => internal_err(e),
    }
}

#[cfg(feature = "superradiant-db")]
#[derive(Debug, Default, Deserialize)]
struct HouseBody {
    #[serde(default)]
    credential_ids: Vec<String>,
}

/// Register the given stored credentials as house competitors in the waiting
/// room (idempotent). They then join the next `go` like external workers.
#[cfg(feature = "superradiant-db")]
async fn api_house(
    State(h): State<SuperradiantState>,
    headers: HeaderMap,
    Query(q): Query<TokenQuery>,
    Json(body): Json<HouseBody>,
) -> Response {
    if let Err(e) = h.check_admin(admin_token(&headers, &q)) {
        return err_response(e);
    }
    let Some(store) = h.credentials.as_ref() else {
        return store_unavailable();
    };
    for id in &body.credential_ids {
        match store.resolve(id).await {
            Ok(c) => {
                h.register_house(&c.id, &c.name, &c.model, &c.client_kind);
            }
            Err(e) => {
                return (
                    StatusCode::BAD_REQUEST,
                    Json(json!({"detail": e.to_string()})),
                )
                    .into_response()
            }
        }
    }
    Json(h.snapshot()).into_response()
}

#[cfg(test)]
mod tests {
    use super::*;
    use axum::body::Body;
    use axum::http::Request;
    use tower::ServiceExt;

    fn app() -> Router {
        router(SuperradiantHandle::new(
            std::env::temp_dir(),
            Some("secret".into()),
        ))
    }

    async fn body_json(resp: Response) -> Value {
        let bytes = axum::body::to_bytes(resp.into_body(), 1 << 20)
            .await
            .unwrap();
        serde_json::from_slice(&bytes).unwrap_or(Value::Null)
    }

    #[tokio::test]
    async fn register_and_heartbeat_roundtrip() {
        let app = app();
        let resp = app
            .clone()
            .oneshot(
                Request::post("/api/superradiant/register")
                    .header("content-type", "application/json")
                    .body(Body::from(r#"{"name":"hermes","kind":"hermes"}"#))
                    .unwrap(),
            )
            .await
            .unwrap();
        assert_eq!(resp.status(), StatusCode::CREATED);
        let v = body_json(resp).await;
        let id = v["agent_id"].as_str().unwrap().to_string();
        let token = v["token"].as_str().unwrap().to_string();

        let resp = app
            .oneshot(
                Request::post("/api/superradiant/heartbeat")
                    .header("content-type", "application/json")
                    .header("x-agent-token", token)
                    .body(Body::from(format!(r#"{{"agent_id":"{id}"}}"#)))
                    .unwrap(),
            )
            .await
            .unwrap();
        assert_eq!(resp.status(), StatusCode::OK);
        let v = body_json(resp).await;
        assert_eq!(v["status"], "waiting");
    }

    #[tokio::test]
    async fn admin_endpoints_require_token() {
        let app = app();
        let resp = app
            .oneshot(
                Request::get("/api/superradiant/state")
                    .body(Body::empty())
                    .unwrap(),
            )
            .await
            .unwrap();
        assert_eq!(resp.status(), StatusCode::FORBIDDEN);
    }

    #[tokio::test]
    async fn admin_state_with_token_ok() {
        let app = app();
        let resp = app
            .oneshot(
                Request::get("/api/superradiant/state?token=secret")
                    .body(Body::empty())
                    .unwrap(),
            )
            .await
            .unwrap();
        assert_eq!(resp.status(), StatusCode::OK);
        let v = body_json(resp).await;
        assert!(v.get("agents").is_some());
        assert!(v.get("benchmarks").is_some());
    }
}
