"""Live point lookups with short timeouts and caching. A failure returns None (the field goes to `missing`)."""
from __future__ import annotations

import logging
import os
from functools import lru_cache

import httpx

log = logging.getLogger("sitesense.live")
NFHL = "https://hazards.fema.gov/arcgis/rest/services/public/NFHL/MapServer/28/query"
TIMEOUT = float(os.getenv("LIVE_TIMEOUT_S", "4"))


@lru_cache(maxsize=2048)
def _fema(lat4: float, lon4: float) -> tuple[str | None, bool | None]:
    params = {"geometry": f"{lon4},{lat4}", "geometryType": "esriGeometryPoint", "inSR": 4326,
              "spatialRel": "esriSpatialRelIntersects", "outFields": "FLD_ZONE,ZONE_SUBTY,SFHA_TF",
              "returnGeometry": "false", "f": "json"}
    r = httpx.get(NFHL, params=params, timeout=TIMEOUT, headers={"User-Agent": "SiteSense-WolfHacks/1.0"})
    r.raise_for_status()
    feats = r.json().get("features", [])
    if not feats:
        return None, None
    a = feats[0]["attributes"]
    return a.get("FLD_ZONE"), a.get("SFHA_TF") == "T"


def fema_flood_zone(lat: float, lon: float) -> tuple[str | None, bool | None]:
    """(zone like 'AE'/'X', in 100-yr floodplain?) or (None, None) if unmapped/unavailable."""
    try:
        return _fema(round(lat, 4), round(lon, 4))
    except Exception as e:
        log.warning("FEMA NFHL lookup failed: %s", e)
        return None, None
