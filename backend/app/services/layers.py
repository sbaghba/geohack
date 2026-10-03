"""GET /api/layers and /api/layers/{name}. Real GeoJSON from data/layers when present, else sample hexes."""
from __future__ import annotations

import json
from functools import lru_cache

from ..config import settings
from ..contract import LAYERS, LayerList, OverlayInfo, OverlayList, build_layer
from .analyze import real_mode

_PHASE = {"suitability": 0.0, "burden": 1.3, "pressure": 2.1, "subsidence": 3.0, "water_stress": 4.2, "nisar_coherence": 5.0}


def _path(name: str):
    return settings.data_dir / "layers" / f"{name}.geojson"


def list_layers() -> LayerList:
    if not real_mode():
        return LAYERS
    return LayerList(layers=[l for l in LAYERS.layers if _path(l.name).exists()])


@lru_cache(maxsize=8)
def _load(path: str) -> dict:
    with open(path) as f:
        return json.load(f)


def get_layer(name: str) -> dict:
    if real_mode():
        p = _path(name)
        return _load(str(p)) if p.exists() else {"type": "FeatureCollection", "features": []}
    return build_layer(35.655, -78.462, _PHASE[name])


def overlays_dir():
    return settings.data_dir / "overlays"


def list_overlays(base_url: str) -> OverlayList:
    meta = overlays_dir() / "overlays.json"
    if not meta.exists():
        return OverlayList(overlays=[])
    items = json.loads(meta.read_text())
    base = base_url.rstrip("/")
    out = []
    for it in items:
        if (overlays_dir() / it["url"].rsplit("/", 1)[-1]).exists():
            out.append(OverlayInfo(**{**it, "url": base + it["url"] if it["url"].startswith("/") else it["url"]}))
    return OverlayList(overlays=out)
