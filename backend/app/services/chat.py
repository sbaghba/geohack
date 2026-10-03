"""
POST /api/chat — Server-Sent Events.

M1: scripted stream that exercises every event type (token, tool_call, report, suggestions, done)
    so the frontend can build the chat UI now.
M2: replace `stream_chat` with the Gemini loop (see scripts/gemini_smoke_test.py for the round trip).
"""
from __future__ import annotations

import asyncio
import json
from typing import AsyncIterator

from ..contract import AnalyzeRequest, ChatRequest, DoneEvent, SuggestRequest, TokenEvent, ToolCallEvent
from .analyze import analyze, suggest


def sse(event: str, data) -> str:
    payload = data.model_dump(mode="json") if hasattr(data, "model_dump") else data
    return f"event: {event}\ndata: {json.dumps(payload)}\n\n"


async def _say(text: str) -> AsyncIterator[str]:
    for word in text.split(" "):
        yield sse("token", TokenEvent(text=word + " "))
        await asyncio.sleep(0.03)


async def stream_chat(req: ChatRequest) -> AsyncIterator[str]:
    last = req.messages[-1].content.lower()
    cfg = req.config or (req.report.request if req.report else AnalyzeRequest(lat=35.655, lon=-78.462))

    if any(w in last for w in ("move", "east", "west", "north", "south", "try")):
        moved = cfg.model_copy(update={"lon": cfg.lon + 0.2})
        yield sse("tool_call", ToolCallEvent(id="tc1", name="move_site", args={"lat": moved.lat, "lon": moved.lon}))
        yield sse("report", analyze(moved))
        async for chunk in _say("I moved the site about 18 km east. (Scripted M1 reply.)"):
            yield chunk
    elif any(w in last for w in ("better", "where", "alternative")):
        yield sse("tool_call", ToolCallEvent(id="tc1", name="find_better_sites", args={"radius_km": 50}))
        yield sse("suggestions", suggest(SuggestRequest(**cfg.model_dump())))
        async for chunk in _say("Here are three nearby sites with lower community burden. (Scripted M1 reply.)"):
            yield chunk
    else:
        async for chunk in _say("Scripted M1 analyst. Ask me to move the site east, or where a better site would be."):
            yield chunk
    yield sse("done", DoneEvent())
