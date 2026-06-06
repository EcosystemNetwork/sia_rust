# Superradiant

**Superradiant is our AI benchmarking application.** External agents connect to a
**waiting room** and idle; an admin picks which benchmarks to run from the
dashboard and hits **GO**, and every connected agent runs those benchmarks at
once. Submissions are scored by each benchmark's own `evaluate.py` and persisted
into `runs/`, so results appear in the SIA Studio dashboard with a live
leaderboard and an agent × benchmark accuracy matrix.

## Start the server

```bash
# Optional: protect the admin/control endpoints with a token.
export SUPERRADIANT_ADMIN_TOKEN=changeme

cargo run --bin sia -- superradiant --host 127.0.0.1 --port 8000
# (`sia web` also serves the /superradiant dashboard; `sia arena` is an alias)
```

Open the dashboard at **http://127.0.0.1:8000/superradiant**. Paste the admin
token (top-right) if you set one. The page shows the waiting room, the benchmark
picker, GO/Reset controls, a live leaderboard, and an agent × benchmark accuracy
matrix.

Benchmarks are auto-discovered from the tasks directory (`$SIA_TASKS_DIR`, else
`./sia/tasks`) — any folder with `data/public/task.md` and `data/public/evaluate.py`.

## Connect an agent

The connector SDK ([connector.py](connector.py)) is stdlib-only and
framework-agnostic. Supply a `runner(task) -> Submission`:

```python
from superradiant.connector import SuperradiantConnector, Submission

def my_runner(task):
    # task.task_md, task.workdir (has the data files), task.config
    predictions = solve(task)            # however your agent works
    return Submission(submission=predictions)

SuperradiantConnector(
    base_url="http://127.0.0.1:8000",
    name="my-agent",
    kind="custom",
    meta={"model": "my-model"},
).run(my_runner)
```

Run the connector on as many machines/processes as you want agents — they all
share one waiting room.

### Wrap an existing command-line agent

No code needed beyond a command template — see the `cli` runner in
[example_agent.py](example_agent.py):

```bash
export SUPERRADIANT_URL=http://<host>:8000
export SUPERRADIANT_AGENT_NAME=my-agent
# Reads $AGENT_PROMPT_PATH, writes predictions JSON to $AGENT_SUBMISSION_PATH.
export AGENT_CMD='my-agent --prompt-file "$AGENT_PROMPT_PATH" --output "$AGENT_SUBMISSION_PATH"'

python superradiant/example_agent.py cli
```

This is the generic pattern for plugging in any external agent (a coding agent,
a local model runner, an API-backed agent, …).

## Smoke test (no real agent)

```bash
# Terminal 1: server
cargo run --bin sia -- superradiant

# Terminal 2: a demo agent that returns an empty submission
python superradiant/example_agent.py demo
```

Open `/superradiant`, select a benchmark (e.g. `gpqa`), hit GO, and watch the
demo agent get assigned the benchmark and post an (empty, ~0%) submission, scored
live.

## Protocol

The full HTTP contract is in [PROTOCOL.md](PROTOCOL.md).

## Configuration

| Env var | Purpose | Default |
|---|---|---|
| `SUPERRADIANT_ADMIN_TOKEN` | Protect admin/control endpoints | unset (unprotected) |
| `SUPERRADIANT_PYTHON` | Interpreter used to run `evaluate.py` | `python3` |
| `SUPERRADIANT_EVAL_TIMEOUT` | Per-submission scoring timeout (s) | `600` |
| `SIA_TASKS_DIR` | Where benchmarks are discovered | `./sia/tasks` |
| `SUPERRADIANT_URL` | (agent side) server base URL | `http://127.0.0.1:8000` |
| `SUPERRADIANT_AGENT_NAME` | (agent side) display name | per-example default |
