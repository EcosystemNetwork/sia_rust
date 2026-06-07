"""WebSocket message schemas and researcher-data -> render-spec mapping."""
from __future__ import annotations
import time
from typing import Any

_COLOR_MAP: dict[str, str] = {
    # Longer / more-specific tokens first so the text scanner finds them before
    # their shorter sub-strings (e.g. "dark blue" before "blue").
    "silver-white":     "#dce8e0",
    "dark blue":        "#1a3a6b",
    "dark green":       "#1a4a20",
    "dark brown":       "#4a2a10",
    "dark grey":        "#444448",
    "dark gray":        "#444448",
    "pale blue":        "#a0c4e0",
    "pale yellow":      "#e8e090",
    "pale green":       "#90c8a0",
    "light blue":       "#88c0e0",
    "light green":      "#80c890",
    "light brown":      "#c09060",
    "blue-green":       "#2a8070",
    "blue green":       "#2a8070",
    "olive-green":      "#6b7c2f",
    "olive green":      "#6b7c2f",
    "yellow-green":     "#9ab820",
    "red-orange":       "#d04010",
    "orange-red":       "#d04010",
    "blue-grey":        "#6080a0",
    "blue-gray":        "#6080a0",
    "greenish":         "#508858",
    "bluish":           "#4870a8",
    "reddish":          "#b04040",
    "yellowish":        "#c8b840",
    "brownish":         "#9a7040",
    "grayish":          "#909090",
    "greyish":          "#909090",
    "silver":           "#b8c8c0",
    "metallic":         "#a0b0b8",
    "iridescent":       "#80c8c0",
    "blue":             "#3a5fa0",
    "green":            "#3a8040",
    "olive":            "#808020",
    "gold":             "#c8a820",
    "golden":           "#d4ac20",
    "yellow":           "#d0c830",
    "orange":           "#d06020",
    "red":              "#c03020",
    "brown":            "#7a5030",
    "black":            "#1a1a22",
    "white":            "#f0f0ec",
    "grey":             "#808080",
    "gray":             "#808080",
    "pink":             "#d48080",
    "purple":           "#7040a0",
    "violet":           "#6040a0",
    "teal":             "#208080",
    "tan":              "#c8a878",
    "cream":            "#e8e0c0",
    "beige":            "#d8c8a0",
    "bronze":           "#a07030",
    "copper":           "#c07030",
    "turquoise":        "#30a0a0",
}

BIOME_BACKGROUNDS: dict[str, tuple[str, str]] = {
    "tropical coral reef":  ("#0d5c7a", "#1a9fc0"),
    "coral reef":           ("#0d5c7a", "#1a9fc0"),
    "deep sea":             ("#020818", "#0a1a40"),
    "deep ocean":           ("#020818", "#0a1a40"),
    "open ocean":           ("#0a2040", "#1040a0"),
    "freshwater lake":      ("#0a2818", "#1a5830"),
    "river":                ("#0a2015", "#1a4828"),
    "cold water":           ("#0a1828", "#182840"),
    "arctic":               ("#0a1c2a", "#182838"),
    "kelp forest":          ("#0a2820", "#1a4830"),
    "mangrove":             ("#0a1c10", "#183820"),
    "tropical freshwater":  ("#0a2018", "#1a4828"),
}

_DEFAULT_BG = ("#0a1628", "#0a2848")


def biome_background(biome: str) -> tuple[str, str]:
    b = biome.lower().strip()
    for key, colors in BIOME_BACKGROUNDS.items():
        if key in b:
            return colors
    return _DEFAULT_BG


# Sorted by length (desc) so longer tokens match before sub-strings
_SORTED_TOKENS = sorted(_COLOR_MAP.keys(), key=len, reverse=True)

# Keywords that signal the dorsal / ventral region in a description
_DORSAL_CUES  = ('above', 'dorsal', 'back', 'upper', 'top')
_VENTRAL_CUES = ('below', 'ventral', 'belly', 'underside', 'lower', 'abdomen', 'white belly')


def color_to_css(token: str | None, fallback: str = "#888888") -> str:
    if not token:
        return fallback
    return _COLOR_MAP.get(token.lower().strip(), fallback)


def _first_color_in_text(text: str) -> str | None:
    """Return the first recognized color token found in a free-text description."""
    if not text:
        return None
    lt = text.lower()
    for token in _SORTED_TOKENS:
        if token in lt:
            return token
    return None


def _color_near_cue(text: str, cues: tuple[str, ...], window: int = 60) -> str | None:
    """Return the first colour token found within `window` chars of any cue word."""
    if not text:
        return None
    lt = text.lower()
    for cue in cues:
        idx = lt.find(cue)
        while idx >= 0:
            snippet = lt[max(0, idx - 10): idx + window]
            color = _first_color_in_text(snippet)
            if color:
                return color
            idx = lt.find(cue, idx + 1)
    return None


def _auto_ventral(dorsal_hex: str) -> str:
    """Derive a pale belly colour by mixing the dorsal colour 55% toward white."""
    try:
        r = int(dorsal_hex[1:3], 16)
        g = int(dorsal_hex[3:5], 16)
        b = int(dorsal_hex[5:7], 16)
        r2 = int(r + (255 - r) * 0.55)
        g2 = int(g + (255 - g) * 0.55)
        b2 = int(b + (255 - b) * 0.55)
        return f'#{r2:02x}{g2:02x}{b2:02x}'
    except Exception:
        return "#c8c8b8"


def hunger_interval_for_color(dorsal_hex: str) -> float:
    """Map dorsal body colour to a feeding interval in seconds.

    Warm bright fish (fast metabolism) → shorter interval.
    Dark fish (slow metabolism) → longer interval.
    """
    try:
        r = int(dorsal_hex[1:3], 16) / 255
        g = int(dorsal_hex[3:5], 16) / 255
        b = int(dorsal_hex[5:7], 16) / 255
        cmax, cmin = max(r, g, b), min(r, g, b)
        l = (cmax + cmin) / 2
        delta = cmax - cmin
        s = 0.0 if delta == 0 else delta / (1 - abs(2 * l - 1) + 1e-9)
        h = 0.0
        if delta > 0:
            if cmax == r:   h = ((g - b) / delta) % 6
            elif cmax == g: h = (b - r) / delta + 2
            else:           h = (r - g) / delta + 4
            h /= 6
        warm = h < 0.16 or h > 0.90
        if l < 0.28:              return 280.0   # dark — very slow metabolism
        if warm and s > 0.38:     return 100.0   # bright warm — fast
        if s > 0.28:              return 160.0   # saturated cool — medium
        return 220.0                              # pale/neutral — slow
    except Exception:
        return 160.0


def build_render_spec(data: dict[str, Any]) -> dict[str, Any]:
    morph = data.get("morphology") or {}
    bp    = data.get("blender_params") or {}
    loco  = data.get("locomotion") or {}
    cd    = morph.get("coloration_details") or {}
    fc    = morph.get("fin_colors") or {}
    fs    = bp.get("fishsim_params") or {}
    fins  = morph.get("fins") or {}

    length_cm      = morph.get("max_length_cm") or 30.0
    body_length_px = round(40 + min(120, (length_cm / 300.0) * 120))

    coloration_desc = morph.get("coloration_description") or ""

    # Dorsal (top/back) colour: prefer explicit field, fall back to text extraction
    dorsal_raw = (
        cd.get("dorsal_color")
        or _color_near_cue(coloration_desc, _DORSAL_CUES)
        or _first_color_in_text(coloration_desc)
    )
    dorsal_color = color_to_css(dorsal_raw, fallback="#4a6a80")

    # Ventral (belly) colour: prefer explicit field, then look near ventral cue words,
    # then auto-derive a paler version of the dorsal colour (typical for most fish)
    ventral_raw = (
        cd.get("ventral_color")
        or _color_near_cue(coloration_desc, _VENTRAL_CUES)
    )
    ventral_color = color_to_css(ventral_raw, fallback=_auto_ventral(dorsal_color))

    dorsal_fin_rays = (fins.get("dorsal_spines") or 0) + (fins.get("dorsal_rays") or 0)
    anal_fin_rays   = (fins.get("anal_spines")   or 0) + (fins.get("anal_rays")   or 0)

    return {
        "body_length_px":       body_length_px,
        "body_depth_ratio":     bp.get("body_depth_ratio")    or 0.28,
        "locomotion_type":      loco.get("type")               or "subcarangiform",
        "body_undulation":      loco.get("body_undulation")    or 0.50,
        "tail_shape":           loco.get("tail_shape")         or "forked",
        "pectoral_fin_role":    loco.get("pectoral_fin_role")  or "steering",
        "stroke_period_frames": fs.get("stroke_period")        or 28,
        "max_tail_angle_deg":   fs.get("max_tail_angle")       or 36,
        "hover_mode":           fs.get("hover_mode")           or False,
        "dorsal_color":         dorsal_color,
        "ventral_color":        ventral_color,
        "fin_colors": {
            "caudal":   color_to_css(fc.get("caudal"),   fallback=dorsal_color),
            "dorsal":   color_to_css(fc.get("dorsal"),   fallback=dorsal_color),
            "anal":     color_to_css(fc.get("anal"),     fallback=dorsal_color),
            "pectoral": color_to_css(fc.get("pectoral"), fallback=dorsal_color),
            "pelvic":   color_to_css(fc.get("pelvic"),   fallback=dorsal_color),
        },
        "lateral_line_color":  color_to_css(cd.get("lateral_line_color"), fallback="") or None,
        "iridescent":          cd.get("iridescent")   or False,
        "pattern":             cd.get("pattern")      or "none",
        "pattern_color":       color_to_css(cd.get("pattern_color"), fallback="") or None,
        "dorsal_fin_rays":     dorsal_fin_rays,
        "anal_fin_rays":       anal_fin_rays,
        "has_finlets":         morph.get("has_finlets") or False,
        "available_actions":   [c["name"] for c in (bp.get("animation_clips") or [])],
        "feeding_style":       bp.get("feeding_style") or "suction",
        "mouth_gape":          bp.get("mouth_gape")    or "medium",
        "hunger_interval":     hunger_interval_for_color(dorsal_color),
        "full_fraction":       0.35,
    }


def msg_aquarium_init(aquarium_id, theme, biome, bg_deep, bg_surface):
    return {"type": "aquarium_init", "aquarium_id": aquarium_id,
            "theme": theme, "biome": biome, "bg_deep": bg_deep, "bg_surface": bg_surface}

def msg_fish_add(aquarium_id, fish_id, species_name, common_name, render_spec, x, y, zone):
    return {"type": "fish_add", "aquarium_id": aquarium_id, "fish_id": fish_id,
            "species_name": species_name, "common_name": common_name,
            "render_spec": render_spec, "position": {"x": x, "y": y}, "zone": zone}

def msg_fish_action(aquarium_id, fish_id, action, duration_ms, target_position=None):
    m = {"type": "fish_action", "aquarium_id": aquarium_id, "fish_id": fish_id,
         "action": action, "duration_ms": duration_ms}
    if target_position:
        m["target_position"] = target_position
    return m

def msg_agent_thought(aquarium_id, message):
    return {"type": "agent_thought", "aquarium_id": aquarium_id, "message": message}

def msg_food_drop(aquarium_id, food_id, x_fraction):
    return {"type": "food_drop", "aquarium_id": aquarium_id,
            "food_id": food_id, "x_fraction": x_fraction}

def msg_fish_fed(aquarium_id, fish_id):
    return {"type": "fish_fed", "aquarium_id": aquarium_id, "fish_id": fish_id,
            "fed_at": time.time()}

def msg_fish_died(aquarium_id, fish_id, cause):
    return {"type": "fish_died", "aquarium_id": aquarium_id,
            "fish_id": fish_id, "cause": cause}

def msg_ping():
    return {"type": "ping", "ts": time.time()}
