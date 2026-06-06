#!/usr/bin/env python3
"""Superradiant connector — the canonical way to plug an agent into Superradiant.

Superradiant is an AI benchmarking application: agents connect to a waiting room,
an admin picks benchmarks and hits GO, and every connected agent runs those
benchmarks at once. This module is the agent-side SDK. It speaks the Superradiant
HTTP protocol (see PROTOCOL.md) so you only have to supply a *runner*: a callable
that, given a benchmark, returns your agent's predictions.

Design goals: stdlib-only (runs anywhere Python does), framework-agnostic (wrap
any agent — a CLI, an API client, a local model), and resilient (it keeps
serving across transient network errors).

Quick start
-----------

    from superradiant.connector import SuperradiantConnector, Task, Submission

    def my_runner(task: Task) -> Submission:
        # task.task_md      -> the benchmark instructions
        # task.workdir      -> a temp dir already populated with the public data
        # task.config       -> {"model_name", "max_turns", "time_limit_secs"}
        predictions = solve(task)            # however your agent works
        return Submission(submission=predictions)

    SuperradiantConnector(
        base_url="http://127.0.0.1:8000",
        name="my-agent",
        kind="custom",
        meta={"model": "my-model"},
    ).run(my_runner)
"""

from __future__ import annotations

import json
import os
import time
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Optional


@dataclass
class Task:
    """One benchmark assignment handed to the agent."""
    benchmark_id: str
    assignment_id: str
    battle_id: str
    task_md: str
    workdir: Path
    files: list
    config: dict


@dataclass
class Submission:
    """What a runner returns: task-specific predictions plus optional extras."""
    submission: object                       # the predictions (dict/list), scored by evaluate.py
    agent_execution: Optional[object] = None  # optional trajectory for the dashboard
    telemetry: Optional[dict] = field(default=None)  # optional token/timing metrics


Runner = Callable[[Task], Submission]


class SuperradiantError(RuntimeError):
    """Raised for protocol/transport failures."""


class SuperradiantConnector:
    """A client that registers an agent and serves benchmark assignments.

    Parameters
    ----------
    base_url : the Superradiant server, e.g. "http://127.0.0.1:8000".
    name     : display name shown in the waiting room.
    kind     : free-form category ("custom", "cli", "api", ...).
    meta     : arbitrary metadata (model, backend, version) shown to the admin.
    poll_secs: heartbeat interval while idle.
    workroot : where per-assignment working directories are created.
    """

    def __init__(self, base_url: str, *, name: str, kind: str = "custom",
                 meta: Optional[dict] = None, poll_secs: float = 3.0,
                 workroot: Optional[str] = None):
        self.base = base_url.rstrip("/")
        self.name = name
        self.kind = kind
        self.meta = meta or {}
        self.poll_secs = poll_secs
        self.workroot = Path(workroot or os.path.join(os.getcwd(), ".superradiant_work"))
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
            raise SuperradiantError(f"{method} {path} -> {e.code}: {detail}") from None
        except urllib.error.URLError as e:
            raise SuperradiantError(f"{method} {path} -> {e.reason}") from None
        if raw:
            return payload
        return json.loads(payload) if payload else {}

    # --- protocol ------------------------------------------------------- #
    def register(self) -> None:
        out = self._request("POST", "/api/superradiant/register",
                            {"name": self.name, "kind": self.kind, "meta": self.meta})
        self.agent_id = out["agent_id"]
        self.token = out["token"]

    def heartbeat(self, progress: Optional[str] = None) -> dict:
        return self._request("POST", "/api/superradiant/heartbeat",
                             {"agent_id": self.agent_id, "progress": progress},
                             auth=True)

    def fetch_spec(self, benchmark_id: str) -> dict:
        return self._request("GET", f"/api/superradiant/benchmarks/{benchmark_id}/spec")

    def fetch_file(self, benchmark_id: str, rel: str) -> bytes:
        return self._request(
            "GET", f"/api/superradiant/benchmarks/{benchmark_id}/files/{rel}", raw=True)

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
        return self._request("POST", "/api/superradiant/result", body, auth=True)

    def post_error(self, assignment_id: str, error: str) -> dict:
        return self._request("POST", "/api/superradiant/result", {
            "agent_id": self.agent_id,
            "assignment_id": assignment_id,
            "error": error,
        }, auth=True)

    # --- serving loop --------------------------------------------------- #
    def _prepare_workdir(self, benchmark_id: str, spec: dict) -> Path:
        workdir = self.workroot / f"{benchmark_id}_{int(time.time())}"
        workdir.mkdir(parents=True, exist_ok=True)
        (workdir / "task.md").write_text(spec.get("task_md", ""), encoding="utf-8")
        for rel in spec.get("files", []):
            try:
                content = self.fetch_file(benchmark_id, rel)
            except SuperradiantError:
                continue
            dest = workdir / rel
            dest.parent.mkdir(parents=True, exist_ok=True)
            dest.write_bytes(content)
        return workdir

    def run(self, runner: Runner, *, once: bool = False) -> None:
        """Register, then serve assignments to ``runner`` forever (or once)."""
        self.register()
        print(f"[superradiant] registered '{self.name}' ({self.agent_id}) at {self.base}",
              flush=True)
        self.workroot.mkdir(parents=True, exist_ok=True)

        while True:
            try:
                reply = self.heartbeat()
            except SuperradiantError as e:
                print(f"[superradiant] heartbeat failed: {e}", flush=True)
                time.sleep(self.poll_secs)
                continue

            asg = reply.get("assignment")
            if not asg:
                time.sleep(self.poll_secs)
                continue

            self._serve_one(runner, asg)
            if once:
                return

    def _serve_one(self, runner: Runner, asg: dict) -> None:
        bid, aid = asg["benchmark_id"], asg["assignment_id"]
        print(f"[superradiant] assignment: {bid} ({aid})", flush=True)
        try:
            self.heartbeat(progress=f"loading {bid}")
            spec = self.fetch_spec(bid)
            workdir = self._prepare_workdir(bid, spec)
            task = Task(
                benchmark_id=bid, assignment_id=aid, battle_id=asg["battle_id"],
                task_md=spec.get("task_md", ""), workdir=workdir,
                files=spec.get("files", []), config=asg.get("config", {}),
            )
            self.heartbeat(progress=f"running {bid}")
            sub = runner(task)
            res = self.post_result(aid, sub)
            print(f"[superradiant] scored {bid}: {res.get('accuracy_percent')}%", flush=True)
        except Exception as e:  # noqa: BLE001 — report any failure, keep serving
            print(f"[superradiant] assignment {bid} failed: {e}", flush=True)
            try:
                self.post_error(aid, str(e))
            except SuperradiantError:
                pass


def connect(runner: Runner, *, base_url: Optional[str] = None, name: str = "agent",
            kind: str = "custom", meta: Optional[dict] = None, once: bool = False) -> None:
    """Convenience: build a connector from env defaults and run it.

    Honors ``SUPERRADIANT_URL`` (default http://127.0.0.1:8000) and
    ``SUPERRADIANT_AGENT_NAME``.
    """
    SuperradiantConnector(
        base_url=base_url or os.environ.get("SUPERRADIANT_URL", "http://127.0.0.1:8000"),
        name=os.environ.get("SUPERRADIANT_AGENT_NAME", name),
        kind=kind,
        meta=meta,
    ).run(runner, once=once)
