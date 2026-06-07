"""Vercel / ASGI entrypoint for the SIA Runs Visualizer.

Vercel's FastAPI backend support imports a module-level ASGI ``app`` object.
Our app is built by the ``create_app(runs_dir)`` factory in
``sia.web.server`` (so the same code can serve any ``runs/`` directory from
the ``sia web`` CLI), so we instantiate it here at import time.

The runs directory comes from the ``SIA_RUNS_DIR`` environment variable and
defaults to ``runs``. A relative value is resolved against this file's
directory (the repo root) rather than the process working directory, so it
works regardless of where the Vercel function is invoked from. The Vercel
deployment sets ``SIA_RUNS_DIR=demo_runs`` to serve the bundled showcase run
(``runs/`` is gitignored and empty on a fresh deploy).
"""

from __future__ import annotations

import os
from pathlib import Path

from sia.web.server import create_app

_ROOT = Path(__file__).resolve().parent
_runs_dir = Path(os.environ.get("SIA_RUNS_DIR", "runs"))
if not _runs_dir.is_absolute():
    _runs_dir = _ROOT / _runs_dir

app = create_app(_runs_dir)
