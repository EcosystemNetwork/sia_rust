"""Attach the agent-controlled aquarium to the SIA Studio FastAPI app.

Surfaced in SIA Studio under the "Aquarium" tab. Each tank is driven by its own
Claude agent (research species, populate, feed on a hunger timer); the browser
renders fish procedurally over a WebSocket.

The agents call the Claude API, so they are **opt-in**: nothing starts unless
``SIA_AQUARIUM`` is truthy *and* ``ANTHROPIC_API_KEY`` is set. This keeps the
background dashboard during ``sia run`` from ever spawning (billable) agents.

Knobs (env):
    SIA_AQUARIUM=1        enable the aquarium backend
    AQUARIUM_COUNT=4      number of tanks
"""
from __future__ import annotations

import json
import logging
import os

log = logging.getLogger(__name__)


def _truthy(val: str | None) -> bool:
    return (val or "").strip().lower() in ("1", "true", "yes", "on")


def is_enabled() -> bool:
    """True when the aquarium agents should run (opt-in + key + SDK present)."""
    if not _truthy(os.getenv("SIA_AQUARIUM")):
        return False
    if not os.getenv("ANTHROPIC_API_KEY"):
        return False
    try:
        import anthropic  # noqa: F401
    except Exception:
        return False
    return True


def register(app) -> None:
    """Register aquarium routes + agent lifecycle on an existing FastAPI app.

    ``/api/aquarium/state`` is always registered (so the frontend tab can detect
    whether the backend is live and render a clear empty state if not). Claude
    agents + the ``/ws/aquarium/{id}`` socket only attach when :func:`is_enabled`.
    """
    enabled = is_enabled()
    count = int(os.getenv("AQUARIUM_COUNT", "4")) if enabled else 0
    ctx: dict = {"manager": None}

    @app.get("/api/aquarium/state")
    def aquarium_state():
        mgr = ctx["manager"]
        if not mgr:
            return {"enabled": enabled, "count": count, "aquariums": {}}
        return {
            "enabled": True,
            "count": len(mgr.states),
            "aquariums": {
                aq_id: json.loads(state.model_dump_json())
                for aq_id, state in mgr.states.items()
            },
        }

    if not enabled:
        log.info(
            "Aquarium backend disabled (set SIA_AQUARIUM=1 and ANTHROPIC_API_KEY to enable)."
        )
        return

    from fastapi import WebSocket, WebSocketDisconnect

    from .manager import AquariumManager
    from .researcher import Researcher
    from .ws_protocol import biome_background, msg_aquarium_init, msg_fish_add

    researcher = Researcher()

    @app.on_event("startup")
    async def _aquarium_startup():
        await researcher.start()
        mgr = AquariumManager(count, researcher)
        mgr.start_agents()
        ctx["manager"] = mgr
        log.info("Launched %d aquarium agents", count)

    @app.on_event("shutdown")
    async def _aquarium_shutdown():
        await researcher.stop()

    @app.websocket("/ws/aquarium/{aquarium_id}")
    async def aquarium_ws(websocket: WebSocket, aquarium_id: str):
        await websocket.accept()
        mgr = ctx["manager"]
        if not mgr or aquarium_id not in mgr.states:
            await websocket.close(code=4004, reason="Unknown aquarium")
            return

        async def send(msg: dict) -> None:
            await websocket.send_text(json.dumps(msg))

        mgr.subscribe(aquarium_id, send)

        state = mgr.get_state(aquarium_id)
        if state:
            bg_deep, bg_surface = biome_background(state.biome)
            await send(msg_aquarium_init(aquarium_id, state.theme, state.biome, bg_deep, bg_surface))
            for fish in state.fish.values():
                await send(msg_fish_add(
                    aquarium_id, fish.fish_id, fish.species_name, fish.common_name,
                    fish.render_spec, fish.x, fish.y, fish.zone,
                ))

        try:
            while True:
                await websocket.receive_text()
        except WebSocketDisconnect:
            pass
        finally:
            mgr.unsubscribe(aquarium_id, send)

    log.info("Aquarium backend enabled: %d tanks", count)
