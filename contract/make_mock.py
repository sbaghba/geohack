"""
Generates mock/*.json from schemas.py so the mocks can never drift from the contract.

    pip install "pydantic>=2" h3
    python make_mock.py

The numbers come from the plan's starting assumptions with placeholder local
values (grid rate, price, tax rate...). They are SAMPLE DATA: every report has
mock = true. The backend can lift `core_metrics()` as a first draft of impact.py.
"""
from __future__ import annotations

import json
import math
from datetime import datetime, timezone
from pathlib import Path

import h3

from schemas import (
    AnalyzeRequest, Candidate, Carbon, Community, Delta, Driver, Economy, Energy,
    Hazards, Land, LandConverted, LayerInfo, LayerList, Mitigation, NriScores, Poi,
    Report, Scores, Site, Source, SuggestRequest, SuggestResponse, Water,
)

OUT = Path(__file__).parent / "mock"

# ---- starting assumptions (plan: Impact model section) ----
PUE = {"evaporative": 1.2, "air": 1.4, "liquid": 1.15}
WUE = {"evaporative": 1.8, "air": 0.2, "liquid": 0.1}          # L/kWh on-site
UTIL = {"ai": 0.8, "mixed": 0.6}
EWIF = 4.5                      # L/kWh off-site (power plants), US average estimate
HOME_KWH = 10_500               # avg US home per year
HOUSEHOLD_M3_YR = 414.5         # ~300 gal/day household
CAR_T_CO2 = 4.6                 # t CO2 per passenger car per year
ACRES_PER_MW = 0.75
CAPEX_PER_MW = 10e6
CONSTRUCTION_JOBS_PER_MW = 10
PERMANENT_JOBS_PER_MW = 0.5
TAXABLE_SHARE = 0.5
GAS_LB_PER_MWH = 900
SOLAR_OFFSET = 0.05

# ---- placeholder local values (backend replaces with real lookups) ----
MOCK_GRID_LB = 650.0            # eGRID subregion rate placeholder
MOCK_PRICE = 75.0               # $/MWh industrial placeholder
MOCK_TAX_RATE = 0.66            # $ per $100 assessed placeholder


SAMPLE_FIELD_SOURCES = {
    "energy": "model", "carbon": "model", "carbon.grid_lb_per_mwh": "egrid", "energy.price_usd_per_mwh": "eia",
    "water": "model", "water.aqueduct_stress": "aqueduct", "water.aqueduct_label": "aqueduct",
    "economy": "model", "economy.county_unemployed": "acs", "economy.property_tax_usd_yr": "ncdor",
    "hazards.nri": "nri", "hazards.fema_zone": "nfhl", "hazards.subsidence_mm_yr": "opera",
    "hazards.nisar_coherence": "nisar", "hazards.nisar_motion_12d_mm": "nisar", "community": "acs", "community.svi_pct": "nri",
    "community.schools_1km": "osm", "land.converted_acres": "worldcover", "scores": "model",
}


def core_metrics(req: AnalyzeRequest, grid_lb: float, price: float, tax_rate: float) -> dict:
    pue, wue = PUE[req.cooling], WUE[req.cooling]
    mwh = req.mw * pue * UTIL[req.workload] * 8760
    lb = {"grid": grid_lb, "grid_solar": grid_lb * (1 - SOLAR_OFFSET), "gas": GAS_LB_PER_MWH}[req.power]
    tons = mwh * lb / 2204.6
    onsite = mwh * wue                      # MWh * 1000 kWh * L/kWh / 1000 L/m3
    capex = req.mw * CAPEX_PER_MW
    return dict(
        pue=pue, wue=wue, mwh=mwh, lb=lb, tons=tons, onsite=onsite, offsite=mwh * EWIF,
        capex=capex, tax=capex * TAXABLE_SHARE * tax_rate / 100, cost=mwh * price,
    )


def build_report(req: AnalyzeRequest) -> Report:
    m = core_metrics(req, MOCK_GRID_LB, MOCK_PRICE, MOCK_TAX_RATE)
    liquid = core_metrics(req.model_copy(update={"cooling": "liquid"}), MOCK_GRID_LB, MOCK_PRICE, MOCK_TAX_RATE)
    solar = core_metrics(req.model_copy(update={"power": "grid_solar"}), MOCK_GRID_LB, MOCK_PRICE, MOCK_TAX_RATE)

    def d(field, before, after):
        return Delta(field=field, before=round(before, 1), after=round(after, 1),
                     pct_change=round((after - before) / before * 100, 1) if before else None)

    return Report(
        mock=True,
        generated_at=datetime.now(timezone.utc).isoformat(timespec="seconds"),
        request=req,
        site=Site(lat=req.lat, lon=req.lon, hex_id=h3.latlng_to_cell(req.lat, req.lon, 7),
                  label="near Clayton, NC (sample)", county="Johnston", state="NC", tier=1),
        scores=Scores(
            suitability=72, burden=58, quadrant="tradeoff", pressure=81, pressure_us_pct=93,
            pressure_drivers=[
                Driver(key="near_230kv_line", label="230 kV line 1.8 km away", direction="+"),
                Driver(key="low_flood_share", label="Little floodplain nearby", direction="+"),
                Driver(key="high_water_stress", label="Medium-high water stress", direction="-"),
            ],
        ),
        energy=Energy(
            pue=m["pue"], annual_mwh=round(m["mwh"]), peak_grid_mw=round(req.mw * m["pue"], 1),
            homes_equiv=round(m["mwh"] * 1000 / HOME_KWH), county_share_pct=None, grid_region="SRVC",
            county_households=88000, county_homes_share_pct=round(m["mwh"] * 1000 / HOME_KWH / 88000 * 100, 1),
            price_usd_per_mwh=MOCK_PRICE, annual_cost_usd=round(m["cost"]),
            nearest_substation_km=3.2, nearest_substation_kv=230, nearest_line_km=1.8, nearest_line_kv=230,
        ),
        carbon=Carbon(grid_lb_per_mwh=m["lb"], tons_co2_yr=round(m["tons"]), cars_equiv=round(m["tons"] / CAR_T_CO2)),
        water=Water(
            wue_l_per_kwh=m["wue"], onsite_m3_yr=round(m["onsite"]), offsite_m3_yr=round(m["offsite"]),
            households_equiv=round(m["onsite"] / HOUSEHOLD_M3_YR), aqueduct_stress=2.6,
            aqueduct_label="Medium-high (20-40%)", drought_category="D0",
        ),
        economy=Economy(
            capex_usd=m["capex"], construction_jobs=round(req.mw * CONSTRUCTION_JOBS_PER_MW),
            permanent_jobs=round(req.mw * PERMANENT_JOBS_PER_MW), county_unemployed=4800,
            county_unemployment_pct=3.4, property_tax_usd_yr=round(m["tax"]), county_levy_share_pct=2.1,
        ),
        hazards=Hazards(
            fema_zone="X", in_floodplain=False,
            nri=NriScores(overall=61.0, hurricane=78.0, heat_wave=55.0, riverine_flooding=40.0,
                          coastal_flooding=None, tornado=66.0),
            nri_rating="Relatively Moderate", subsidence_mm_yr=-0.8, nisar_coherence=0.62, nisar_motion_12d_mm=-1.2,
        ),
        community=Community(
            pop_1km=850, pop_3km=9400, pop_5km=24100, homes_1km=310, svi_pct=47, median_income_usd=68000,
            schools_1km=[Poi(name="Sample Elementary School", kind="school", lat=req.lat + 0.006,
                             lon=req.lon - 0.004, distance_km=0.7)],
            hospitals_1km=[],
        ),
        land=Land(
            acres=req.mw * ACRES_PER_MW,
            converted_acres=LandConverted(forest=req.mw * ACRES_PER_MW * 0.45, cropland=req.mw * ACRES_PER_MW * 0.35,
                                          pasture=req.mw * ACRES_PER_MW * 0.1, wetland=0,
                                          developed=req.mw * ACRES_PER_MW * 0.05, other=req.mw * ACRES_PER_MW * 0.05),
            flags=[],
        ),
        mitigations=[
            Mitigation(key="closed_loop_cooling", label="Switch to closed-loop liquid cooling", deltas=[
                d("water.onsite_m3_yr", m["onsite"], liquid["onsite"]),
                d("energy.annual_mwh", m["mwh"], liquid["mwh"]),
            ]),
            Mitigation(key="onsite_solar", label="Add on-site solar", deltas=[
                d("carbon.tons_co2_yr", m["tons"], solar["tons"]),
            ]),
        ],
        sources=[
            Source(key="model", name="SiteSense impact model (assumptions in backend/app/reference.py)", url="https://github.com/sbaghba/geohack"),
            Source(key="egrid", name="EPA eGRID", url="https://www.epa.gov/egrid"),
            Source(key="aqueduct", name="WRI Aqueduct", url="https://www.wri.org/aqueduct"),
            Source(key="nri", name="FEMA National Risk Index", url="https://hazards.fema.gov/nri/"),
            Source(key="nfhl", name="FEMA National Flood Hazard Layer", url="https://www.fema.gov/flood-maps/national-flood-hazard-layer"),
            Source(key="acs", name="US Census ACS", url="https://www.census.gov/data/developers.html"),
            Source(key="eia", name="EIA state electricity profiles (2024 avg retail price)", url="https://www.eia.gov/electricity/state/"),
            Source(key="ncdor", name="NC Dept. of Revenue county tax rates 2025-26", url="https://www.ncdor.gov/taxes-forms/property-tax/property-tax-rates/county-property-tax-rates-and-reappraisal-schedules/fiscal-year-2025-2026"),
            Source(key="osm", name="OpenStreetMap", url="https://www.openstreetmap.org"),
            Source(key="worldcover", name="ESA WorldCover 2021", url="https://esa-worldcover.org"),
            Source(key="opera", name="OPERA DISP-S1", url="https://www.earthdata.nasa.gov/data/catalog/asf-opera-l3-disp-s1-v1-1"),
            Source(key="nisar", name="NASA-ISRO NISAR L2 (ASF DAAC)", url="https://nisar-docs.asf.alaska.edu/"),
        ],
        missing=["energy.county_share_pct", "hazards.nri.coastal_flooding"],
        field_sources=SAMPLE_FIELD_SOURCES,
    )


def build_suggest(req: SuggestRequest, report: Report) -> SuggestResponse:
    offsets = [(0.18, -0.25, "near Smithfield, NC (sample)"), (-0.12, 0.30, "near Wendell, NC (sample)"),
               (0.30, 0.10, "near Benson, NC (sample)")]
    cands = []
    for i, (dlat, dlon, label) in enumerate(offsets[: req.n], start=1):
        lat, lon = req.lat + dlat, req.lon + dlon
        dist = math.dist((0, 0), (dlat * 111, dlon * 111 * math.cos(math.radians(req.lat))))
        cands.append(Candidate(
            rank=i, lat=round(lat, 4), lon=round(lon, 4), hex_id=h3.latlng_to_cell(lat, lon, 7), label=label,
            distance_km=round(dist, 1),
            scores=Scores(suitability=74 - i, burden=36 + 4 * i, quadrant="good", pressure=70 - 5 * i),
            deltas=[Delta(field="water.aqueduct_stress", before=2.6, after=1.1 + 0.2 * i, pct_change=None),
                    Delta(field="community.pop_3km", before=9400, after=3100 + 800 * i, pct_change=None)],
            reason="Same 230 kV access, lower water stress, fewer residents within 3 km (sample)",
        ))
    return SuggestResponse(origin=report.site, origin_scores=report.scores, radius_km=req.radius_km, candidates=cands)


def build_layer(lat: float, lon: float, phase: float) -> dict:
    center = h3.latlng_to_cell(lat, lon, 7)
    feats = []
    for cell in h3.grid_disk(center, 8):
        clat, clon = h3.cell_to_latlng(cell)
        value = 50 + 45 * math.sin(clat * 40 + phase) * math.cos(clon * 35 - phase)
        ring = [[round(x, 5), round(y, 5)] for y, x in h3.cell_to_boundary(cell)]
        ring.append(ring[0])
        feats.append({"type": "Feature", "geometry": {"type": "Polygon", "coordinates": [ring]},
                      "properties": {"hex_id": cell, "value": round(value, 1)}})
    return {"type": "FeatureCollection", "features": feats}


LAYERS = LayerList(layers=[
    LayerInfo(name="suitability", label="Site suitability", unit="score 0-100", min=0, max=100, higher_is="better"),
    LayerInfo(name="burden", label="Community burden", unit="score 0-100", min=0, max=100, higher_is="worse"),
    LayerInfo(name="pressure", label="Siting pressure (model)", unit="score 0-100", min=0, max=100, higher_is="neutral"),
    LayerInfo(name="subsidence", label="Ground movement (OPERA)", unit="mm/yr", min=-20, max=20, higher_is="neutral"),
    LayerInfo(name="water_stress", label="Water stress (Aqueduct)", unit="0-5", min=0, max=5, higher_is="worse"),
    LayerInfo(name="nisar_coherence", label="Ground stability (NISAR radar coherence)", unit="0-1", min=0, max=1, higher_is="better"),
])


def main():
    OUT.mkdir(exist_ok=True)
    req = SuggestRequest(lat=35.655, lon=-78.462, mw=100)
    report = build_report(AnalyzeRequest(**req.model_dump(exclude={"radius_km", "n"})))
    files = {
        "analyze.json": report.model_dump(mode="json"),
        "suggest.json": build_suggest(req, report).model_dump(mode="json"),
        "layers.json": LAYERS.model_dump(mode="json"),
        "layer_sample.geojson": build_layer(req.lat, req.lon, 0.0),
        "overlays.json": {"overlays": []},
    }
    for name, data in files.items():
        (OUT / name).write_text(json.dumps(data, indent=2))
        print("wrote", OUT / name)


if __name__ == "__main__":
    main()
