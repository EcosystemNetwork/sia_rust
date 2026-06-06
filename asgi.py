"""Vercel / ASGI entrypoint for the SIA Runs Visualizer.

Vercel's FastAPI backend support imports a module-level ASGI ``app`` object.
Our app is built by the ``create_app(runs_dir)`` factory in
``sia.web.server`` (so the same code can serve any ``runs/`` directory from
the ``sia web`` CLI), so we instantiate it here at import time.

The runs directory defaults to ``./runs`` and can be overridden with the
``SIA_RUNS_DIR`` environment variable. Note that ``runs/`` is gitignored, so a
fresh deploy serves the dashboard UI with no run data until a runs directory is
provided.
"""

from __future__ import annotations

import os

from sia.web.server import create_app

app = create_app(os.environ.get("SIA_RUNS_DIR", "runs"))
