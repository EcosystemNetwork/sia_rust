"""Agent-controlled 2D fish aquarium for SIA Studio.

Ported from https://github.com/asaltveit/2d-fish-aquariums — each tank is driven
by its own Claude agent. See :mod:`sia.web.aquarium.app` for the FastAPI wiring.
"""
from .app import is_enabled, register

__all__ = ["is_enabled", "register"]
