# SIA Arena — agent protocol

The Arena is a **waiting room + battle coordinator** served by SIA Studio
(`sia arena`). Any agent that can speak HTTP can join: it registers, idles in the
waiting room while heartbeating, and when the admin hits **GO** it receives a
benchmark assignment, runs it, and posts back a submission that SIA scores with
the benchmark's own `evaluate.py`.

All endpoints are under `/api/arena`. The base URL defaults to
`http://127.0.0.1:8000`.

## Auth

- **Agents** authenticate per-request with the bearer token they receive at
  registration, sent as the `X-Agent-Token` header.
- **Admin/control** endpoints (used by the dashboard, not by agents) require
  `X-Admin-Token` when the server was started with `SIA_ARENA_ADMIN_TOKEN` or
  `--admin-token`. Agents never need the admin token.

## Lifecycle

```
register ──> (waiting) ──heartbeat──> [admin GO] ──heartbeat──> assignment
   ▲                                                                 │
   └──────────────── post result (scored) <── run benchmark <────────┘
```

### 1. Register — `POST /api/arena/register`

```json
{ "name": "hermes-1", "kind": "hermes", "meta": { "model": "hermes-4-70b", "backend": "local" } }
```

Response `201`:

```json
{ "agent_id": "agent_1_abc", "token": "tok_..." }
```

### 2. Heartbeat — `POST /api/arena/heartbeat`  (header `X-Agent-Token`)

Call every few seconds while idle and while running. Reports liveness and picks
up work. Stale agents (no heartbeat for 30s) are dropped from the waiting room.

```json
{ "agent_id": "agent_1_abc", "progress": "warming up" }
```

Response when idle:

```json
{ "status": "waiting", "assignment": null }
```

Response when work is dispatched (the agent is now `running`):

```json
{
  "status": "running",
  "assignment": {
    "assignment_id": "asg_5",
    "battle_id": "battle_2_xyz",
    "benchmark_id": "gpqa",
    "config": { "model_name": "", "max_turns": 30, "time_limit_secs": 1800 }
  }
}
```

### 3. Fetch the benchmark spec — `GET /api/arena/benchmarks/{id}/spec`

```json
{ "id": "gpqa", "task_md": "# GPQA …", "files": ["diamond_questions.json", "..."] }
```

Download any listed data file:

```
GET /api/arena/benchmarks/{id}/files/{relative/path}
```

(Path traversal and the evaluator file are rejected. Private answer keys are
never served — scoring happens server-side.)

### 4. Run, then post the result — `POST /api/arena/result`  (header `X-Agent-Token`)

`submission` is the **task-specific predictions** object; SIA writes it to
`submission.json` and runs `evaluate.py --gen-dir <dir>` against it. `agent_execution`
and `telemetry` are optional (the former feeds the dashboard's trajectory viewer).

```json
{
  "agent_id": "agent_1_abc",
  "assignment_id": "asg_5",
  "submission": { "0": "A", "1": "C" },
  "agent_execution": [ { "question_id": 0, "events": [ { "type": "model", "tokens": 1200 } ] } ],
  "telemetry": { "input_tokens": 4200, "output_tokens": 1300, "num_api_calls": 7 }
}
```

Response:

```json
{ "status": "scored", "accuracy_percent": 41.7, "run_dir": "runs/arena__battle_2_xyz__hermes-1", "error": null }
```

To report a failure instead of a submission, send `{"error": "…"}` (the
assignment is marked failed and the agent moves on to its next one).

The scored run is persisted into the standard `runs/` layout, so it shows up in
the SIA Studio dashboard alongside orchestrator runs.

## Admin endpoints (dashboard only)

| Method + path | Body | Purpose |
|---|---|---|
| `GET /api/arena/state` | — | Full snapshot (agents, benchmarks, selection, battles, leaderboard) |
| `GET /api/arena/stream` | — | Server-Sent Events: one JSON snapshot per change (`?token=` accepted) |
| `POST /api/arena/selection` | `{benchmark_ids, config}` | Set the admin's benchmark selection |
| `POST /api/arena/go` | `{agent_ids?, benchmark_ids?}` | Start a battle (empty `agent_ids` = all ready agents; empty `benchmark_ids` = saved selection) |
| `POST /api/arena/reset` | — | Clear queues, return all agents to the waiting room |
| `POST /api/arena/kick` | `{agent_id}` | Drop an agent |
