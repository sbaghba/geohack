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


USDM = "https://gis.fema.gov/arcgis/rest/services/Partner/Drought_Current/MapServer/0/query"
_DM = {0: "D0", 1: "D1", 2: "D2", 3: "D3", 4: "D4"}


@lru_cache(maxsize=2048)
def _usdm(lat2: float, lon2: float) -> str:
    params = {"geometry": f"{lon2},{lat2}", "geometryType": "esriGeometryPoint", "inSR": 4326,
              "spatialRel": "esriSpatialRelIntersects", "outFields": "dm", "returnGeometry": "false", "f": "json"}
    r = httpx.get(USDM, params=params, timeout=TIMEOUT, headers={"User-Agent": "SiteSense-WolfHacks/1.0"})
    r.raise_for_status()
    js = r.json()
    if "error" in js:
        raise RuntimeError(js["error"])
    levels = [f["attributes"].get("dm") for f in js.get("features", []) if f["attributes"].get("dm") is not None]
    return _DM[max(levels)] if levels else "none"     # USDM categories nest; the worst one applies


def usdm_drought(lat: float, lon: float) -> str | None:
    """Current US Drought Monitor category at a point: 'none', 'D0'..'D4', or None if unavailable."""
    try:
        return _usdm(round(lat, 2), round(lon, 2))
    except Exception as e:
        log.warning("USDM lookup failed: %s", e)
        return None


PADUS = ("https://services.arcgis.com/v01gqwM5QqNysAAi/arcgis/rest/services/"
         "PADUS_Protection_Status_by_GAP_Status_Code/FeatureServer/0/query")


def _padus_query(lon: float, lat: float, meters: int) -> list[dict]:
    params = {"geometry": f"{lon},{lat}", "geometryType": "esriGeometryPoint", "inSR": 4326,
              "spatialRel": "esriSpatialRelIntersects", "distance": meters, "units": "esriSRUnit_Meter",
              "where": "GAP_Sts IN ('1','2','3')",   # GAP 4 = no protection mandate (city squares, school lots)
              "outFields": "Unit_Nm,DesTp_Desc,MngNm_Desc,GAP_Sts,GIS_Acres", "returnGeometry": "false", "f": "json"}
    r = httpx.get(PADUS, params=params, timeout=TIMEOUT, headers={"User-Agent": "SiteSense-WolfHacks/1.0"})
    r.raise_for_status()
    js = r.json()
    if "error" in js:
        raise RuntimeError(js["error"])
    return [f["attributes"] for f in js.get("features", [])]


@lru_cache(maxsize=2048)
def _padus(lat4: float, lon4: float) -> tuple:
    inside = {a.get("Unit_Nm") for a in _padus_query(lon4, lat4, 0)}
    seen, out = set(), []
    for a in _padus_query(lon4, lat4, 1000):
        name = (a.get("Unit_Nm") or "Unnamed protected area").strip()
        if name in seen:
            continue
        seen.add(name)
        mgr = a.get("MngNm_Desc")
        out.append({"name": name[:80], "designation": a.get("DesTp_Desc"),
                    "manager": None if mgr in (None, "Unknown") else mgr,
                    "gap_status": int(a["GAP_Sts"]) if str(a.get("GAP_Sts", "")).isdigit() else None,
                    "acres": a.get("GIS_Acres"), "contains_site": a.get("Unit_Nm") in inside})
    out.sort(key=lambda d: (not d["contains_site"], -(d["acres"] or 0)))
    return tuple(out[:5])


def protected_areas(lat: float, lon: float) -> list[dict] | None:
    """USGS PAD-US (GAP 1-3) areas containing the point or within 1 km; None if the service is unavailable."""
    try:
        return list(_padus(round(lat, 4), round(lon, 4)))
    except Exception as e:
        log.warning("PAD-US lookup failed: %s", e)
        return None


def point_lookups(lat: float, lon: float):
    """FEMA flood zone, drought category and PAD-US protected areas in parallel (each capped by LIVE_TIMEOUT_S).
    Returns ((zone, in_sfha), drought, protected_areas)."""
    from concurrent.futures import ThreadPoolExecutor

    with ThreadPoolExecutor(max_workers=3) as ex:
        f1, f2, f3 = ex.submit(fema_flood_zone, lat, lon), ex.submit(usdm_drought, lat, lon), ex.submit(protected_areas, lat, lon)
        return f1.result(), f2.result(), f3.result()
