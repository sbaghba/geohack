"""
M3b: Siting Pressure model — where are developers likely to build the next data center?

Trained on the A100 server; nothing here runs at demo time.

    pip install -r requirements-etl.txt xgboost
    python -u scripts/pressure_model.py all 2>&1 | tee pressure.log      # data -> train -> predict
    python -u scripts/pressure_model.py data | train | predict             # one step (data is cached)
    python -u scripts/build_grid.py --only score,layers                  # merge into the NC grid + 'pressure' layer

Design (say this to judges):
  * Labels: US hexes (H3 res 6, ~36 km2) containing an existing data center mapped in OpenStreetMap.
    Everything else is "unlabeled", not "negative" (positive-unlabeled setting) -> output is a relative score.
  * Features: grid access (distance to 230 kV+ substations and lines), people (population within 10/50 km),
    hazards + social vulnerability (FEMA NRI), water stress (WRI Aqueduct), state power price + grid carbon.
    Deliberately NOT used: distance to existing data centers (would leak the clustering) and lat/lon.
  * Validation: 5-fold GroupKFold by STATE — whole states are held out, so the model must generalize geographically.
    Reported: ROC-AUC, average precision, and "capture@10%": share of held-out data centers in the top-10% hexes.
  * Model: XGBoost on GPU (device=cuda), class-weighted. Explanations: exact TreeSHAP (pred_contribs) -> top-3 drivers.
Outputs:
    data/models/pressure_xgb.json, data/models/pressure_metrics.json
    data/interim/feat_pressure.parquet   (NC res-7 hexes: pressure 0-100, pressure_drivers JSON)
"""
from __future__ import annotations

import io
import json
import math
import os
import sys
import time
import zipfile
from datetime import datetime
from pathlib import Path

import h3
import numpy as np
import pandas as pd
import requests

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from app.reference import EGRID_STATE_LB, EIA_PRICE_CENTS, US_STATE_ABBR  # noqa: E402

DATA = Path(os.getenv("DATA_DIR", Path(__file__).resolve().parents[1] / "data"))
RAW, INTERIM, MODELS, GRID = DATA / "raw", DATA / "interim", DATA / "models", DATA / "grid"
US_DIR = INTERIM / "us"
for d in (RAW, INTERIM, MODELS, US_DIR):
    d.mkdir(parents=True, exist_ok=True)

RES_US = 6
CONUS = (24.4, -125.0, 49.5, -66.8)  # S W N E
GAZ = "https://www2.census.gov/geo/docs/maps-data/data/gazetteer/2023_Gazetteer/2023_Gaz_tracts_national.zip"
NRI = "https://services.arcgis.com/XG15cJAlne2vxtgt/arcgis/rest/services/National_Risk_Index_Census_Tracts/FeatureServer/0/query"
OVERPASS = ["https://overpass-api.de/api/interpreter", "https://overpass.kumi.systems/api/interpreter",
            "https://overpass.private.coffee/api/interpreter", "https://maps.mail.ru/osm/tools/overpass/api/interpreter"]
UA = {"User-Agent": "SiteSense-WolfHacks/1.0 (NC State hackathon)"}
EARTH_R = 6371.0088
HV = "(115|138|161|230|345|500|765)000"
EHV = "(230|345|500|765)000"

FEATURES = ["dist_sub_km", "dist_line_km", "log_pop_10km", "log_pop_50km", "nri_overall", "nri_hurricane",
            "nri_tornado", "nri_heat_wave", "nri_inland_flood", "svi", "bws_score", "price_cents", "egrid_lb"]


def log(msg):
    print(f"[{datetime.now():%H:%M:%S}] {msg}", flush=True)


def _xyz(lat, lon):
    la, lo = np.radians(lat), np.radians(lon)
    return np.c_[np.cos(la) * np.cos(lo), np.cos(la) * np.sin(lo), np.sin(la)]


def _km_to_chord(km):
    return 2 * math.sin(km / (2 * EARTH_R))


def _chord_to_km(c):
    return 2 * EARTH_R * np.arcsin(np.clip(c / 2, 0, 1))


# -------------------------------------------------------------------- data ---

def _overpass(q: str) -> list[dict]:
    for attempt in range(3):
        for url in OVERPASS:
            try:
                r = requests.post(url, data={"data": q}, timeout=1000, headers=UA)
                r.raise_for_status()
                return r.json()["elements"]
            except Exception as e:
                log(f"    {url.split('/')[2]}: {str(e)[:100]}")
                time.sleep(3)
        log(f"    all mirrors busy; waiting {30 * (attempt + 1)}s")
        time.sleep(30 * (attempt + 1))
    raise RuntimeError("Overpass failed")


def _tiles(nx=6, ny=4):
    s, w, n, e = CONUS
    for i in range(nx):
        for j in range(ny):
            yield (s + (n - s) * j / ny, w + (e - w) * i / nx, s + (n - s) * (j + 1) / ny, w + (e - w) * (i + 1) / nx)


def _osm_points(kind: str, filt: str) -> pd.DataFrame:
    out = US_DIR / f"{kind}.parquet"
    if out.exists():
        return pd.read_parquet(out)
    rows = []
    for k, (s, w, n, e) in enumerate(_tiles()):
        cache = US_DIR / f"{kind}_{k}.json"
        if cache.exists():
            els = json.loads(cache.read_text())
        else:
            q = f"[out:json][timeout:900];({filt.replace('BB', f'({s},{w},{n},{e})')});out center tags;"
            els = _overpass(q)
            cache.write_text(json.dumps(els))
            time.sleep(2)
        for el in els:
            lat = el.get("lat", (el.get("center") or {}).get("lat"))
            lon = el.get("lon", (el.get("center") or {}).get("lon"))
            if lat is not None:
                rows.append((lat, lon, (el.get("tags") or {}).get("name", ""), f"{el['type'][0]}{el['id']}"))
        log(f"  {kind} tile {k + 1}/24: total {len(rows):,}")
    df = pd.DataFrame(rows, columns=["lat", "lon", "name", "osm_id"]).drop_duplicates("osm_id")
    df.to_parquet(out)
    return df


def _osm_lines() -> pd.DataFrame:
    out = US_DIR / "lines_ehv.parquet"
    if out.exists():
        return pd.read_parquet(out)
    rows = []
    for k, (s, w, n, e) in enumerate(_tiles()):
        cache = US_DIR / f"lines_{k}.json"
        if cache.exists():
            els = json.loads(cache.read_text())
        else:
            els = _overpass(f'[out:json][timeout:900];way["power"="line"]["voltage"~"{EHV}"]({s},{w},{n},{e});out geom;')
            cache.write_text(json.dumps(els))
            time.sleep(2)
        for el in els:
            g = el.get("geometry") or []
            for a, b in zip(g, g[1:]):
                d = _hav(a["lat"], a["lon"], b["lat"], b["lon"])
                k2 = max(1, int(math.ceil(d / 1.0)))       # vertex every ~1 km
                for i in range(k2):
                    f = i / k2
                    rows.append((a["lat"] + f * (b["lat"] - a["lat"]), a["lon"] + f * (b["lon"] - a["lon"])))
        log(f"  lines tile {k + 1}/24: {len(rows):,} vertices")
    df = pd.DataFrame(rows, columns=["lat", "lon"]).round(4).drop_duplicates()
    df.to_parquet(out)
    return df


def _hav(lat1, lon1, lat2, lon2):
    p1, p2 = math.radians(lat1), math.radians(lat2)
    a = math.sin((p2 - p1) / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(math.radians(lon2 - lon1) / 2) ** 2
    return 2 * EARTH_R * math.asin(math.sqrt(a))


def _tracts() -> pd.DataFrame:
    out = US_DIR / "tracts.parquet"
    if out.exists():
        return pd.read_parquet(out)
    log("  Census gazetteer (tract centroids)")
    z = RAW / "2023_Gaz_tracts_national.zip"
    if not z.exists():
        r = requests.get(GAZ, timeout=300, headers=UA)
        r.raise_for_status()
        z.write_bytes(r.content)
    with zipfile.ZipFile(z) as zf:
        name = [n for n in zf.namelist() if n.endswith(".txt")][0]
        gaz = pd.read_csv(zf.open(name), sep="\t", dtype={"GEOID": str})
    gaz.columns = [c.strip() for c in gaz.columns]
    gaz = gaz[["GEOID", "INTPTLAT", "INTPTLONG"]].rename(columns={"INTPTLAT": "lat", "INTPTLONG": "lon"})

    log("  FEMA NRI (all US tracts)")
    fields = ["TRACTFIPS", "POPULATION", "RISK_SCORE", "HRCN_RISKS", "TRND_RISKS", "HWAV_RISKS", "IFLD_RISKS", "SOVI_SCORE"]
    rows, offset = [], 0
    while True:
        r = requests.get(NRI, params={"where": "1=1", "outFields": ",".join(fields), "returnGeometry": "false", "f": "json",
                                      "resultOffset": offset, "resultRecordCount": 2000, "orderByFields": "TRACTFIPS"},
                         timeout=180, headers=UA)
        r.raise_for_status()
        js = r.json()
        feats = [f["attributes"] for f in js.get("features", [])]
        rows += feats
        if not feats or not js.get("exceededTransferLimit"):
            break
        offset += len(feats)
        if offset % 20000 == 0:
            log(f"    {offset:,} tracts")
    nri = pd.DataFrame(rows).rename(columns={"TRACTFIPS": "GEOID", "POPULATION": "pop", "RISK_SCORE": "nri_overall",
                                             "HRCN_RISKS": "nri_hurricane", "TRND_RISKS": "nri_tornado",
                                             "HWAV_RISKS": "nri_heat_wave", "IFLD_RISKS": "nri_inland_flood", "SOVI_SCORE": "svi"})
    t = gaz.merge(nri, on="GEOID", how="left")
    t["state"] = t.GEOID.str[:2].map(US_STATE_ABBR)
    t = t[t.state.notna() & ~t.state.isin(["AK", "HI"])]
    t.to_parquet(out)
    log(f"  {len(t):,} CONUS tracts, population {t['pop'].sum():,.0f}")
    return t


def step_data():
    import geopandas as gpd
    from scipy.spatial import cKDTree

    log("US feature table (H3 res 6)")
    states = gpd.read_file(f"zip://{RAW / 'cb_2023_us_state_20m.zip'}").to_crs(4326)
    conus = states[~states.STUSPS.isin(["AK", "HI", "PR"])].geometry.union_all()
    cells = sorted(h3.geo_to_cells(conus.__geo_interface__, RES_US))
    ll = np.array([h3.cell_to_latlng(c) for c in cells])
    hx = pd.DataFrame({"hex_id": cells, "lat": ll[:, 0], "lon": ll[:, 1]})
    log(f"  {len(hx):,} hexes")
    X = _xyz(hx.lat.values, hx.lon.values)

    t = _tracts()
    tt = cKDTree(_xyz(t.lat.values, t.lon.values))
    _, idx = tt.query(X)
    near = t.iloc[idx].reset_index(drop=True)
    for c in ["nri_overall", "nri_hurricane", "nri_tornado", "nri_heat_wave", "nri_inland_flood", "svi"]:
        hx[c] = pd.to_numeric(near[c], errors="coerce").values
    hx["state"] = near.state.values
    pops = pd.to_numeric(t["pop"], errors="coerce").fillna(0).values
    for km in (10, 50):
        nb = tt.query_ball_point(X, _km_to_chord(km))
        hx[f"log_pop_{km}km"] = np.log1p([pops[i].sum() for i in nb])
    hx["price_cents"] = hx.state.map(EIA_PRICE_CENTS)
    hx["egrid_lb"] = hx.state.map(EGRID_STATE_LB)

    log("  OSM substations 115 kV+, lines 230 kV+, data centers (24 tiles each; cached)")
    subs = _osm_points("subs_hv", f'nwr["power"="substation"]["voltage"~"{HV}"]BB;')
    lines = _osm_lines()
    dcs = _osm_points("datacenters", 'nwr["telecom"="data_center"]BB;nwr["building"="data_center"]BB;')
    hx["dist_sub_km"] = _chord_to_km(cKDTree(_xyz(subs.lat.values, subs.lon.values)).query(X)[0])
    hx["dist_line_km"] = _chord_to_km(cKDTree(_xyz(lines.lat.values, lines.lon.values)).query(X)[0])
    dc_cells = pd.Series([h3.latlng_to_cell(a, b, RES_US) for a, b in zip(dcs.lat, dcs.lon)]).value_counts()
    hx["n_dc"] = hx.hex_id.map(dc_cells).fillna(0).astype(int)
    hx["label"] = (hx.n_dc > 0).astype(int)

    log("  WRI Aqueduct water stress")
    import pyogrio

    z = RAW / "aqueduct-4-0-water-risk-data.zip"
    names = zipfile.ZipFile(z).namelist()
    cands = [f"/vsizip/{z}/{g}" for g in sorted({n.split(".gdb/")[0] + ".gdb" for n in names if ".gdb/" in n})]
    cands += [f"/vsizip/{z}/{n}" for n in names if n.endswith((".gpkg", ".shp"))]
    src = layer = None
    for c in cands:
        try:
            hits = [n for n, _ in pyogrio.list_layers(c) if "baseline" in n.lower() and "annual" in n.lower()]
        except Exception:
            continue
        if hits:
            src, layer = c, hits[0]
            break
    if not src:
        raise RuntimeError("Aqueduct baseline annual layer not found (run build_grid.py --only aqueduct first)")
    s, w, n, e = CONUS
    aq = gpd.read_file(src, layer=layer, bbox=(w, s, e, n))
    cols = {c.lower(): c for c in aq.columns}
    aq = aq.rename(columns={cols["bws_score"]: "bws_score"})[["bws_score", "geometry"]]
    aq.loc[aq.bws_score < 0, "bws_score"] = np.nan
    pts = gpd.GeoDataFrame(hx[["hex_id"]], geometry=gpd.points_from_xy(hx.lon, hx.lat), crs=4326)
    j = gpd.sjoin(pts, aq.to_crs(4326), how="left", predicate="within").drop_duplicates("hex_id").set_index("hex_id")
    hx["bws_score"] = hx.hex_id.map(j.bws_score)

    hx.to_parquet(US_DIR / "us_features.parquet")
    log(f"  wrote us_features.parquet: {len(hx):,} hexes, {hx.label.sum():,} with data centers "
        f"({dcs.shape[0]:,} OSM features), {hx.state.nunique()} states")


# ------------------------------------------------------------------- train ---

def _xgb_params(device):
    return dict(objective="binary:logistic", eval_metric="aucpr", tree_method="hist", device=device, max_depth=6,
                learning_rate=0.05, subsample=0.8, colsample_bytree=0.8, min_child_weight=5, reg_lambda=1.0)


def _device():
    import warnings

    import xgboost as xgb

    try:
        with warnings.catch_warnings(record=True) as w:
            warnings.simplefilter("always")
            xgb.train({"device": "cuda", "tree_method": "hist"}, xgb.DMatrix(np.zeros((4, 1)), label=[0, 1, 0, 1]), 1)
        return "cpu" if any("GPU to CPU" in str(x.message) or "No visible GPU" in str(x.message) for x in w) else "cuda"
    except Exception:
        return "cpu"


def step_train():
    import xgboost as xgb
    from sklearn.metrics import average_precision_score, roc_auc_score
    from sklearn.model_selection import GroupKFold

    df = pd.read_parquet(US_DIR / "us_features.parquet")
    X, y, groups = df[FEATURES].astype("float32"), df.label.values, df.state.values
    pos = y.sum()
    if pos < 20:
        raise RuntimeError(f"only {pos} positive hexes; check the OSM data-center download")
    device = _device()
    params = {**_xgb_params(device), "scale_pos_weight": float((len(y) - pos) / pos)}
    log(f"training on {device}: {len(y):,} hexes, {pos:,} positive ({100 * pos / len(y):.2f}%), {len(FEATURES)} features")

    oof = np.zeros(len(y))
    folds = []
    n_splits = min(5, len(set(groups)))
    for k, (tr, te) in enumerate(GroupKFold(n_splits=n_splits).split(X, y, groups)):
        dtr, dte = xgb.DMatrix(X.iloc[tr], label=y[tr]), xgb.DMatrix(X.iloc[te], label=y[te])
        bst = xgb.train(params, dtr, num_boost_round=600, evals=[(dte, "heldout")], early_stopping_rounds=50, verbose_eval=False)
        oof[te] = bst.predict(dte, iteration_range=(0, bst.best_iteration + 1))
        held = sorted(set(groups[te]))
        auc = roc_auc_score(y[te], oof[te]) if 0 < y[te].sum() < len(te) else float("nan")
        folds.append({"fold": k, "states": held, "auc": round(auc, 3), "positives": int(y[te].sum()), "trees": bst.best_iteration + 1})
        log(f"  fold {k}: AUC {auc:.3f}, {y[te].sum()} DC hexes, held out {len(held)} states ({', '.join(held[:8])}...)")

    thr = np.quantile(oof, 0.9)
    metrics = {
        "trained_at": datetime.now().isoformat(timespec="seconds"), "device": device, "hexes": int(len(y)), "positives": int(pos),
        "features": FEATURES, "cv": f"GroupKFold({n_splits}) by state",
        "auc": round(float(roc_auc_score(y, oof)), 3), "average_precision": round(float(average_precision_score(y, oof)), 3),
        "base_rate": round(float(pos / len(y)), 4),
        "capture_at_10pct": round(float(y[oof >= thr].sum() / pos), 3),
        "folds": folds,
    }
    by_state = pd.DataFrame({"state": groups, "y": y, "hit": (oof >= thr) & (y == 1)}).groupby("state").agg(dc=("y", "sum"), hit=("hit", "sum"))
    top = by_state[by_state.dc >= 10].assign(capture=lambda d: (d.hit / d.dc).round(2)).sort_values("dc", ascending=False).head(8)
    metrics["capture_by_state"] = top.reset_index().to_dict(orient="records")
    log(f"  OVERALL (states held out): AUC {metrics['auc']}, AP {metrics['average_precision']} (base rate {metrics['base_rate']}), "
        f"top-10% hexes capture {100 * metrics['capture_at_10pct']:.0f}% of data-center hexes")
    for r in metrics["capture_by_state"]:
        log(f"    {r['state']}: {r['dc']} DC hexes, {100 * r['capture']:.0f}% in top 10% when {r['state']} was held out")

    n_rounds = int(np.median([f["trees"] for f in folds]))
    final = xgb.train(params, xgb.DMatrix(X, label=y), num_boost_round=max(50, n_rounds))
    final.save_model(str(MODELS / "pressure_xgb.json"))
    df["score"] = final.predict(xgb.DMatrix(X))
    df[["hex_id", "score"]].to_parquet(US_DIR / "us_scores.parquet")
    gain = final.get_score(importance_type="gain")
    metrics["importance_gain"] = {f: round(gain.get(f, 0.0), 2) for f in sorted(FEATURES, key=lambda f: -gain.get(f, 0.0))}
    (MODELS / "pressure_metrics.json").write_text(json.dumps(metrics, indent=2))
    log(f"  saved model ({max(50, n_rounds)} trees); top features: {list(metrics['importance_gain'])[:5]}")


# ----------------------------------------------------------------- predict ---

def _label(f: str, v: float) -> str:
    if v is None or (isinstance(v, float) and math.isnan(v)):
        return f.replace("_", " ")
    return {
        "dist_sub_km": f"{v:.0f} km to a 115 kV+ substation",
        "dist_line_km": f"{v:.0f} km to a 230 kV+ power line",
        "log_pop_10km": f"{math.expm1(v):,.0f} people within 10 km",
        "log_pop_50km": f"{math.expm1(v) / 1e6:.1f}M people within 50 km",
        "nri_overall": f"overall hazard risk {v:.0f}/100",
        "nri_hurricane": f"hurricane risk {v:.0f}/100",
        "nri_tornado": f"tornado risk {v:.0f}/100",
        "nri_heat_wave": f"heat-wave risk {v:.0f}/100",
        "nri_inland_flood": f"inland flood risk {v:.0f}/100",
        "svi": f"social vulnerability {v:.0f}/100",
        "bws_score": f"water stress {v:.1f}/5",
        "price_cents": f"state power price {v:.1f} cents/kWh",
        "egrid_lb": f"state grid carbon {v:.0f} lb/MWh",
    }.get(f, f)


def step_predict():
    import xgboost as xgb

    df = pd.read_parquet(US_DIR / "us_features.parquet")
    scores = pd.read_parquet(US_DIR / "us_scores.parquet")
    bst = xgb.Booster()
    bst.load_model(str(MODELS / "pressure_xgb.json"))
    nc_grid = pd.read_parquet(GRID / "nc_h3r7.parquet", columns=["hex_id"])
    nc_grid["parent"] = [h3.cell_to_parent(h, RES_US) for h in nc_grid.hex_id]
    sub = df[df.hex_id.isin(set(nc_grid.parent))].reset_index(drop=True)
    contrib = bst.predict(xgb.DMatrix(sub[FEATURES].astype("float32")), pred_contribs=True)[:, :-1]   # exact TreeSHAP
    us_rank = scores.score.rank(pct=True)
    pct = dict(zip(scores.hex_id, (100 * us_rank).round(1)))
    drivers = {}
    for i, row in sub.iterrows():
        order = np.argsort(-np.abs(contrib[i]))[:3]
        drivers[row.hex_id] = json.dumps([{"key": FEATURES[j], "label": _label(FEATURES[j], float(row[FEATURES[j]])),
                                           "direction": "+" if contrib[i][j] > 0 else "-"} for j in order])
    out = pd.DataFrame({"hex_id": nc_grid.hex_id,
                        "pressure": nc_grid.parent.map(pct),
                        "pressure_drivers": nc_grid.parent.map(drivers)})
    out.to_parquet(INTERIM / "feat_pressure.parquet")
    log(f"NC: {out.pressure.notna().sum():,} hexes scored; pressure = percentile among all CONUS hexes "
        f"(NC median {out.pressure.median():.0f}, max {out.pressure.max():.0f})")
    log("next: python scripts/build_grid.py --only score,layers")


def main():
    cmd = sys.argv[1] if len(sys.argv) > 1 else "all"
    steps = {"data": step_data, "train": step_train, "predict": step_predict}
    for name in (list(steps) if cmd == "all" else [cmd]):
        t0 = time.time()
        steps[name]()
        log(f"OK {name} ({time.time() - t0:.0f}s)")


if __name__ == "__main__":
    main()
