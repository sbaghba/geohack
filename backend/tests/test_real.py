"""Real-data path on a tiny synthetic grid (no network, no GIS stack): analyze tier 1 + tier 2, suggest, layers."""
from __future__ import annotations

import dataclasses
import json
import sys
from pathlib import Path

import h3
import numpy as np
import pandas as pd
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from fastapi.testclient import TestClient  # noqa: E402

import app.real as real  # noqa: E402
import app.services.analyze as analyze_svc  # noqa: E402
import app.services.layers as layers_svc  # noqa: E402
import app.store as store_mod  # noqa: E402
from app.config import settings  # noqa: E402
from app.contract import Report, SuggestResponse  # noqa: E402
from app.main import app  # noqa: E402

CENTER = (35.70, -78.55)


@pytest.fixture()
def real_data(tmp_path, monkeypatch):
    from shapely import box, to_wkb

    (tmp_path / "grid").mkdir()
    (tmp_path / "points").mkdir()
    (tmp_path / "layers").mkdir()
    cells = list(h3.grid_disk(h3.latlng_to_cell(*CENTER, 7), 12))
    rng = np.random.default_rng(1)
    ll = np.array([h3.cell_to_latlng(c) for c in cells])
    n = len(cells)
    g = pd.DataFrame({
        "hex_id": cells, "lat": ll[:, 0], "lon": ll[:, 1], "area_km2": 5.16,
        "county": np.where(ll[:, 1] < -78.55, "Wake", "Johnston"), "county_fips": "37183",
        "pop": rng.uniform(50, 3000, n), "households": rng.uniform(20, 1200, n), "median_income_usd": 70000.0,
        "bws_score": rng.uniform(0.5, 3.5, n), "bws_label": "Medium-high (20-40%)",
        "lc_forest": 0.4, "lc_pasture": 0.15, "lc_cropland": 0.2, "lc_developed": 0.15, "lc_wetland": 0.05, "lc_water": 0.02, "lc_other": 0.03,
        "nri_overall": 60.0, "nri_hurricane": 70.0, "nri_heat_wave": 50.0, "nri_riverine_flooding": 40.0, "nri_coastal_flooding": np.nan,
        "nri_tornado": 65.0, "svi_pct": rng.uniform(10, 90, n), "nri_rating": "Relatively Moderate",
        "dist_sub_km": rng.uniform(0.5, 20, n), "suitability": rng.uniform(20, 80, n), "burden": rng.uniform(20, 80, n),
        "hard_flag": False,
    })
    g["pop_dens_km2"] = g["pop"] / 5.16
    g["hh_dens_km2"] = g["households"] / 5.16
    g["pop_3km"] = g["pop"] * 5
    g["pop_5km"] = g["pop"] * 15
    g["quadrant"] = np.where(g.suitability >= 50, np.where(g.burden < 50, "good", "tradeoff"), np.where(g.burden < 50, "poor", "avoid"))
    g.to_parquet(tmp_path / "grid" / "nc_h3r7.parquet", index=False)
    pd.DataFrame({"county": ["Wake", "Johnston"], "county_fips": ["37183", "37101"], "households": [450000.0, 88000.0],
                  "labor_force": [650000.0, 120000.0], "unemployed": [20000.0, 4000.0], "unemployment_pct": [3.1, 3.3]}
                 ).to_parquet(tmp_path / "grid" / "counties_nc.parquet")
    pd.DataFrame({"stusps": ["NC", "VA"], "wkb": [to_wkb(box(-84.4, 33.8, -75.4, 36.55)), to_wkb(box(-83.7, 36.55, -75.2, 39.5))]}
                 ).to_parquet(tmp_path / "grid" / "us_states.parquet")
    pd.DataFrame({"lat": [35.71, 35.5], "lon": [-78.56, -78.3], "name": ["Sub A", "Sub B"], "kv": [230.0, 115.0]}).to_parquet(tmp_path / "points" / "substations.parquet")
    pd.DataFrame({"lat": np.linspace(35.4, 36.0, 50), "lon": -78.5, "kv": 230.0}).to_parquet(tmp_path / "points" / "lines.parquet")
    pd.DataFrame({"lat": [CENTER[0] + 0.003], "lon": [CENTER[1]], "name": ["Test Elementary"], "kv": [np.nan]}).to_parquet(tmp_path / "points" / "schools.parquet")
    pd.DataFrame({"lat": [35.9], "lon": [-78.9], "name": ["Far Hospital"], "kv": [np.nan]}).to_parquet(tmp_path / "points" / "hospitals.parquet")
    (tmp_path / "layers" / "suitability.geojson").write_text(json.dumps({"type": "FeatureCollection", "features": []}))

    s2 = dataclasses.replace(settings, data_dir=tmp_path, use_mock=False)
    fresh = store_mod.Store()
    store_mod._points.cache_clear()
    layers_svc._load.cache_clear()
    for mod in (store_mod, analyze_svc, layers_svc):
        monkeypatch.setattr(mod, "settings", s2)
    for mod in (store_mod, real):
        monkeypatch.setattr(mod, "store", fresh)
    monkeypatch.setattr(real, "fema_flood_zone", lambda lat, lon: ("AE", True) if lat > 35.75 else ("X", False))
    yield tmp_path
    store_mod._points.cache_clear()


def test_tier1_report(real_data):
    c = TestClient(app)
    r = Report.model_validate(c.post("/api/analyze", json={"lat": CENTER[0], "lon": CENTER[1], "mw": 100}).json())
    assert r.mock is False and r.site.tier == 1 and r.site.state == "NC"
    assert r.energy.grid_region == "SRVC" and r.carbon.grid_lb_per_mwh == 593.4
    assert r.energy.nearest_substation_kv == 230 and r.energy.nearest_substation_km < 2
    assert r.economy.property_tax_usd_yr is not None and r.energy.county_households in (450000, 88000)
    assert [p.name for p in r.community.schools_1km] == ["Test Elementary"]
    assert "school_within_500m" in r.land.flags and r.hazards.fema_zone == "X"
    assert "hazards.nri.coastal_flooding" in r.missing and "energy.county_share_pct" in r.missing
    assert r.field_sources["water.aqueduct_stress"] == "aqueduct"
    keys = {s.key for s in r.sources}
    assert {"egrid", "aqueduct", "nri", "acs", "osm"} <= keys


def test_floodplain_flag(real_data):
    c = TestClient(app)
    r = Report.model_validate(c.post("/api/analyze", json={"lat": 35.80, "lon": -78.55}).json())
    assert r.hazards.in_floodplain is True and "in_floodplain" in r.land.flags


def test_tier2_outside_nc(real_data):
    c = TestClient(app)
    r = Report.model_validate(c.post("/api/analyze", json={"lat": 37.54, "lon": -77.43}).json())  # Richmond, VA
    assert r.site.tier == 2 and r.site.state == "VA" and "outside_nc" in r.land.flags
    assert r.carbon.grid_lb_per_mwh == 536.9 and "scores.suitability" in r.missing


def test_suggest_real(real_data):
    c = TestClient(app)
    s = SuggestResponse.model_validate(c.post("/api/suggest", json={"lat": CENTER[0], "lon": CENTER[1], "radius_km": 25}).json())
    assert 1 <= len(s.candidates) <= 3
    for cand in s.candidates:
        assert cand.distance_km <= 25 and cand.reason
        assert cand.scores.suitability - cand.scores.burden > s.origin_scores.suitability - s.origin_scores.burden


def test_layers_real(real_data):
    c = TestClient(app)
    assert [l["name"] for l in c.get("/api/layers").json()["layers"]] == ["suitability"]
    assert c.get("/api/layers/burden").json()["features"] == []
