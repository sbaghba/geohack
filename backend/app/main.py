"""
SiteSense backend.

    cd backend
    pip install -r requirements.txt
    uvicorn app.main:app --reload --port 8000      # docs at http://localhost:8000/docs
"""
from __future__ import annotations

from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.middleware.gzip import GZipMiddleware
from fastapi.responses import StreamingResponse
from fastapi.staticfiles import StaticFiles

from . import errors
from .config import settings
from .contract import (
    CONTRACT_VERSION, AnalyzeRequest, ChatRequest, Health, LayerList, LayerName, OverlayList, Report,
    SuggestRequest, SuggestResponse,
)
from .services import analyze as analyze_svc
from .services import chat as chat_svc
from .services import layers as layers_svc

app = FastAPI(title="SiteSense API", version=CONTRACT_VERSION)

app.add_middleware(
    CORSMiddleware,
    allow_origins=settings.allowed_origins,
    allow_origin_regex=settings.allow_origin_regex,
    allow_methods=["GET", "POST", "OPTIONS"],
    allow_headers=["*"],
)
# layer GeoJSON is ~7 MB raw, ~1 MB gzipped (Starlette skips text/event-stream, so chat streaming is unaffected)
app.add_middleware(GZipMiddleware, minimum_size=1000)
errors.install(app)
layers_svc.overlays_dir().mkdir(parents=True, exist_ok=True)
app.mount("/static/overlays", StaticFiles(directory=str(layers_svc.overlays_dir())), name="overlays")


def _grid_rows() -> int:
    if not settings.grid_path.exists():
        return 0
    try:
        import pyarrow.parquet as pq

        return pq.ParquetFile(settings.grid_path).metadata.num_rows
    except Exception:
        return 0


@app.get("/", include_in_schema=False)
def root():
    return {"service": "sitesense", "docs": "/docs", "health": "/api/health"}


@app.get("/api/health", response_model=Health)
def health():
    return Health(ok=True, llm_ok=bool(settings.gemini_api_key), model=settings.gemini_model if settings.gemini_api_key else None,
                  grid_rows=_grid_rows())


@app.post("/api/analyze", response_model=Report)
def analyze(req: AnalyzeRequest):
    return analyze_svc.analyze(req)


@app.post("/api/suggest", response_model=SuggestResponse)
def suggest(req: SuggestRequest):
    return analyze_svc.suggest(req)


@app.get("/api/layers", response_model=LayerList)
def layers():
    return layers_svc.list_layers()


@app.get("/api/layers/{name}")
def layer(name: LayerName):
    return layers_svc.get_layer(name)


@app.get("/api/overlays", response_model=OverlayList)
def overlays(request: Request):
    return layers_svc.list_overlays(str(request.base_url))


@app.get("/api/model")
def model_card():
    """Siting Pressure model card: validation metrics (states held out) and feature importance."""
    p = settings.data_dir / "models" / "pressure_metrics.json"
    if not p.exists():
        return {"available": False}
    import json

    return {"available": True, **json.loads(p.read_text())}


@app.post("/api/chat")
async def chat(req: ChatRequest):
    return StreamingResponse(chat_svc.stream_chat(req), media_type="text/event-stream",
                             headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})
