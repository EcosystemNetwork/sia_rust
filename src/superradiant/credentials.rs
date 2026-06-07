//! Postgres-backed, encrypted credential store for user-supplied LLM providers.
//!
//! Gated behind the `superradiant-db` cargo feature. This is what lets a user
//! enter an API key in the Superradiant UI ("test Google's gemini-2.5-pro vs an
//! OpenAI-compatible endpoint") without exporting an environment variable: the
//! provider config + key are persisted server-side (Postgres on Railway), the
//! key encrypted at rest with AES-256-GCM, and decrypted *just-in-time* when a
//! house competitor builds an LLM client (see [`crate::superradiant::house`]).
//!
//! Security posture:
//! - The key is **never** returned to clients — [`ProviderCredential`] (the
//!   serialized row) carries no key field; only [`ResolvedCredential`]
//!   (server-internal) holds the plaintext, and only transiently.
//! - Encryption key comes from `SUPERRADIANT_SECRET_KEY` (base64 → 32 bytes).
//! - All credential HTTP endpoints are admin-token gated (see `routes.rs`).
//!
//! `query!`-style compile-time macros are intentionally avoided so `cargo build`
//! never needs a live `DATABASE_URL`.

use std::str::FromStr;

use aes_gcm::aead::{Aead, KeyInit};
use aes_gcm::{Aes256Gcm, Key, Nonce};
use base64::Engine;
use serde::{Deserialize, Serialize};
use sqlx::postgres::{PgConnectOptions, PgPoolOptions, PgRow};
use sqlx::{PgPool, Row};
use uuid::Uuid;

use crate::error::{SiaError, SiaResult};

/// Env var holding the base64-encoded 32-byte AES-256-GCM key-encryption key.
pub const SECRET_KEY_ENV: &str = "SUPERRADIANT_SECRET_KEY";

/// A stored provider credential as exposed to clients — **never** carries the
/// API key. Used for the masked list the UI renders.
#[derive(Debug, Clone, Serialize)]
pub struct ProviderCredential {
    pub id: String,
    pub name: String,
    pub client_kind: String,
    pub base_url: Option<String>,
    pub model: String,
    pub created_at: String,
}

/// Input for creating a credential. Carries the plaintext API key, which is
/// encrypted before it touches the database and is never persisted as-is.
#[derive(Debug, Clone, Deserialize)]
pub struct NewCredential {
    pub name: String,
    pub client_kind: String,
    #[serde(default)]
    pub base_url: Option<String>,
    pub model: String,
    pub api_key: String,
}

/// A decrypted credential, ready to build an LLM client. Server-internal only;
/// holds the plaintext key transiently and is never serialized to clients.
#[derive(Debug, Clone)]
pub struct ResolvedCredential {
    pub id: String,
    pub name: String,
    pub client_kind: String,
    pub base_url: Option<String>,
    pub model: String,
    pub api_key: String,
}

fn db_err(e: impl std::fmt::Display) -> SiaError {
    SiaError::new(format!("credential store error: {e}"))
}

/// Load + validate the 32-byte key-encryption key from `SUPERRADIANT_SECRET_KEY`.
fn secret_key() -> SiaResult<[u8; 32]> {
    let b64 = std::env::var(SECRET_KEY_ENV).map_err(|_| {
        SiaError::new(format!(
            "{SECRET_KEY_ENV} is not set (need a base64-encoded 32-byte key)"
        ))
    })?;
    let raw = base64::engine::general_purpose::STANDARD
        .decode(b64.trim())
        .map_err(|e| SiaError::new(format!("{SECRET_KEY_ENV} is not valid base64: {e}")))?;
    raw.as_slice().try_into().map_err(|_| {
        SiaError::new(format!(
            "{SECRET_KEY_ENV} must decode to exactly 32 bytes, got {}",
            raw.len()
        ))
    })
}

/// Encrypt a plaintext key → `(ciphertext, nonce)`.
fn encrypt_key(plaintext: &str) -> SiaResult<(Vec<u8>, Vec<u8>)> {
    let key = secret_key()?;
    let cipher = Aes256Gcm::new(Key::<Aes256Gcm>::from_slice(&key));
    // 96-bit random nonce; unique per ciphertext (never reused with this key).
    let nonce_bytes: [u8; 12] = rand::random();
    let nonce = Nonce::from_slice(&nonce_bytes);
    let ct = cipher
        .encrypt(nonce, plaintext.as_bytes())
        .map_err(|e| SiaError::new(format!("failed to encrypt API key: {e}")))?;
    Ok((ct, nonce_bytes.to_vec()))
}

/// Decrypt a `(ciphertext, nonce)` pair back to the plaintext key.
fn decrypt_key(ciphertext: &[u8], nonce: &[u8]) -> SiaResult<String> {
    let key = secret_key()?;
    let cipher = Aes256Gcm::new(Key::<Aes256Gcm>::from_slice(&key));
    if nonce.len() != 12 {
        return Err(SiaError::new("stored nonce has unexpected length"));
    }
    let nonce = Nonce::from_slice(nonce);
    let pt = cipher
        .decrypt(nonce, ciphertext)
        .map_err(|e| SiaError::new(format!("failed to decrypt API key: {e}")))?;
    String::from_utf8(pt).map_err(|e| SiaError::new(format!("decrypted key is not UTF-8: {e}")))
}

/// Validate a client_kind against the providers registry.
fn validate_kind(kind: &str) -> SiaResult<String> {
    let kind = kind.trim().to_lowercase();
    if crate::providers::VALID_CLIENT_KINDS.contains(&kind.as_str()) {
        Ok(kind)
    } else {
        Err(SiaError::new(format!(
            "invalid client_kind '{kind}'. Expected one of: {}.",
            crate::providers::VALID_CLIENT_KINDS.join(", ")
        )))
    }
}

/// Cloneable handle to the Postgres-backed credential store.
#[derive(Clone)]
pub struct CredentialStore {
    pool: PgPool,
}

impl CredentialStore {
    /// Connect with `database_url` and run embedded migrations. Validates that
    /// `SUPERRADIANT_SECRET_KEY` is present + well-formed up front so a
    /// misconfigured deployment fails loudly at startup, not at first write.
    pub async fn connect(database_url: &str) -> SiaResult<Self> {
        // Fail fast if the encryption key is missing/malformed.
        secret_key()?;

        // Supabase (and any PgBouncer transaction-mode pooler) does not support
        // persistent server-side prepared statements, which sqlx caches by
        // default — that surfaces as intermittent "prepared statement already
        // exists" errors. Disabling the statement cache keeps us compatible with
        // both the pooler and a direct connection. SSL mode is taken from the
        // URL (`?sslmode=require` for Supabase); a plain local URL still works.
        let opts = PgConnectOptions::from_str(database_url)
            .map_err(|e| SiaError::new(format!("invalid DATABASE_URL: {e}")))?
            .statement_cache_capacity(0);

        let pool = PgPoolOptions::new()
            .max_connections(5)
            .connect_with(opts)
            .await
            .map_err(|e| SiaError::new(format!("could not connect to Postgres: {e}")))?;
        sqlx::migrate!("./migrations")
            .run(&pool)
            .await
            .map_err(|e| SiaError::new(format!("migration failed: {e}")))?;
        Ok(Self { pool })
    }

    /// List all stored credentials as masked rows (no API key), newest first.
    pub async fn list(&self) -> SiaResult<Vec<ProviderCredential>> {
        let rows = sqlx::query(
            "SELECT id, name, client_kind, base_url, model, created_at \
             FROM superradiant_credentials ORDER BY created_at DESC",
        )
        .fetch_all(&self.pool)
        .await
        .map_err(db_err)?;
        rows.iter().map(row_to_masked).collect()
    }

    /// Create a credential, encrypting its key. Returns the masked row.
    pub async fn create(&self, input: NewCredential) -> SiaResult<ProviderCredential> {
        let kind = validate_kind(&input.client_kind)?;
        if input.api_key.trim().is_empty() {
            return Err(SiaError::new("api_key must not be empty"));
        }
        if input.model.trim().is_empty() {
            return Err(SiaError::new("model must not be empty"));
        }
        let name = if input.name.trim().is_empty() {
            input.model.trim().to_string()
        } else {
            input.name.trim().to_string()
        };
        let base_url = input
            .base_url
            .as_deref()
            .map(str::trim)
            .filter(|b| !b.is_empty())
            .map(str::to_string);
        let (ct, nonce) = encrypt_key(&input.api_key)?;
        let id = Uuid::new_v4();
        let row = sqlx::query(
            "INSERT INTO superradiant_credentials \
             (id, name, client_kind, base_url, model, key_ciphertext, key_nonce) \
             VALUES ($1, $2, $3, $4, $5, $6, $7) \
             RETURNING id, name, client_kind, base_url, model, created_at",
        )
        .bind(id)
        .bind(&name)
        .bind(&kind)
        .bind(base_url.as_deref())
        .bind(input.model.trim())
        .bind(ct)
        .bind(nonce)
        .fetch_one(&self.pool)
        .await
        .map_err(db_err)?;
        row_to_masked(&row)
    }

    /// Delete a credential by id. Returns whether a row was removed.
    pub async fn delete(&self, id: &str) -> SiaResult<bool> {
        let uid = Uuid::parse_str(id).map_err(|_| SiaError::new("invalid credential id"))?;
        let res = sqlx::query("DELETE FROM superradiant_credentials WHERE id = $1")
            .bind(uid)
            .execute(&self.pool)
            .await
            .map_err(db_err)?;
        Ok(res.rows_affected() > 0)
    }

    /// Resolve a credential by id, decrypting its key for client construction.
    pub async fn resolve(&self, id: &str) -> SiaResult<ResolvedCredential> {
        let uid = Uuid::parse_str(id).map_err(|_| SiaError::new("invalid credential id"))?;
        let row = sqlx::query(
            "SELECT id, name, client_kind, base_url, model, key_ciphertext, key_nonce \
             FROM superradiant_credentials WHERE id = $1",
        )
        .bind(uid)
        .fetch_optional(&self.pool)
        .await
        .map_err(db_err)?
        .ok_or_else(|| SiaError::new("credential not found"))?;

        let ct: Vec<u8> = row.try_get("key_ciphertext").map_err(db_err)?;
        let nonce: Vec<u8> = row.try_get("key_nonce").map_err(db_err)?;
        let api_key = decrypt_key(&ct, &nonce)?;
        let id: Uuid = row.try_get("id").map_err(db_err)?;
        Ok(ResolvedCredential {
            id: id.to_string(),
            name: row.try_get("name").map_err(db_err)?,
            client_kind: row.try_get("client_kind").map_err(db_err)?,
            base_url: row.try_get("base_url").map_err(db_err)?,
            model: row.try_get("model").map_err(db_err)?,
            api_key,
        })
    }

    // --- persisted leaderboard ------------------------------------------- //

    /// Record one scored (battle × agent × benchmark) result. Best-effort at the
    /// call site; surfaces DB errors so the caller can log them.
    #[allow(clippy::too_many_arguments)]
    pub async fn record_result(
        &self,
        battle_id: &str,
        agent_name: &str,
        agent_kind: &str,
        benchmark_id: &str,
        accuracy_percent: f64,
        model: Option<&str>,
        run_dir: Option<&str>,
    ) -> SiaResult<()> {
        sqlx::query(
            "INSERT INTO superradiant_results \
             (battle_id, agent_name, agent_kind, benchmark_id, accuracy_percent, model, run_dir) \
             VALUES ($1, $2, $3, $4, $5, $6, $7)",
        )
        .bind(battle_id)
        .bind(agent_name)
        .bind(agent_kind)
        .bind(benchmark_id)
        .bind(accuracy_percent)
        .bind(model)
        .bind(run_dir)
        .execute(&self.pool)
        .await
        .map_err(db_err)?;
        Ok(())
    }

    /// All-time leaderboard: average accuracy per agent across every persisted
    /// result, best first.
    pub async fn leaderboard(&self) -> SiaResult<Vec<LeaderboardRow>> {
        let rows = sqlx::query(
            "SELECT agent_name, \
                    max(agent_kind) AS agent_kind, \
                    count(*) AS benchmarks_scored, \
                    avg(accuracy_percent) AS avg_acc, \
                    max(created_at) AS last_at \
             FROM superradiant_results \
             GROUP BY agent_name \
             ORDER BY avg_acc DESC",
        )
        .fetch_all(&self.pool)
        .await
        .map_err(db_err)?;
        rows.iter()
            .map(|r| {
                let last: chrono::DateTime<chrono::Utc> = r.try_get("last_at").map_err(db_err)?;
                let avg: f64 = r.try_get("avg_acc").map_err(db_err)?;
                Ok(LeaderboardRow {
                    agent_name: r.try_get("agent_name").map_err(db_err)?,
                    agent_kind: r.try_get("agent_kind").map_err(db_err)?,
                    benchmarks_scored: r.try_get("benchmarks_scored").map_err(db_err)?,
                    avg_accuracy_percent: (avg * 100.0).round() / 100.0,
                    last_scored_at: last.to_rfc3339(),
                })
            })
            .collect()
    }
}

/// One aggregated all-time leaderboard row.
#[derive(Debug, Clone, Serialize)]
pub struct LeaderboardRow {
    pub agent_name: String,
    pub agent_kind: String,
    pub benchmarks_scored: i64,
    pub avg_accuracy_percent: f64,
    pub last_scored_at: String,
}

/// Map a selected DB row to the masked, client-facing struct.
fn row_to_masked(row: &PgRow) -> SiaResult<ProviderCredential> {
    let id: Uuid = row.try_get("id").map_err(db_err)?;
    let created: chrono::DateTime<chrono::Utc> = row.try_get("created_at").map_err(db_err)?;
    Ok(ProviderCredential {
        id: id.to_string(),
        name: row.try_get("name").map_err(db_err)?,
        client_kind: row.try_get("client_kind").map_err(db_err)?,
        base_url: row.try_get("base_url").map_err(db_err)?,
        model: row.try_get("model").map_err(db_err)?,
        created_at: created.to_rfc3339(),
    })
}

#[cfg(test)]
mod tests {
    use super::*;

    /// 32 zero bytes, base64-encoded — a deterministic test KEK.
    const TEST_KEK_B64: &str = "AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA=";

    /// Serializes tests that mutate the process-global `SUPERRADIANT_SECRET_KEY`
    /// env var, which otherwise race when the suite runs multi-threaded.
    static ENV_LOCK: std::sync::Mutex<()> = std::sync::Mutex::new(());

    /// Acquire the env lock, recovering from a prior test's panic-poisoning.
    fn env_guard() -> std::sync::MutexGuard<'static, ()> {
        ENV_LOCK.lock().unwrap_or_else(|e| e.into_inner())
    }

    fn with_kek<T>(f: impl FnOnce() -> T) -> T {
        let _g = env_guard();
        let saved = std::env::var(SECRET_KEY_ENV).ok();
        std::env::set_var(SECRET_KEY_ENV, TEST_KEK_B64);
        let out = f();
        match saved {
            Some(v) => std::env::set_var(SECRET_KEY_ENV, v),
            None => std::env::remove_var(SECRET_KEY_ENV),
        }
        out
    }

    #[test]
    fn encrypt_decrypt_round_trips() {
        with_kek(|| {
            let (ct, nonce) = encrypt_key("sk-secret-123").unwrap();
            // Ciphertext must not contain the plaintext.
            assert!(!ct.windows(3).any(|w| w == b"sk-"));
            assert_eq!(nonce.len(), 12);
            assert_eq!(decrypt_key(&ct, &nonce).unwrap(), "sk-secret-123");
        });
    }

    #[test]
    fn distinct_nonces_per_encryption() {
        with_kek(|| {
            let (_, n1) = encrypt_key("same").unwrap();
            let (_, n2) = encrypt_key("same").unwrap();
            assert_ne!(n1, n2, "nonce must be random per encryption");
        });
    }

    #[test]
    fn decrypt_fails_with_wrong_nonce() {
        with_kek(|| {
            let (ct, _) = encrypt_key("x").unwrap();
            assert!(decrypt_key(&ct, &[0u8; 12]).is_err());
        });
    }

    #[test]
    fn secret_key_rejects_wrong_length() {
        let _g = env_guard();
        let saved = std::env::var(SECRET_KEY_ENV).ok();
        std::env::set_var(
            SECRET_KEY_ENV,
            base64::engine::general_purpose::STANDARD.encode([0u8; 16]),
        );
        assert!(secret_key().is_err());
        match saved {
            Some(v) => std::env::set_var(SECRET_KEY_ENV, v),
            None => std::env::remove_var(SECRET_KEY_ENV),
        }
    }

    #[test]
    fn validate_kind_accepts_known_rejects_unknown() {
        assert_eq!(validate_kind("OpenAI").unwrap(), "openai");
        assert!(validate_kind("tesla").is_err());
    }
}
