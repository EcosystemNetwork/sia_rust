"""Tool implementations for aquarium Claude agents."""
from __future__ import annotations
import json
import random
import time
import uuid
from typing import TYPE_CHECKING, Any

from .models import AquariumState, FishState
from .ws_protocol import (
    build_render_spec, biome_background,
    msg_aquarium_init, msg_fish_add, msg_fish_action,
    msg_food_drop, msg_fish_fed, msg_fish_died,
)

if TYPE_CHECKING:
    from .researcher import Researcher

_ZONE_Y: dict[str, tuple[float, float]] = {
    "surface": (0.05, 0.25),
    "mid":     (0.25, 0.65),
    "deep":    (0.55, 0.80),
    "bottom":  (0.75, 0.92),
}

_FOOD_REACH = 0.18   # horizontal fraction within which a fish can eat dropped food

TOOL_DEFINITIONS = [
    {
        "name": "research_species",
        "description": (
            "Look up detailed morphological, locomotion, and coloration data for a fish species "
            "by its common or scientific name. Returns structured JSON used to add the fish."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "species_name": {
                    "type": "string",
                    "description": "Common or scientific name, e.g. 'clownfish' or 'Amphiprion ocellaris'",
                }
            },
            "required": ["species_name"],
        },
    },
    {
        "name": "add_fish",
        "description": "Add a fish to this aquarium using species data from research_species. Returns fish_id.",
        "input_schema": {
            "type": "object",
            "properties": {
                "species_data": {"type": "object", "description": "Full JSON from research_species"},
                "count": {"type": "integer", "description": "Number to add (default 1, max 4)", "default": 1},
                "zone": {"type": "string", "enum": ["surface", "mid", "deep", "bottom"], "default": "mid"},
            },
            "required": ["species_data"],
        },
    },
    {
        "name": "feed_fish",
        "description": (
            "Drop food at a horizontal position along the top of the aquarium. "
            "Fish within ±18% of that x position will eat it — IF they are hungry. "
            "WARNING: feeding a FULL fish (just fed) kills it. "
            "Returns which fish ate and their updated hunger status."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "x_fraction": {
                    "type": "number",
                    "description": "Horizontal drop position: 0.0=left edge, 1.0=right edge. "
                                   "Match this to a hungry fish's x position from describe_scene.",
                }
            },
            "required": ["x_fraction"],
        },
    },
    {
        "name": "set_fish_behavior",
        "description": "Trigger a behavior/animation for one or more fish.",
        "input_schema": {
            "type": "object",
            "properties": {
                "fish_id": {"type": "string", "description": "fish_id, species common name, or 'all'"},
                "action": {"type": "string", "description": "Animation name e.g. 'idle_swim', 'startle', 'feeding_bite'"},
                "duration_seconds": {"type": "number", "default": 5},
                "target_zone": {"type": "string", "enum": ["surface", "mid", "deep", "bottom"]},
            },
            "required": ["fish_id", "action"],
        },
    },
    {
        "name": "describe_scene",
        "description": (
            "Get current aquarium state: fish present, zones, positions, and HUNGER STATUS. "
            "Always call this first each tick to check which fish need feeding."
        ),
        "input_schema": {"type": "object", "properties": {}},
    },
    {
        "name": "set_theme",
        "description": "Set the biome/theme label and background color for this aquarium.",
        "input_schema": {
            "type": "object",
            "properties": {
                "biome": {"type": "string", "description": "e.g. 'tropical coral reef', 'deep sea', 'freshwater lake'"},
                "theme": {"type": "string", "description": "Short display name, e.g. 'Coral Reef'"},
            },
            "required": ["biome"],
        },
    },
]


class AquariumTools:
    def __init__(self, state: AquariumState, researcher, broadcast_fn) -> None:
        self._state      = state
        self._researcher = researcher
        self._broadcast  = broadcast_fn

    async def execute(self, tool_name: str, tool_input: dict[str, Any]) -> str:
        if tool_name == "research_species":
            return await self._research_species(tool_input["species_name"])
        if tool_name == "add_fish":
            return await self._add_fish(
                tool_input["species_data"],
                tool_input.get("count", 1),
                tool_input.get("zone", "mid"),
            )
        if tool_name == "feed_fish":
            return await self._feed_fish(float(tool_input["x_fraction"]))
        if tool_name == "set_fish_behavior":
            return await self._set_fish_behavior(
                tool_input["fish_id"],
                tool_input["action"],
                tool_input.get("duration_seconds", 5),
                tool_input.get("target_zone"),
            )
        if tool_name == "describe_scene":
            return self._describe_scene()
        if tool_name == "set_theme":
            biome = tool_input.get("biome", "")
            theme = tool_input.get("theme", biome.title())
            await self.set_aquarium_theme(theme, biome)
            return json.dumps({"theme": theme, "biome": biome})
        return f"Unknown tool: {tool_name}"

    async def _research_species(self, name: str) -> str:
        try:
            data = await self._researcher.research_species(name)
            return json.dumps(data)
        except Exception as exc:
            return f"Error researching '{name}': {exc}"

    async def _add_fish(self, species_data: dict, count: int, zone: str) -> str:
        count = max(1, min(4, int(count)))
        zone  = zone if zone in _ZONE_Y else "mid"
        render_spec  = build_render_spec(species_data)
        species_name = (species_data.get("species") or {}).get("scientific_name", "unknown")
        common_name  = (species_data.get("species") or {}).get("common_name", species_name)

        hunger_interval = render_spec.get("hunger_interval", 160.0)
        full_fraction   = render_spec.get("full_fraction", 0.35)
        now             = time.time()

        added_ids = []
        y_min, y_max = _ZONE_Y[zone]
        for _ in range(count):
            fish = FishState(
                species_name=species_name,
                common_name=common_name,
                render_spec=render_spec,
                x=random.uniform(0.1, 0.9),
                y=random.uniform(y_min, y_max),
                zone=zone,
                hunger_interval=hunger_interval,
                full_fraction=full_fraction,
                fed_at=now,
            )
            self._state.fish[fish.fish_id] = fish
            await self._broadcast(msg_fish_add(
                self._state.aquarium_id, fish.fish_id, species_name, common_name,
                render_spec, fish.x, fish.y, zone,
            ))
            added_ids.append(fish.fish_id)

        return json.dumps({"added_fish_ids": added_ids, "species": common_name, "zone": zone,
                           "hunger_interval_s": hunger_interval})

    async def _feed_fish(self, x_fraction: float) -> str:
        x_fraction = max(0.0, min(1.0, x_fraction))
        food_id    = str(uuid.uuid4())[:8]
        now        = time.time()

        await self._broadcast(msg_food_drop(self._state.aquarium_id, food_id, x_fraction))

        results = []
        to_remove: list[str] = []

        for fish in list(self._state.fish.values()):
            if not fish.alive:
                continue
            if fish.zone == "bottom":
                continue
            if abs(fish.x - x_fraction) > _FOOD_REACH:
                continue

            full_until  = fish.fed_at + fish.hunger_interval * fish.full_fraction
            must_eat_by = fish.fed_at + fish.hunger_interval

            if now < full_until:
                secs_full = round(full_until - now)
                fish.alive = False
                to_remove.append(fish.fish_id)
                await self._broadcast(msg_fish_died(
                    self._state.aquarium_id, fish.fish_id, "overfed"
                ))
                results.append({
                    "fish_id": fish.fish_id,
                    "species": fish.common_name,
                    "outcome": f"DIED (overfed — was full for another {secs_full}s)",
                })
            else:
                fish.fed_at = now
                await self._broadcast(msg_fish_fed(self._state.aquarium_id, fish.fish_id))
                await self._broadcast(msg_fish_action(
                    self._state.aquarium_id, fish.fish_id, "feeding_bite", 2500
                ))
                results.append({
                    "fish_id": fish.fish_id,
                    "species": fish.common_name,
                    "outcome": "fed",
                    "next_hungry_in_s": round(fish.hunger_interval * fish.full_fraction),
                })

        for fid in to_remove:
            self._state.fish.pop(fid, None)

        if not results:
            return json.dumps({
                "food_id": food_id,
                "x_fraction": x_fraction,
                "ate": [],
                "note": "No fish were near that position (range ±0.18). Check fish x positions via describe_scene.",
            })
        return json.dumps({"food_id": food_id, "x_fraction": x_fraction, "ate": results})

    async def _set_fish_behavior(self, fish_id: str, action: str, duration_s: float, target_zone) -> str:
        duration_ms = int(duration_s * 1000)
        targets     = self._resolve_targets(fish_id)
        if not targets:
            return f"No fish found matching '{fish_id}'"

        target_pos = None
        if target_zone and target_zone in _ZONE_Y:
            y_min, y_max = _ZONE_Y[target_zone]
            target_pos = {"x": random.uniform(0.1, 0.9), "y": random.uniform(y_min, y_max)}

        now = time.time()
        for fid in targets:
            fish = self._state.fish[fid]
            fish.current_action    = action
            fish.action_expires_at = now + duration_s
            await self._broadcast(msg_fish_action(
                self._state.aquarium_id, fid, action, duration_ms, target_pos
            ))

        return json.dumps({"triggered": action, "fish_count": len(targets), "duration_s": duration_s})

    def _resolve_targets(self, fish_id: str) -> list[str]:
        if fish_id == "all":
            return list(self._state.fish.keys())
        if fish_id in self._state.fish:
            return [fish_id]
        lower = fish_id.lower()
        return [
            fid for fid, f in self._state.fish.items()
            if lower in f.common_name.lower() or lower in f.species_name.lower()
        ]

    def _describe_scene(self) -> str:
        now = time.time()
        if not self._state.fish:
            return (
                f"Aquarium '{self._state.aquarium_id}' — biome: {self._state.biome or 'not set'}. "
                "No fish yet. Canvas is 16:10 aspect ratio."
            )
        lines = [
            f"Aquarium '{self._state.aquarium_id}' — biome: {self._state.biome}. "
            f"{len(self._state.fish)} fish:"
        ]
        for fish in self._state.fish.values():
            full_until  = fish.fed_at + fish.hunger_interval * fish.full_fraction
            must_eat_by = fish.fed_at + fish.hunger_interval

            if now < full_until:
                secs = round(full_until - now)
                hunger_str = f"FULL — do NOT feed for {secs}s (feeding kills)"
            elif now < must_eat_by:
                secs = round(must_eat_by - now)
                urgency = "⚠ URGENT" if secs < 30 else "HUNGRY"
                hunger_str = f"{urgency} — feed within {secs}s or it dies"
            else:
                hunger_str = "STARVING (overdue — should have been caught by monitor)"

            lines.append(
                f"  * {fish.common_name} [id={fish.fish_id}] "
                f"zone={fish.zone}, x={fish.x:.2f}, {hunger_str}, "
                f"interval={round(fish.hunger_interval)}s"
            )
        return "\n".join(lines)

    async def set_aquarium_theme(self, theme: str, biome: str) -> None:
        self._state.theme  = theme
        self._state.biome  = biome
        bg_deep, bg_surface = biome_background(biome)
        self._state.background_color = bg_deep
        await self._broadcast(msg_aquarium_init(
            self._state.aquarium_id, theme, biome, bg_deep, bg_surface
        ))
