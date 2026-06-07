"""Claude LLM agent that autonomously manages one aquarium."""
from __future__ import annotations
import asyncio
import logging
import random
from typing import Any

import anthropic

from .models import AquariumState
from .tools import AquariumTools, TOOL_DEFINITIONS
from .researcher import Researcher

log = logging.getLogger(__name__)

_SYSTEM_PROMPT = """\
You are an autonomous aquarium curator agent managing a single 2D aquarium.

== CRITICAL: FEEDING ==
Every fish has a hunger timer shown in describe_scene. You MUST feed fish on time:
- "HUNGRY — feed within Xs" means drop food near that fish's x position NOW
- "FULL — do NOT feed for Xs" means waiting is mandatory — feeding a full fish KILLS it
- feed_fish(x_fraction) drops food at that horizontal position (0=left, 1=right)
- Fish only eat if within ±0.18 of x_fraction — match to the fish's x coordinate
- Warm-colored fish have short intervals (~100s); dark fish have long intervals (~280s)
- You may call feed_fish multiple times per tick if multiple hungry fish are at different x positions

== OTHER DUTIES ==
1. Set a distinctive biome/theme; research 3-5 species; populate with correct zones
2. Occasionally trigger behaviors (startle, zone changes) when all fish are well-fed
"""

_SETUP_PROMPT_TEMPLATE = """\
Initialize your aquarium. The biome has already been set to "{biome}".
1. Research 3-5 species that authentically live in a {biome} — pick ones with visual variety \
(different sizes, colors, locomotion styles).
2. Add them with ecologically correct zones (surface/mid/deep/bottom).
3. Call describe_scene to confirm.
Do NOT call set_theme — it is already set.
"""

_BEHAVIOR_PROMPT = """\
Call describe_scene first to check hunger states.
- Feed every fish that shows HUNGRY by calling feed_fish near their x position.
- Do NOT feed fish that show FULL (it kills them).
- After handling hunger, optionally trigger one interesting behavior.
"""


class AquariumAgent:
    def __init__(self, state: AquariumState, researcher: Researcher,
                 broadcast_fn, model: str = "claude-haiku-4-5-20251001",
                 assigned_biome: str = "") -> None:
        self._state         = state
        self._tools         = AquariumTools(state, researcher, broadcast_fn)
        self._client        = anthropic.AsyncAnthropic()
        self._model         = model
        self._assigned_biome = assigned_biome
        self._messages: list[dict] = []

    async def run(self) -> None:
        self._state.agent_running = True
        try:
            await self._setup_phase()
            while True:
                await asyncio.sleep(random.uniform(12, 22))
                await self._behavior_tick()
        except asyncio.CancelledError:
            pass
        except Exception:
            log.exception("Agent %s crashed", self._state.aquarium_id)
        finally:
            self._state.agent_running = False

    async def _setup_phase(self) -> None:
        log.info("Agent %s: starting setup (biome=%s)", self._state.aquarium_id, self._assigned_biome)
        self._messages = []
        # Apply the shared biome immediately so the background is set before any fish arrive
        if self._assigned_biome:
            theme = self._assigned_biome.title()
            await self._tools.set_aquarium_theme(theme, self._assigned_biome)
            setup_prompt = _SETUP_PROMPT_TEMPLATE.format(biome=self._assigned_biome)
        else:
            setup_prompt = _SETUP_PROMPT_TEMPLATE.format(biome="a suitable biome of your choice")
        await self._run_tool_loop(setup_prompt, max_tool_rounds=14)
        log.info("Agent %s setup complete; fish count=%d",
                 self._state.aquarium_id, len(self._state.fish))

    async def _behavior_tick(self) -> None:
        scene  = self._tools._describe_scene()
        prompt = f"Current scene:\n{scene}\n\n{_BEHAVIOR_PROMPT}"
        try:
            await self._run_tool_loop(prompt, max_tool_rounds=6)
        except Exception:
            log.exception("Agent %s behavior tick failed", self._state.aquarium_id)

    def _trim_dangling_tool_use(self) -> None:
        """Remove trailing assistant messages whose tool_use blocks have no following tool_result."""
        while self._messages:
            last = self._messages[-1]
            if last["role"] != "assistant":
                break
            content = last["content"]
            has_tool_use = any(
                (isinstance(b, dict) and b.get("type") == "tool_use")
                or (hasattr(b, "type") and b.type == "tool_use")
                for b in content
            )
            if has_tool_use:
                log.warning("Agent %s: trimming dangling tool_use from message history",
                            self._state.aquarium_id)
                self._messages.pop()
            else:
                break

    async def _run_tool_loop(self, user_message: str, max_tool_rounds: int = 8) -> str:
        self._trim_dangling_tool_use()
        self._messages.append({"role": "user", "content": user_message})

        for _ in range(max_tool_rounds):
            response = await self._client.messages.create(
                model=self._model,
                max_tokens=1024,
                system=_SYSTEM_PROMPT,
                tools=TOOL_DEFINITIONS,
                messages=self._messages,
            )

            assistant_content = response.content
            self._messages.append({"role": "assistant", "content": assistant_content})

            if response.stop_reason == "end_turn":
                texts = [b.text for b in assistant_content if hasattr(b, "text")]
                return " ".join(texts)

            if response.stop_reason != "tool_use":
                break

            tool_results = []
            for block in assistant_content:
                if block.type != "tool_use":
                    continue
                try:
                    result = await self._execute_tool(block.name, block.input)
                except Exception as exc:
                    log.warning("Agent %s: tool %s failed: %s",
                                self._state.aquarium_id, block.name, exc)
                    result = f"Error executing {block.name}: {exc}"
                tool_results.append({
                    "type": "tool_result",
                    "tool_use_id": block.id,
                    "content": result,
                })

            if tool_results:
                self._messages.append({"role": "user", "content": tool_results})

        return ""

    async def _execute_tool(self, name: str, input_data: dict[str, Any]) -> str:
        log.info("Agent %s: %s(%s)", self._state.aquarium_id, name, list(input_data.keys()))
        return await self._tools.execute(name, input_data)
