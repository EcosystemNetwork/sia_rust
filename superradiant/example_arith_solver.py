#!/usr/bin/env python3
"""A REAL Superradiant agent (no mock) that solves the `arithmetic-mc` benchmark.

Unlike the demo runner, this actually reads the benchmark data and computes
answers: it parses each "What is <expr>?" question, evaluates the arithmetic
expression (honoring order of operations), and selects the option letter whose
value matches. It should score 100% — proving the full connect → run → score →
leaderboard pipeline with genuine, non-zero results.

Run it against a live server:

    SUPERRADIANT_URL=http://127.0.0.1:8010 \\
        python3 superradiant/example_arith_solver.py
"""

from __future__ import annotations

import json
import os
import re
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from superradiant.connector import Submission, Task, connect  # noqa: E402

_ALLOWED = set("0123456789+-*/(). ")


def _safe_eval(expr: str):
    """Evaluate an arithmetic expression after whitelisting characters."""
    if not expr or any(c not in _ALLOWED for c in expr):
        return None
    try:
        return eval(expr, {"__builtins__": {}}, {})  # noqa: S307 — whitelisted chars only
    except Exception:  # noqa: BLE001
        return None


def _match_letter(value, options: dict):
    """Return the option letter whose numeric value equals `value`."""
    if value is None:
        return None
    for letter, text in options.items():
        try:
            if abs(float(str(text).strip()) - float(value)) < 1e-9:
                return letter
        except (TypeError, ValueError):
            continue
    return None


def arith_runner(task: Task) -> Submission:
    questions = json.loads((task.workdir / "questions.json").read_text(encoding="utf-8"))
    details = []
    solved = 0
    for q in questions:
        qid = q.get("id")
        text = q.get("Question", "")
        m = re.search(r"What is\s+(.+?)\s*\?", text)
        expr = m.group(1) if m else text
        value = _safe_eval(expr)
        letter = _match_letter(value, q.get("options", {}))
        if letter:
            solved += 1
        details.append({"question_id": qid, "model_answer": letter or "A"})

    print(f"[arith-solver] solved {solved}/{len(questions)} items", flush=True)
    submission = {
        "model": "arith-solver",
        "total_questions": len(questions),
        "details": details,
    }
    telemetry = {"input_tokens": 0, "output_tokens": 0, "num_api_calls": 0,
                 "num_tool_calls": len(questions)}
    return Submission(submission=submission, telemetry=telemetry)


if __name__ == "__main__":
    connect(arith_runner, name="arith-solver", kind="solver",
            meta={"model": "deterministic-arith"})
