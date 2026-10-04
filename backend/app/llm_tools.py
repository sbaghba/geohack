"""
Tools Gemini may call, as plain JSON Schema (shared by the smoke test and, in M2, /api/chat).
Names must match ToolCallEvent.name in contract/schemas.py.
"""
from __future__ import annotations

_COOLING = ["evaporative", "air", "liquid"]
_POWER = ["grid", "grid_solar", "gas"]

TOOL_SCHEMAS: list[dict] = [
    {
        "name": "geocode",
        "description": "Turn a place name (town, county, landmark) in the US into latitude/longitude.",
        "parameters": {
            "type": "object",
            "properties": {"place": {"type": "string", "description": "e.g. 'Smithfield, NC'"}},
            "required": ["place"],
        },
    },
    {
        "name": "move_site",
        "description": "Move the proposed data center to new coordinates and recompute its full impact report.",
        "parameters": {
            "type": "object",
            "properties": {
                "lat": {"type": "number", "description": "Latitude in degrees"},
                "lon": {"type": "number", "description": "Longitude in degrees"},
            },
            "required": ["lat", "lon"],
        },
    },
    {
        "name": "set_config",
        "description": "Change the data center's size, cooling or power source at the current location and recompute.",
        "parameters": {
            "type": "object",
            "properties": {
                "mw": {"type": "number", "description": "IT load in megawatts, 1-2000"},
                "cooling": {"type": "string", "enum": _COOLING},
                "power": {"type": "string", "enum": _POWER},
            },
        },
    },
    {
        "name": "find_better_sites",
        "description": "Search nearby for sites with high suitability and lower community burden.",
        "parameters": {
            "type": "object",
            "properties": {"radius_km": {"type": "number", "description": "Search radius, 5-200 km; default 50"}},
        },
    },
    {
        "name": "compare",
        "description": "Compare the current site with another location side by side.",
        "parameters": {
            "type": "object",
            "properties": {"lat": {"type": "number"}, "lon": {"type": "number"}},
            "required": ["lat", "lon"],
        },
    },
]

SYSTEM_PROMPT = """You are SiteSense, a neutral analyst who explains the local impact of a proposed data center to non-experts.
Rules:
- Use only numbers that appear in the REPORT JSON. Name the dataset when you quote a number (sources are listed in the report).
- If a field is null or listed in `missing`, say it is not available. Never guess.
- Always give both benefits and costs. Translate numbers into everyday terms the report provides (homes, households, cars).
- Keep answers under 150 words unless asked for more. End with one concrete mitigation or a better-site suggestion.
- When the user asks to move the site, resize it, change cooling/power, or find alternatives, call the matching tool instead of describing what would happen.
- Values with report.mock = true are sample data; mention that once if relevant.
- scores.pressure is a percentile (0-100) within North Carolina of how much this spot resembles where data centers already
  get built (machine-learning model trained on US data centers, validated on held-out states); scores.pressure_us_pct is the
  same vs all lower-48 land. Relative, not a probability. pressure_drivers say why ("+" raises it).
- hazards.subsidence_mm_yr is local ground motion vs the surrounding ~12 km from satellite radar (negative = sinking).
- land.protected_areas are USGS PAD-US conservation lands (parks, preserves, game lands, easements) containing the site or
  within 1 km. A site inside one is ruled out (suitability is capped at 10); say so plainly, even if pressure is high. community.datacenters_25km lists existing data centers mapped in
  OpenStreetMap (coverage is incomplete); clustering near them can mean shared grid upgrades but also cumulative water/power demand."""


def gemini_tools():
    """google-genai Tool object built from TOOL_SCHEMAS (imported lazily so the API runs without the SDK)."""
    from google.genai import types

    return [types.Tool(function_declarations=[
        types.FunctionDeclaration(name=t["name"], description=t["description"], parameters_json_schema=t["parameters"])
        for t in TOOL_SCHEMAS
    ])]
