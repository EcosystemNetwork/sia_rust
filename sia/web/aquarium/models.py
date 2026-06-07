from __future__ import annotations
import time
import uuid
from pydantic import BaseModel, Field


class FishState(BaseModel):
    fish_id: str = Field(default_factory=lambda: str(uuid.uuid4())[:8])
    species_name: str
    common_name: str = ""
    render_spec: dict
    x: float
    y: float
    zone: str = "mid"
    current_action: str = "idle_swim"
    action_expires_at: float | None = None
    # Hunger
    hunger_interval: float = 120.0   # seconds per full feeding cycle
    full_fraction: float  = 0.35     # fraction of interval where fish is "full" (feeding kills)
    fed_at: float = Field(default_factory=time.time)
    alive: bool = True


class AquariumState(BaseModel):
    aquarium_id: str
    theme: str = ""
    biome: str = ""
    background_color: str = "#0a1628"
    fish: dict[str, FishState] = Field(default_factory=dict)
    agent_running: bool = False
