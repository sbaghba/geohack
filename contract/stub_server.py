"""
Contract stub: every endpoint, real shapes, sample numbers. Deploy this in the
first hour so the frontend can hit a live URL (CORS, HTTPS) right away; the
backend then replaces each handler with the real thing, one at a time.

    pip install fastapi uvicorn "pydantic>=2" h3
    uvicorn stub_server:app --reload --port 8000
    open http://localhost:8000/docs
"""
from __future__ import annotations

import asyncio
import json

from fastapi import FastAPI, HTTPException, Request
from fastapi.exceptions import RequestValidationError
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse, StreamingResponse

from make_mock import LAYERS, build_layer, build_report, build_suggest
from schemas import (
    CONTRACT_VERSION, AnalyzeRequest, ChatRequest, DoneEvent, Health, LayerList, LayerName,
    Report, SuggestRequest, SuggestResponse, TokenEvent, ToolCallEvent,
)

app = FastAPI(title="SiteSense API (stub)", version=CONTRACT_VERSION)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],          # real backend: list the frontend domain + localhost ports
    allow_methods=["*"],
    allow_headers=["*"],
)


# ---- errors always look like {"error": ..., "detail": ...} ----

@app.exception_handler(RequestValidationError)
async def on_validation(_: Request, exc: RequestValidationError):
    first = exc.errors()[0]
    where = ".".join(str(p) for p in first["loc"][1:])
    return JSONResponse(status_code=422, content={"error": "invalid_request", "detail": f"{where}: {first['msg']}"})


@app.exception_handler(HTTPException)
async def on_http(_: Request, exc: HTTPException):
    code = {404: "invalid_request", 429: "rate_limited", 503: "upstream_unavailable"}.get(exc.status_code, "internal")
    return JSONResponse(status_code=exc.status_code, content={"error": code, "detail": str(exc.detail)})


class _OutOfCoverage(Exception):
    pass


def check_coverage(lat: float, lon: float):
    # Rough contiguous-US box. Real backend: tier 1 = NC grid, tier 2 = rest of US, else 422.
    if not (24 <= lat <= 50 and -125 <= lon <= -66):
        raise _OutOfCoverage()


@app.exception_handler(_OutOfCoverage)
async def on_coverage(_: Request, __: _OutOfCoverage):
    return JSONResponse(status_code=422, content={"error": "out_of_coverage", "detail": "SiteSense covers the contiguous US only"})


# ---- endpoints ----

@app.get("/api/health", response_model=Health)
def health():
    return Health(ok=True, llm_ok=False, model=None, grid_rows=0)


@app.post("/api/analyze", response_model=Report)
def analyze(req: AnalyzeRequest):
    check_coverage(req.lat, req.lon)
    return build_report(req)


@app.post("/api/suggest", response_model=SuggestResponse)
def suggest(req: SuggestRequest):
    check_coverage(req.lat, req.lon)
    base = AnalyzeRequest(**req.model_dump(exclude={"radius_km", "n"}))
    return build_suggest(req, build_report(base))


@app.get("/api/layers", response_model=LayerList)
def layers():
    return LAYERS


@app.get("/api/layers/{name}")
def layer(name: LayerName):
    phase = {"suitability": 0, "burden": 1.3, "pressure": 2.1, "subsidence": 3.0, "water_stress": 4.2}[name]
    return build_layer(35.655, -78.462, phase)


def sse(event: str, data) -> str:
    payload = data.model_dump(mode="json") if hasattr(data, "model_dump") else data
    return f"event: {event}\ndata: {json.dumps(payload)}\n\n"


@app.post("/api/chat")
async def chat(req: ChatRequest):
    last = req.messages[-1].content.lower()
    cfg = req.config or (req.report.request if req.report else AnalyzeRequest(lat=35.655, lon=-78.462))

    async def stream():
        async def say(text: str):
            for word in text.split(" "):
                yield sse("token", TokenEvent(text=word + " "))
                await asyncio.sleep(0.03)

        if any(w in last for w in ("move", "east", "west", "north", "south", "try")):
            moved = cfg.model_copy(update={"lon": cfg.lon + 0.2})
            yield sse("tool_call", ToolCallEvent(id="tc1", name="move_site", args={"lat": moved.lat, "lon": moved.lon}))
            await asyncio.sleep(0.4)
            yield sse("report", build_report(moved))
            async for chunk in say("I moved the site about 18 km east. (Stub reply: sample data.)"):
                yield chunk
        elif any(w in last for w in ("better", "where", "alternative")):
            sreq = SuggestRequest(**cfg.model_dump())
            yield sse("tool_call", ToolCallEvent(id="tc1", name="find_better_sites", args={"radius_km": 50}))
            await asyncio.sleep(0.4)
            yield sse("suggestions", build_suggest(sreq, build_report(cfg)))
            async for chunk in say("Here are three nearby sites with lower community burden. (Stub reply: sample data.)"):
                yield chunk
        else:
            async for chunk in say("This is the stub analyst. Try asking me to move the site east, "
                                   "or where a better site would be."):
                yield chunk
        yield sse("done", DoneEvent())

    return StreamingResponse(stream(), media_type="text/event-stream",
                             headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})
