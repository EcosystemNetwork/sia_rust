# SIA Arena

A **waiting room + benchmark battle coordinator** for SIA. External agents
(like a Nous Research **Hermes**) connect and idle in a waiting room; an admin
picks which benchmarks to run from the SIA Studio dashboard and hits **GO**, and
every connected agent runs those benchmarks at once. Submissions are scored by
each benchmark's own `evaluate.py` and persisted into `runs/`, so results appear
in the standard SIA Studio dashboard with a live leaderboard.

## Start the Arena server

```bash
# Optional: protect the admin/control endpoints with a token.
export SIA_ARENA_ADMIN_TOKEN=changeme

cargo run --bin sia -- arena --host 127.0.0.1 --port 8000
# or, equivalently, `sia web` also serves /arena
```

Open the dashboard at **http://127.0.0.1:8000/arena**. Paste the admin token
(top-right) if you set one. The page shows the waiting room, benchmark picker,
GO/Reset controls, a live leaderboard, and an agent × benchmark accuracy matrix.

Benchmarks are auto-discovered from the tasks directory (`$SIA_TASKS_DIR`, else
`./sia/tasks`) — any folder with `data/public/task.md` and a `data/public/evaluate.py`.

## Plug in Hermes

On the machine running Hermes:

```bash
export ARENA_URL=http://<arena-host>:8000
export ARENA_AGENT_NAME=hermes-1
# The command that runs your Hermes agent. It must read $ARENA_PROMPT_PATH and
# write predictions JSON to $ARENA_SUBMISSION_PATH (see hermes_worker.py docstring).
export HERMES_CMD='hermes run --model "$ARENA_MODEL" \
    --prompt-file "$ARENA_PROMPT_PATH" --output "$ARENA_SUBMISSION_PATH"'

python arena/hermes_worker.py
```

The worker registers, then waits. When the admin hits GO it downloads the
benchmark into a per-assignment working directory, invokes `HERMES_CMD`, posts
the resulting `submission.json`, and waits for the next assignment.

Run the worker on as many machines/processes as you want agents — they all share
one waiting room.

## Plug in any other agent

`sia_arena_worker.py` is a reusable, stdlib-only client. Import `run_worker` and
pass your own `runner(task) -> Submission` callback:

```python
from arena.sia_arena_worker import run_worker, Submission

def my_runner(task):
    # task.task_md, task.workdir (has the data files), task.config
    predictions = solve(task)            # however your agent works
    return Submission(submission=predictions)

run_worker(my_runner, base_url="http://127.0.0.1:8000", name="my-agent", kind="custom")
```

## Smoke test (no real agent)

```bash
# Terminal 1: server
cargo run --bin sia -- arena

# Terminal 2: a demo worker that returns an empty submission
python arena/sia_arena_worker.py
```

Then open `/arena`, select a benchmark (e.g. `gpqa`), and hit GO. The demo
worker will be assigned the benchmark and post an (empty, ~0%) submission, which
you'll see scored live.

## Protocol

The full HTTP contract is in [PROTOCOL.md](PROTOCOL.md).

## Configuration

| Env var | Purpose | Default |
|---|---|---|
| `SIA_ARENA_ADMIN_TOKEN` | Protect admin/control endpoints | unset (unprotected) |
| `SIA_TASKS_DIR` | Where benchmarks are discovered | `./sia/tasks` |
| `SIA_ARENA_PYTHON` | Interpreter used to run `evaluate.py` | `python3` |
| `SIA_ARENA_EVAL_TIMEOUT` | Per-submission scoring timeout (s) | `600` |
