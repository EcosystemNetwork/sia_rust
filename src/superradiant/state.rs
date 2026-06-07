//! In-memory state for the agent Superradiant: a waiting room of connected agents plus
//! the admin-driven "battle" coordinator that fans benchmark assignments out to
//! many agents at once.
//!
//! The Superradiant is a layer on top of the existing runs visualizer. It owns no
//! durable storage of its own: every scored assignment is persisted into the
//! standard `runs/` layout (see [`crate::superradiant::eval`]) so the existing SIA
//! Studio dashboard renders trajectories and accuracy for free. This module is
//! purely the live coordination state — who is connected, what they have been
//! told to run, and how far along they are.

use std::collections::{HashMap, VecDeque};
use std::path::PathBuf;
use std::sync::{Arc, Mutex};
use std::time::{SystemTime, UNIX_EPOCH};

use serde::{Deserialize, Serialize};
use serde_json::{json, Value};
use tokio::sync::broadcast;

use crate::superradiant::benchmarks::{discover_benchmarks, BenchmarkRef};

/// How long an agent may go without a heartbeat before it is considered stale
/// and dropped from the waiting room.
const STALE_AFTER_MS: u128 = 30_000;

/// Lifecycle of a connected agent in the waiting room.
#[derive(Debug, Clone, Copy, PartialEq, Eq, Serialize, Deserialize)]
#[serde(rename_all = "snake_case")]
pub enum AgentStatus {
    /// Connected and idle, waiting for the admin to start a battle.
    Waiting,
    /// Has pending assignments queued but has not picked one up yet.
    Assigned,
    /// Actively executing an assignment.
    Running,
    /// Finished every queued assignment in the current battle.
    Done,
}

/// Lifecycle of a single (agent × benchmark) assignment.
#[derive(Debug, Clone, Copy, PartialEq, Eq, Serialize, Deserialize)]
#[serde(rename_all = "snake_case")]
pub enum AssignmentStatus {
    Pending,
    Running,
    Scored,
    Failed,
}

/// One benchmark handed to one agent as part of a battle.
#[derive(Debug, Clone, Serialize)]
pub struct Assignment {
    pub id: String,
    pub battle_id: String,
    pub benchmark_id: String,
    pub status: AssignmentStatus,
    pub started_ms: Option<u128>,
    pub finished_ms: Option<u128>,
    pub accuracy_percent: Option<f64>,
    /// Run directory (under `runs/`) where the scored submission was persisted.
    pub run_dir: Option<String>,
    pub error: Option<String>,
}

impl Assignment {
    fn new(id: String, battle_id: String, benchmark_id: String) -> Self {
        Assignment {
            id,
            battle_id,
            benchmark_id,
            status: AssignmentStatus::Pending,
            started_ms: None,
            finished_ms: None,
            accuracy_percent: None,
            run_dir: None,
            error: None,
        }
    }
}

/// A connected agent (e.g. a Hermes worker) sitting in the waiting room.
#[derive(Debug, Clone, Serialize)]
pub struct AgentSession {
    pub id: String,
    pub name: String,
    pub kind: String,
    /// Bearer token issued at registration. Never serialized to clients.
    #[serde(skip)]
    pub token: String,
    pub status: AgentStatus,
    pub registered_at_ms: u128,
    pub last_heartbeat_ms: u128,
    /// Free-form agent-reported metadata (model, backend, version, ...).
    pub meta: Value,
    /// Human-readable progress string the agent reports while running.
    pub progress: Option<String>,
    /// Assignments not yet picked up, in dispatch order.
    pub queue: VecDeque<Assignment>,
    /// Assignments picked up this session (running + finished), newest last.
    pub history: Vec<Assignment>,
}

impl AgentSession {
    /// All assignments (queued + history) for snapshot purposes.
    fn all_assignments(&self) -> Vec<Assignment> {
        let mut out: Vec<Assignment> = self.history.clone();
        out.extend(self.queue.iter().cloned());
        out
    }
}

/// A battle: one admin "Go" — a set of benchmarks fanned across a set of agents.
#[derive(Debug, Clone, Serialize)]
pub struct BattleSession {
    pub id: String,
    pub created_ms: u128,
    pub benchmark_ids: Vec<String>,
    pub agent_ids: Vec<String>,
    /// "running" until every assignment is scored/failed, then "complete".
    pub status: String,
}

/// Suggested run configuration the admin sets and agents read at pickup time.
#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct SuperradiantRunConfig {
    /// Suggested model id (agents may honor or ignore it).
    #[serde(default)]
    pub model_name: String,
    #[serde(default = "default_max_turns")]
    pub max_turns: u32,
    #[serde(default = "default_time_limit")]
    pub time_limit_secs: u64,
}

fn default_max_turns() -> u32 {
    30
}
fn default_time_limit() -> u64 {
    1800
}

impl Default for SuperradiantRunConfig {
    fn default() -> Self {
        SuperradiantRunConfig {
            model_name: String::new(),
            max_turns: default_max_turns(),
            time_limit_secs: default_time_limit(),
        }
    }
}

/// What an agent receives when it picks up work on a heartbeat.
#[derive(Debug, Clone, Serialize)]
pub struct DispatchedAssignment {
    pub assignment_id: String,
    pub battle_id: String,
    pub benchmark_id: String,
    pub config: SuperradiantRunConfig,
}

/// Reply to an agent heartbeat.
#[derive(Debug, Clone, Serialize)]
pub struct HeartbeatReply {
    pub status: AgentStatus,
    /// Present when the agent should start running a benchmark now.
    pub assignment: Option<DispatchedAssignment>,
}

/// Context captured when an agent begins posting a result, used by the route
/// layer to score and persist before committing the outcome back to state.
#[derive(Debug, Clone)]
pub struct ResultContext {
    pub agent_name: String,
    pub agent_kind: String,
    pub battle_id: String,
    pub benchmark_id: String,
    pub assignment_index: usize,
}

/// Final outcome of scoring a posted submission.
#[derive(Debug, Clone)]
pub struct AssignmentOutcome {
    pub accuracy_percent: Option<f64>,
    pub run_dir: Option<String>,
    pub error: Option<String>,
}

/// Mutable inner state guarded by a mutex.
struct SuperradiantInner {
    agents: HashMap<String, AgentSession>,
    benchmarks: Vec<BenchmarkRef>,
    selection: Vec<String>,
    config: SuperradiantRunConfig,
    battles: Vec<BattleSession>,
    seq: u64,
}

/// Errors surfaced by Superradiant operations (mapped to HTTP status in the route layer).
#[derive(Debug, Clone, PartialEq, Eq)]
pub enum SuperradiantError {
    UnknownAgent,
    BadToken,
    UnknownAssignment,
    UnknownBenchmark,
    Forbidden,
}

/// Shared, cloneable handle to the Superradiant. All mutation goes through `&self`.
pub struct SuperradiantHandle {
    inner: Mutex<SuperradiantInner>,
    events: broadcast::Sender<String>,
    pub runs_root: PathBuf,
    /// When set, admin/control endpoints require a matching `X-Admin-Token`.
    pub admin_token: Option<String>,
    /// Postgres-backed store for user-supplied provider credentials. `None`
    /// when `DATABASE_URL` is unset — the credential UI / house competitors are
    /// disabled but external workers keep working.
    #[cfg(feature = "superradiant-db")]
    pub credentials: Option<crate::superradiant::credentials::CredentialStore>,
}

fn now_ms() -> u128 {
    SystemTime::now()
        .duration_since(UNIX_EPOCH)
        .map(|d| d.as_millis())
        .unwrap_or(0)
}

impl SuperradiantHandle {
    /// Build a handle, discovering selectable benchmarks up front. No credential
    /// store (house competitors disabled).
    pub fn new(runs_root: impl Into<PathBuf>, admin_token: Option<String>) -> Arc<Self> {
        Self::build(
            runs_root.into(),
            admin_token,
            #[cfg(feature = "superradiant-db")]
            None,
        )
    }

    /// Build a handle with an optional Postgres credential store, enabling the
    /// credential UI + in-process house competitors.
    #[cfg(feature = "superradiant-db")]
    pub fn with_credentials(
        runs_root: impl Into<PathBuf>,
        admin_token: Option<String>,
        credentials: Option<crate::superradiant::credentials::CredentialStore>,
    ) -> Arc<Self> {
        Self::build(runs_root.into(), admin_token, credentials)
    }

    fn build(
        runs_root: PathBuf,
        admin_token: Option<String>,
        #[cfg(feature = "superradiant-db")] credentials: Option<
            crate::superradiant::credentials::CredentialStore,
        >,
    ) -> Arc<Self> {
        let (events, _rx) = broadcast::channel(256);
        let benchmarks = discover_benchmarks();
        Arc::new(SuperradiantHandle {
            inner: Mutex::new(SuperradiantInner {
                agents: HashMap::new(),
                benchmarks,
                selection: Vec::new(),
                config: SuperradiantRunConfig::default(),
                battles: Vec::new(),
                seq: 0,
            }),
            events,
            runs_root,
            admin_token,
            #[cfg(feature = "superradiant-db")]
            credentials,
        })
    }

    /// Subscribe to the live snapshot stream (one JSON snapshot per change).
    pub fn subscribe(&self) -> broadcast::Receiver<String> {
        self.events.subscribe()
    }

    /// Validate an admin token against the configured one (no-op if unset).
    ///
    /// The comparison is constant-time (via [`subtle`]) so a network attacker
    /// cannot recover the token byte-by-byte from response-timing differences.
    /// When no token is configured this is a no-op — production deploys must set
    /// `SUPERRADIANT_ADMIN_TOKEN`; the server refuses to expose a credential
    /// store or bind non-loopback without one (see `web::server`).
    pub fn check_admin(&self, provided: Option<&str>) -> Result<(), SuperradiantError> {
        match &self.admin_token {
            None => Ok(()),
            Some(expected) => {
                use subtle::ConstantTimeEq;
                // `ct_eq` on slices returns 0 (false) for unequal lengths and
                // otherwise compares every byte without early exit.
                let provided = provided.unwrap_or("");
                if provided.as_bytes().ct_eq(expected.as_bytes()).into() {
                    Ok(())
                } else {
                    Err(SuperradiantError::Forbidden)
                }
            }
        }
    }

    // --- agent-facing ---------------------------------------------------- //

    /// Register a new agent into the waiting room. Returns `(agent_id, token)`.
    pub fn register(&self, name: &str, kind: &str, meta: Value) -> (String, String) {
        let mut g = self.inner.lock().unwrap();
        g.seq += 1;
        let seq = g.seq;
        let now = now_ms();
        let id = format!("agent_{seq:x}_{:x}", now & 0xffffff);
        let token = format!("tok_{seq:x}{:x}", now);
        let name = if name.trim().is_empty() {
            format!("agent-{seq}")
        } else {
            name.to_string()
        };
        let session = AgentSession {
            id: id.clone(),
            name,
            kind: if kind.is_empty() {
                "agent".into()
            } else {
                kind.into()
            },
            token: token.clone(),
            status: AgentStatus::Waiting,
            registered_at_ms: now,
            last_heartbeat_ms: now,
            meta,
            progress: None,
            queue: VecDeque::new(),
            history: Vec::new(),
        };
        g.agents.insert(id.clone(), session);
        self.broadcast(&g);
        (id, token)
    }

    /// Record a heartbeat, prune stale peers, and dispatch the next assignment
    /// if the agent has one queued.
    pub fn heartbeat(
        &self,
        agent_id: &str,
        token: &str,
        progress: Option<String>,
    ) -> Result<HeartbeatReply, SuperradiantError> {
        let mut g = self.inner.lock().unwrap();
        self.prune_stale(&mut g);
        let config = g.config.clone();

        let agent = g
            .agents
            .get_mut(agent_id)
            .ok_or(SuperradiantError::UnknownAgent)?;
        if agent.token != token {
            return Err(SuperradiantError::BadToken);
        }
        agent.last_heartbeat_ms = now_ms();
        if let Some(p) = progress {
            agent.progress = Some(p);
        }

        // Hand out the next queued assignment, if idle and work is pending.
        let mut dispatched = None;
        if agent.status != AgentStatus::Running {
            if let Some(mut a) = agent.queue.pop_front() {
                a.status = AssignmentStatus::Running;
                a.started_ms = Some(now_ms());
                dispatched = Some(DispatchedAssignment {
                    assignment_id: a.id.clone(),
                    battle_id: a.battle_id.clone(),
                    benchmark_id: a.benchmark_id.clone(),
                    config: config.clone(),
                });
                agent.history.push(a);
                agent.status = AgentStatus::Running;
            }
        }
        let status = agent.status;
        self.broadcast(&g);
        Ok(HeartbeatReply {
            status,
            assignment: dispatched,
        })
    }

    /// Begin posting a result: validate, locate the running assignment, and
    /// return the context the route layer needs to persist + score it.
    pub fn begin_result(
        &self,
        agent_id: &str,
        token: &str,
        assignment_id: &str,
    ) -> Result<ResultContext, SuperradiantError> {
        let g = self.inner.lock().unwrap();
        let agent = g
            .agents
            .get(agent_id)
            .ok_or(SuperradiantError::UnknownAgent)?;
        if agent.token != token {
            return Err(SuperradiantError::BadToken);
        }
        let (idx, a) = agent
            .history
            .iter()
            .enumerate()
            .find(|(_, a)| a.id == assignment_id)
            .ok_or(SuperradiantError::UnknownAssignment)?;
        Ok(ResultContext {
            agent_name: agent.name.clone(),
            agent_kind: agent.kind.clone(),
            battle_id: a.battle_id.clone(),
            benchmark_id: a.benchmark_id.clone(),
            assignment_index: idx,
        })
    }

    /// Commit a scored outcome back into state and advance the agent.
    pub fn complete_result(
        &self,
        agent_id: &str,
        assignment_id: &str,
        outcome: AssignmentOutcome,
    ) -> Result<(), SuperradiantError> {
        let mut g = self.inner.lock().unwrap();
        {
            let agent = g
                .agents
                .get_mut(agent_id)
                .ok_or(SuperradiantError::UnknownAgent)?;
            let a = agent
                .history
                .iter_mut()
                .find(|a| a.id == assignment_id)
                .ok_or(SuperradiantError::UnknownAssignment)?;
            a.finished_ms = Some(now_ms());
            a.accuracy_percent = outcome.accuracy_percent;
            a.run_dir = outcome.run_dir;
            a.error = outcome.error.clone();
            a.status = if outcome.error.is_some() {
                AssignmentStatus::Failed
            } else {
                AssignmentStatus::Scored
            };
            agent.progress = None;
            // Idle again: either grab next work on the upcoming heartbeat or done.
            agent.status = if agent.queue.is_empty() {
                AgentStatus::Done
            } else {
                AgentStatus::Assigned
            };
        }
        self.refresh_battle_status(&mut g);
        self.broadcast(&g);
        Ok(())
    }

    /// Drop an agent that disconnected mid-flight (best-effort).
    pub fn kick(&self, agent_id: &str) {
        let mut g = self.inner.lock().unwrap();
        g.agents.remove(agent_id);
        self.broadcast(&g);
    }

    // --- admin-facing ---------------------------------------------------- //

    /// Set the admin's benchmark selection and run config.
    pub fn set_selection(
        &self,
        benchmark_ids: Vec<String>,
        config: Option<SuperradiantRunConfig>,
    ) -> Result<(), SuperradiantError> {
        let mut g = self.inner.lock().unwrap();
        for id in &benchmark_ids {
            if !g.benchmarks.iter().any(|b| &b.id == id) {
                return Err(SuperradiantError::UnknownBenchmark);
            }
        }
        g.selection = benchmark_ids;
        if let Some(c) = config {
            g.config = c;
        }
        self.broadcast(&g);
        Ok(())
    }

    /// Start a battle: fan the given benchmarks across the given agents. When
    /// `agent_ids` is empty, every waiting/done agent is included; when
    /// `benchmark_ids` is empty, the current selection is used.
    pub fn go(
        &self,
        agent_ids: Vec<String>,
        benchmark_ids: Vec<String>,
    ) -> Result<BattleSession, SuperradiantError> {
        let mut g = self.inner.lock().unwrap();

        let benches = if benchmark_ids.is_empty() {
            g.selection.clone()
        } else {
            benchmark_ids
        };
        if benches.is_empty() {
            return Err(SuperradiantError::UnknownBenchmark);
        }
        for id in &benches {
            if !g.benchmarks.iter().any(|b| &b.id == id) {
                return Err(SuperradiantError::UnknownBenchmark);
            }
        }

        let targets: Vec<String> = if agent_ids.is_empty() {
            g.agents
                .values()
                .filter(|a| matches!(a.status, AgentStatus::Waiting | AgentStatus::Done))
                .map(|a| a.id.clone())
                .collect()
        } else {
            agent_ids
        };
        if targets.is_empty() {
            return Err(SuperradiantError::UnknownAgent);
        }

        g.seq += 1;
        let battle_id = format!("battle_{:x}_{:x}", g.seq, now_ms() & 0xffffff);
        let mut assigned_agents = Vec::new();

        for aid in &targets {
            let Some(agent) = g.agents.get(aid) else {
                continue;
            };
            // Skip agents that are mid-run.
            if agent.status == AgentStatus::Running {
                continue;
            }
            let mut new_queue: VecDeque<Assignment> = VecDeque::new();
            for bid in &benches {
                g.seq += 1;
                let asg_id = format!("asg_{:x}", g.seq);
                new_queue.push_back(Assignment::new(asg_id, battle_id.clone(), bid.clone()));
            }
            // Re-borrow mutably now that ids are minted.
            if let Some(agent) = g.agents.get_mut(aid) {
                agent.queue = new_queue;
                agent.history.clear();
                agent.progress = None;
                agent.status = AgentStatus::Assigned;
                assigned_agents.push(aid.clone());
            }
        }

        let battle = BattleSession {
            id: battle_id,
            created_ms: now_ms(),
            benchmark_ids: benches,
            agent_ids: assigned_agents,
            status: "running".into(),
        };
        g.battles.push(battle.clone());
        self.broadcast(&g);
        Ok(battle)
    }

    /// Clear all queues/history and return every agent to the waiting room.
    pub fn reset(&self) {
        let mut g = self.inner.lock().unwrap();
        for agent in g.agents.values_mut() {
            agent.queue.clear();
            agent.history.clear();
            agent.progress = None;
            agent.status = AgentStatus::Waiting;
        }
        g.battles.clear();
        self.broadcast(&g);
    }

    // --- snapshots ------------------------------------------------------- //

    /// Build the current public snapshot (also what the SSE stream emits).
    pub fn snapshot(&self) -> Value {
        let mut g = self.inner.lock().unwrap();
        self.prune_stale(&mut g);
        self.snapshot_locked(&g)
    }

    fn snapshot_locked(&self, g: &SuperradiantInner) -> Value {
        let mut agents: Vec<Value> = g
            .agents
            .values()
            .map(|a| {
                json!({
                    "id": a.id,
                    "name": a.name,
                    "kind": a.kind,
                    "status": a.status,
                    "registered_at_ms": a.registered_at_ms,
                    "last_heartbeat_ms": a.last_heartbeat_ms,
                    "meta": a.meta,
                    "progress": a.progress,
                    "assignments": a.all_assignments(),
                })
            })
            .collect();
        agents.sort_by_key(|v| {
            v.get("registered_at_ms")
                .and_then(|x| x.as_u64())
                .unwrap_or(0)
        });

        // Leaderboard: best accuracy per agent across the latest battle.
        let leaderboard = self.leaderboard(g);

        json!({
            "agents": agents,
            "benchmarks": g.benchmarks,
            "selection": g.selection,
            "config": g.config,
            "battles": g.battles,
            "leaderboard": leaderboard,
            "ts_ms": now_ms(),
        })
    }

    fn leaderboard(&self, g: &SuperradiantInner) -> Vec<Value> {
        let mut rows: Vec<Value> = Vec::new();
        for a in g.agents.values() {
            let scored: Vec<&Assignment> = a
                .history
                .iter()
                .filter(|x| x.status == AssignmentStatus::Scored)
                .collect();
            if scored.is_empty() {
                continue;
            }
            let total: f64 = scored.iter().filter_map(|x| x.accuracy_percent).sum();
            let count = scored
                .iter()
                .filter(|x| x.accuracy_percent.is_some())
                .count();
            let avg = if count > 0 { total / count as f64 } else { 0.0 };
            rows.push(json!({
                "agent_id": a.id,
                "name": a.name,
                "kind": a.kind,
                "benchmarks_scored": count,
                "avg_accuracy_percent": (avg * 100.0).round() / 100.0,
            }));
        }
        rows.sort_by(|x, y| {
            let ax = x
                .get("avg_accuracy_percent")
                .and_then(|v| v.as_f64())
                .unwrap_or(0.0);
            let ay = y
                .get("avg_accuracy_percent")
                .and_then(|v| v.as_f64())
                .unwrap_or(0.0);
            ay.partial_cmp(&ax).unwrap_or(std::cmp::Ordering::Equal)
        });
        rows
    }

    // --- internals ------------------------------------------------------- //

    fn refresh_battle_status(&self, g: &mut SuperradiantInner) {
        // A battle is complete once no agent has pending/running work for it.
        let active_battles: std::collections::HashSet<String> = g
            .agents
            .values()
            .flat_map(|a| {
                a.queue.iter().map(|x| x.battle_id.clone()).chain(
                    a.history
                        .iter()
                        .filter(|x| {
                            matches!(
                                x.status,
                                AssignmentStatus::Pending | AssignmentStatus::Running
                            )
                        })
                        .map(|x| x.battle_id.clone()),
                )
            })
            .collect();
        for b in g.battles.iter_mut() {
            if b.status == "running" && !active_battles.contains(&b.id) {
                b.status = "complete".into();
            }
        }
    }

    fn prune_stale(&self, g: &mut SuperradiantInner) {
        let now = now_ms();
        // Never drop an agent that is mid-run; only idle stragglers. House
        // competitors are server-driven (no heartbeats) so they are never pruned.
        g.agents.retain(|_, a| {
            if a.status == AgentStatus::Running || a.kind == "house" {
                return true;
            }
            now.saturating_sub(a.last_heartbeat_ms) <= STALE_AFTER_MS
        });
    }

    fn broadcast(&self, g: &SuperradiantInner) {
        let snap = self.snapshot_locked(g);
        // Best-effort: a send error just means no current subscribers.
        let _ = self.events.send(snap.to_string());
    }

    // --- house competitors (in-process, server-driven) ------------------- //

    /// Register (or idempotently refresh) a house competitor backed by a stored
    /// credential. House agents sit in the waiting room like external workers
    /// but are driven by the server during a battle. Returns the agent id.
    #[cfg(feature = "superradiant-db")]
    pub fn register_house(
        &self,
        credential_id: &str,
        name: &str,
        model: &str,
        client_kind: &str,
    ) -> String {
        let mut g = self.inner.lock().unwrap();
        // Reuse the existing house agent for this credential if present.
        if let Some(existing) = g.agents.values().find(|a| {
            a.kind == "house"
                && a.meta.get("credential_id").and_then(|v| v.as_str()) == Some(credential_id)
        }) {
            let id = existing.id.clone();
            self.broadcast(&g);
            return id;
        }
        g.seq += 1;
        let seq = g.seq;
        let now = now_ms();
        let id = format!("house_{seq:x}_{:x}", now & 0xffffff);
        let session = AgentSession {
            id: id.clone(),
            name: if name.trim().is_empty() {
                model.to_string()
            } else {
                name.to_string()
            },
            kind: "house".into(),
            token: String::new(),
            status: AgentStatus::Waiting,
            registered_at_ms: now,
            last_heartbeat_ms: now,
            meta: json!({
                "credential_id": credential_id,
                "model": model,
                "provider": client_kind,
                "house": true,
            }),
            progress: None,
            queue: VecDeque::new(),
            history: Vec::new(),
        };
        g.agents.insert(id.clone(), session);
        self.broadcast(&g);
        id
    }

    /// On startup, re-register every stored credential as a house competitor so
    /// they reappear in the waiting room without the operator re-selecting them.
    /// Idempotent (reuses existing house agents) and best-effort. No-op without a
    /// credential store.
    #[cfg(feature = "superradiant-db")]
    pub async fn rehydrate_house_from_store(&self) {
        let Some(store) = self.credentials.clone() else {
            return;
        };
        match store.list().await {
            Ok(creds) => {
                let n = creds.len();
                for c in creds {
                    self.register_house(&c.id, &c.name, &c.model, &c.client_kind);
                }
                if n > 0 {
                    println!(
                        "Superradiant: rehydrated {n} house competitor(s) from stored credentials."
                    );
                }
            }
            Err(e) => eprintln!("WARNING: could not rehydrate house competitors: {e}"),
        }
    }

    /// Pop the next queued assignment for a house agent and mark it running.
    /// Mirrors the heartbeat dispatch path but for the server-driven loop.
    #[cfg(feature = "superradiant-db")]
    fn dispatch_house(&self, agent_id: &str) -> Option<DispatchedAssignment> {
        let mut g = self.inner.lock().unwrap();
        let config = g.config.clone();
        let agent = g.agents.get_mut(agent_id)?;
        if agent.status == AgentStatus::Running {
            return None;
        }
        let mut a = agent.queue.pop_front()?;
        a.status = AssignmentStatus::Running;
        a.started_ms = Some(now_ms());
        let dispatched = DispatchedAssignment {
            assignment_id: a.id.clone(),
            battle_id: a.battle_id.clone(),
            benchmark_id: a.benchmark_id.clone(),
            config,
        };
        agent.progress = Some(format!("running {}", a.benchmark_id));
        agent.history.push(a);
        agent.status = AgentStatus::Running;
        self.broadcast(&g);
        Some(dispatched)
    }

    /// Current display name of an agent, for the persisted run directory.
    #[cfg(feature = "superradiant-db")]
    fn agent_name_of(&self, agent_id: &str) -> Option<String> {
        let g = self.inner.lock().unwrap();
        g.agents.get(agent_id).map(|a| a.name.clone())
    }

    /// Mark every outstanding assignment for a house agent as failed (used when
    /// its credential can't be resolved before any work runs).
    #[cfg(feature = "superradiant-db")]
    fn fail_remaining(&self, agent_id: &str, msg: &str) {
        let mut g = self.inner.lock().unwrap();
        if let Some(agent) = g.agents.get_mut(agent_id) {
            while let Some(mut a) = agent.queue.pop_front() {
                a.status = AssignmentStatus::Failed;
                a.finished_ms = Some(now_ms());
                a.error = Some(msg.to_string());
                agent.history.push(a);
            }
            for a in agent.history.iter_mut() {
                if matches!(a.status, AssignmentStatus::Running | AssignmentStatus::Pending) {
                    a.status = AssignmentStatus::Failed;
                    a.finished_ms = Some(now_ms());
                    a.error = Some(msg.to_string());
                }
            }
            agent.progress = None;
            agent.status = AgentStatus::Done;
        }
        self.refresh_battle_status(&mut g);
        self.broadcast(&g);
    }

    /// After a `go`, spawn a background driver per house agent that has queued
    /// work. Each driver runs its assignments serially in-process and commits
    /// results through the same path external workers use. No-op without a
    /// credential store. Must be called from within the Tokio runtime.
    #[cfg(feature = "superradiant-db")]
    pub fn spawn_house_drivers(self: &Arc<Self>) {
        let Some(store) = self.credentials.clone() else {
            return;
        };
        let targets: Vec<(String, String)> = {
            let g = self.inner.lock().unwrap();
            g.agents
                .values()
                .filter(|a| {
                    a.kind == "house" && !a.queue.is_empty() && a.status != AgentStatus::Running
                })
                .map(|a| {
                    (
                        a.id.clone(),
                        a.meta
                            .get("credential_id")
                            .and_then(|v| v.as_str())
                            .unwrap_or_default()
                            .to_string(),
                    )
                })
                .collect()
        };
        for (agent_id, cred_id) in targets {
            let handle = Arc::clone(self);
            let store = store.clone();
            tokio::spawn(async move {
                handle.run_house_agent(agent_id, cred_id, store).await;
            });
        }
    }

    /// Drive one house agent's queue to completion.
    #[cfg(feature = "superradiant-db")]
    async fn run_house_agent(
        self: Arc<Self>,
        agent_id: String,
        cred_id: String,
        store: crate::superradiant::credentials::CredentialStore,
    ) {
        let cred = match store.resolve(&cred_id).await {
            Ok(c) => c,
            Err(e) => {
                self.fail_remaining(&agent_id, &format!("credential error: {e}"));
                return;
            }
        };
        while let Some(d) = self.dispatch_house(&agent_id) {
            let agent_name = self
                .agent_name_of(&agent_id)
                .unwrap_or_else(|| agent_id.clone());
            let battle_id = d.battle_id.clone();
            let benchmark_id = d.benchmark_id.clone();
            let model = cred.model.clone();
            let assignment_id = d.assignment_id.clone();
            // Independent clones moved into the blocking closure; the originals
            // above stay live for recording the result afterwards.
            let cred_run = cred.clone();
            let runs_root_run = self.runs_root.clone();
            let agent_name_run = agent_name.clone();
            let battle_id_run = battle_id.clone();
            let benchmark_id_run = benchmark_id.clone();
            let config = d.config.clone();
            let outcome = tokio::task::spawn_blocking(move || {
                crate::superradiant::house::run_house_assignment(
                    &cred_run,
                    &runs_root_run,
                    &battle_id_run,
                    &agent_name_run,
                    &benchmark_id_run,
                    &config,
                )
            })
            .await
            .unwrap_or_else(|e| AssignmentOutcome {
                accuracy_percent: None,
                run_dir: None,
                error: Some(format!("house task panicked: {e}")),
            });
            // Persist to the all-time leaderboard before the outcome is moved.
            let acc = outcome.accuracy_percent;
            let run_dir = outcome.run_dir.clone();
            let _ = self.complete_result(&agent_id, &assignment_id, outcome);
            if let Some(acc) = acc {
                if let Err(e) = store
                    .record_result(
                        &battle_id,
                        &agent_name,
                        "house",
                        &benchmark_id,
                        acc,
                        Some(&model),
                        run_dir.as_deref(),
                    )
                    .await
                {
                    eprintln!("WARNING: could not record leaderboard result (house): {e}");
                }
            }
        }
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    fn handle() -> std::sync::Arc<SuperradiantHandle> {
        SuperradiantHandle::new(std::env::temp_dir(), None)
    }

    #[test]
    fn register_then_waiting() {
        let h = handle();
        let (id, token) = h.register("hermes", "hermes", json!({"model": "x"}));
        let reply = h.heartbeat(&id, &token, None).unwrap();
        assert_eq!(reply.status, AgentStatus::Waiting);
        assert!(reply.assignment.is_none());
    }

    #[test]
    fn bad_token_rejected() {
        let h = handle();
        let (id, _token) = h.register("a", "agent", json!({}));
        assert_eq!(
            h.heartbeat(&id, "nope", None).unwrap_err(),
            SuperradiantError::BadToken
        );
    }

    #[test]
    fn go_dispatches_on_heartbeat() {
        let h = handle();
        // Use a benchmark id that discovery will have found, or fall back: inject.
        let bench = first_benchmark_id(&h).unwrap_or_else(|| "gpqa".to_string());
        let (id, token) = h.register("a", "agent", json!({}));
        h.set_selection(vec![bench.clone()], None).ok(); // ok if unknown -> err, but then go() uses explicit
        let battle = h.go(vec![id.clone()], vec![bench.clone()]);
        // If the benchmark isn't present in this checkout, skip the rest.
        let Ok(_battle) = battle else { return };
        let reply = h.heartbeat(&id, &token, None).unwrap();
        assert_eq!(reply.status, AgentStatus::Running);
        let a = reply.assignment.expect("assignment dispatched");
        assert_eq!(a.benchmark_id, bench);
    }

    #[test]
    fn unknown_benchmark_go_errors() {
        let h = handle();
        let (id, _t) = h.register("a", "agent", json!({}));
        assert_eq!(
            h.go(vec![id], vec!["does-not-exist-xyz".into()])
                .unwrap_err(),
            SuperradiantError::UnknownBenchmark
        );
    }

    fn first_benchmark_id(h: &SuperradiantHandle) -> Option<String> {
        let snap = h.snapshot();
        snap.get("benchmarks")
            .and_then(|b| b.as_array())
            .and_then(|arr| arr.first())
            .and_then(|b| b.get("id"))
            .and_then(|v| v.as_str())
            .map(|s| s.to_string())
    }
}
