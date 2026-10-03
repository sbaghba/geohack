"""GET /api/layers and /api/layers/{name}. M1: sample hexes. M3: GeoJSON exported from the grid."""
from __future__ import annotations

import json

from ..config import settings
from ..contract import LAYERS, LayerList, build_layer

_PHASE = {"suitability": 0.0, "burden": 1.3, "pressure": 2.1, "subsidence": 3.0, "water_stress": 4.2}


def list_layers() -> LayerList:
    return LAYERS


def get_layer(name: str) -> dict:
    real = settings.data_dir / "layers" / f"{name}.geojson"
    if not settings.use_mock and real.exists():
        return json.loads(real.read_text())
    return build_layer(35.655, -78.462, _PHASE[name])
