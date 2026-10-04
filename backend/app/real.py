"""Builds real Reports and suggestions from the prebuilt grid + live lookups."""
from __future__ import annotations

import json
import math
from datetime import datetime, timezone

import h3
import numpy as np
import pandas as pd

from . import reference as R
from .contract import (
    AnalyzeRequest, Candidate, Carbon, Community, Delta, Driver, Economy, Energy, Hazards, Land, LandConverted, NriScores,
    Poi, ProtectedArea, Report, Scores, Site, Source, SuggestRequest, SuggestResponse, Water,
)
from .errors import out_of_coverage
from .impact import metrics, mitigations
from .live import in_protected, point_lookups
from .store import haversine_km, store

PIN_HEX_SPACING_KM = 2.4  # center-to-center distance of res-7 hexes


def _f(v):
    """NaN/None -> None, numpy -> float."""
    if v is None:
        return None
    try:
        return None if pd.isna(v) else float(v)
    except (TypeError, ValueError):
        return None


def _f1(v, nd=1):
    v = _f(v)
    return None if v is None else round(v, nd)


def _missing(obj, prefix="") -> list[str]:
    out = []
    if isinstance(obj, dict):
        for k, v in obj.items():
            out += _missing(v, f"{prefix}{k}.") if isinstance(v, dict) else ([f"{prefix}{k}"] if v is None else [])
    return out


VINTAGE = {"pressure_model": "trained Oct 2026", "usdm": "current week", "opera": "2021-2025", "nisar": "Jul-Oct 2026", "egrid": "2023", "eia": "2024", "ncdor": "2025-26", "acs": "2019-2023", "aqueduct": "4.0 (2023)",
           "worldcover": "2021", "census_tiger": "2023", "osm": "Oct 2026"}


def _sources(keys: set[str]) -> list[Source]:
    return [Source(key=k, name=R.SOURCES[k][0], url=R.SOURCES[k][1], vintage=VINTAGE.get(k)) for k in R.SOURCES if k in keys]


FIELD_SOURCES_NC = {
    "energy": "model", "energy.grid_region": "egrid", "energy.price_usd_per_mwh": "eia", "energy.county_households": "acs",
    "energy.nearest_substation_km": "osm", "energy.nearest_substation_kv": "osm", "energy.nearest_line_km": "osm",
    "energy.nearest_line_kv": "osm", "carbon": "model", "carbon.grid_lb_per_mwh": "egrid",
    "water": "model", "water.aqueduct_stress": "aqueduct", "water.aqueduct_label": "aqueduct", "water.drought_category": "usdm",
    "economy": "model", "economy.county_unemployed": "acs", "economy.county_unemployment_pct": "acs",
    "economy.property_tax_usd_yr": "ncdor", "hazards.fema_zone": "nfhl", "hazards.in_floodplain": "nfhl",
    "hazards.nri": "nri", "hazards.nri_rating": "nri", "hazards.subsidence_mm_yr": "opera",
    "hazards.nisar_coherence": "nisar", "hazards.nisar_motion_12d_mm": "nisar", "community": "acs", "community.svi_pct": "nri",
    "community.schools_1km": "osm", "community.hospitals_1km": "osm", "community.datacenters_25km": "osm",
    "community.nearest_datacenter": "osm", "land.protected_areas": "padus", "land": "model", "land.converted_acres": "worldcover",
    "scores": "model", "scores.pressure": "pressure_model", "scores.pressure_us_pct": "pressure_model", "scores.pressure_drivers": "pressure_model",
    "site.county": "census_tiger",
}


def _field_sources(row) -> dict:
    if row.get("pop_src") == "nri":  # ACS fallback: population is Census 2020 via FEMA NRI
        return {**FIELD_SOURCES_NC, "community": "nri", "energy.county_households": "acs"}
    return FIELD_SOURCES_NC


def _drivers(row) -> list[Driver]:
    raw = row.get("pressure_drivers")
    if not isinstance(raw, str):
        return []
    try:
        return [Driver(**d) for d in json.loads(raw)][:3]
    except Exception:
        return []


def _pois(kind: str, name: str, lat: float, lon: float, km: float) -> list[Poi]:
    return [Poi(name=(r.get("name") or f"Unnamed {kind}")[:80], kind=kind, lat=float(r.lat), lon=float(r.lon), distance_km=round(d, 2))
            for d, r in store.within(name, lat, lon, km)[:25]]


_DC_CACHE: dict = {}


def _dc_campuses() -> pd.DataFrame:
    """OSM data-center points merged into campuses (OSM often maps each building separately)."""
    if "df" not in _DC_CACHE:
        df = store.points("datacenters").copy()
        if df.empty:
            _DC_CACHE["df"] = df
            return df
        df["name"] = [R.DATACENTER_NAMES.get(str(o)) or (n if isinstance(n, str) and n.strip() else None)
                      for o, n in zip(df.get("osm_id", [None] * len(df)), df["name"])]
        rows, used = [], np.zeros(len(df), bool)
        lat, lon = df.lat.values, df.lon.values
        for i in range(len(df)):
            if used[i]:
                continue
            grp = np.where(~used & (haversine_km(lat[i], lon[i], lat, lon) <= R.DATACENTER_CAMPUS_KM))[0]
            used[grp] = True
            names = [n for n in df.name.values[grp] if n]
            rows.append({"lat": float(lat[grp].mean()), "lon": float(lon[grp].mean()),
                         "name": max(set(names), key=names.count) if names else None})
        _DC_CACHE["df"] = pd.DataFrame(rows)
    return _DC_CACHE["df"]


def _datacenters(lat: float, lon: float) -> tuple[list[Poi], Poi | None]:
    df = _dc_campuses()
    if df.empty:
        return [], None
    d = haversine_km(lat, lon, df.lat.values, df.lon.values)
    order = np.argsort(d)
    poi = lambda i: Poi(name=(df.name.iloc[i] or "Data center (unnamed in OpenStreetMap)")[:80], kind="data_center",  # noqa: E731
                        lat=round(float(df.lat.iloc[i]), 5), lon=round(float(df.lon.iloc[i]), 5), distance_km=round(float(d[i]), 1))
    near = [poi(i) for i in order if d[i] <= 25][:10]
    return near, poi(int(order[0]))


def _protected(pas) -> tuple[list[ProtectedArea], list[str]]:
    if not pas:
        return [], []
    objs = [ProtectedArea(**p) for p in pas]
    return objs, (["protected_area"] if any(p.contains_site for p in objs) else ["protected_area_within_1km"])


def report(req: AnalyzeRequest) -> Report:
    hex_id = store.hex_of(req.lat, req.lon)
    row = store.row(hex_id)
    if row is None:
        return _tier2(req)

    county = row.get("county") if isinstance(row.get("county"), str) else None
    region = "SRTV" if county in R.SRTV_NC_COUNTIES else "SRVC"
    grid_lb = R.EGRID_SUBREGION_LB[region]
    price = R.EIA_PRICE_CENTS["NC"] * 10
    tax_rate = R.NC_COUNTY_TAX.get(county)
    m = metrics(req, grid_lb, price, tax_rate)
    c = store.counties.loc[county] if county in getattr(store.counties, "index", []) else None
    hh = _f(c["households"]) if c is not None else None
    unemployed = _f(c["unemployed"]) if c is not None else None

    sub_km, sub_kv, _ = store.nearest("substations", req.lat, req.lon, R.MIN_GRID_KV)
    line_km, line_kv, _ = store.nearest("lines", req.lat, req.lon, R.MIN_GRID_KV)
    schools = _pois("school", "schools", req.lat, req.lon, 1.0)
    hospitals = _pois("hospital", "hospitals", req.lat, req.lon, 1.0)
    (zone, sfha), drought, pas = point_lookups(req.lat, req.lon)
    dcs, dc_nearest = _datacenters(req.lat, req.lon)
    protected, pa_flags = _protected(pas)

    flags = list(pa_flags)
    if sfha:
        flags.append("in_floodplain")
    if (_f(row.get("lc_wetland")) or 0) > 0.3:
        flags.append("in_wetland")
    if any(p.distance_km <= 0.5 for p in schools):
        flags.append("school_within_500m")

    acres = m["acres"]
    share = lambda k: round(acres * (_f(row.get(f"lc_{k}")) or 0), 1)  # noqa: E731
    pop_d, hh_d = _f(row.get("pop_dens_km2")), _f(row.get("hh_dens_km2"))
    s, b = _f(row.get("suitability")) or 50.0, _f(row.get("burden")) or 50.0
    quadrant = row.get("quadrant") or "tradeoff"
    if any(p.contains_site for p in protected):          # protected land is a hard constraint
        s = min(s, R.PROTECTED_SUIT_CAP)
        quadrant = "avoid" if b >= 50 else "poor"

    rep = Report(
        generated_at=datetime.now(timezone.utc).isoformat(timespec="seconds"),
        request=req,
        site=Site(lat=req.lat, lon=req.lon, hex_id=hex_id, label=f"{county} County, NC" if county else "North Carolina",
                  county=county, state="NC", tier=1),
        scores=Scores(suitability=round(s, 1), burden=round(b, 1), quadrant=quadrant,
                      pressure=_f(row.get("pressure")), pressure_us_pct=_f(row.get("pressure_us_pct")),
                      pressure_drivers=_drivers(row)),
        energy=Energy(pue=m["pue"], annual_mwh=round(m["mwh"]), peak_grid_mw=round(m["peak_mw"], 1), homes_equiv=round(m["homes"]),
                      county_share_pct=None, county_households=hh,
                      county_homes_share_pct=round(100 * m["homes"] / hh, 1) if hh else None,
                      grid_region=region, price_usd_per_mwh=round(price, 1), annual_cost_usd=round(m["cost"]),
                      nearest_substation_km=None if sub_km is None else round(sub_km, 2), nearest_substation_kv=sub_kv,
                      nearest_line_km=None if line_km is None else round(line_km, 2), nearest_line_kv=line_kv),
        carbon=Carbon(grid_lb_per_mwh=round(m["lb"], 1), tons_co2_yr=round(m["tons"]), cars_equiv=round(m["cars"])),
        water=Water(wue_l_per_kwh=m["wue"], onsite_m3_yr=round(m["onsite_m3"]), offsite_m3_yr=round(m["offsite_m3"]),
                    households_equiv=round(m["households_water"]), aqueduct_stress=_f1(row.get("bws_score"), 2),
                    aqueduct_label=row.get("bws_label") if isinstance(row.get("bws_label"), str) else None, drought_category=drought),
        economy=Economy(capex_usd=m["capex"], construction_jobs=round(m["construction_jobs"]), permanent_jobs=round(m["permanent_jobs"]),
                        county_unemployed=unemployed, county_unemployment_pct=None if c is None else round(_f(c["unemployment_pct"]) or 0, 1),
                        property_tax_usd_yr=None if m["tax"] is None else round(m["tax"]), county_levy_share_pct=None),
        hazards=Hazards(fema_zone=zone, in_floodplain=sfha,
                        nri=NriScores(overall=_f1(row.get("nri_overall")), hurricane=_f1(row.get("nri_hurricane")),
                                      heat_wave=_f1(row.get("nri_heat_wave")), riverine_flooding=_f1(row.get("nri_riverine_flooding")),
                                      coastal_flooding=_f1(row.get("nri_coastal_flooding")), tornado=_f1(row.get("nri_tornado"))),
                        nri_rating=row.get("nri_rating") if isinstance(row.get("nri_rating"), str) else None,
                        subsidence_mm_yr=_f1(row.get("subsidence_mm_yr"), 2),
                        nisar_coherence=_f1(row.get("nisar_coherence"), 2),
                        nisar_motion_12d_mm=_f1(row.get("nisar_motion_12d_mm"), 1)),
        community=Community(pop_1km=None if pop_d is None else round(pop_d * math.pi), pop_3km=_round(row.get("pop_3km")),
                            pop_5km=_round(row.get("pop_5km")), homes_1km=None if hh_d is None else round(hh_d * math.pi),
                            svi_pct=_f1(row.get("svi_pct")), median_income_usd=_round(row.get("median_income_usd")),
                            schools_1km=schools, hospitals_1km=hospitals, datacenters_25km=dcs, nearest_datacenter=dc_nearest),
        land=Land(acres=acres, converted_acres=LandConverted(forest=share("forest"), cropland=share("cropland"), pasture=share("pasture"),
                                                             wetland=share("wetland"), developed=share("developed"),
                                                             other=round(acres * ((_f(row.get("lc_other")) or 0) + (_f(row.get("lc_water")) or 0)), 1)),
                  flags=flags, protected_areas=protected),
        mitigations=mitigations(req, grid_lb, price, tax_rate, m),
        sources=_sources(set(_field_sources(row).values()) | {"model"}),
        field_sources=_field_sources(row),
    )
    rep.missing = _missing(rep.model_dump(exclude={"request", "mitigations", "sources", "missing", "field_sources"}))
    if pas is None:
        rep.missing.append("land.protected_areas")
    return rep


def _round(v):
    v = _f(v)
    return None if v is None else round(v)


def _tier2(req: AnalyzeRequest) -> Report:
    st = store.state_of(req.lat, req.lon)
    if st is None or st not in R.EGRID_STATE_LB:
        raise out_of_coverage()
    grid_lb = R.EGRID_STATE_LB[st]
    price = R.EIA_PRICE_CENTS.get(st, 12.94) * 10
    m = metrics(req, grid_lb, price, None)
    (zone, sfha), drought, pas = point_lookups(req.lat, req.lon)
    protected, pa_flags = _protected(pas)
    dcs, dc_nearest = _datacenters(req.lat, req.lon)
    flags = ["outside_nc"] + (["in_floodplain"] if sfha else []) + pa_flags
    fs = {"energy": "model", "energy.grid_region": "egrid", "energy.price_usd_per_mwh": "eia", "carbon": "model",
          "carbon.grid_lb_per_mwh": "egrid", "water": "model", "water.drought_category": "usdm", "economy": "model",
          "hazards.fema_zone": "nfhl", "land": "model", "land.protected_areas": "padus",
          "community.datacenters_25km": "osm", "community.nearest_datacenter": "osm"}
    rep = Report(
        generated_at=datetime.now(timezone.utc).isoformat(timespec="seconds"), request=req,
        site=Site(lat=req.lat, lon=req.lon, hex_id=None, label=f"{st} (coarse data outside NC)", county=None, state=st, tier=2),
        scores=Scores(suitability=50, burden=50, quadrant="tradeoff"),
        energy=Energy(pue=m["pue"], annual_mwh=round(m["mwh"]), peak_grid_mw=round(m["peak_mw"], 1), homes_equiv=round(m["homes"]),
                      grid_region=f"{st} (state avg)", price_usd_per_mwh=round(price, 1), annual_cost_usd=round(m["cost"])),
        carbon=Carbon(grid_lb_per_mwh=grid_lb, tons_co2_yr=round(m["tons"]), cars_equiv=round(m["cars"])),
        water=Water(wue_l_per_kwh=m["wue"], onsite_m3_yr=round(m["onsite_m3"]), offsite_m3_yr=round(m["offsite_m3"]),
                    households_equiv=round(m["households_water"]), drought_category=drought),
        economy=Economy(capex_usd=m["capex"], construction_jobs=round(m["construction_jobs"]), permanent_jobs=round(m["permanent_jobs"])),
        hazards=Hazards(fema_zone=zone, in_floodplain=sfha, nri=NriScores()),
        community=Community(datacenters_25km=dcs, nearest_datacenter=dc_nearest),
        land=Land(acres=m["acres"], converted_acres=LandConverted(), flags=flags, protected_areas=protected),
        mitigations=mitigations(req, grid_lb, price, None, m),
        sources=_sources(set(fs.values()) | {"model"}), field_sources=fs,
    )
    rep.missing = ["scores.suitability", "scores.burden"] + _missing(
        rep.model_dump(exclude={"request", "mitigations", "sources", "missing", "field_sources"}))
    return rep


# --------------------------------------------------------------- suggest ---

def suggest(req: SuggestRequest) -> SuggestResponse:
    origin_hex = store.hex_of(req.lat, req.lon)
    o = store.row(origin_hex)
    site = Site(lat=req.lat, lon=req.lon, hex_id=origin_hex if o is not None else None,
                label=(f"{o.get('county')} County, NC" if o is not None else None),
                county=o.get("county") if o is not None else None, state="NC" if o is not None else None, tier=1 if o is not None else 2)
    if o is None:
        return SuggestResponse(origin=site, origin_scores=Scores(suitability=50, burden=50, quadrant="tradeoff"),
                               radius_km=req.radius_km, candidates=[])
    o_scores = _scores(o)
    k = max(2, math.ceil(req.radius_km / PIN_HEX_SPACING_KM))
    cells = [c for c in h3.grid_disk(origin_hex, k) if c in store.grid.index]
    df = store.grid.loc[cells].copy()
    df["dist"] = haversine_km(req.lat, req.lon, df.lat.values, df.lon.values)
    df = df[(df.dist <= req.radius_km) & (df.dist >= 3) & ~df.get("hard_flag", pd.Series(False, index=df.index)).fillna(False).astype(bool)]
    df["gain"] = (df.suitability - df.burden) - (o_scores.suitability - o_scores.burden)
    df = df.sort_values("gain", ascending=False)
    picked = []
    for hid, r in df.iterrows():
        if r.gain <= 0 or any(haversine_km(r.lat, r.lon, p[1].lat, p[1].lon) < 8 for p in picked):
            continue
        if in_protected(float(r.lat), float(r.lon)):     # never suggest a park / preserve / easement
            continue
        picked.append((hid, r))
        if len(picked) == req.n:
            break
    cands = []
    for i, (hid, r) in enumerate(picked, 1):
        deltas = [Delta(field="scores.burden", before=o_scores.burden, after=_f(r.burden)),
                  Delta(field="community.pop_3km", before=_round(o.get("pop_3km")), after=_round(r.get("pop_3km"))),
                  Delta(field="water.aqueduct_stress", before=_f(o.get("bws_score")), after=_f(r.get("bws_score"))),
                  Delta(field="energy.nearest_substation_km", before=_r2(o.get("dist_sub_km")), after=_r2(r.get("dist_sub_km")))]
        for d in deltas:
            if d.before and d.after is not None:
                d.pct_change = round((d.after - d.before) / d.before * 100, 1)
        cands.append(Candidate(rank=i, lat=round(float(r.lat), 5), lon=round(float(r.lon), 5), hex_id=hid,
                               label=f"{r.get('county')} County, NC", distance_km=round(float(r.dist), 1),
                               scores=_scores(r), deltas=deltas, reason=_reason(o, r)))
    return SuggestResponse(origin=site, origin_scores=o_scores, radius_km=req.radius_km, candidates=cands)


def _r2(v):
    v = _f(v)
    return None if v is None else round(v, 2)


def _scores(r) -> Scores:
    return Scores(suitability=_f(r.get("suitability")) or 50, burden=_f(r.get("burden")) or 50,
                  quadrant=r.get("quadrant") or "tradeoff", pressure=_f(r.get("pressure")), pressure_us_pct=_f(r.get("pressure_us_pct")))


def _reason(o, r) -> str:
    bits = [f"burden {r.burden:.0f} vs {o.burden:.0f}"]
    po, pr = _f(o.get("pop_3km")), _f(r.get("pop_3km"))
    if po and pr is not None and pr < 0.95 * po:
        bits.append(f"{(1 - pr / po) * 100:.0f}% fewer people within 3 km")
    bo, br = _f(o.get("bws_score")), _f(r.get("bws_score"))
    if bo is not None and br is not None and br < bo - 0.2:
        bits.append(f"water stress {br:.1f} vs {bo:.1f}")
    ds = _f(r.get("dist_sub_km"))
    if ds is not None:
        bits.append(f"substation {ds:.1f} km away")
    return "; ".join(bits)
