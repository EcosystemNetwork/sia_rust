-- Persisted Superradiant results: one row per scored (battle × agent ×
-- benchmark). The all-time leaderboard is aggregated from this table, so it
-- survives restarts and spans every battle (unlike the in-memory live view).
CREATE TABLE IF NOT EXISTS superradiant_results (
    id               BIGSERIAL PRIMARY KEY,
    battle_id        TEXT             NOT NULL,
    agent_name       TEXT             NOT NULL,
    agent_kind       TEXT             NOT NULL DEFAULT 'agent',
    benchmark_id     TEXT             NOT NULL,
    accuracy_percent DOUBLE PRECISION NOT NULL,
    model            TEXT,
    run_dir          TEXT,
    created_at       TIMESTAMPTZ      NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS superradiant_results_agent_idx
    ON superradiant_results (agent_name);
CREATE INDEX IF NOT EXISTS superradiant_results_battle_idx
    ON superradiant_results (battle_id);
