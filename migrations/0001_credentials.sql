-- Superradiant user-supplied LLM provider credentials.
-- The API key is stored encrypted (AES-256-GCM): `key_ciphertext` + `key_nonce`.
-- The key-encryption key itself lives in the SUPERRADIANT_SECRET_KEY env var,
-- never in the database.
CREATE TABLE IF NOT EXISTS superradiant_credentials (
    id             UUID PRIMARY KEY,
    name           TEXT        NOT NULL,
    -- One of: 'anthropic' | 'openai' | 'google' (OpenAI-compatible custom
    -- endpoints use 'openai' + a base_url).
    client_kind    TEXT        NOT NULL,
    -- Optional override; NULL falls back to the per-client_kind default URL.
    base_url       TEXT,
    model          TEXT        NOT NULL,
    key_ciphertext BYTEA       NOT NULL,
    key_nonce      BYTEA       NOT NULL,
    created_at     TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS superradiant_credentials_created_at_idx
    ON superradiant_credentials (created_at DESC);
