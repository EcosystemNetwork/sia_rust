#!/usr/bin/env python3
"""Reusable SIA Arena worker.

A small, dependency-light client that implements the Arena protocol
(see PROTOCOL.md): it registers an agent, idles in the waiting room while
heartbeating, and when the admin hits GO it downloads the benchmark, invokes a
*runner callback* to produce a submission, and posts the result back for scoring.

Plug in any agent by passing a ``runner`` callable to :func:`run_worker`. The
runner receives a :class:`Task` (benchmark id, task.md text, a local working
directory already populated with the public data files, and the run config) and
must return a ``submission`` dict (the task-specific predictions). It may also
return optional ``agent_execution`` and ``telemetry`` payloads.

Only the standard library is used (urllib), so this runs anywhere Python does.
"""

from __future__ import annotations

import json
import os
import sys
import time
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Optional


@dataclass
class Task:
    benchmark_id: str
    assignment_id: str
    battle_id: str
    task_md: str
    workdir: Path
    files: list[str]
    config: dict


@dataclass
class Submission:
    submission: dict | list
    agent_execution: Optional[object] = None
    telemetry: Optional[dict] = field(default=None)


Runner = Callable[[Task], Submission]


class ArenaClient:
    def __init__(self, base_url: str):
        self.base = base_url.rstrip("/")
        self.agent_id: Optional[str] = None
        self.token: Optional[str] = None

    # --- low-level HTTP ------------------------------------------------- #
    def _request(self, method: str, path: str, body: Optional[dict] = None,
                 auth: bool = False, raw: bool = False):
        url = self.base + path
        data = json.dumps(body).encode() if body is not None else None
        req = urllib.request.Request(url, data=data, method=method)
        if body is not None:
            req.add_header("Content-Type", "application/json")
        if auth and self.token:
            req.add_header("X-Agent-Token", self.token)
        try:
            with urllib.request.urlopen(req, timeout=60) as resp:
                payload = resp.read()
        except urllib.error.HTTPError as e:
            detail = e.read().decode(errors="replace")
            raise RuntimeError(f"{method} {path} -> {e.code}: {detail}") from None
        if raw:
            return payload
        return json.loads(payload) if payload else {}

    # --- protocol ------------------------------------------------------- #
    def register(self, name: str, kind: str, meta: dict) -> None:
        out = self._request("POST", "/api/arena/register",
                            {"name": name, "kind": kind, "meta": meta})
        self.agent_id = out["agent_id"]
        self.token = out["token"]

    def heartbeat(self, progress: Optional[str] = None) -> dict:
        return self._request("POST", "/api/arena/heartbeat",
                             {"agent_id": self.agent_id, "progress": progress},
                             auth=True)

    def fetch_spec(self, benchmark_id: str) -> dict:
        return self._request("GET", f"/api/arena/benchmarks/{benchmark_id}/spec")

    def fetch_file(self, benchmark_id: str, rel: str) -> bytes:
        return self._request(
            "GET", f"/api/arena/benchmarks/{benchmark_id}/files/{rel}", raw=True)

    def post_result(self, assignment_id: str, sub: Submission) -> dict:
        body = {
            "agent_id": self.agent_id,
            "assignment_id": assignment_id,
            "submission": sub.submission,
        }
        if sub.agent_execution is not None:
            body["agent_execution"] = sub.agent_execution
        if sub.telemetry is not None:
            body["telemetry"] = sub.telemetry
        return self._request("POST", "/api/arena/result", body, auth=True)

    def post_error(self, assignment_id: str, error: str) -> dict:
        return self._request("POST", "/api/arena/result", {
            "agent_id": self.agent_id,
            "assignment_id": assignment_id,
            "error": error,
        }, auth=True)


def _prepare_workdir(client: ArenaClient, benchmark_id: str, spec: dict,
                     root: Path) -> Path:
    workdir = root / f"{benchmark_id}_{int(time.time())}"
    workdir.mkdir(parents=True, exist_ok=True)
    (workdir / "task.md").write_text(spec.get("task_md", ""), encoding="utf-8")
    for rel in spec.get("files", []):
        try:
            content = client.fetch_file(benchmark_id, rel)
        except RuntimeError:
            continue
        dest = workdir / rel
        dest.parent.mkdir(parents=True, exist_ok=True)
        dest.write_bytes(content)
    return workdir


def run_worker(runner: Runner, *, base_url: str, name: str, kind: str,
               meta: Optional[dict] = None, poll_secs: float = 3.0,
               workroot: Optional[str] = None, once: bool = False) -> None:
    """Register and run the heartbeat loop, dispatching assignments to ``runner``."""
    client = ArenaClient(base_url)
    client.register(name, kind, meta or {})
    print(f"[arena] registered as {name} ({client.agent_id}) at {base_url}", flush=True)
    root = Path(workroot or os.path.join(os.getcwd(), ".arena_work"))
    root.mkdir(parents=True, exist_ok=True)

    while True:
        try:
            reply = client.heartbeat()
        except RuntimeError as e:
            print(f"[arena] heartbeat failed: {e}", flush=True)
            time.sleep(poll_secs)
            continue

        asg = reply.get("assignment")
        if not asg:
            time.sleep(poll_secs)
            continue

        bid = asg["benchmark_id"]
        aid = asg["assignment_id"]
        print(f"[arena] assignment: {bid} ({aid})", flush=True)
        try:
            client.heartbeat(progress=f"loading {bid}")
            spec = client.fetch_spec(bid)
            workdir = _prepare_workdir(client, bid, spec, root)
            task = Task(
                benchmark_id=bid, assignment_id=aid, battle_id=asg["battle_id"],
                task_md=spec.get("task_md", ""), workdir=workdir,
                files=spec.get("files", []), config=asg.get("config", {}),
            )
            client.heartbeat(progress=f"running {bid}")
            sub = runner(task)
            res = client.post_result(aid, sub)
            print(f"[arena] scored {bid}: {res.get('accuracy_percent')}%", flush=True)
        except Exception as e:  # noqa: BLE001 — report any failure, keep serving
            print(f"[arena] assignment {bid} failed: {e}", flush=True)
            try:
                client.post_error(aid, str(e))
            except RuntimeError:
                pass

        if once:
            return


def _demo_runner(task: Task) -> Submission:
    """A trivial runner that returns an empty submission (scores ~0).

    Replace this with a real agent. It demonstrates the contract: read
    ``task.task_md`` / files in ``task.workdir`` and return a submission dict.
    """
    print(f"[demo] would solve {task.benchmark_id} using {task.task_md[:60]!r}…", flush=True)
    return Submission(submission={}, telemetry={"input_tokens": 0, "output_tokens": 0})


if __name__ == "__main__":
    base = os.environ.get("ARENA_URL", "http://127.0.0.1:8000")
    run_worker(
        _demo_runner,
        base_url=base,
        name=os.environ.get("ARENA_AGENT_NAME", "demo-worker"),
        kind="demo",
        meta={"model": os.environ.get("ARENA_MODEL", "")},
        once="--once" in sys.argv,
    )
