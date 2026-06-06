#!/usr/bin/env python3
"""Plug a Nous Research Hermes agent into the SIA Arena.

This adapter joins the Arena waiting room and, when the admin starts a battle,
drives Hermes against the assigned benchmark and posts back a submission.

Because every Hermes install/backend is invoked a little differently (local,
Docker, SSH, Modal, ...; ``hermes``, ``run_agent.py``, ``batch_runner.py``),
this adapter shells out to a **command template** you provide via ``HERMES_CMD``.
SIA prepares a working directory for each assignment and hands Hermes everything
it needs through files + environment variables; Hermes just has to write its
predictions to ``$ARENA_SUBMISSION_PATH``.

Per-assignment working directory layout (created by the worker):

    <workdir>/task.md            # the benchmark instructions
    <workdir>/<data files...>    # the public dataset (e.g. questions.json)
    <workdir>/prompt.txt         # task.md + an instruction to emit the submission
    <workdir>/submission.json    # <-- Hermes must write its predictions here

Environment passed to ``HERMES_CMD`` (cwd = workdir):

    ARENA_BENCHMARK       benchmark id (e.g. "gpqa")
    ARENA_WORKDIR         absolute path to the working directory
    ARENA_PROMPT_PATH     absolute path to prompt.txt
    ARENA_SUBMISSION_PATH absolute path where Hermes must write submission.json
    ARENA_MODEL           suggested model id (may be empty)
    ARENA_MAX_TURNS       suggested max turns
    ARENA_TIME_LIMIT      suggested time limit (seconds)

Example:

    export ARENA_URL=http://127.0.0.1:8000
    export ARENA_AGENT_NAME=hermes-1
    export HERMES_CMD='hermes run --model "$ARENA_MODEL" \\
        --prompt-file "$ARENA_PROMPT_PATH" --output "$ARENA_SUBMISSION_PATH"'
    python arena/hermes_worker.py

If ``HERMES_CMD`` is unset, the worker prints setup instructions and exits.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from sia_arena_worker import Submission, Task, run_worker  # noqa: E402


PROMPT_SUFFIX = """

--- ARENA INSTRUCTIONS ---
You are competing in a benchmark. The public data files for this task are in the
current working directory. Solve the task described above and write ONLY your
final answer/predictions, as JSON in the exact format the task requires, to the
file at: {submission_path}
Do not write anything else to that file.
"""


def _build_prompt(task: Task) -> str:
    submission_path = str((task.workdir / "submission.json").resolve())
    return task.task_md + PROMPT_SUFFIX.format(submission_path=submission_path)


def hermes_runner(task: Task) -> Submission:
    cmd = os.environ.get("HERMES_CMD")
    if not cmd:
        raise RuntimeError(
            "HERMES_CMD is not set. Set it to the shell command that runs your "
            "Hermes agent (see this file's docstring for the contract)."
        )

    submission_path = (task.workdir / "submission.json").resolve()
    prompt_path = (task.workdir / "prompt.txt").resolve()
    prompt_path.write_text(_build_prompt(task), encoding="utf-8")
    # A fresh file so a stale one from a previous run can't be mistaken for output.
    if submission_path.exists():
        submission_path.unlink()

    env = dict(os.environ)
    env.update({
        "ARENA_BENCHMARK": task.benchmark_id,
        "ARENA_WORKDIR": str(task.workdir.resolve()),
        "ARENA_PROMPT_PATH": str(prompt_path),
        "ARENA_SUBMISSION_PATH": str(submission_path),
        "ARENA_MODEL": str(task.config.get("model_name", "")),
        "ARENA_MAX_TURNS": str(task.config.get("max_turns", "")),
        "ARENA_TIME_LIMIT": str(task.config.get("time_limit_secs", "")),
    })

    time_limit = int(task.config.get("time_limit_secs") or 0) or None
    proc = subprocess.run(
        cmd, shell=True, cwd=str(task.workdir), env=env,
        capture_output=True, text=True, timeout=time_limit,
    )
    if proc.returncode != 0:
        tail = (proc.stderr or proc.stdout or "").strip()[-800:]
        raise RuntimeError(f"Hermes exited {proc.returncode}: {tail}")
    if not submission_path.exists():
        raise RuntimeError("Hermes did not write submission.json")

    submission = json.loads(submission_path.read_text(encoding="utf-8"))
    telemetry = _read_optional_json(task.workdir / "telemetry.json")
    return Submission(submission=submission, telemetry=telemetry)


def _read_optional_json(path):
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:  # noqa: BLE001
        return None


def main() -> None:
    if not os.environ.get("HERMES_CMD"):
        print(__doc__)
        print("\nERROR: set HERMES_CMD before running.", file=sys.stderr)
        sys.exit(2)
    base = os.environ.get("ARENA_URL", "http://127.0.0.1:8000")
    run_worker(
        hermes_runner,
        base_url=base,
        name=os.environ.get("ARENA_AGENT_NAME", "hermes"),
        kind="hermes",
        meta={
            "model": os.environ.get("ARENA_MODEL", ""),
            "backend": os.environ.get("HERMES_BACKEND", "local"),
        },
    )


if __name__ == "__main__":
    main()
