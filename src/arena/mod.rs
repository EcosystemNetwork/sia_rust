//! The agent Arena: a waiting room where external agents (e.g. Hermes) connect
//! and idle, an admin selects benchmarks and hits "Go", and many agents run
//! those benchmarks at once. Results are scored by each benchmark's own
//! `evaluate.py` and persisted into the standard `runs/` layout so the existing
//! SIA Studio dashboard renders them.
//!
//! Layers:
//! - [`state`]: the live coordination state (waiting room + battle queues).
//! - [`benchmarks`]: discovery + serving of task specs/data.
//! - [`eval`]: submission persistence + scoring bridge.
//! - [`routes`]: the axum HTTP surface (agent-facing + admin-facing + SSE).

pub mod benchmarks;
pub mod eval;
pub mod routes;
pub mod state;

pub use routes::{arena_index_html, router};
pub use state::ArenaHandle;
