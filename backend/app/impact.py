"""Impact formulas (plan: Impact model section). Pure functions; constants live in reference.py."""
from __future__ import annotations

from . import reference as R
from .contract import AnalyzeRequest, Delta, Mitigation


def metrics(req: AnalyzeRequest, grid_lb: float, price_usd_mwh: float | None, tax_rate: float | None) -> dict:
    pue, wue = R.PUE[req.cooling], R.WUE_L_PER_KWH[req.cooling]
    mwh = req.mw * pue * R.UTILIZATION[req.workload] * 8760
    lb = {"grid": grid_lb, "grid_solar": grid_lb * (1 - R.SOLAR_OFFSET), "gas": R.GAS_LB_PER_MWH}[req.power]
    tons = mwh * lb / 2204.6
    onsite = mwh * wue                     # MWh*1000 kWh * L/kWh / 1000 L/m3
    capex = req.mw * R.CAPEX_USD_PER_MW
    return {
        "pue": pue, "wue": wue, "mwh": mwh, "lb": lb, "tons": tons,
        "onsite_m3": onsite, "offsite_m3": mwh * R.EWIF_L_PER_KWH,
        "homes": mwh * 1000 / R.HOME_KWH_YR, "households_water": onsite / R.HOUSEHOLD_M3_YR, "cars": tons / R.CAR_T_CO2_YR,
        "capex": capex, "tax": capex * R.TAXABLE_SHARE * tax_rate / 100 if tax_rate else None,
        "cost": mwh * price_usd_mwh if price_usd_mwh else None,
        "construction_jobs": req.mw * R.CONSTRUCTION_JOBS_PER_MW, "permanent_jobs": req.mw * R.PERMANENT_JOBS_PER_MW,
        "acres": req.mw * R.ACRES_PER_MW, "peak_mw": req.mw * pue,
    }


def _d(field: str, before: float | None, after: float | None) -> Delta:
    pct = round((after - before) / before * 100, 1) if before and after is not None else None
    return Delta(field=field, before=None if before is None else round(before, 1),
                 after=None if after is None else round(after, 1), pct_change=pct)


def mitigations(req: AnalyzeRequest, grid_lb: float, price: float | None, tax: float | None, base: dict) -> list[Mitigation]:
    out = []
    if req.cooling != "liquid":
        m = metrics(req.model_copy(update={"cooling": "liquid"}), grid_lb, price, tax)
        out.append(Mitigation(key="closed_loop_cooling", label="Switch to closed-loop liquid cooling", deltas=[
            _d("water.onsite_m3_yr", base["onsite_m3"], m["onsite_m3"]), _d("energy.annual_mwh", base["mwh"], m["mwh"])]))
    if req.power == "grid":
        m = metrics(req.model_copy(update={"power": "grid_solar"}), grid_lb, price, tax)
        out.append(Mitigation(key="onsite_solar", label="Add on-site solar", deltas=[
            _d("carbon.tons_co2_yr", base["tons"], m["tons"])]))
    if req.mw > 50:
        small = req.model_copy(update={"mw": max(20, req.mw / 2)})
        m = metrics(small, grid_lb, price, tax)
        out.append(Mitigation(key="smaller_campus", label=f"Build {small.mw:.0f} MW instead", deltas=[
            _d("water.onsite_m3_yr", base["onsite_m3"], m["onsite_m3"]), _d("economy.permanent_jobs", base["permanent_jobs"], m["permanent_jobs"])]))
    return out
