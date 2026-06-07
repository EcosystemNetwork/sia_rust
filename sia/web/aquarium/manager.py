"""Manages N aquariums and their agent tasks."""
from __future__ import annotations
import asyncio
import logging
import random
import time
from typing import Callable

from .models import AquariumState
from .agent import AquariumAgent
from .researcher import Researcher
from .ws_protocol import msg_fish_died

_BIOMES = [
    "tropical coral reef",
    "deep sea",
    "open ocean",
    "freshwater lake",
    "kelp forest",
    "arctic",
    "mangrove",
    "river",
    "tropical freshwater",
]

log = logging.getLogger(__name__)

_HUNGER_CHECK_INTERVAL = 3   # seconds between starvation checks


class AquariumManager:
    def __init__(self, count: int, researcher: Researcher) -> None:
        self._researcher = researcher
        self.states: dict[str, AquariumState] = {
            f"aq-{i}": AquariumState(aquarium_id=f"aq-{i}")
            for i in range(count)
        }
        self._subscribers: dict[str, set[Callable]] = {
            aq_id: set() for aq_id in self.states
        }
        self._tasks: dict[str, asyncio.Task] = {}

    def subscribe(self, aquarium_id: str, send_fn: Callable) -> None:
        self._subscribers.setdefault(aquarium_id, set()).add(send_fn)

    def unsubscribe(self, aquarium_id: str, send_fn: Callable) -> None:
        self._subscribers.get(aquarium_id, set()).discard(send_fn)

    async def broadcast(self, aquarium_id: str, message: dict) -> None:
        for send_fn in list(self._subscribers.get(aquarium_id, set())):
            try:
                await send_fn(message)
            except Exception:
                log.debug("Broadcast failed for %s", aquarium_id)

    def start_agents(self, shared_biome: str | None = None) -> None:
        asyncio.create_task(self._hunger_monitor(), name="hunger-monitor")

        biome = shared_biome or random.choice(_BIOMES)
        log.info("All aquariums will use biome: %s", biome)

        for aq_id, state in self.states.items():
            if aq_id in self._tasks and not self._tasks[aq_id].done():
                continue

            async def _broadcast(msg, _id=aq_id):
                await self.broadcast(_id, msg)

            agent = AquariumAgent(state, self._researcher, _broadcast, assigned_biome=biome)
            task  = asyncio.create_task(agent.run(), name=f"agent-{aq_id}")
            self._tasks[aq_id] = task
            log.info("Started agent for %s (biome=%s)", aq_id, biome)

    async def _hunger_monitor(self) -> None:
        """Remove fish that have starved, broadcast fish_died."""
        while True:
            await asyncio.sleep(_HUNGER_CHECK_INTERVAL)
            now = time.time()
            for aq_id, state in self.states.items():
                dead: list[str] = []
                for fish_id, fish in list(state.fish.items()):
                    if not fish.alive:
                        dead.append(fish_id)
                        continue
                    if now > fish.fed_at + fish.hunger_interval:
                        fish.alive = False
                        dead.append(fish_id)
                        await self.broadcast(aq_id, msg_fish_died(aq_id, fish_id, "starved"))
                        log.info("Fish %s (%s) starved in %s", fish_id, fish.common_name, aq_id)
                for fish_id in dead:
                    state.fish.pop(fish_id, None)

    def aquarium_ids(self) -> list[str]:
        return list(self.states.keys())

    def get_state(self, aquarium_id: str) -> AquariumState | None:
        return self.states.get(aquarium_id)
