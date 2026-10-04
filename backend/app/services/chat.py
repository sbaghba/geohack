"""
POST /api/chat — Server-Sent Events (see contract/CONTRACT.md "Chat").

With GEMINI_API_KEY set: Gemini streams the answer; when it calls a tool, the backend runs it,
emits `tool_call` (+ `report` / `suggestions`), feeds a compact result back to Gemini, and keeps streaming.
Without a key: a scripted stream that exercises every event type (useful for frontend dev and tests).
"""
from __future__ import annotations

import asyncio
import json
import logging
from typing import Any, AsyncIterator

import httpx

from ..config import settings
from ..contract import AnalyzeRequest, ChatRequest, DoneEvent, Report, SuggestRequest, TokenEvent, ToolCallEvent
from ..errors import ApiException
from ..llm_tools import SYSTEM_PROMPT, gemini_tools
from .analyze import analyze, suggest

log = logging.getLogger("sitesense.chat")
MAX_TOOL_ROUNDS = 4
DEFAULT_SITE = AnalyzeRequest(lat=35.655, lon=-78.462)


def sse(event: str, data) -> str:
    payload = data.model_dump(mode="json") if hasattr(data, "model_dump") else data
    return f"event: {event}\ndata: {json.dumps(payload)}\n\n"


def _llm_enabled() -> bool:
    return bool(settings.gemini_api_key)


_client = None


def _gemini():
    global _client
    if _client is None:
        from google import genai

        _client = genai.Client(api_key=settings.gemini_api_key)
    return _client


# ------------------------------------------------------------ helpers ---

def brief(r: Report) -> dict:
    """Compact view of a report for tool results (keeps Gemini's context small)."""
    return {
        "mock": r.mock,
        "site": {"lat": r.site.lat, "lon": r.site.lon, "label": r.site.label, "county": r.site.county,
                 "state": r.site.state, "tier": r.site.tier},
        "config": r.request.model_dump(),
        "scores": {**r.scores.model_dump(exclude={"pressure_drivers"}),
                   "pressure_drivers": [f"{d.label} ({d.direction})" for d in r.scores.pressure_drivers]},
        "energy": {"annual_mwh": r.energy.annual_mwh, "homes_equiv": r.energy.homes_equiv,
                   "county_homes_share_pct": r.energy.county_homes_share_pct,
                   "nearest_substation_km": r.energy.nearest_substation_km, "annual_cost_usd": r.energy.annual_cost_usd},
        "carbon": {"tons_co2_yr": r.carbon.tons_co2_yr, "cars_equiv": r.carbon.cars_equiv},
        "water": {"onsite_m3_yr": r.water.onsite_m3_yr, "households_equiv": r.water.households_equiv,
                  "aqueduct_stress": r.water.aqueduct_stress, "aqueduct_label": r.water.aqueduct_label,
                  "drought_category": r.water.drought_category},
        "economy": {"construction_jobs": r.economy.construction_jobs, "permanent_jobs": r.economy.permanent_jobs,
                    "county_unemployed": r.economy.county_unemployed, "property_tax_usd_yr": r.economy.property_tax_usd_yr},
        "hazards": {"fema_zone": r.hazards.fema_zone, "nri_overall": r.hazards.nri.overall, "nri_rating": r.hazards.nri_rating,
                    "subsidence_mm_yr": r.hazards.subsidence_mm_yr, "nisar_coherence": r.hazards.nisar_coherence},
        "community": {"pop_3km": r.community.pop_3km, "svi_pct": r.community.svi_pct,
                      "schools_1km": len(r.community.schools_1km), "hospitals_1km": len(r.community.hospitals_1km),
                      "existing_datacenters_25km": [f"{p.name} ({p.distance_km} km)" for p in r.community.datacenters_25km[:5]],
                      "nearest_datacenter": None if r.community.nearest_datacenter is None else
                      f"{r.community.nearest_datacenter.name} ({r.community.nearest_datacenter.distance_km} km)"},
        "land": {"acres": r.land.acres, "flags": r.land.flags,
                 "protected_areas": [f"{a.name} ({a.designation}, {'contains site' if a.contains_site else 'within 1 km'})"
                                     for a in r.land.protected_areas]},
        "missing": r.missing,
    }


def _system(report: Report | None, cfg: AnalyzeRequest) -> str:
    ctx = report.model_dump_json(exclude={"mitigations"}) if report else json.dumps({"config": cfg.model_dump(), "note": "no report yet"})
    return (f"{SYSTEM_PROMPT}\n\nWhen citing a number, use `field_sources` to name the right dataset "
            f"(a field inherits its section's source; 'model' = SiteSense impact model).\n\nREPORT:\n{ctx}")


async def _geocode(place: str) -> dict:
    async with httpx.AsyncClient(timeout=8, headers={"User-Agent": "SiteSense-WolfHacks/1.0"}) as c:
        r = await c.get("https://nominatim.openstreetmap.org/search",
                        params={"q": place, "format": "json", "limit": 1, "countrycodes": "us"})
        r.raise_for_status()
        hits = r.json()
    if not hits:
        return {"ok": False, "error": f"no match for {place!r}"}
    h = hits[0]
    return {"ok": True, "lat": float(h["lat"]), "lon": float(h["lon"]), "display_name": h.get("display_name")}


async def run_tool(name: str, args: dict, state: dict) -> tuple[dict, list[str]]:
    """Execute one tool. Returns (result for Gemini, SSE events for the browser)."""
    cfg: AnalyzeRequest = state["cfg"]
    try:
        if name == "geocode":
            return await _geocode(str(args.get("place", ""))), []

        if name in ("move_site", "set_config"):
            upd = {k: args[k] for k in ("lat", "lon", "mw", "cooling", "power") if args.get(k) is not None}
            new = AnalyzeRequest(**{**cfg.model_dump(), **upd})
            rep = await asyncio.to_thread(analyze, new)
            state["cfg"], state["report"] = new, rep
            return {"ok": True, **brief(rep)}, [sse("report", rep)]

        if name == "find_better_sites":
            radius = float(args.get("radius_km") or 50)
            resp = await asyncio.to_thread(suggest, SuggestRequest(**cfg.model_dump(), radius_km=max(5, min(200, radius))))
            cands = [{"rank": c.rank, "label": c.label, "lat": c.lat, "lon": c.lon, "distance_km": c.distance_km,
                      "scores": c.scores.model_dump(exclude={"pressure_drivers"}), "reason": c.reason} for c in resp.candidates]
            return {"ok": True, "radius_km": resp.radius_km, "candidates": cands}, [sse("suggestions", resp)]

        if name == "compare":
            other = await asyncio.to_thread(analyze, AnalyzeRequest(**{**cfg.model_dump(), "lat": args["lat"], "lon": args["lon"]}))
            current = state["report"] or await asyncio.to_thread(analyze, cfg)
            return {"ok": True, "current": brief(current), "other": brief(other)}, []

        return {"ok": False, "error": f"unknown tool {name}"}, []
    except ApiException as e:
        return {"ok": False, "error": e.error, "detail": e.detail}, []
    except Exception as e:  # never let a tool crash the stream
        log.exception("tool %s failed", name)
        return {"ok": False, "error": type(e).__name__, "detail": str(e)[:200]}, []


# ------------------------------------------------------------- gemini ---

async def _gemini_chat(req: ChatRequest) -> AsyncIterator[str]:
    from google.genai import types

    cfg = req.config or (req.report.request if req.report else DEFAULT_SITE)
    state: dict[str, Any] = {"cfg": cfg, "report": req.report}
    contents = [types.Content(role="user" if m.role == "user" else "model", parts=[types.Part.from_text(text=m.content)])
                for m in req.messages]
    models = [settings.gemini_model, settings.gemini_fallback_model]
    streamed_any = False
    n_calls = 0

    for _ in range(MAX_TOOL_ROUNDS + 1):
        config = types.GenerateContentConfig(
            system_instruction=_system(state["report"], state["cfg"]),
            tools=gemini_tools(),
            automatic_function_calling=types.AutomaticFunctionCallingConfig(disable=True),
            temperature=0.3,
        )
        parts, calls = [], []
        for attempt, model in enumerate(models):
            try:
                stream = await _gemini().aio.models.generate_content_stream(model=model, contents=contents, config=config)
                async for chunk in stream:
                    cand = chunk.candidates[0] if chunk.candidates else None
                    if not cand or not cand.content or not cand.content.parts:
                        continue
                    for part in cand.content.parts:
                        parts.append(part)
                        if part.function_call:
                            calls.append(part.function_call)
                        elif part.text and not part.thought:
                            streamed_any = True
                            yield sse("token", TokenEvent(text=part.text))
                break
            except Exception as e:
                log.warning("gemini %s failed: %s", model, e)
                if streamed_any or parts or attempt == len(models) - 1:
                    msg = "The AI analyst is busy (rate limit). Try again in a minute." if "429" in str(e) else f"AI error: {type(e).__name__}"
                    yield sse("error", {"message": msg})
                    yield sse("done", DoneEvent(finish_reason="error"))
                    return
        if not calls:
            break

        contents.append(types.Content(role="model", parts=parts))
        responses = []
        for call in calls:
            n_calls += 1
            args = dict(call.args or {})
            yield sse("tool_call", ToolCallEvent(id=call.id or f"tc{n_calls}", name=call.name, args=args)
                      if call.name in ("geocode", "move_site", "set_config", "find_better_sites", "compare")
                      else {"id": f"tc{n_calls}", "name": call.name, "args": args})
            result, events = await run_tool(call.name, args, state)
            for ev in events:
                yield ev
            responses.append(types.Part.from_function_response(name=call.name, response=result))
        contents.append(types.Content(role="user", parts=responses))

    yield sse("done", DoneEvent())


# ------------------------------------------------------------ scripted ---

async def _say(text: str) -> AsyncIterator[str]:
    for word in text.split(" "):
        yield sse("token", TokenEvent(text=word + " "))
        await asyncio.sleep(0.03)


async def _scripted_chat(req: ChatRequest) -> AsyncIterator[str]:
    last = req.messages[-1].content.lower()
    cfg = req.config or (req.report.request if req.report else DEFAULT_SITE)
    if any(w in last for w in ("move", "east", "west", "north", "south", "try")):
        moved = cfg.model_copy(update={"lon": cfg.lon + 0.2})
        yield sse("tool_call", ToolCallEvent(id="tc1", name="move_site", args={"lat": moved.lat, "lon": moved.lon}))
        yield sse("report", analyze(moved))
        async for chunk in _say("I moved the site about 18 km east. (Scripted reply: no Gemini key set.)"):
            yield chunk
    elif any(w in last for w in ("better", "where", "alternative")):
        yield sse("tool_call", ToolCallEvent(id="tc1", name="find_better_sites", args={"radius_km": 50}))
        yield sse("suggestions", suggest(SuggestRequest(**cfg.model_dump())))
        async for chunk in _say("Here are three nearby sites with lower community burden. (Scripted reply.)"):
            yield chunk
    else:
        async for chunk in _say("Scripted analyst (no Gemini key). Ask me to move the site east, or where a better site would be."):
            yield chunk
    yield sse("done", DoneEvent())


async def stream_chat(req: ChatRequest) -> AsyncIterator[str]:
    gen = _gemini_chat(req) if _llm_enabled() else _scripted_chat(req)
    async for chunk in gen:
        yield chunk
