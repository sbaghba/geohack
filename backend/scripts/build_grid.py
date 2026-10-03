"""
M2: build the North Carolina H3 grid (resolution 7, ~5 km² per hex) with every P0 feature joined in.
Run on the A100 server (needs internet + ~5 GB disk). Takes ~10-30 min the first time; downloads are cached.

    pip install -r requirements-etl.txt
    python scripts/build_grid.py                 # everything
    python scripts/build_grid.py --only osm      # re-run one step (boundaries,census,nri,aqueduct,landcover,osm,score,layers)

Outputs (commit these three folders; they are small):
    data/grid/nc_h3r7.parquet       one row per hex, all features + scores
    data/grid/counties_nc.parquet   county households, unemployment
    data/grid/us_states.parquet     state polygons (WKB) for tier-2 lookups
    data/points/*.parquet           substations, line vertices, schools, hospitals, data centers (exact distances at request time)
    data/layers/*.geojson           suitability, burden, water_stress hex layers for the map
"""
from __future__ import annotations

import argparse
import io
import json
import math
import os
import sys
import time
import zipfile
from datetime import datetime, timezone
from pathlib import Path

import h3
import numpy as np
import pandas as pd
import requests

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from app.reference import MIN_GRID_KV  # noqa: E402

DATA = Path(os.getenv("DATA_DIR", Path(__file__).resolve().parents[1] / "data"))
RAW, INTERIM = DATA / "raw", DATA / "interim"
GRID, POINTS, LAYERS = DATA / "grid", DATA / "points", DATA / "layers"
for d in (RAW, INTERIM, GRID, POINTS, LAYERS):
    d.mkdir(parents=True, exist_ok=True)

RES = 7
NC_FIPS = "37"
NC_BBOX = (33.7, -84.5, 36.7, -75.3)       # S, W, N, E  (a bit beyond NC so border hexes see neighbors' substations)
CB = "https://www2.census.gov/geo/tiger/GENZ2023/shp/{name}.zip"
ACS = "https://api.census.gov/data/2023/acs/acs5"
NRI = "https://services.arcgis.com/XG15cJAlne2vxtgt/arcgis/rest/services/National_Risk_Index_Census_Tracts/FeatureServer/0/query"
AQUEDUCT_ZIP = "https://files.wri.org/aqueduct/aqueduct-4-0-water-risk-data.zip"
OVERPASS = ["https://overpass-api.de/api/interpreter", "https://overpass.kumi.systems/api/interpreter"]
UA = {"User-Agent": "SiteSense-WolfHacks/1.0 (NC State hackathon)"}
EARTH_R = 6371.0088


def log(msg):
    print(f"[{datetime.now():%H:%M:%S}] {msg}", flush=True)


def download(url: str, dest: Path, timeout=600) -> Path:
    if dest.exists() and dest.stat().st_size > 0:
        return dest
    log(f"  download {url}")
    tmp = dest.with_suffix(dest.suffix + ".part")
    with requests.get(url, stream=True, timeout=timeout, headers=UA) as r:
        r.raise_for_status()
        with open(tmp, "wb") as f:
            for chunk in r.iter_content(1 << 20):
                f.write(chunk)
    tmp.rename(dest)
    return dest


# ----------------------------------------------------------- boundaries ---

def step_boundaries():
    import geopandas as gpd
    from shapely.geometry import Polygon

    log("boundaries + hex grid")
    states = gpd.read_file(f"zip://{download(CB.format(name='cb_2023_us_state_20m'), RAW / 'cb_2023_us_state_20m.zip')}")
    counties = gpd.read_file(f"zip://{download(CB.format(name='cb_2023_us_county_500k'), RAW / 'cb_2023_us_county_500k.zip')}")
    tracts = gpd.read_file(f"zip://{download(CB.format(name='cb_2023_37_tract_500k'), RAW / 'cb_2023_37_tract_500k.zip')}")
    states, counties, tracts = (g.to_crs(4326) for g in (states, counties, tracts))
    counties = counties[counties.STATEFP == NC_FIPS]

    pd.DataFrame({"stusps": states.STUSPS, "wkb": states.geometry.to_wkb()}).to_parquet(GRID / "us_states.parquet")

    nc = states[states.STUSPS == "NC"].geometry.union_all()
    cells = sorted(h3.geo_to_cells(nc.__geo_interface__, RES))
    log(f"  {len(cells):,} hexes at res {RES}")
    polys = [Polygon([(lng, lat) for lat, lng in h3.cell_to_boundary(c)]) for c in cells]
    hexes = gpd.GeoDataFrame({"hex_id": cells}, geometry=polys, crs=4326)
    ll = np.array([h3.cell_to_latlng(c) for c in cells])
    hexes["lat"], hexes["lon"] = ll[:, 0], ll[:, 1]
    hexes["area_km2"] = hexes.to_crs(5070).area / 1e6

    cent = gpd.GeoDataFrame(hexes[["hex_id"]], geometry=gpd.points_from_xy(hexes.lon, hexes.lat), crs=4326)
    j = gpd.sjoin(cent, counties[["GEOID", "NAME", "geometry"]], how="left", predicate="within")
    j = j.drop_duplicates("hex_id").set_index("hex_id")
    hexes["county_fips"] = hexes.hex_id.map(j.GEOID)
    hexes["county"] = hexes.hex_id.map(j.NAME)
    # border hexes whose center falls just outside: nearest county
    miss = hexes.county.isna()
    if miss.any():
        nn = gpd.sjoin_nearest(cent[miss.values].to_crs(5070), counties[["GEOID", "NAME", "geometry"]].to_crs(5070), how="left")
        nn = nn.drop_duplicates("hex_id").set_index("hex_id")
        hexes.loc[miss, "county_fips"] = hexes.loc[miss, "hex_id"].map(nn.GEOID)
        hexes.loc[miss, "county"] = hexes.loc[miss, "hex_id"].map(nn.NAME)

    hexes.to_parquet(INTERIM / "hexes.parquet")
    tracts[["GEOID", "geometry"]].to_parquet(INTERIM / "tracts.parquet")

    # hex x tract intersection weights (used by census + nri)
    inter = gpd.overlay(hexes[["hex_id", "geometry"]].to_crs(5070), tracts[["GEOID", "geometry"]].to_crs(5070),
                        how="intersection", keep_geom_type=True)
    inter["a"] = inter.area
    tract_area = tracts.to_crs(5070).set_index("GEOID").area
    inter["w_tract"] = inter.a / inter.GEOID.map(tract_area)                      # share of tract inside hex
    inter["w_hex"] = inter.a / inter.groupby("hex_id").a.transform("sum")        # share of hex covered by tract
    inter[["hex_id", "GEOID", "w_tract", "w_hex"]].to_parquet(INTERIM / "hex_tract_weights.parquet")
    log(f"  {len(inter):,} hex-tract pieces")


def _hexes():
    return pd.read_parquet(INTERIM / "hexes.parquet")


def _weights():
    return pd.read_parquet(INTERIM / "hex_tract_weights.parquet")


def _merge(cols: pd.DataFrame, name: str):
    cols.to_parquet(INTERIM / f"feat_{name}.parquet")


# --------------------------------------------------------------- census ---

def _acs(get: list[str], geo: dict) -> pd.DataFrame:
    params = {"get": ",".join(get), **geo}
    if os.getenv("CENSUS_API_KEY"):
        params["key"] = os.getenv("CENSUS_API_KEY")
    r = requests.get(ACS, params=params, timeout=120, headers=UA)
    r.raise_for_status()
    rows = r.json()
    df = pd.DataFrame(rows[1:], columns=rows[0])
    for c in get:
        df[c] = pd.to_numeric(df[c], errors="coerce")
        df.loc[df[c] < 0, c] = np.nan            # ACS sentinels like -666666666
    return df


def step_census():
    log("census ACS 2023 5-year")
    t = _acs(["B01003_001E", "B11001_001E", "B19013_001E"], {"for": "tract:*", "in": f"state:{NC_FIPS}"})
    t["GEOID"] = t.state + t.county + t.tract
    c = _acs(["B11001_001E", "B23025_003E", "B23025_005E"], {"for": "county:*", "in": f"state:{NC_FIPS}"})
    c["county_fips"] = c.state + c.county
    c = c.rename(columns={"B11001_001E": "households", "B23025_003E": "labor_force", "B23025_005E": "unemployed"})
    c["unemployment_pct"] = 100 * c.unemployed / c.labor_force
    names = _hexes().drop_duplicates("county_fips").set_index("county_fips").county
    c["county"] = c.county_fips.map(names)
    c[["county_fips", "county", "households", "labor_force", "unemployed", "unemployment_pct"]].to_parquet(GRID / "counties_nc.parquet")

    w = _weights().merge(t[["GEOID", "B01003_001E", "B11001_001E", "B19013_001E"]], on="GEOID", how="left")
    w["pop"] = w.B01003_001E * w.w_tract
    w["hh"] = w.B11001_001E * w.w_tract
    w["inc_w"] = w.B19013_001E * w.w_hex
    w["inc_wt"] = np.where(w.B19013_001E.notna(), w.w_hex, 0)
    g = w.groupby("hex_id").agg(pop=("pop", "sum"), households=("hh", "sum"), inc_w=("inc_w", "sum"), inc_wt=("inc_wt", "sum"))
    g["median_income_usd"] = g.inc_w / g.inc_wt.replace(0, np.nan)
    _merge(g[["pop", "households", "median_income_usd"]].reset_index(), "census")
    log(f"  NC population in grid: {g['pop'].sum():,.0f}")


# ------------------------------------------------------------------ NRI ---

NRI_FIELDS = {"RISK_SCORE": "nri_overall", "HRCN_RISKS": "nri_hurricane", "HWAV_RISKS": "nri_heat_wave",
              "IFLD_RISKS": "nri_riverine_flooding", "CFLD_RISKS": "nri_coastal_flooding", "TRND_RISKS": "nri_tornado",
              "SOVI_SCORE": "svi_pct", "RESL_SCORE": "resilience"}


def step_nri():
    log("FEMA National Risk Index (tracts)")
    rows, offset = [], 0
    while True:
        r = requests.get(NRI, params={"where": "STATEABBRV='NC'", "outFields": ",".join(["TRACTFIPS", "RISK_RATNG", *NRI_FIELDS]),
                                      "returnGeometry": "false", "f": "json", "resultOffset": offset, "resultRecordCount": 2000},
                         timeout=120, headers=UA)
        r.raise_for_status()
        js = r.json()
        feats = [f["attributes"] for f in js.get("features", [])]
        rows += feats
        if not feats or not js.get("exceededTransferLimit"):
            break
        offset += len(feats)
    nri = pd.DataFrame(rows).rename(columns={"TRACTFIPS": "GEOID", **NRI_FIELDS})
    log(f"  {len(nri):,} tracts")
    w = _weights().merge(nri, on="GEOID", how="left")
    out = {}
    for col in NRI_FIELDS.values():
        val = pd.to_numeric(w[col], errors="coerce")
        out[col] = (val * w.w_hex).groupby(w.hex_id).sum() / (w.w_hex.where(val.notna(), 0)).groupby(w.hex_id).sum().replace(0, np.nan)
    df = pd.DataFrame(out)
    top = w.sort_values("w_hex", ascending=False).drop_duplicates("hex_id").set_index("hex_id")
    df["nri_rating"] = top.RISK_RATNG
    _merge(df.reset_index(), "nri")


# ------------------------------------------------------------- aqueduct ---

def step_aqueduct():
    import geopandas as gpd
    import pyogrio

    log("WRI Aqueduct 4.0 baseline water stress")
    z = download(AQUEDUCT_ZIP, RAW / "aqueduct-4-0-water-risk-data.zip", timeout=1800)
    names = zipfile.ZipFile(z).namelist()
    gdbs = sorted({n.split(".gdb/")[0] + ".gdb" for n in names if ".gdb/" in n})
    candidates = [f"/vsizip/{z}/{g}" for g in gdbs] + [f"/vsizip/{z}/{n}" for n in names if n.endswith((".gpkg", ".shp"))]
    src = layer = None
    for c in candidates:
        try:
            for name, _ in pyogrio.list_layers(c):
                if "baseline" in name.lower() and "annual" in name.lower():
                    src, layer = c, name
                    break
        except Exception:
            continue
        if src:
            break
    if not src:
        raise RuntimeError(f"no baseline annual layer found; zip has: {names[:30]}")
    log(f"  reading {layer} from {src}")
    s, w_, n, e = NC_BBOX
    aq = gpd.read_file(src, layer=layer, bbox=(w_, s, e, n))
    cols = {c.lower(): c for c in aq.columns}
    aq = aq.rename(columns={cols["bws_score"]: "bws_score", cols["bws_label"]: "bws_label"})[["bws_score", "bws_label", "geometry"]]
    aq.loc[aq.bws_score < 0, "bws_score"] = np.nan
    hx = _hexes()
    cent = gpd.GeoDataFrame(hx[["hex_id"]], geometry=gpd.points_from_xy(hx.lon, hx.lat), crs=4326)
    j = gpd.sjoin(cent, aq.to_crs(4326), how="left", predicate="within").drop_duplicates("hex_id")
    _merge(j[["hex_id", "bws_score", "bws_label"]], "aqueduct")
    log(f"  matched {j.bws_score.notna().mean():.0%} of hexes")


# ------------------------------------------------------------ landcover ---

WC_CLASSES = {"forest": [10], "pasture": [20, 30], "cropland": [40], "developed": [50], "wetland": [90, 95],
              "water": [80], "other": [60, 70, 100]}


def step_landcover():
    import rasterio
    from rasterio.enums import Resampling

    log("ESA WorldCover land-cover shares (49 samples per hex)")
    hx = _hexes()
    kids = [(h, *h3.cell_to_latlng(k)) for h in hx.hex_id for k in h3.cell_to_children(h, RES + 2)]
    pts = pd.DataFrame(kids, columns=["hex_id", "lat", "lon"])
    pts["cls"] = 0
    tiles = sorted((RAW / "landcover").glob("ESA_WorldCover_*_Map.tif"))
    if not tiles:
        raise RuntimeError("no WorldCover tiles in data/raw/landcover — run scripts/download_data.py landcover")
    for t in tiles:
        with rasterio.open(t) as src:
            f = 10  # read at ~100 m using overviews
            out_h, out_w = src.height // f, src.width // f
            arr = src.read(1, out_shape=(out_h, out_w), resampling=Resampling.mode)
            tr = src.transform * src.transform.scale(src.width / out_w, src.height / out_h)
            b = src.bounds
            m = (pts.lon >= b.left) & (pts.lon < b.right) & (pts.lat > b.bottom) & (pts.lat <= b.top) & (pts.cls == 0)
            if not m.any():
                continue
            cols, rows = ~tr * (pts.lon[m].values, pts.lat[m].values)
            rows = np.clip(rows.astype(int), 0, out_h - 1)
            cols = np.clip(cols.astype(int), 0, out_w - 1)
            pts.loc[m, "cls"] = arr[rows, cols]
            log(f"  {t.name}: {m.sum():,} samples")
    out = pd.DataFrame({"hex_id": hx.hex_id})
    grp = pts.groupby("hex_id").cls
    for name, codes in WC_CLASSES.items():
        out[f"lc_{name}"] = out.hex_id.map(grp.apply(lambda s, c=codes: s.isin(c).mean()))
    _merge(out, "landcover")


# ------------------------------------------------------------------ OSM ---

def _overpass(q: str) -> list[dict]:
    for url in OVERPASS:
        try:
            r = requests.post(url, data={"data": q}, timeout=1000, headers=UA)
            r.raise_for_status()
            return r.json()["elements"]
        except Exception as e:
            log(f"  overpass {url} failed: {e}")
            time.sleep(5)
    raise RuntimeError("all Overpass endpoints failed")


def _kv(tag: str | None) -> float:
    if not tag:
        return np.nan
    vals = []
    for p in str(tag).replace(",", ";").split(";"):
        try:
            v = float(p.strip())
            vals.append(v / 1000 if v > 1000 else v)
        except ValueError:
            pass
    return max(vals) if vals else np.nan


def _center(e):
    if "lat" in e:
        return e["lat"], e["lon"]
    c = e.get("center") or {}
    return c.get("lat"), c.get("lon")


def step_osm():
    log("OpenStreetMap via Overpass (bbox around NC)")
    s, w, n, e = NC_BBOX
    bb = f"({s},{w},{n},{e})"
    pts_q = {
        "substations": f'[out:json][timeout:900];nwr["power"="substation"]{bb};out center tags;',
        "schools": f'[out:json][timeout:900];nwr["amenity"~"^(school|kindergarten)$"]{bb};out center tags;',
        "hospitals": f'[out:json][timeout:900];nwr["amenity"="hospital"]{bb};out center tags;',
        "datacenters": f'[out:json][timeout:900];(nwr["telecom"="data_center"]{bb};nwr["building"="data_center"]{bb};);out center tags;',
    }
    for name, q in pts_q.items():
        els = _overpass(q)
        rows = []
        for el in els:
            lat, lon = _center(el)
            if lat is None:
                continue
            t = el.get("tags", {})
            rows.append({"lat": lat, "lon": lon, "name": t.get("name") or t.get("operator") or "", "kv": _kv(t.get("voltage")),
                         "osm_id": f"{el['type'][0]}{el['id']}"})
        df = pd.DataFrame(rows, columns=["lat", "lon", "name", "kv", "osm_id"])
        df.to_parquet(POINTS / f"{name}.parquet")
        log(f"  {name}: {len(df):,}")
        time.sleep(2)

    els = _overpass(f'[out:json][timeout:900];way["power"="line"]{bb};out geom tags;')
    rows = []
    for el in els:
        kv = _kv(el.get("tags", {}).get("voltage"))
        g = el.get("geometry") or []
        for a, b in zip(g, g[1:]):
            d = _hav(a["lat"], a["lon"], b["lat"], b["lon"])
            k = max(1, int(math.ceil(d / 0.25)))          # a vertex every ~250 m
            for i in range(k):
                f = i / k
                rows.append((a["lat"] + f * (b["lat"] - a["lat"]), a["lon"] + f * (b["lon"] - a["lon"]), kv))
        if g:
            rows.append((g[-1]["lat"], g[-1]["lon"], kv))
    lines = pd.DataFrame(rows, columns=["lat", "lon", "kv"])
    lines.to_parquet(POINTS / "lines.parquet")
    log(f"  line vertices: {len(lines):,} from {len(els):,} lines")

    hx = _hexes()
    subs = pd.read_parquet(POINTS / "substations.parquet")
    big_subs = subs[subs.kv >= MIN_GRID_KV]
    if len(big_subs) < 50:  # voltage often untagged: fall back to all substations
        big_subs = subs
    big_lines = lines[lines.kv >= MIN_GRID_KV]
    feat = pd.DataFrame({"hex_id": hx.hex_id})
    feat["dist_sub_km"], idx = _nearest(hx.lat.values, hx.lon.values, big_subs)
    feat["sub_kv"] = big_subs.kv.values[idx]
    feat["dist_line_km"], idx = _nearest(hx.lat.values, hx.lon.values, big_lines)
    feat["line_kv"] = big_lines.kv.values[idx]
    sch = pd.read_parquet(POINTS / "schools.parquet")
    feat["schools_2km"] = _count_within(hx.lat.values, hx.lon.values, sch, 2.0)
    _merge(feat, "osm")


def _xyz(lat, lon):
    la, lo = np.radians(lat), np.radians(lon)
    return np.c_[np.cos(la) * np.cos(lo), np.cos(la) * np.sin(lo), np.sin(la)]


def _nearest(lat, lon, pts: pd.DataFrame):
    from scipy.spatial import cKDTree

    tree = cKDTree(_xyz(pts.lat.values, pts.lon.values))
    chord, idx = tree.query(_xyz(lat, lon))
    return 2 * EARTH_R * np.arcsin(np.clip(chord / 2, 0, 1)), idx


def _count_within(lat, lon, pts: pd.DataFrame, km: float):
    from scipy.spatial import cKDTree

    tree = cKDTree(_xyz(pts.lat.values, pts.lon.values))
    chord = 2 * math.sin(km / (2 * EARTH_R))
    return np.array([len(x) for x in tree.query_ball_point(_xyz(lat, lon), chord)])


def _hav(lat1, lon1, lat2, lon2):
    p1, p2 = math.radians(lat1), math.radians(lat2)
    a = math.sin((p2 - p1) / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(math.radians(lon2 - lon1) / 2) ** 2
    return 2 * EARTH_R * math.asin(math.sqrt(a))


# ---------------------------------------------------------------- score ---

def _pct(s: pd.Series, higher_better=True) -> pd.Series:
    r = s.rank(pct=True)
    return (r if higher_better else 1 - r).fillna(0.5)


def step_score():
    log("assemble grid + scores")
    g = _hexes().drop(columns=["geometry"])
    for f in sorted(INTERIM.glob("feat_*.parquet")):
        g = g.merge(pd.read_parquet(f), on="hex_id", how="left")
        log(f"  + {f.stem[5:]}")
    for col in ["pop", "households"]:
        if col not in g:
            g[col] = np.nan
    pop = dict(zip(g.hex_id, g["pop"].fillna(0)))
    area = dict(zip(g.hex_id, g.area_km2))

    def disk_pop(h, k, r_km):
        cells = [c for c in h3.grid_disk(h, k) if c in pop]
        a = sum(area[c] for c in cells)
        return sum(pop[c] for c in cells) * (math.pi * r_km ** 2) / a if a else np.nan

    g["pop_dens_km2"] = g["pop"] / g.area_km2
    g["hh_dens_km2"] = g["households"] / g.area_km2
    g["pop_3km"] = [disk_pop(h, 1, 3) for h in g.hex_id]
    g["pop_5km"] = [disk_pop(h, 2, 5) for h in g.hex_id]

    def col(name):
        return g[name] if name in g else pd.Series(np.nan, index=g.index)

    land_easy = col("lc_developed").fillna(0) + col("lc_cropland").fillna(0) + col("lc_pasture").fillna(0)
    sensitive = col("lc_forest").fillna(0) + 2 * col("lc_wetland").fillna(0)
    suit_parts = [_pct(col("dist_sub_km"), False), _pct(col("dist_line_km"), False), _pct(col("bws_score"), False),
                  _pct(col("nri_overall"), False), _pct(land_easy), _pct(col("lc_water").fillna(0), False)]
    burden_parts = [_pct(col("pop_3km")), _pct(col("svi_pct")), _pct(col("bws_score")), _pct(sensitive), _pct(col("schools_2km"))]
    g["suitability"] = (100 * sum(suit_parts) / len(suit_parts)).round(1)
    g["burden"] = (100 * sum(burden_parts) / len(burden_parts)).round(1)
    hi_s, hi_b = g.suitability >= 50, g.burden >= 50
    g["quadrant"] = np.select([hi_s & ~hi_b, hi_s & hi_b, ~hi_s & ~hi_b], ["good", "tradeoff", "poor"], "avoid")
    g["hard_flag"] = (col("lc_water").fillna(0) > 0.5) | (col("lc_wetland").fillna(0) > 0.5)
    g.to_parquet(GRID / "nc_h3r7.parquet", index=False)
    meta = {"built_at": datetime.now(timezone.utc).isoformat(timespec="seconds"), "rows": len(g), "res": RES,
            "columns": list(g.columns), "features": [f.stem[5:] for f in sorted(INTERIM.glob("feat_*.parquet"))]}
    (GRID / "meta.json").write_text(json.dumps(meta, indent=2))
    log(f"  wrote {GRID / 'nc_h3r7.parquet'}: {len(g):,} rows x {len(g.columns)} cols")
    print(g[["suitability", "burden", "pop", "dist_sub_km", "bws_score", "nri_overall", "svi_pct"]
            if "nri_overall" in g and "bws_score" in g else ["suitability", "burden"]].describe().round(2).to_string())


# --------------------------------------------------------------- layers ---

def step_layers():
    log("export map layers")
    g = pd.read_parquet(GRID / "nc_h3r7.parquet")
    for name, col in {"suitability": "suitability", "burden": "burden", "water_stress": "bws_score"}.items():
        if col not in g:
            continue
        feats = []
        for h, v in zip(g.hex_id, g[col]):
            ring = [[round(lng, 4), round(lat, 4)] for lat, lng in h3.cell_to_boundary(h)]
            ring.append(ring[0])
            feats.append({"type": "Feature", "geometry": {"type": "Polygon", "coordinates": [ring]},
                          "properties": {"hex_id": h, "value": None if pd.isna(v) else round(float(v), 2)}})
        p = LAYERS / f"{name}.geojson"
        p.write_text(json.dumps({"type": "FeatureCollection", "features": feats}, separators=(",", ":")))
        log(f"  {p.name}: {p.stat().st_size / 1e6:.1f} MB")


STEPS = {"boundaries": step_boundaries, "census": step_census, "nri": step_nri, "aqueduct": step_aqueduct,
         "landcover": step_landcover, "osm": step_osm, "score": step_score, "layers": step_layers}


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--only", help="comma-separated steps: " + ",".join(STEPS))
    a = ap.parse_args()
    todo = a.only.split(",") if a.only else list(STEPS)
    failed = []
    for name in todo:
        if name != "boundaries" and not (INTERIM / "hexes.parquet").exists():
            step_boundaries()
        t0 = time.time()
        try:
            STEPS[name]()
            log(f"OK {name} ({time.time() - t0:.0f}s)")
        except Exception as e:
            failed.append(name)
            log(f"FAILED {name}: {type(e).__name__}: {e}")
            if name in ("boundaries", "score"):
                raise
    log("DONE" + (f" — failed steps: {failed} (grid still built; those columns are empty)" if failed else ""))


if __name__ == "__main__":
    main()
