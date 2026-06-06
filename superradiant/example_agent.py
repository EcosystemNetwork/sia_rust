#!/usr/bin/env python3
"""Examples of connecting an agent to Superradiant.

Two reference runners are included:

  * ``demo``  — returns an empty submission (scores ~0). Use it to smoke-test the
    full loop end to end without a real agent.

  * ``cli``   — wraps ANY command-line agent. Superradiant prepares a working
    directory per assignment; you provide a shell command (via ``$AGENT_CMD``)
    that reads the prompt and writes predictions JSON. This is the generic
    pattern for plugging in external agents (a coding agent, a local model
    runner, an API-backed agent, etc.).

Usage:

    # demo
    python superradiant/example_agent.py demo

    # wrap a CLI agent
    export AGENT_CMD='my-agent --prompt-file "$AGENT_PROMPT_PATH" \\
                               --output "$AGENT_SUBMISSION_PATH"'
    python superradiant/example_agent.py cli

Environment (both modes):
    SUPERRADIANT_URL         server base URL (default http://127.0.0.1:8000)
    SUPERRADIANT_AGENT_NAME  display name in the waiting room

The ``cli`` runner passes these to ``$AGENT_CMD`` (cwd = the working dir):
    AGENT_BENCHMARK        benchmark id (e.g. "gpqa")
    AGENT_WORKDIR          absolute path to the working directory (has data files)
    AGENT_PROMPT_PATH      absolute path to prompt.txt (task.md + an output instruction)
    AGENT_SUBMISSION_PATH  absolute path where the agent must write predictions JSON
    AGENT_MODEL            suggested model id (may be empty)
    AGENT_MAX_TURNS        suggested max turns
    AGENT_TIME_LIMIT       suggested time limit (seconds)
"""

from __future__ import annotations

import json
import os
import subprocess
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from superradiant.connector import Submission, Task, connect  # noqa: E402


# --------------------------------------------------------------------------- #
# demo runner
# --------------------------------------------------------------------------- #
def demo_runner(task: Task) -> Submission:
    print(f"[demo] would solve {task.benchmark_id}: {task.task_md[:60]!r}…", flush=True)
    return Submission(submission={}, telemetry={"input_tokens": 0, "output_tokens": 0})


# --------------------------------------------------------------------------- #
# generic CLI-agent runner
# --------------------------------------------------------------------------- #
_PROMPT_SUFFIX = """

--- SUPERRADIANT INSTRUCTIONS ---
The public data files for this benchmark are in the current working directory.
Solve the task above and write ONLY your final predictions, as JSON in the exact
format the task requires, to: {submission_path}
Write nothing else to that file.
"""


def cli_runner(task: Task) -> Submission:
    cmd = os.environ.get("AGENT_CMD")
    if not cmd:
        raise RuntimeError("AGENT_CMD is not set (see this file's docstring).")

    submission_path = (task.workdir / "submission.json").resolve()
    prompt_path = (task.workdir / "prompt.txt").resolve()
    prompt_path.write_text(
        task.task_md + _PROMPT_SUFFIX.format(submission_path=submission_path),
        encoding="utf-8",
    )
    if submission_path.exists():
        submission_path.unlink()  # avoid mistaking a stale file for fresh output

    env = dict(os.environ)
    env.update({
        "AGENT_BENCHMARK": task.benchmark_id,
        "AGENT_WORKDIR": str(task.workdir.resolve()),
        "AGENT_PROMPT_PATH": str(prompt_path),
        "AGENT_SUBMISSION_PATH": str(submission_path),
        "AGENT_MODEL": str(task.config.get("model_name", "")),
        "AGENT_MAX_TURNS": str(task.config.get("max_turns", "")),
        "AGENT_TIME_LIMIT": str(task.config.get("time_limit_secs", "")),
    })
    time_limit = int(task.config.get("time_limit_secs") or 0) or None

    proc = subprocess.run(cmd, shell=True, cwd=str(task.workdir), env=env,
                          capture_output=True, text=True, timeout=time_limit)
    if proc.returncode != 0:
        tail = (proc.stderr or proc.stdout or "").strip()[-800:]
        raise RuntimeError(f"agent exited {proc.returncode}: {tail}")
    if not submission_path.exists():
        raise RuntimeError("agent did not write submission.json")

    submission = json.loads(submission_path.read_text(encoding="utf-8"))
    return Submission(submission=submission)


def main() -> None:
    mode = sys.argv[1] if len(sys.argv) > 1 else "demo"
    if mode == "demo":
        connect(demo_runner, name="demo-agent", kind="demo",
                meta={"model": os.environ.get("AGENT_MODEL", "")})
    elif mode == "cli":
        if not os.environ.get("AGENT_CMD"):
            print(__doc__)
            print("\nERROR: set AGENT_CMD before running `cli`.", file=sys.stderr)
            sys.exit(2)
        connect(cli_runner, name="cli-agent", kind="cli",
                meta={"model": os.environ.get("AGENT_MODEL", ""),
                      "cmd": os.environ.get("AGENT_CMD", "")})
    else:
        print(f"unknown mode {mode!r}; use 'demo' or 'cli'", file=sys.stderr)
        sys.exit(2)


if __name__ == "__main__":
    main()
