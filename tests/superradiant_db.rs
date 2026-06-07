//! Live integration test for the Postgres-backed credential store.
//!
//! Self-skips unless a real `DATABASE_URL` is set (so it's a no-op in CI and on
//! machines without a database). Point it at the Supabase **connection-pooler**
//! URI (transaction mode, port 6543) to prove the prod path:
//!
//! ```text
//! SUPERRADIANT_SECRET_KEY=<base64-32-bytes> \
//! DATABASE_URL='postgresql://postgres.<ref>:<pw>@aws-0-<region>.pooler.supabase.com:6543/postgres?sslmode=require' \
//!   cargo test --features superradiant-db --test superradiant_db -- --nocapture
//! ```
//!
//! Exercises: connect + auto-migrate (the advisory-lock path that can bite
//! through PgBouncer), then a full create → list → resolve(decrypt) → delete
//! round-trip. Cleans up the row it creates.

#![cfg(feature = "superradiant-db")]

use sia::superradiant::credentials::{CredentialStore, NewCredential};

#[tokio::test]
async fn live_credential_store_round_trip() {
    let Ok(url) = std::env::var("DATABASE_URL") else {
        eprintln!("SKIP live_credential_store_round_trip: DATABASE_URL not set");
        return;
    };
    if url.trim().is_empty() {
        eprintln!("SKIP live_credential_store_round_trip: DATABASE_URL empty");
        return;
    }
    if std::env::var("SUPERRADIANT_SECRET_KEY").is_err() {
        panic!("SUPERRADIANT_SECRET_KEY must be set to run the live DB test");
    }

    // connect() runs embedded migrations — this is the boot path against the
    // real pooler, including the pg_advisory_lock taken by sqlx::migrate!.
    let store = CredentialStore::connect(&url)
        .await
        .expect("connect + migrate against live Postgres");
    eprintln!("✓ connected + migrated");

    let secret = "sk-live-test-do-not-use-1234567890";
    let created = store
        .create(NewCredential {
            name: "live-test".into(),
            client_kind: "openai".into(),
            base_url: Some("https://example.test/v1".into()),
            model: "gpt-4o-mini".into(),
            api_key: secret.into(),
        })
        .await
        .expect("create credential");
    eprintln!("✓ created credential id={}", created.id);

    // Masked list must never carry the key, and must include our row.
    let listed = store.list().await.expect("list credentials");
    assert!(
        listed.iter().any(|c| c.id == created.id),
        "created credential should appear in list"
    );
    eprintln!("✓ listed {} credential(s)", listed.len());

    // Resolve decrypts the key — the AES-GCM round-trip through real BYTEA columns.
    let resolved = store.resolve(&created.id).await.expect("resolve credential");
    assert_eq!(resolved.api_key, secret, "decrypted key must match input");
    assert_eq!(resolved.client_kind, "openai");
    eprintln!("✓ resolved + decrypted key matches");

    // Clean up.
    let removed = store.delete(&created.id).await.expect("delete credential");
    assert!(removed, "delete should remove the row");
    assert!(
        store.resolve(&created.id).await.is_err(),
        "resolve after delete must fail"
    );
    eprintln!("✓ deleted + verified gone — live round-trip OK");
}
